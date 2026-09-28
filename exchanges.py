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

import jwt
import requests

TIMEOUT = 10


class ExchangeError(Exception):
    pass


class OrderUnconfirmed(ExchangeError):
    """주문은 나갔을 수 있지만 체결 여부를 확인하지 못함 → 중복 주문 방지를 위해 해당 코인 일시정지"""


def _floor(x, digits=8):
    f = 10 ** digits
    return math.floor(x * f) / f


class _JwtExchange:
    name = ""
    base_url = ""

    def __init__(self, access_key="", secret_key=""):
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()

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
            raise ExchangeError(f"{self.name} 오류 HTTP {r.status_code}: {err}")
        return data

    def _get(self, path, params=None, auth=False):
        headers = self._headers(params) if auth else {}
        r = requests.get(self.base_url + path, params=params, headers=headers, timeout=TIMEOUT)
        return self._check(r)

    def _post(self, path, body):
        r = requests.post(self.base_url + path, data=json.dumps(body), headers=self._headers(body), timeout=TIMEOUT)
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

    # ---------- 주문 ----------
    def market_buy(self, market, krw):
        coin = market.split("-")[1]
        before_krw, before_coin = self.balance("KRW"), self.balance(coin)
        if before_krw < krw * 1.003:  # 수수료 여유분 포함
            raise ExchangeError(f"원화 잔고 부족: 필요 {krw:,.0f}원 / 보유 {before_krw:,.0f}원")
        oid = self._place_safe(market, "bid", price=str(int(krw)))
        return self._settle(oid, market, "bid", before_krw, before_coin)

    def market_sell(self, market, volume):
        coin = market.split("-")[1]
        before_krw, before_coin = self.balance("KRW"), self.balance(coin)
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
            raise ExchangeError(f"코인원 오류 {data.get('error_code', r.status_code)}: {data.get('error_msg', data)}")
        return data

    def _public(self, path, params=None):
        return self._check_co(requests.get(self.base_url + path, params=params, timeout=TIMEOUT))

    def _private(self, path, body=None):
        if not self.access_key or not self.secret_key:
            raise ExchangeError("API 키가 설정되지 않았습니다.")
        payload = {"access_token": self.access_key, "nonce": str(uuid.uuid4()), **(body or {})}
        encoded = base64.b64encode(json.dumps(payload).encode())
        sig = hmac.new(self.secret_key.encode(), encoded, hashlib.sha512).hexdigest()
        headers = {"Content-Type": "application/json", "X-COINONE-PAYLOAD": encoded.decode(), "X-COINONE-SIGNATURE": sig}
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
    def balances(self):
        data = self._private("/v2.1/account/balance/all")
        return {b["currency"].upper(): float(b["available"]) + float(b.get("limit") or 0) for b in data["balances"]}

    # 주문
    def _place(self, market, side, price=None, volume=None):
        q, t = self._split(market)
        body = {"side": "BUY" if side == "bid" else "SELL", "quote_currency": q, "target_currency": t, "type": "MARKET"}
        if price:
            body["amount"] = price
        if volume:
            body["qty"] = volume
        return self._private("/v2.1/order", body)["order_id"]

    def _order(self, oid, market):
        q, t = self._split(market)
        return self._private("/v2.1/order/info", {"order_id": oid, "quote_currency": q, "target_currency": t})["order"]

    def _settle(self, oid, market, side, before_krw, before_coin):
        """코인원: 주문 완료를 확인한 뒤 잔고 변화로 정확한 체결 금액·수량 계산"""
        coin = market.split("-")[1]
        o = None
        for _ in range(20):
            time.sleep(0.5)
            try:
                o = self._order(oid, market)
            except (ExchangeError, requests.RequestException):
                continue
            if o.get("status") in ("FILLED", "CANCELED", "PARTIALLY_CANCELED"):
                break
        time.sleep(0.5)
        try:
            b = self.balances()
        except (ExchangeError, requests.RequestException):
            b = None
        if b is not None:
            dk, dc = b.get("KRW", 0) - before_krw, b.get(coin, 0) - before_coin
            if side == "bid" and dc > 0 and dk < 0:
                return {"volume": dc, "krw": -dk, "price": -dk / dc, "fee": float((o or {}).get("fee") or 0)}
            if side == "ask" and dc < 0 and dk > 0:
                return {"volume": -dc, "krw": dk, "price": dk / -dc, "fee": float((o or {}).get("fee") or 0)}
        if o and float(o.get("executed_qty") or 0) > 0:
            vol, px = float(o["executed_qty"]), float(o.get("average_executed_price") or 0)
            fee = float(o.get("fee") or 0)
            if px > 0:
                gross = vol * px
                return {"volume": vol, "krw": gross + fee if side == "bid" else gross - fee, "price": px, "fee": fee}
        raise OrderUnconfirmed("코인원 주문 체결을 확인하지 못했습니다. 거래소 앱에서 직접 확인해 주세요.")


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
