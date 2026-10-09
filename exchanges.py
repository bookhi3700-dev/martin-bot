"""업비트 / 빗썸 / 코인원 API 연동 + 모의투자 거래소

 - 업비트: https://docs.upbit.com            — JWT(HS256) + query_hash(SHA512)
 - 빗썸(API 2.0): https://apidocs.bithumb.com — 업비트와 같은 방식
 - 코인원(API v2.1): https://docs.coinone.co.kr — 본문 Base64 + HMAC-SHA512 서명
프로그램 내부에서는 모두 'KRW-BTC' 형식의 마켓 코드를 씁니다.
"""
import base64
import hashlib
import hmac
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, unquote

import socket
import threading

import jwt
import requests
import urllib3.util.connection as _urllib3_conn

# 거래소 API 허용 IP는 보통 IPv4(예: 121.134.x.x)로 등록합니다.
# PC가 IPv6로 접속하면 등록한 IP와 달라 "IP not allowed" 가 나므로 항상 IPv4로만 접속합니다.
_urllib3_conn.allowed_gai_family = lambda: socket.AF_INET


def my_public_ip():
    """이 프로그램이 인터넷에 나갈 때 쓰는 공인 IP (거래소에 등록해야 하는 값)"""
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ifconfig.me/ip"):
        try:
            ip = requests.get(url, timeout=5).text.strip()
            if ip and len(ip) < 50:
                return ip
        except requests.RequestException:
            continue
    return None

TIMEOUT = 10


class ExchangeError(Exception):
    pass


class OrderUnconfirmed(ExchangeError):
    """주문은 나갔을 수 있지만 체결 여부를 확인하지 못함 → 중복 주문 방지를 위해 해당 코인 일시정지"""


class OrderNotFound(ExchangeError):
    """거래소에 해당 주문이 없음 (주문이 접수되지 않았거나 ID가 틀림)"""


# ---- 같은 계좌를 쓰는 봇(마틴·그리드)끼리 주문 순서를 맞추는 잠금 + 호출 간격 ----
_GUARD = threading.Lock()
_ACCOUNT_LOCKS = {}
_PACE = {}
MIN_GAP_SEC = 0.1   # 같은 거래소로 보내는 요청 사이 최소 간격 (초당 약 10회)


def account_lock(exchange_name, access_key):
    """같은 거래소·같은 키를 쓰는 모든 봇이 공유하는 잠금.
    마틴봇의 '주문→체결 확인'과 그리드의 '주문/취소'가 서로 끼어들지 않게 합니다."""
    k = (exchange_name, access_key or "")
    with _GUARD:
        if k not in _ACCOUNT_LOCKS:
            _ACCOUNT_LOCKS[k] = threading.RLock()
        return _ACCOUNT_LOCKS[k]


def _pace(exchange_name):
    with _GUARD:
        last = _PACE.get(exchange_name, 0.0)
        wait = last + MIN_GAP_SEC - time.monotonic()
        _PACE[exchange_name] = max(time.monotonic(), last + MIN_GAP_SEC)
    if wait > 0:
        time.sleep(wait)


def _dec(step):
    """호가/수량 단위의 소수 자릿수"""
    s = f"{step:.12f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def fmt_num(x, step):
    d = _dec(step)
    return f"{x:.{d}f}" if d else str(int(round(x)))


def floor_step(x, step):
    return math.floor(x / step + 1e-9) * step


def fee_is_coin(fee, fee_rate, qty, price):
    """체결 수수료가 코인으로 떼였는지(True) 원화로 떼였는지(False) 추정"""
    if fee <= 0 or qty <= 0 or price <= 0:
        return False
    if fee_rate and fee_rate > 0:
        return abs(fee - qty * fee_rate) < abs(fee - qty * price * fee_rate)
    as_coin, as_krw = fee / qty, fee / (qty * price)
    return as_coin <= 0.01 and not (as_krw <= 0.01 and as_krw >= 0.00005) and as_coin >= 0.00005


def _floor(x, digits=8):
    f = 10 ** digits
    return math.floor(x * f) / f


class _JwtExchange:
    name = ""
    base_url = ""

    def __init__(self, access_key="", secret_key=""):
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()

    @property
    def lock(self):
        return account_lock(self.name, self.access_key)

    # ---------- 공통 ----------
    def _headers(self, params=None):
        if not self.access_key or not self.secret_key:
            raise ExchangeError("API 키가 설정되지 않았습니다.")
        payload = {"access_key": self.access_key, "nonce": str(uuid.uuid4()),
                   "timestamp": round(time.time() * 1000)}
        if params:
            q = unquote(urlencode(params, doseq=True)).encode()
            payload["query_hash"] = hashlib.sha512(q).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _check(self, r):
        try:
            data = r.json()
        except ValueError:
            raise ExchangeError(f"{self.name} 응답 오류 HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            err = data.get("error", data) if isinstance(data, dict) else data
            if r.status_code == 404 or "not_found" in json.dumps(err, ensure_ascii=False):
                raise OrderNotFound(f"{self.name} 주문 없음: {err}")
            raise ExchangeError(f"{self.name} 오류 HTTP {r.status_code}: {err}")
        return data

    def _get(self, path, params=None, auth=False):
        headers = self._headers(params) if auth else {}
        _pace(self.name)
        r = requests.get(self.base_url + path, params=params, headers=headers, timeout=TIMEOUT)
        return self._check(r)

    def _post(self, path, body):
        _pace(self.name)
        r = requests.post(self.base_url + path, data=json.dumps(body), headers=self._headers(body), timeout=TIMEOUT)
        return self._check(r)

    def _delete(self, path, params):
        _pace(self.name)
        r = requests.delete(self.base_url + path, params=params, headers=self._headers(params), timeout=TIMEOUT)
        return self._check(r)

    # ---------- 시세 (공개) ----------
    def price(self, market):
        return self.prices([market])[market]

    def prices(self, markets):
        data = self._get("/v1/ticker", {"markets": ",".join(markets)})
        return {d["market"]: float(d["trade_price"]) for d in data}

    def candles(self, market, unit_minutes=60, count=200, to=None):
        params = {"market": market, "count": count}
        if to:
            params["to"] = to
        return self._get(f"/v1/candles/minutes/{unit_minutes}", params)

    # ---------- 계좌 ----------
    def balances(self):
        data = self._get("/v1/accounts", auth=True)
        return {a["currency"]: float(a["balance"]) for a in data}

    def balance(self, currency):
        return self.balances().get(currency, 0.0)

    def available(self, currency):
        """주문에 묶이지 않고 바로 쓸 수 있는 잔고 (업비트·빗썸 balance 는 원래 사용 가능 잔고)"""
        return self._bal_pair()[1].get(currency, 0.0)

    def _bal_pair(self):
        """(체결 계산용 잔고, 사용 가능 잔고)"""
        b = self.balances()
        return b, b

    # ---------- 주문 ----------
    def market_buy(self, market, krw):
        coin = market.split("-")[1]
        with self.lock:
            b, av = self._bal_pair()
            before_krw, before_coin = b.get("KRW", 0.0), b.get(coin, 0.0)
            avail = av.get("KRW", 0.0)
            if avail < krw * 1.003:  # 수수료 여유분 포함
                raise ExchangeError(f"원화 잔고 부족: 필요 {krw:,.0f}원 / 사용 가능 {avail:,.0f}원")
            oid = self._place_safe(market, "bid", price=str(int(krw)))
            return self._settle(oid, market, "bid", before_krw, before_coin)

    def market_sell(self, market, volume):
        coin = market.split("-")[1]
        with self.lock:
            b = self.balances()
            before_krw, before_coin = b.get("KRW", 0.0), b.get(coin, 0.0)
            vol = _floor(min(volume, before_coin))
            if vol <= 0:
                raise ExchangeError(f"{coin} 보유 수량이 없습니다.")
            oid = self._place_safe(market, "ask", volume=f"{vol:.8f}")
            return self._settle(oid, market, "ask", before_krw, before_coin)

    def _place_safe(self, market, side, **kw):
        try:
            return self._place(market, side, **kw)
        except requests.RequestException as e:
            # 요청이 거래소에 도달했는지 알 수 없음 (타임아웃 등)
            raise OrderUnconfirmed(f"주문 전송 중 연결 오류({type(e).__name__}) — 주문이 들어갔는지 거래소 앱에서 확인해 주세요.")

    def _settle(self, oid, market, side, before_krw, before_coin):
        """체결 결과 확인. 주문 조회 실패 시 잔고 변화로 계산."""
        coin = market.split("-")[1]
        for _ in range(20):
            time.sleep(0.5)
            try:
                o = self._order(oid)
            except (ExchangeError, requests.RequestException):
                continue
            if o.get("state") in ("done", "cancel") and float(o.get("executed_volume") or 0) > 0:
                vol = float(o["executed_volume"])
                funds = o.get("executed_funds")
                funds = float(funds) if funds not in (None, "") else sum(float(t["funds"]) for t in o.get("trades", []))
                fee = float(o.get("paid_fee") or 0)
                krw = funds + fee if side == "bid" else funds - fee
                return {"volume": vol, "krw": krw, "price": funds / vol if vol else 0, "fee": fee}
        # fallback: 잔고 차이
        time.sleep(1)
        try:
            b = self.balances()
        except (ExchangeError, requests.RequestException):
            raise OrderUnconfirmed("주문 후 잔고 조회에 실패했습니다. 거래소 앱에서 체결 여부를 확인해 주세요.")
        dk, dc = b.get("KRW", 0) - before_krw, b.get(coin, 0) - before_coin
        if side == "bid" and dc > 0:
            return {"volume": dc, "krw": -dk, "price": -dk / dc, "fee": 0}
        if side == "ask" and dc < 0:
            return {"volume": -dc, "krw": dk, "price": dk / -dc, "fee": 0}
        raise OrderUnconfirmed("주문 체결을 확인하지 못했습니다. 거래소 앱에서 직접 확인해 주세요.")


    # ---------- 지정가 (그리드용) — 업비트·빗썸 공통 ----------
    _cid_param = "identifier"

    def market_info(self, market):
        """호가 단위·수량 단위·최소 주문금액"""
        tick = None
        try:
            tick = self._tick_from_instruments(market)
        except Exception:
            tick = None
        if not tick:
            data = self._get("/v1/orderbook", {"markets": market})
            units = data[0]["orderbook_units"]
            ps = sorted({float(u["ask_price"]) for u in units} | {float(u["bid_price"]) for u in units})
            diffs = [round(b - a, 10) for a, b in zip(ps, ps[1:]) if b - a > 1e-12]
            tick = min(diffs) if diffs else 1.0
        return {"tick": float(tick), "qty_step": 1e-8, "min_krw": 5000.0}

    def _tick_from_instruments(self, market):
        return None

    def limit_order(self, market, side, price, volume, cid, tick=1.0, qty_step=1e-8):
        """지정가 주문. side: bid(매수)/ask(매도). cid = 봇이 정한 주문 ID (조회·취소에 사용)"""
        body = self._limit_body(market, side, fmt_num(price, tick), fmt_num(volume, qty_step), cid)
        with self.lock:
            try:
                self._post(self._order_path, body)
            except requests.RequestException as e:
                raise OrderUnconfirmed(f"주문 전송 중 연결 오류({type(e).__name__})")
        return cid

    def get_order(self, cid, market):
        o = self._get("/v1/order", {self._cid_param: cid}, auth=True)
        return self._norm(o)

    def cancel_order(self, cid, market):
        with self.lock:
            try:
                self._delete(self._cancel_path, {self._cid_param: cid})
            except OrderNotFound:
                pass
            except ExchangeError as e:
                # 이미 체결/취소된 주문이면 무시 (최종 상태는 get_order 로 확인)
                if "done" not in str(e) and "cancel" not in str(e).lower():
                    raise

    @staticmethod
    def _norm(o):
        st = o.get("state")
        state = "open" if st in ("wait", "watch") else ("done" if st == "done" else "cancel")
        qty = float(o.get("executed_volume") or 0)
        funds = o.get("executed_funds")
        if funds in (None, ""):
            funds = sum(float(t.get("funds") or 0) for t in o.get("trades", []) or [])
            if not funds and qty:
                funds = qty * float(o.get("price") or 0)
        funds = float(funds or 0)
        return {"state": state, "qty": qty, "avg": funds / qty if qty else 0.0, "funds": funds,
                "fee": float(o.get("paid_fee") or 0), "fee_coin": False,
                "remain": float(o.get("remaining_volume") or 0)}


class Upbit(_JwtExchange):
    name = "업비트"
    base_url = "https://api.upbit.com"

    def _place(self, market, side, price=None, volume=None):
        body = {"market": market, "side": side, "ord_type": "price" if side == "bid" else "market"}
        if price:
            body["price"] = price
        if volume:
            body["volume"] = volume
        return self._post("/v1/orders", body)["uuid"]

    def _order(self, oid):
        return self._get("/v1/order", {"uuid": oid}, auth=True)

    _order_path = "/v1/orders"
    _cancel_path = "/v1/order"

    def _limit_body(self, market, side, price, volume, cid):
        return {"market": market, "side": side, "ord_type": "limit", "price": price, "volume": volume, "identifier": cid}

    def _tick_from_instruments(self, market):
        data = self._get("/v1/orderbook/instruments", {"markets": market})
        return float(data[0]["tick_size"])


class Bithumb(_JwtExchange):
    name = "빗썸"
    base_url = "https://api.bithumb.com"

    def _place(self, market, side, price=None, volume=None):
        cid = "mb-" + uuid.uuid4().hex[:24]
        body = {"market": market, "side": side, "order_type": "price" if side == "bid" else "market"}
        if price:
            body["price"] = price
        if volume:
            body["volume"] = volume
        body["client_order_id"] = cid
        self._post("/v2/orders", body)
        return cid

    def _order(self, cid):
        return self._get("/v1/order", {"client_order_id": cid}, auth=True)

    _order_path = "/v2/orders"
    _cancel_path = "/v2/order"
    _cid_param = "client_order_id"

    def _limit_body(self, market, side, price, volume, cid):
        return {"market": market, "side": side, "order_type": "limit", "price": price, "volume": volume,
                "client_order_id": cid}


class Coinone(_JwtExchange):
    name = "코인원"
    base_url = "https://api.coinone.co.kr"

    @staticmethod
    def _split(market):
        q, t = market.split("-")
        return q, t

    def _check_co(self, r):
        try:
            data = r.json()
        except ValueError:
            raise ExchangeError(f"코인원 응답 오류 HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400 or data.get("result") != "success":
            code, msg = str(data.get("error_code", r.status_code)), str(data.get("error_msg", data))
            low = msg.lower()
            if code == "104" or "not exist" in low or "not found" in low or "존재하지" in msg:
                raise OrderNotFound(f"코인원 주문 없음 {code}: {msg}")
            raise ExchangeError(f"코인원 오류 {code}: {msg}")
        return data

    def _public(self, path, params=None):
        _pace(self.name)
        return self._check_co(requests.get(self.base_url + path, params=params, timeout=TIMEOUT))

    def _private(self, path, body=None):
        if not self.access_key or not self.secret_key:
            raise ExchangeError("API 키가 설정되지 않았습니다.")
        payload = {"access_token": self.access_key, "nonce": str(uuid.uuid4()), **(body or {})}
        encoded = base64.b64encode(json.dumps(payload).encode())
        sig = hmac.new(self.secret_key.encode(), encoded, hashlib.sha512).hexdigest()
        headers = {"Content-Type": "application/json", "X-COINONE-PAYLOAD": encoded.decode(), "X-COINONE-SIGNATURE": sig}
        _pace(self.name)
        return self._check_co(requests.post(self.base_url + path, data=encoded, headers=headers, timeout=TIMEOUT))

    # 시세
    def prices(self, markets):
        out = {}
        if len(markets) > 1:
            try:
                data = self._public("/public/v2/ticker_new/KRW")
                by = {t["target_currency"].upper(): float(t["last"]) for t in data["tickers"]}
                out = {m: by[self._split(m)[1]] for m in markets if self._split(m)[1] in by}
            except ExchangeError:
                out = {}
        for m in markets:
            if m not in out:
                q, t = self._split(m)
                data = self._public(f"/public/v2/ticker_new/{q}/{t}")
                out[m] = float(data["tickers"][0]["last"])
        return out

    def candles(self, market, unit_minutes=60, count=200, to=None):
        """업비트 형식으로 변환해서 반환 (최신이 앞)"""
        q, t = self._split(market)
        interval = {1: "1m", 3: "3m", 5: "5m", 10: "10m", 15: "15m", 30: "30m", 60: "1h", 240: "4h"}[unit_minutes]
        params = {"interval": interval, "size": min(count, 500)}
        if to:
            dt = datetime.strptime(to.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            params["timestamp"] = int(dt.timestamp() * 1000) - 1
        data = self._public(f"/public/v2/chart/{q}/{t}", params)
        out = []
        for c in data.get("chart", []):
            ts = datetime.fromtimestamp(int(c["timestamp"]) / 1000, tz=timezone.utc)
            out.append({"candle_date_time_utc": ts.strftime("%Y-%m-%dT%H:%M:%S"),
                        "candle_date_time_kst": (ts + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S"),
                        "opening_price": c["open"], "high_price": c["high"], "low_price": c["low"], "trade_price": c["close"]})
        out.sort(key=lambda x: x["candle_date_time_utc"], reverse=True)
        return out

    # 계좌
    def _bal_pair(self):
        data = self._private("/v2.1/account/balance/all")["balances"]
        total = {b["currency"].upper(): float(b["available"]) + float(b.get("limit") or 0) for b in data}
        avail = {b["currency"].upper(): float(b["available"]) for b in data}
        return total, avail

    def balances(self):
        """보유 수량 (주문에 묶인 수량 포함)"""
        return self._bal_pair()[0]

    # 주문 (마틴봇 시장가)
    def _place(self, market, side, price=None, volume=None):
        q, t = self._split(market)
        body = {"side": "BUY" if side == "bid" else "SELL", "quote_currency": q, "target_currency": t, "type": "MARKET"}
        if price:
            body["amount"] = price
        if volume:
            body["qty"] = volume
        return self._private("/v2.1/order", body)["order_id"]

    _detail_path = "/v2.1/order/detail"

    def _order(self, oid, market, key="order_id"):
        """주문 상세. 공식 문서 경로(/order/detail)를 먼저 쓰고, 안 되면 예전 경로(/order/info)"""
        q, t = self._split(market)
        body = {key: oid, "quote_currency": q, "target_currency": t}
        try:
            return self._private(self._detail_path, body)["order"]
        except OrderNotFound:
            raise
        except ExchangeError:
            alt = "/v2.1/order/info" if self._detail_path == "/v2.1/order/detail" else "/v2.1/order/detail"
            o = self._private(alt, body)["order"]
            Coinone._detail_path = alt
            return o

    @staticmethod
    def _fill_from_order(o, side):
        """주문 내역으로 계산한 체결 결과 (수수료가 코인/원화 어느 쪽으로 떼였는지 자동 판별)"""
        if not o:
            return None
        qty, px = float(o.get("executed_qty") or 0), float(o.get("average_executed_price") or 0)
        if qty <= 0 or px <= 0:
            return None
        fee, rate = float(o.get("fee") or 0), float(o.get("fee_rate") or 0)
        coin_fee = fee_is_coin(fee, rate, qty, px)
        traded = float(o.get("traded_amount") or 0)
        gross = traded if (side == "bid" and traded > 0) else qty * px
        if side == "bid":
            vol, krw = qty - (fee if coin_fee else 0), gross + (0 if coin_fee else fee)
        else:
            vol, krw = qty, gross - (fee * px if coin_fee else fee)
        return {"volume": vol, "krw": krw, "price": krw / vol if vol else px, "fee": fee * px if coin_fee else fee}

    def _settle(self, oid, market, side, before_krw, before_coin):
        """코인원 체결 확인.
        수량은 이 코인 잔고 변화로(가장 정확), 원화는 잔고 변화와 주문 내역을 비교해서 정합니다.
        같은 계좌에서 그리드 봇의 지정가 주문이 그 사이 체결되면 원화 잔고 변화에 섞이므로,
        둘이 1% 넘게 다르면 주문 내역 금액을 씁니다."""
        coin = market.split("-")[1]
        o = None
        for _ in range(20):
            time.sleep(0.5)
            try:
                o = self._order(oid, market)
            except (ExchangeError, requests.RequestException):
                continue
            if o.get("status") in ("FILLED", "CANCELED", "PARTIALLY_CANCELED") or str(o.get("status", "")).startswith("CANCELED"):
                break
        time.sleep(0.5)
        try:
            b = self.balances()
        except (ExchangeError, requests.RequestException):
            b = None
        by_order = self._fill_from_order(o, side)
        fee = by_order["fee"] if by_order else float((o or {}).get("fee") or 0)
        if b is not None:
            dk, dc = b.get("KRW", 0) - before_krw, b.get(coin, 0) - before_coin
            if (side == "bid" and dc > 0) or (side == "ask" and dc < 0):
                vol = abs(dc)
                krw = -dk if side == "bid" else dk
                if by_order and (krw <= 0 or abs(krw - by_order["krw"]) > max(by_order["krw"] * 0.01, 50)):
                    krw = by_order["krw"]
                if krw > 0:
                    return {"volume": vol, "krw": krw, "price": krw / vol, "fee": fee}
        if by_order:
            return by_order
        raise OrderUnconfirmed("코인원 주문 체결을 확인하지 못했습니다. 거래소 앱에서 직접 확인해 주세요.")

    # ---------- 지정가 (그리드용) ----------
    def market_info(self, market):
        q, t = self._split(market)
        m = self._public(f"/public/v2/markets/{q}/{t}")["markets"][0]
        return {"tick": float(m["price_unit"]), "qty_step": float(m["qty_unit"]),
                "min_krw": float(m.get("min_order_amount") or 5000)}

    def limit_order(self, market, side, price, volume, cid, tick=1.0, qty_step=1e-8):
        q, t = self._split(market)
        body = {"side": "BUY" if side == "bid" else "SELL", "quote_currency": q, "target_currency": t,
                "type": "LIMIT", "price": fmt_num(price, tick), "qty": fmt_num(volume, qty_step),
                "post_only": False, "user_order_id": cid}
        with self.lock:
            try:
                self._private("/v2.1/order", body)
            except requests.RequestException as e:
                raise OrderUnconfirmed(f"주문 전송 중 연결 오류({type(e).__name__})")
        return cid

    def get_order(self, cid, market):
        o = self._order(cid, market, key="user_order_id")
        st = str(o.get("status", ""))
        state = "open" if st in ("LIVE", "PARTIALLY_FILLED") else ("done" if st == "FILLED" else "cancel")
        qty, px = float(o.get("executed_qty") or 0), float(o.get("average_executed_price") or 0)
        fee, rate = float(o.get("fee") or 0), float(o.get("fee_rate") or 0)
        return {"state": state, "qty": qty, "avg": px, "funds": qty * px, "fee": fee,
                "fee_coin": fee_is_coin(fee, rate, qty, px), "remain": float(o.get("remain_qty") or 0)}

    def cancel_order(self, cid, market):
        q, t = self._split(market)
        with self.lock:
            try:
                self._private("/v2.1/order/cancel", {"user_order_id": cid, "quote_currency": q, "target_currency": t})
            except OrderNotFound:
                pass
            except ExchangeError:
                pass  # 이미 체결/취소된 경우 — 최종 상태는 get_order 로 확인


EXCHANGES = {"upbit": Upbit, "bithumb": Bithumb, "coinone": Coinone}
EXCHANGE_NAMES = {"upbit": "업비트", "bithumb": "빗썸", "coinone": "코인원"}


def exchange_class(name):
    return EXCHANGES.get(name, Upbit)


class Paper:
    """모의투자: 실제 시세를 받아오되 주문은 가상으로 체결"""

    def __init__(self, price_source, fee_pct=0.05):
        self.src = price_source
        self.name = f"모의투자({price_source.name} 시세)"
        self.fee = fee_pct / 100

    def price(self, market):
        return self.src.price(market)

    def prices(self, markets):
        return self.src.prices(markets)

    def candles(self, *a, **k):
        return self.src.candles(*a, **k)

    def balances(self):
        return {}

    def market_buy(self, market, krw):
        p = self.price(market)
        fee = krw * self.fee
        vol = (krw - fee) / p
        return {"volume": vol, "krw": krw, "price": p, "fee": fee}

    def market_sell(self, market, volume):
        p = self.price(market)
        gross = volume * p
        fee = gross * self.fee
        return {"volume": volume, "krw": gross - fee, "price": p, "fee": fee}


def make_exchange(cfg):
    real = exchange_class(cfg["exchange"])(cfg.get("access_key"), cfg.get("secret_key"))
    if cfg["mode"] == "live":
        return real
    return Paper(real, cfg.get("fee_pct", 0.05))
