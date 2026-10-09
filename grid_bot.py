"""그리드 자동매매 실행기 — Martin Bot과 같은 프로그램 안에서 별도 스레드로 동작 (여러 코인)

설정 = 공통(거래소·모드·API 키·수수료·확인 주기) + 코인별(범위·간격·칸 수·금액·손절)
코인마다 상태 파일·주문·일시정지·손절이 따로이고, 시작/중지는 함께 합니다.

안전장치
 - 주문마다 봇이 정한 ID(cid)를 붙이고, 주문을 보내기 '전에' 상태 파일에 저장합니다.
   전송 중 연결이 끊겨도 다음 확인 때 그 ID로 거래소에 조회해서 실제로 들어갔는지 판단합니다.
 - 자기가 낸 주문(ID)만 조회·취소합니다. Martin Bot 주문이나 직접 낸 주문은 건드리지 않습니다.
 - 같은 거래소·같은 키를 쓰는 Martin Bot과는 주문 잠금(account_lock)을 공유해 서로 끼어들지 않습니다.
"""
import csv
import json
import os
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime

import requests

from exchanges import (exchange_class, ExchangeError, OrderUnconfirmed, OrderNotFound, Paper, floor_step)
from grid_strategy import (GRID_GLOBAL_DEFAULTS, GRID_COIN_DEFAULTS, build_levels, new_cells, buy_window,
                           can_place_buy, validate, summary, conflict_with_martin, apply_range_mode)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "grid_config.json")
RUN_FLAG = os.path.join(DATA_DIR, "grid_running.flag")
os.makedirs(DATA_DIR, exist_ok=True)
SECRET_KEYS = ("access_key", "secret_key")
COIN_LEVEL_KEYS = ("lower", "upper", "grids", "spacing", "krw_per_grid", "range_mode", "gap_pct")
LIVE_EDITABLE_GLOBAL = ("check_interval_sec", "fee_pct")
LIVE_EDITABLE_COIN = ("stop_loss_enabled", "stop_loss_price", "max_buy_orders")
MAX_PLACE_PER_TICK = 8
MAX_COINS = 10


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cast(default, v):
    if isinstance(default, bool):
        return v in (True, "true", "on", 1, "1")
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    return str(v).strip()


def normalize_coin(c):
    out = dict(GRID_COIN_DEFAULTS)
    for k, v in (c or {}).items():
        if k in GRID_COIN_DEFAULTS and v is not None and v != "":
            out[k] = _cast(GRID_COIN_DEFAULTS[k], v)
    out["coin"] = out["coin"].strip().upper()
    return out


def load_config():
    raw = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    cfg = dict(GRID_GLOBAL_DEFAULTS)
    for k, v in raw.items():
        if k in GRID_GLOBAL_DEFAULTS:
            try:
                cfg[k] = _cast(GRID_GLOBAL_DEFAULTS[k], v)
            except (TypeError, ValueError):
                pass
    if "coins" in raw:
        coins = raw["coins"]
    elif "coin" in raw:          # 예전(코인 1개) 설정 → 코인 목록으로 이전
        coins = [{k: v for k, v in raw.items() if k in GRID_COIN_DEFAULTS}]
    else:
        coins = [dict(GRID_COIN_DEFAULTS)]
    out = []
    for c in coins:
        try:
            out.append(normalize_coin(c))
        except (TypeError, ValueError):
            pass
    cfg["coins"] = out or [dict(GRID_COIN_DEFAULTS)]
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


class PaperBook:
    """모의투자용 지정가 거래소: 실제 시세를 받아오고, 가격이 주문가에 닿으면 체결된 것으로 처리.
    코인마다 주문 장부(books[market])를 따로 두며, 각 장부는 그 코인의 상태 파일에 함께 저장됩니다."""

    def __init__(self, real, fee_pct):
        self.real = real
        self.name = f"모의투자({real.name} 시세)"
        self.fee = fee_pct / 100
        self.books = {}
        self._market_paper = Paper(real, fee_pct)

    def price(self, market):
        return self.real.price(market)

    def prices(self, markets):
        return self.real.prices(markets)

    def market_info(self, market):
        try:
            return self.real.market_info(market)
        except Exception:
            return {"tick": 0.0, "qty_step": 1e-8, "min_krw": 5000.0}

    def observe(self, market, lo, hi):
        for o in self.books.get(market, {}).values():
            if o["state"] != "open":
                continue
            if (o["side"] == "bid" and lo <= o["price"]) or (o["side"] == "ask" and hi >= o["price"]):
                o["state"] = "done"

    def limit_order(self, market, side, price, volume, cid, tick=1.0, qty_step=1e-8):
        self.books.setdefault(market, {})[cid] = {"side": side, "price": price, "qty": volume, "state": "open"}
        return cid

    def get_order(self, cid, market):
        book = self.books.get(market, {})
        o = book.get(cid)
        if not o:
            raise OrderNotFound("모의 주문 없음")
        funds = o["price"] * o["qty"]
        done = o["state"] == "done"
        res = {"state": o["state"], "qty": o["qty"] if done else 0.0, "avg": o["price"],
               "funds": funds if done else 0.0, "fee": funds * self.fee if done else 0.0, "fee_coin": False,
               "remain": 0.0 if done else o["qty"]}
        if o["state"] != "open":
            book.pop(cid, None)
        return res

    def cancel_order(self, cid, market):
        o = self.books.get(market, {}).get(cid)
        if o and o["state"] == "open":
            o["state"] = "cancel"

    def market_sell(self, market, volume):
        return self._market_paper.market_sell(market, volume)


class GridCoin:
    """코인 하나의 격자 — 상태·주문·체결 처리"""

    def __init__(self, bot, coin):
        self.bot, self.coin = bot, coin
        self.last_price = None
        self._prev = None
        self._rr = 0
        self.state = self._load()

    # ---------- 상태 ----------
    @property
    def cfg(self):
        return self.bot.coin_cfg(self.coin)

    @property
    def key(self):
        g = self.bot.cfg
        return f"{g['mode']}_{g['exchange']}_{self.coin}"

    @property
    def market(self):
        return f"KRW-{self.coin}"

    def _path(self):
        return os.path.join(DATA_DIR, f"grid_state_{self.key}.json")

    @staticmethod
    def _empty():
        return {"levels": [], "cells": [], "minfo": {}, "paused": "", "paper_orders": {},
                "stats": {"round_trips": 0, "realized": 0.0, "fees": 0.0, "history": []}}

    def _load(self):
        st = self._empty()
        if os.path.exists(self._path()):
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            st.update({k: v for k, v in d.items() if k in st})
            st["stats"] = {**self._empty()["stats"], **d.get("stats", {})}
        return st

    def reload(self):
        self.state = self._load()
        self._prev = None

    def save(self):
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path())

    def active_cells(self):
        return [c for c in self.state["cells"] if c["st"] != "idle"]

    def open_cells(self):
        return [c for c in self.state["cells"] if c["st"] in ("buy", "sell")]

    def log(self, msg, notify=False):
        self.bot.log(f"{self.coin} {msg}", notify=notify)

    def warn_once(self, key, msg, every=600, notify=False):
        self.bot.warn_once(f"{self.coin}:{key}", f"{self.coin} {msg}", every=every, notify=notify)

    def log_trade(self, side, c, price, qty, krw, fee, profit=None):
        path = os.path.join(DATA_DIR, f"grid_trades_{self.key}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각", "구분", "칸", "체결가", "수량", "원화", "수수료", "손익"])
            w.writerow([now(), side, c["i"] + 1 if c else "-", round(price, 4), f"{qty:.8f}", round(krw), round(fee, 2),
                        "" if profit is None else round(profit)])

    # ---------- 준비 ----------
    def prepare(self, ex):
        src = ex.real if isinstance(ex, PaperBook) else ex
        try:
            mi = src.market_info(self.market)
            if mi.get("tick"):
                self.state["minfo"] = mi
        except Exception as e:
            self.warn_once("minfo", f"⚠ 호가 단위 조회 실패({e}) — 저장된 값으로 진행합니다.")
        mi = self.state.get("minfo") or {"tick": 0.0, "qty_step": 1e-8, "min_krw": 5000.0}
        if isinstance(ex, PaperBook):
            ex.books[self.market] = self.state["paper_orders"]
        with self.bot.lock:
            levels = build_levels(self.cfg, mi["tick"])
            if not self.state["cells"] or (not self.active_cells() and levels != self.state["levels"]):
                self.state["levels"] = levels
                self.state["cells"] = new_cells(levels)
                self.save()
                self.log(f"격자 {len(levels) - 1}칸 생성: {levels[0]:,.0f} ~ {levels[-1]:,.0f} (호가 단위 {mi['tick']:g})")
        return mi

    # ---------- 한 번의 확인 ----------
    def tick(self, ex, price, mi):
        prev = self._prev if self._prev is not None else price
        lo, hi = min(prev, price), max(prev, price)
        self.last_price = price
        self._prev = price
        if isinstance(ex, PaperBook):
            ex.observe(self.market, lo, hi)
        cfg = self.cfg
        if cfg["stop_loss_enabled"] and cfg["stop_loss_price"] > 0 and price <= cfg["stop_loss_price"]:
            self.stop_loss(ex, price)
            return
        tick = mi["tick"]
        cells = self.state["cells"]

        # 1) 걸어둔 주문 확인 — 가격이 지나간 주문 + 아직 접수 확인 안 된 주문 + 돌아가며 2개
        open_cells = self.open_cells()
        must = [c for c in open_cells if not c["ok"] or (c["st"] == "buy" and c["buy"] >= lo - tick)
                or (c["st"] == "sell" and c["sell"] <= hi + tick)]
        rest = [c for c in open_cells if c not in must]
        if rest:
            for k in range(min(2, len(rest))):
                must.append(rest[(self._rr + k) % len(rest)])
            self._rr = (self._rr + 2) % max(1, len(rest))
        for c in must:
            if not self.bot.running or self.state["paused"]:
                return
            self._check_cell(ex, c)
        if self.state["paused"] or not self.bot.running:
            return

        # 2) 산 칸은 바로 위 가격에 매도
        for c in cells:
            if c["st"] == "hold" and self.bot.running:
                self._place_sell(ex, c, mi)

        # 3) 현재가에서 먼 매수 주문은 풀기 (미리 걸어둘 매수 수를 정했을 때)
        place_ok, keep = buy_window(cells, price, tick, int(cfg.get("max_buy_orders") or 0))
        for i, c in enumerate(cells):
            if c["st"] == "buy" and c["ok"] and i not in keep and self.bot.running:
                self._release_buy(ex, c)

        # 4) 현재가 아래 빈 칸에 매수 (현재가에 가까운 칸부터, 한 번에 최대 8개)
        if time.time() < self.bot.krw_short_until:
            return
        placed = 0
        for i in range(len(cells) - 1, -1, -1):
            if placed >= MAX_PLACE_PER_TICK or not self.bot.running:
                break
            if i in place_ok and can_place_buy(cells, i, price, tick):
                if self._place_buy(ex, cells[i], mi):
                    placed += 1
                elif time.time() < self.bot.krw_short_until:
                    break

    def _release_buy(self, ex, c):
        """현재가에서 멀어진 매수 주문 취소 → 묶인 원화를 풂. 가격이 다시 내려오면 그때 다시 겁니다"""
        try:
            ex.cancel_order(c["cid"], self.market)
            o = ex.get_order(c["cid"], self.market)
        except OrderNotFound:
            return
        except (ExchangeError, requests.RequestException) as e:
            self.warn_once("release", f"⚠ 먼 칸 매수 주문 취소 실패: {e}", every=300)
            return
        if o["state"] == "open":
            return          # 취소가 아직 반영 안 됨 → 다음 확인 때 다시
        with self.bot.lock:
            self._absorb(c, o)      # 그 사이 체결됐으면 보유로, 아니면 빈 칸으로
            self.save()
        if o["qty"] <= 0:
            self.warn_once(f"rel{c['i']}", f"{c['i'] + 1}칸 매수 주문 해제 (현재가에서 멀어져 원화를 풂)", every=60)

    def _new_cid(self):
        return "grd-" + uuid.uuid4().hex[:24]

    def _place_buy(self, ex, c, mi):
        qty = floor_step(self.cfg["krw_per_grid"] / c["buy"], mi["qty_step"])
        if qty * c["buy"] < mi["min_krw"]:
            self.warn_once("minbuy", f"⚠ {c['i'] + 1}칸 매수 금액이 최소 주문금액보다 작습니다. 칸당 금액을 늘려주세요.")
            return False
        with self.bot.lock:
            c.update(st="buy", cid=self._new_cid(), ok=False, oqty=qty, at=now())
            self.save()      # 보내기 전에 ID 저장 → 연결이 끊겨도 다음에 조회 가능
        try:
            ex.limit_order(self.market, "bid", c["buy"], qty, c["cid"], mi["tick"], mi["qty_step"])
        except OrderUnconfirmed as e:
            self.log(f"⚠ {c['i'] + 1}칸 매수 접수 확인 불가({e}) — 다음 확인 때 주문 ID로 다시 조회합니다.")
            return True
        except ExchangeError as e:
            with self.bot.lock:
                c.update(st="idle", cid="", ok=False)
                self.save()
            if "잔고" in str(e) or "insufficient" in str(e).lower() or "부족" in str(e):
                self.bot.krw_short_until = time.time() + 60
                self.bot.warn_once("krw", f"⚠ 원화 잔고가 부족해 매수 주문을 더 걸지 못했습니다 ({e}). 1분 뒤 다시 시도합니다.",
                                   notify=True)
            else:
                self.warn_once("buyerr", f"⚠ {c['i'] + 1}칸 매수 주문 실패: {e}", every=120, notify=True)
            return False
        with self.bot.lock:
            c["ok"] = True
            self.save()
        return True

    def _place_sell(self, ex, c, mi):
        qty = floor_step(c["qty"], mi["qty_step"])
        if qty <= 0 or qty * c["sell"] < mi["min_krw"]:
            self.warn_once(f"dust{c['i']}", f"⚠ {c['i'] + 1}칸 보유량({c['qty']:.8f})이 최소 주문금액보다 작아 매도 주문을 못 겁니다.")
            return
        with self.bot.lock:
            c.update(st="sell", cid=self._new_cid(), ok=False, oqty=qty)
            self.save()
        try:
            ex.limit_order(self.market, "ask", c["sell"], qty, c["cid"], mi["tick"], mi["qty_step"])
        except OrderUnconfirmed as e:
            self.log(f"⚠ {c['i'] + 1}칸 매도 접수 확인 불가({e}) — 다음 확인 때 주문 ID로 다시 조회합니다.")
            return
        except ExchangeError as e:
            with self.bot.lock:
                c.update(st="hold", cid="", ok=False)
                self.save()
            self.warn_once(f"sellerr{c['i']}", f"⚠ {c['i'] + 1}칸 매도 주문 실패: {e}", every=120, notify=True)
            return
        with self.bot.lock:
            c["ok"] = True
            self.save()

    def _check_cell(self, ex, c):
        try:
            o = ex.get_order(c["cid"], self.market)
        except OrderNotFound:
            with self.bot.lock:
                if not c["ok"]:
                    # 접수 확인 전에 끊겼는데 거래소에도 없음 → 주문이 안 들어간 것
                    c.update(st="idle" if c["st"] == "buy" else "hold", cid="", ok=False)
                    self.save()
                    return
            self.pause(f"{c['i'] + 1}칸 주문(ID {c['cid']})을 거래소에서 찾을 수 없습니다. "
                       "거래소 앱에서 직접 취소·체결했는지 확인하세요.", ex, skip=c)
            return
        except (ExchangeError, requests.RequestException) as e:
            self.warn_once("chk", f"⚠ 주문 조회 실패: {e}", every=120)
            return
        with self.bot.lock:
            c["ok"] = True
            if o["state"] == "open":
                return
            self._absorb(c, o)
            self.save()

    def _absorb(self, c, o):
        """체결(또는 취소) 결과를 칸에 반영"""
        side = c["st"]
        qty = o["qty"]
        if qty <= 0:
            c.update(st="idle" if side == "buy" else "hold", cid="", ok=False)
            return
        st = self.state["stats"]
        if side == "buy":
            net = qty - (o["fee"] if o["fee_coin"] else 0)
            cost = o["funds"] + (0 if o["fee_coin"] else o["fee"])
            fee_krw = o["fee"] * o["avg"] if o["fee_coin"] else o["fee"]
            c.update(st="hold", cid="", ok=False, qty=c["qty"] + net, cost=c["cost"] + cost, at=now())
            st["fees"] += fee_krw
            self.log_trade("매수", c, o["avg"], net, cost, fee_krw)
            self.log(f"🟢 {c['i'] + 1}칸 매수 체결 {cost:,.0f}원 @ {o['avg']:,.2f} → {c['sell']:,.2f}에 매도 대기")
        else:
            fee_krw = o["fee"] * o["avg"] if o["fee_coin"] else o["fee"]
            proceeds = o["funds"] - fee_krw
            part = min(1.0, qty / c["qty"]) if c["qty"] > 0 else 1.0
            cost_part = c["cost"] * part
            profit = proceeds - cost_part
            st["fees"] += fee_krw
            st["realized"] += profit
            self.log_trade("매도", c, o["avg"], qty, proceeds, fee_krw, profit)
            if part >= 0.999:
                st["round_trips"] += 1
                st["history"] = ([{"at": now(), "cell": c["i"] + 1, "buy": c["buy"], "sell": o["avg"],
                                   "cost": round(cost_part), "profit": round(profit)}] + st["history"])[:300]
                c.update(st="idle", cid="", ok=False, qty=0.0, cost=0.0, at="")
                self.log(f"🔴 {c['i'] + 1}칸 매도 체결 @ {o['avg']:,.2f} — 수익 {profit:+,.0f}원 "
                         f"(누적 {st['realized']:+,.0f}원 · {st['round_trips']}회)", notify=True)
            else:
                c.update(st="hold", cid="", ok=False, qty=c["qty"] - qty, cost=c["cost"] - cost_part)
                self.log(f"🔴 {c['i'] + 1}칸 일부 매도 {qty:.8f} — 수익 {profit:+,.0f}원, 남은 수량은 다시 매도 주문")

    def cancel_all(self, ex, skip=None):
        cells = [c for c in self.open_cells() if c is not skip]
        for c in cells:
            try:
                ex.cancel_order(c["cid"], self.market)
            except Exception as e:
                self.log(f"⚠ {c['i'] + 1}칸 주문 취소 실패: {e}")
        if cells and not isinstance(ex, PaperBook):
            time.sleep(1)
        for c in cells:
            try:
                o = ex.get_order(c["cid"], self.market)
            except OrderNotFound:
                with self.bot.lock:
                    if not c["ok"]:
                        c.update(st="idle" if c["st"] == "buy" else "hold", cid="", ok=False)
                continue
            except Exception as e:
                self.log(f"⚠ {c['i'] + 1}칸 주문 상태 확인 실패: {e}")
                continue
            with self.bot.lock:
                if o["state"] == "open":
                    self.log(f"⚠ {c['i'] + 1}칸 주문이 아직 살아 있습니다 — 거래소 앱에서 확인하세요.")
                    continue
                self._absorb(c, o)
        with self.bot.lock:
            self.save()

    def pause(self, reason, ex=None, skip=None):
        """이 코인만 멈춤 — 다른 Grid 주문은 취소하고 보유분은 그대로"""
        with self.bot.lock:
            self.state["paused"] = reason
            self.save()
        self.log(f"⛔ 일시정지 — {reason}", notify=True)
        if ex is not None:
            try:
                self.cancel_all(ex, skip=skip)
            except Exception as e:
                self.log(f"⚠ 주문 취소 중 오류: {e} — 거래소 앱에서 미체결 주문을 확인하세요.", notify=True)

    def stop_loss(self, ex, price):
        self.log(f"🛑 손절가 {self.cfg['stop_loss_price']:,.0f} 이탈 (현재 {price:,.0f}) — 주문 취소 후 보유분 전량 매도", notify=True)
        self.cancel_all(ex)
        try:
            self.sell_holdings(ex, "손절")
        except Exception as e:
            self.log(f"⚠ 손절 매도 실패: {e} — 거래소 앱에서 직접 확인하세요.", notify=True)
        with self.bot.lock:
            self.state["paused"] = f"손절가 이탈로 정리 후 멈췄습니다 ({now()}). 범위를 다시 정하고 [기록 비우기] 후 다시 시작하세요."
            self.save()

    def sell_holdings(self, ex, reason):
        hold = [c for c in self.state["cells"] if c["st"] == "hold" and c["qty"] > 0]
        vol, cost = sum(c["qty"] for c in hold), sum(c["cost"] for c in hold)
        if vol <= 0:
            return None
        fill = ex.market_sell(self.market, vol)
        profit = fill["krw"] - cost
        with self.bot.lock:
            st = self.state["stats"]
            st["realized"] += profit
            st["fees"] += fill.get("fee", 0)
            st["history"] = ([{"at": now(), "cell": "전체", "buy": cost / vol, "sell": fill["price"],
                               "cost": round(cost), "profit": round(profit), "reason": reason}] + st["history"])[:300]
            for c in hold:
                c.update(st="idle", cid="", ok=False, qty=0.0, cost=0.0, at="")
            self.save()
        self.log_trade(f"{reason} 매도", None, fill["price"], fill["volume"], fill["krw"], fill.get("fee", 0), profit)
        self.log(f"🔴 {reason}: 보유 {vol:.8f} 시장가 매도 {fill['krw']:,.0f}원 — 손익 {profit:+,.0f}원", notify=True)
        return profit

    def forget(self):
        with self.bot.lock:
            stats = self.state["stats"]
            self.state = self._empty()
            self.state["stats"] = stats
            self._prev = None
            self.save()
        self.log("기록(칸·주문)을 비웠습니다. 누적 손익은 유지됩니다. 다음 시작 때 현재 설정으로 격자를 새로 만듭니다.")

    def status(self):
        cfg, st, price = self.cfg, self.state, self.last_price
        cells = st["cells"]
        hold = [c for c in cells if c["st"] in ("hold", "sell")]
        qty = sum(c["qty"] for c in hold)
        cost = sum(c["cost"] for c in hold)
        value = qty * price if price else None
        unreal = (value * (1 - cfg["fee_pct"] / 100) - cost) if value is not None and qty else 0.0
        buy_krw = sum(c.get("oqty", 0) * c["buy"] for c in cells if c["st"] == "buy")
        tick = (st.get("minfo") or {}).get("tick", 0.0)
        return {
            "coin": self.coin, "enabled": cfg["enabled"], "price": price, "paused": st.get("paused", ""),
            "max_buy_orders": int(cfg.get("max_buy_orders") or 0),
            "cells": [{k: c.get(k) for k in ("i", "buy", "sell", "st", "qty", "cost", "at")} for c in cells],
            "stats": st["stats"],
            "total": {"hold_qty": qty, "hold_cost": cost, "value": value, "unrealized": unreal, "buy_orders_krw": buy_krw,
                      "budget": cfg["krw_per_grid"] * (len(cells) or cfg["grids"]),
                      "in_range": bool(cells and price and cells[0]["buy"] <= price <= cells[-1]["sell"])},
            "summary": summary(cfg, tick), "minfo": st.get("minfo", {}),
        }


class GridBot:
    def __init__(self, martin_bot):
        self.mb = martin_bot
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.running = False
        self.stopping = False
        self.thread = None
        self.logs = deque(maxlen=400)
        self.last_error = ""
        self.krw_short_until = 0
        self._warned = {}
        self.slots = {}
        self._sync_slots()

    # ---------- 도우미 ----------
    def coin_cfg(self, coin):
        c = next((c for c in self.cfg["coins"] if c["coin"] == coin), None) or {**GRID_COIN_DEFAULTS, "coin": coin}
        return {**{k: self.cfg[k] for k in GRID_GLOBAL_DEFAULTS}, **c}

    def _sync_slots(self, reload_all=False):
        names = [c["coin"] for c in self.cfg["coins"]]
        for n in list(self.slots):
            if n not in names:
                del self.slots[n]
        for n in names:
            if n not in self.slots:
                self.slots[n] = GridCoin(self, n)
            elif reload_all:
                self.slots[n].reload()

    def enabled_slots(self):
        return [self.slots[c["coin"]] for c in self.cfg["coins"] if c["enabled"] and c["coin"] in self.slots]

    def log(self, msg, notify=False):
        line = f"[{now()}] {msg}"
        self.logs.appendleft(line)
        print("[grid] " + line, flush=True)
        if notify:
            self._telegram(msg)

    def warn_once(self, key, msg, every=600, notify=False):
        t = time.time()
        if t - self._warned.get(key, 0) >= every:
            self._warned[key] = t
            self.log(msg, notify=notify)

    def _telegram(self, msg):
        g = self.mb.cfg
        t, c = g.get("telegram_token"), g.get("telegram_chat_id")
        if not t or not c:
            return
        try:
            mode = "실전" if self.cfg["mode"] == "live" else "모의"
            requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                          json={"chat_id": c, "text": f"[Grid·{mode}] {msg}"}, timeout=5)
        except Exception:
            pass

    # ---------- 거래소 ----------
    def _keys(self, cfg=None):
        c, m = cfg or self.cfg, self.mb.cfg
        if c["access_key"]:
            return c["access_key"], c["secret_key"]
        if m.get("exchange") == c["exchange"]:
            return m.get("access_key", ""), m.get("secret_key", "")
        return "", ""

    def make_exchange(self):
        real = exchange_class(self.cfg["exchange"])(*self._keys())
        if self.cfg["mode"] == "live":
            return real
        return PaperBook(real, self.cfg["fee_pct"])

    # ---------- 설정 ----------
    def public_config(self):
        c = json.loads(json.dumps(self.cfg))
        for k in SECRET_KEYS:
            if c.get(k):
                c[k] = c[k][:4] + "••••••••"
        return c

    def check_conflict(self, grid_cfg=None, martin_cfg=None):
        """실전 + 같은 거래소에서 Martin과 같은 코인이면 막음 (grid_cfg 는 coins 목록이 있는 전체 설정)"""
        g = grid_cfg or self.cfg
        errs = []
        for c in g["coins"]:
            msg = conflict_with_martin({**{k: g[k] for k in GRID_GLOBAL_DEFAULTS}, **c}, martin_cfg or self.mb.cfg,
                                       from_martin=martin_cfg is not None)
            if msg:
                errs.append(msg)
        return errs

    def update_config(self, new):
        with self.lock:
            merged = json.loads(json.dumps(self.cfg))
            for k, v in new.items():
                if k not in GRID_GLOBAL_DEFAULTS:
                    continue
                if k in SECRET_KEYS and isinstance(v, str) and "•" in v:
                    continue
                try:
                    merged[k] = _cast(GRID_GLOBAL_DEFAULTS[k], v)
                except (TypeError, ValueError):
                    return [f"'{k}' 값이 올바르지 않습니다."]
            if "coins" in new:
                try:
                    merged["coins"] = [normalize_coin(c) for c in new["coins"]][:MAX_COINS]
                except (TypeError, ValueError):
                    return ["코인 설정에 숫자가 아닌 값이 있습니다."]
            names = [c["coin"] for c in merged["coins"]]
            if not names:
                return ["코인을 한 개 이상 등록하세요."]
            if len(names) != len(set(names)):
                return ["Grid에 같은 코인이 두 번 등록되어 있습니다."]

            identity = any(merged[k] != self.cfg[k] for k in ("exchange", "mode"))
            changed_global = [k for k in GRID_GLOBAL_DEFAULTS if merged[k] != self.cfg[k]]
            old = {c["coin"]: c for c in self.cfg["coins"]}
            if self.running or self.stopping:
                if any(k not in LIVE_EDITABLE_GLOBAL for k in changed_global) or set(names) != set(old):
                    return ["실행 중에는 수수료·확인 주기·손절만 바꿀 수 있습니다. 거래소·모드·코인 목록·범위를 바꾸려면 먼저 Grid를 중지하세요."]
            if identity and any(s.active_cells() for s in self.slots.values()):
                return ["코인을 들고 있거나 주문이 걸린 칸이 있어 거래소·모드를 바꿀 수 없습니다. 중지 후 [정리]하고 바꾸세요."]
            errs = []
            for name in old:
                s = self.slots.get(name)
                if name not in names and s and s.active_cells() and not identity:
                    errs.append(f"{name} 은(는) 들고 있는 코인이나 걸린 주문이 있어 삭제할 수 없습니다. 끄기만 하거나 [정리] 후 삭제하세요.")
            # 코인별 계산·검증
            for c in merged["coins"]:
                tick, min_krw = 0.0, 5000.0
                try:
                    mi = exchange_class(merged["exchange"])().market_info(f"KRW-{c['coin']}")
                    tick, min_krw = mi["tick"], mi["min_krw"]
                except Exception:
                    pass
                apply_range_mode(c, tick)
                o = old.get(c["coin"])
                s = self.slots.get(c["coin"])
                if o and not identity:
                    ch = [k for k in GRID_COIN_DEFAULTS if c[k] != o[k]]
                    if (self.running or self.stopping) and any(k not in LIVE_EDITABLE_COIN for k in ch):
                        errs.append(f"{c['coin']}: 실행 중에는 손절만 바꿀 수 있습니다. 범위·칸·금액·켜고 끄기는 Grid를 중지한 뒤 바꾸세요.")
                        continue
                    if s and s.active_cells() and any(k in COIN_LEVEL_KEYS for k in ch):
                        errs.append(f"{c['coin']}: 코인을 들고 있거나 주문이 걸린 칸이 있어 범위·칸·금액을 바꿀 수 없습니다. "
                                    "중지 후 [정리]로 보유분을 매도하거나 기록을 비운 뒤 바꾸세요.")
                        continue
                errs += [f"{c['coin']}: {e}" for e in validate({**{k: merged[k] for k in GRID_GLOBAL_DEFAULTS}, **c}, tick, min_krw)]
            if merged["mode"] == "live" and not self._keys(merged)[0]:
                errs.append("실전 모드는 API 키가 필요합니다. Grid용 키를 넣거나, Martin Bot과 같은 거래소라면 Martin Bot 설정의 키를 사용합니다.")
            errs += self.check_conflict(merged)
            if errs:
                return errs
            self.cfg = merged
            save_config(self.cfg)
            self._sync_slots(reload_all=identity)
            self.log("Grid 설정이 저장되었습니다.")
            return []

    # ---------- 실행 ----------
    def _set_flag(self, on):
        try:
            if on:
                open(RUN_FLAG, "w").close()
            elif os.path.exists(RUN_FLAG):
                os.remove(RUN_FLAG)
        except OSError:
            pass

    def resume_if_needed(self):
        if os.path.exists(RUN_FLAG) and not self.running:
            self.log("재시작 감지 — Grid 자동매매를 이어서 진행합니다 (걸어둔 주문은 그대로 이어서 확인).", notify=True)
            errs = self.start(resume=True)
            if errs:
                self.log("⚠ 이어서 시작하지 못했습니다: " + " ".join(errs), notify=True)

    def start(self, resume=False):
        with self.lock:
            if self.running or self.stopping:
                return ["Grid가 이미 실행 중이거나 정지 처리 중입니다."]
            slots = self.enabled_slots()
            if not slots:
                return ["켜진 Grid 코인이 없습니다. Set up에서 코인을 켜주세요."]
            errs = []
            for s in slots:
                if not s.state.get("paused"):
                    errs += [f"{s.coin}: {e}" for e in validate(s.cfg, (s.state.get("minfo") or {}).get("tick", 0.0))]
            if all(s.state.get("paused") for s in slots):
                errs.append("켜진 코인이 모두 일시정지 상태입니다. Grid 탭에서 [기록 비우기]로 정리한 뒤 시작하세요.")
            errs += self.check_conflict()
            if self.cfg["mode"] == "live" and not self._keys()[0]:
                errs.append("실전 모드는 API 키가 필요합니다.")
            if errs:
                return errs
            self.running = True
            self._set_flag(True)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        if not resume:
            mode = "실전" if self.cfg["mode"] == "live" else "모의투자"
            desc = ", ".join(f"{s.coin} {s.cfg['lower']:,.0f}~{s.cfg['upper']:,.0f}·{s.cfg['grids']}칸" for s in slots
                             if not s.state.get("paused"))
            self.log(f"▶ Grid 시작 — {mode} / {self.cfg['exchange']} / {desc}", notify=True)
        return []

    def stop(self):
        """중지: Grid가 걸어둔 주문을 모두 취소합니다. 보유 중인 코인은 그대로 두고 기록도 유지합니다."""
        with self.lock:
            if not self.running:
                return ["실행 중이 아닙니다."]
            self.running = False
            self.stopping = True
            self._set_flag(False)
        self.log("■ Grid 중지 요청 — 걸어둔 주문을 취소합니다…", notify=True)
        return []

    def _loop(self):
        ex = None
        minfo = {}
        try:
            ex = self.make_exchange()
            while self.running:
                try:
                    active = [s for s in self.enabled_slots() if not s.state.get("paused")]
                    for s in active:
                        if s.coin not in minfo:
                            minfo[s.coin] = s.prepare(ex)
                    if active:
                        prices = ex.prices([s.market for s in active])
                        for s in active:
                            if not self.running:
                                break
                            p = prices.get(s.market)
                            if p is None:
                                continue
                            try:
                                s.tick(ex, p, minfo[s.coin])
                            except (ExchangeError, requests.RequestException) as e:
                                s.warn_once("tick", f"⚠ 처리 중 오류: {e}", every=300, notify=True)
                            except Exception as e:
                                s.warn_once("tickx", f"⚠ 처리 중 오류: {e}", every=300)
                                traceback.print_exc()
                    self.last_error = ""
                except Exception as e:
                    self.last_error = str(e)
                    self.warn_once("loop:" + str(e)[:60], f"⚠ Grid 시세 조회 오류: {e}", every=300,
                                   notify=isinstance(e, ExchangeError))
                    traceback.print_exc()
                    for _ in range(150):
                        if not self.running:
                            break
                        time.sleep(0.1)
                for _ in range(int(self.cfg["check_interval_sec"] * 10)):
                    if not self.running:
                        break
                    time.sleep(0.1)
        finally:
            if ex is not None and not self.running:
                for s in list(self.slots.values()):
                    try:
                        s.cancel_all(ex)
                    except Exception as e:
                        s.log(f"⚠ 주문 취소 중 오류: {e} — 거래소 앱에서 미체결 주문을 확인하세요.", notify=True)
            self.stopping = False
            left = sum(len(s.open_cells()) for s in self.slots.values())
            if not self.running:
                self.log("■ Grid 중지 완료" + (f" — ⚠ 취소 확인이 안 된 주문 {left}개" if left else
                                              " — 주문 취소 완료. 보유 코인은 그대로입니다."), notify=True)

    def clear(self, coin, action):
        """중지 상태에서 코인 하나 정리. sell = 보유분 시장가 매도 후 격자 비우기 / forget = 기록만 비움(거래소 잔고는 그대로)"""
        if self.running or self.stopping:
            return ["먼저 Grid를 중지하세요."]
        s = self.slots.get((coin or "").upper())
        if not s:
            return ["없는 코인입니다."]
        if action == "sell":
            if s.open_cells():
                return ["아직 취소 확인이 안 된 주문이 있습니다. 거래소 앱에서 Grid 미체결 주문을 직접 취소한 뒤 [기록 비우기]를 쓰세요."]
            try:
                s.sell_holdings(self.make_exchange(), "정리")
            except Exception as e:
                return [f"정리 매도 실패: {e}"]
            s.forget()
            return []
        if action == "forget":
            s.forget()
            return []
        return ["알 수 없는 동작입니다."]

    # ---------- 현황 ----------
    def status(self):
        with self.lock:
            coins = [self.slots[c["coin"]].status() for c in self.cfg["coins"] if c["coin"] in self.slots]
            tot = {"realized": sum(c["stats"]["realized"] for c in coins),
                   "round_trips": sum(c["stats"]["round_trips"] for c in coins),
                   "fees": sum(c["stats"]["fees"] for c in coins),
                   "unrealized": sum(c["total"]["unrealized"] or 0 for c in coins),
                   "hold_cost": sum(c["total"]["hold_cost"] for c in coins),
                   "buy_orders_krw": sum(c["total"]["buy_orders_krw"] for c in coins),
                   "budget": sum(c["total"]["budget"] for c in coins if c["enabled"])}
            return {"running": self.running, "stopping": self.stopping, "mode": self.cfg["mode"],
                    "exchange": self.cfg["exchange"], "coins": coins, "total": tot,
                    "logs": list(self.logs)[:200], "last_error": self.last_error}
