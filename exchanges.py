"""업비트 / 빗썸 API 연동 + 모의투자 거래소

두 거래소 모두 JWT(HS256) + query_hash(SHA512) 인증 방식이며 마켓 코드는 'KRW-BTC' 형식입니다.
 - 업비트: https://docs.upbit.com
 - 빗썸(API 2.0): https://apidocs.bithumb.com
"""
import hashlib
import json
import math
import time
import uuid
from urllib.parse import urlencode, unquote

import jwt
import requests

TIMEOUT = 10


class ExchangeError(Exception):
    pass


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
        data = self._get("/v1/ticker", {"markets": market})
        return float(data[0]["trade_price"])

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
        oid = self._place(market, "bid", price=str(int(krw)))
        return self._settle(oid, market, "bid", before_krw, before_coin)

    def market_sell(self, market, volume):
        coin = market.split("-")[1]
        before_krw, before_coin = self.balance("KRW"), self.balance(coin)
        vol = _floor(min(volume, before_coin))
        if vol <= 0:
            raise ExchangeError(f"{coin} 보유 수량이 없습니다.")
        oid = self._place(market, "ask", volume=f"{vol:.8f}")
        return self._settle(oid, market, "ask", before_krw, before_coin)

    def _settle(self, oid, market, side, before_krw, before_coin):
        """체결 결과 확인. 주문 조회 실패 시 잔고 변화로 계산."""
        coin = market.split("-")[1]
        for _ in range(20):
            time.sleep(0.5)
            try:
                o = self._order(oid)
            except ExchangeError:
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
        b = self.balances()
        dk, dc = b.get("KRW", 0) - before_krw, b.get(coin, 0) - before_coin
        if side == "bid" and dc > 0:
            return {"volume": dc, "krw": -dk, "price": -dk / dc, "fee": 0}
        if side == "ask" and dc < 0:
            return {"volume": -dc, "krw": dk, "price": dk / -dc, "fee": 0}
        raise ExchangeError("주문 체결을 확인하지 못했습니다. 거래소 앱에서 직접 확인해 주세요.")


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


class Paper:
    """모의투자: 실제 시세를 받아오되 주문은 가상으로 체결"""

    def __init__(self, price_source, fee_pct=0.05):
        self.src = price_source
        self.name = f"모의투자({price_source.name} 시세)"
        self.fee = fee_pct / 100

    def price(self, market):
        return self.src.price(market)

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
    cls = Bithumb if cfg["exchange"] == "bithumb" else Upbit
    real = cls(cfg.get("access_key"), cfg.get("secret_key"))
    if cfg["mode"] == "live":
        return real
    return Paper(real, cfg.get("fee_pct", 0.05))
