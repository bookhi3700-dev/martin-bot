"""그리드 자동매매 실행기 — Martin Bot과 같은 프로그램 안에서 별도 스레드로 동작

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
from grid_strategy import (GRID_DEFAULTS, build_levels, new_cells, can_place_buy, validate, summary,
                           conflict_with_martin, apply_range_mode)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "grid_config.json")
RUN_FLAG = os.path.join(DATA_DIR, "grid_running.flag")
os.makedirs(DATA_DIR, exist_ok=True)
SECRET_KEYS = ("access_key", "secret_key")
LEVEL_KEYS = ("exchange", "mode", "coin", "lower", "upper", "grids", "spacing", "krw_per_grid", "range_mode", "gap_pct")
LIVE_EDITABLE = ("check_interval_sec", "fee_pct", "stop_loss_enabled", "stop_loss_price")
MAX_PLACE_PER_TICK = 8


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


def load_config():
    cfg = dict(GRID_DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.items():
            if k in GRID_DEFAULTS:
                try:
                    cfg[k] = _cast(GRID_DEFAULTS[k], v)
                except (TypeError, ValueError):
                    pass
    cfg["coin"] = cfg["coin"].upper()
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


class PaperBook:
    """모의투자용 지정가 거래소: 실제 시세를 받아오고, 가격이 주문가에 닿으면 체결된 것으로 처리"""

    def __init__(self, real, fee_pct, orders):
        self.real = real
        self.name = f"모의투자({real.name} 시세)"
        self.fee = fee_pct / 100
        self.orders = orders            # 상태 파일에 함께 저장되는 dict
        self._market_paper = Paper(real, fee_pct)

    def price(self, market):
        return self.real.price(market)

    def market_info(self, market):
        try:
            return self.real.market_info(market)
        except Exception:
            return {"tick": 0.0, "qty_step": 1e-8, "min_krw": 5000.0}

    def observe(self, lo, hi):
        for o in self.orders.values():
            if o["state"] != "open":
                continue
            if (o["side"] == "bid" and lo <= o["price"]) or (o["side"] == "ask" and hi >= o["price"]):
                o["state"] = "done"

    def limit_order(self, market, side, price, volume, cid, tick=1.0, qty_step=1e-8):
        self.orders[cid] = {"side": side, "price": price, "qty": volume, "state": "open"}
        return cid

    def get_order(self, cid, market):
        o = self.orders.get(cid)
        if not o:
            raise OrderNotFound("모의 주문 없음")
        funds = o["price"] * o["qty"]
        done = o["state"] == "done"
        res = {"state": o["state"], "qty": o["qty"] if done else 0.0, "avg": o["price"],
               "funds": funds if done else 0.0, "fee": funds * self.fee if done else 0.0, "fee_coin": False,
               "remain": 0.0 if done else o["qty"]}
        if o["state"] != "open":
            self.orders.pop(cid, None)
        return res

    def cancel_order(self, cid, market):
        o = self.orders.get(cid)
        if o and o["state"] == "open":
            o["state"] = "cancel"

    def market_sell(self, market, volume):
        return self._market_paper.market_sell(market, volume)

    def available(self, currency):
        return float("inf")


class GridBot:
    def __init__(self, martin_bot):
        self.mb = martin_bot
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.running = False
        self.thread = None
        self.stopping = False
        self.logs = deque(maxlen=300)
        self.last_error = ""
        self.last_price = None
        self._prev_price = None
        self._rr = 0
        self._krw_short_until = 0
        self._warned = {}
        self.state = self._load_state()

    # ---------- 상태 파일 ----------
    @property
    def key(self):
        c = self.cfg
        return f"{c['mode']}_{c['exchange']}_{c['coin']}"

    @property
    def market(self):
        return f"KRW-{self.cfg['coin']}"

    def _state_path(self):
        return os.path.join(DATA_DIR, f"grid_state_{self.key}.json")

    def _empty_state(self):
        return {"levels": [], "cells": [], "minfo": {}, "paused": "", "paper_orders": {},
                "stats": {"round_trips": 0, "realized": 0.0, "fees": 0.0, "history": []}}

    def _load_state(self):
        st = self._empty_state()
        if os.path.exists(self._state_path()):
            with open(self._state_path(), encoding="utf-8") as f:
                d = json.load(f)
            st.update({k: v for k, v in d.items() if k in st})
            st["stats"] = {**self._empty_state()["stats"], **d.get("stats", {})}
        return st

    def save(self):
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._state_path())

    def active_cells(self):
        return [c for c in self.state["cells"] if c["st"] != "idle"]

    # ---------- 로그·알림 ----------
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
                          json={"chat_id": c, "text": f"[그리드·{mode}] {msg}"}, timeout=5)
        except Exception:
            pass

    def log_trade(self, side, c, price, qty, krw, fee, profit=None):
        path = os.path.join(DATA_DIR, f"grid_trades_{self.key}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각", "구분", "칸", "체결가", "수량", "원화", "수수료", "손익"])
            w.writerow([now(), side, c["i"] + 1 if c else "-", round(price, 4), f"{qty:.8f}", round(krw), round(fee, 2),
                        "" if profit is None else round(profit)])

    # ---------- 거래소 ----------
    def _keys(self):
        c, m = self.cfg, self.mb.cfg
        if c["access_key"]:
            return c["access_key"], c["secret_key"]
        if m.get("exchange") == c["exchange"]:
            return m.get("access_key", ""), m.get("secret_key", "")
        return "", ""

    def make_exchange(self):
        real = exchange_class(self.cfg["exchange"])(*self._keys())
        if self.cfg["mode"] == "live":
            return real
        return PaperBook(real, self.cfg["fee_pct"], self.state["paper_orders"])

    def market_info(self, ex=None):
        ex = ex or exchange_class(self.cfg["exchange"])()
        try:
            mi = ex.market_info(self.market)
            if mi.get("tick"):
                self.state["minfo"] = mi
        except Exception as e:
            self.warn_once("minfo", f"⚠ 호가 단위 조회 실패({e}) — 저장된 값으로 진행합니다.")
        return self.state.get("minfo") or {"tick": 0.0, "qty_step": 1e-8, "min_krw": 5000.0}

    # ---------- 설정 ----------
    def public_config(self):
        c = dict(self.cfg)
        for k in SECRET_KEYS:
            if c.get(k):
                c[k] = c[k][:4] + "••••••••"
        return c

    def check_conflict(self, grid_cfg=None, martin_cfg=None):
        msg = conflict_with_martin(grid_cfg or self.cfg, martin_cfg or self.mb.cfg, from_martin=martin_cfg is not None)
        return [msg] if msg else []

    def update_config(self, new):
        with self.lock:
            merged = dict(self.cfg)
            for k, v in new.items():
                if k not in GRID_DEFAULTS:
                    continue
                if k in SECRET_KEYS and isinstance(v, str) and "•" in v:
                    continue
                try:
                    merged[k] = _cast(GRID_DEFAULTS[k], v)
                except (TypeError, ValueError):
                    return [f"'{k}' 값이 올바르지 않습니다."]
            merged["coin"] = merged["coin"].upper()
            tick, min_krw = 0.0, 5000.0
            try:
                mi = exchange_class(merged["exchange"])().market_info(f"KRW-{merged['coin']}")
                tick, min_krw = mi["tick"], mi["min_krw"]
            except Exception:
                pass
            apply_range_mode(merged, tick)
            changed = [k for k in GRID_DEFAULTS if merged[k] != self.cfg[k]]
            if self.running and any(k not in LIVE_EDITABLE for k in changed):
                return ["실행 중에는 수수료·확인 주기·손절만 바꿀 수 있습니다. 범위·칸 수·금액을 바꾸려면 먼저 중지하세요."]
            if any(k in LEVEL_KEYS for k in changed) and self.active_cells():
                return ["코인을 들고 있거나 주문이 걸린 칸이 있어 범위·칸·금액·코인·거래소·모드를 바꿀 수 없습니다. "
                        "중지 후 [정리]로 보유분을 매도하거나 기록을 비운 뒤 바꾸세요."]
            if merged["mode"] == "live" and not (merged["access_key"] or
                                                 (self.mb.cfg.get("exchange") == merged["exchange"] and self.mb.cfg.get("access_key"))):
                return ["실전 모드는 API 키가 필요합니다. 그리드용 키를 넣거나, Martin Bot과 같은 거래소라면 Martin Bot 설정의 키를 사용합니다."]
            errs = validate(merged, tick, min_krw) + self.check_conflict(merged)
            if errs:
                return errs
            identity = any(k in ("exchange", "mode", "coin") for k in changed)
            self.cfg = merged
            save_config(self.cfg)
            if identity:
                self.state = self._load_state()
            self.log("그리드 설정이 저장되었습니다.")
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
            self.log("재시작 감지 — 그리드 자동매매를 이어서 진행합니다 (걸어둔 주문은 그대로 이어서 확인).", notify=True)
            errs = self.start(resume=True)
            if errs:
                self.log("⚠ 이어서 시작하지 못했습니다: " + " ".join(errs), notify=True)

    def start(self, resume=False):
        with self.lock:
            if self.running or self.stopping:
                return ["그리드가 이미 실행 중이거나 정지 처리 중입니다."]
            if self.state.get("paused"):
                return [f"일시정지 상태입니다: {self.state['paused']} — [정리]로 정리한 뒤 다시 시작하세요."]
            errs = validate(self.cfg, (self.state.get("minfo") or {}).get("tick", 0.0)) + self.check_conflict()
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
            self.log(f"▶ 그리드 시작 — {mode} / {self.cfg['exchange']} / {self.cfg['coin']} "
                     f"{self.cfg['lower']:,.0f}~{self.cfg['upper']:,.0f} · {self.cfg['grids']}칸 · 칸당 {self.cfg['krw_per_grid']:,}원",
                     notify=True)
        return []

    def stop(self):
        """중지: 그리드가 걸어둔 주문을 모두 취소합니다. 보유 중인 코인은 그대로 두고 기록도 유지합니다."""
        with self.lock:
            if not self.running:
                return ["실행 중이 아닙니다."]
            self.running = False
            self.stopping = True
            self._set_flag(False)
        self.log("■ 그리드 중지 요청 — 걸어둔 주문을 취소합니다…", notify=True)
        return []

    def _prepare(self, ex):
        mi = self.market_info(ex if self.cfg["mode"] == "live" else ex.real)
        with self.lock:
            levels = build_levels(self.cfg, mi["tick"])
            if not self.state["cells"] or (not self.active_cells() and levels != self.state["levels"]):
                self.state["levels"] = levels
                self.state["cells"] = new_cells(levels)
                self.save()
                self.log(f"격자 {len(levels) - 1}칸 생성: {levels[0]:,.0f} ~ {levels[-1]:,.0f} (호가 단위 {mi['tick']:g})")
        return mi

    def _loop(self):
        ex = None
        try:
            ex = self.make_exchange()
            mi = None
            while self.running:
                try:
                    if mi is None:
                        mi = self._prepare(ex)
                    self.tick(ex, mi)
                    self.last_error = ""
                except Exception as e:
                    self.last_error = str(e)
                    self.warn_once("loop:" + str(e)[:60], f"⚠ 그리드 오류: {e}", every=300,
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
                try:
                    self._cancel_all(ex)
                except Exception as e:
                    self.log(f"⚠ 주문 취소 중 오류: {e} — 거래소 앱에서 미체결 주문을 확인하세요.", notify=True)
            self.stopping = False
            left = [c for c in self.state["cells"] if c["st"] in ("buy", "sell")]
            if not self.running:
                self.log("■ 그리드 중지 완료" + (f" — ⚠ 취소 확인이 안 된 주문 {len(left)}개" if left else
                                              " — 주문 취소 완료. 보유 코인은 그대로입니다."), notify=True)

    # ---------- 한 번의 확인 ----------
    def tick(self, ex, mi):
        price = ex.price(self.market)
        prev = self._prev_price if self._prev_price is not None else price
        lo, hi = min(prev, price), max(prev, price)
        self.last_price = price
        self._prev_price = price
        if isinstance(ex, PaperBook):
            ex.observe(lo, hi)
        cfg = self.cfg
        if cfg["stop_loss_enabled"] and cfg["stop_loss_price"] > 0 and price <= cfg["stop_loss_price"]:
            self._stop_loss(ex, price)
            return
        tick = mi["tick"]
        cells = self.state["cells"]

        # 1) 걸어둔 주문 확인 — 가격이 지나간 주문 + 아직 접수 확인 안 된 주문 + 돌아가며 2개
        open_cells = [c for c in cells if c["st"] in ("buy", "sell")]
        must = [c for c in open_cells if not c["ok"] or (c["st"] == "buy" and c["buy"] >= lo - tick)
                or (c["st"] == "sell" and c["sell"] <= hi + tick)]
        rest = [c for c in open_cells if c not in must]
        if rest:
            for k in range(min(2, len(rest))):
                must.append(rest[(self._rr + k) % len(rest)])
            self._rr = (self._rr + 2) % max(1, len(rest))
        for c in must:
            if not self.running:
                return
            self._check_cell(ex, c)

        # 2) 산 칸은 바로 위 가격에 매도
        for c in cells:
            if c["st"] == "hold" and self.running:
                self._place_sell(ex, c, mi)

        # 3) 현재가 아래 빈 칸에 매수 (현재가에 가까운 칸부터, 한 번에 최대 8개)
        if time.time() < self._krw_short_until:
            return
        placed = 0
        for i in range(len(cells) - 1, -1, -1):
            if placed >= MAX_PLACE_PER_TICK or not self.running:
                break
            if can_place_buy(cells, i, price, tick):
                if self._place_buy(ex, cells[i], mi):
                    placed += 1
                elif time.time() < self._krw_short_until:
                    break

    def _new_cid(self):
        return "grd-" + uuid.uuid4().hex[:24]

    def _place_buy(self, ex, c, mi):
        qty = floor_step(self.cfg["krw_per_grid"] / c["buy"], mi["qty_step"])
        if qty * c["buy"] < mi["min_krw"]:
            self.warn_once("minbuy", f"⚠ {c['i'] + 1}칸 매수 금액이 최소 주문금액보다 작습니다. 칸당 금액을 늘려주세요.")
            return False
        with self.lock:
            c.update(st="buy", cid=self._new_cid(), ok=False, oqty=qty, at=now())
            self.save()      # 보내기 전에 ID 저장 → 연결이 끊겨도 다음에 조회 가능
        try:
            ex.limit_order(self.market, "bid", c["buy"], qty, c["cid"], mi["tick"], mi["qty_step"])
        except OrderUnconfirmed as e:
            self.log(f"⚠ {c['i'] + 1}칸 매수 접수 확인 불가({e}) — 다음 확인 때 주문 ID로 다시 조회합니다.")
            return True
        except ExchangeError as e:
            with self.lock:
                c.update(st="idle", cid="", ok=False)
                self.save()
            if "잔고" in str(e) or "insufficient" in str(e).lower() or "부족" in str(e):
                self._krw_short_until = time.time() + 60
                self.warn_once("krw", f"⚠ 원화 잔고가 부족해 매수 주문을 더 걸지 못했습니다 ({e}). 1분 뒤 다시 시도합니다.",
                               notify=True)
            else:
                self.warn_once("buyerr", f"⚠ {c['i'] + 1}칸 매수 주문 실패: {e}", every=120, notify=True)
            return False
        with self.lock:
            c["ok"] = True
            self.save()
        return True

    def _place_sell(self, ex, c, mi):
        qty = floor_step(c["qty"], mi["qty_step"])
        if qty <= 0 or qty * c["sell"] < mi["min_krw"]:
            self.warn_once(f"dust{c['i']}", f"⚠ {c['i'] + 1}칸 보유량({c['qty']:.8f})이 최소 주문금액보다 작아 매도 주문을 못 겁니다.")
            return
        with self.lock:
            c.update(st="sell", cid=self._new_cid(), ok=False, oqty=qty)
            self.save()
        try:
            ex.limit_order(self.market, "ask", c["sell"], qty, c["cid"], mi["tick"], mi["qty_step"])
        except OrderUnconfirmed as e:
            self.log(f"⚠ {c['i'] + 1}칸 매도 접수 확인 불가({e}) — 다음 확인 때 주문 ID로 다시 조회합니다.")
            return
        except ExchangeError as e:
            with self.lock:
                c.update(st="hold", cid="", ok=False)
                self.save()
            self.warn_once(f"sellerr{c['i']}", f"⚠ {c['i'] + 1}칸 매도 주문 실패: {e}", every=120, notify=True)
            return
        with self.lock:
            c["ok"] = True
            self.save()

    def _check_cell(self, ex, c):
        try:
            o = ex.get_order(c["cid"], self.market)
        except OrderNotFound:
            with self.lock:
                if not c["ok"]:
                    # 접수 확인 전에 끊겼는데 거래소에도 없음 → 주문이 안 들어간 것
                    c.update(st="idle" if c["st"] == "buy" else "hold", cid="", ok=False)
                    self.save()
                    return
                self.state["paused"] = (f"{c['i'] + 1}칸 주문(ID {c['cid']})을 거래소에서 찾을 수 없습니다. "
                                        "거래소 앱에서 직접 취소·체결했는지 확인하세요.")
                self.save()
            self.running = False
            self._set_flag(False)
            self.log(f"⛔ 그리드 일시정지 — {self.state['paused']}", notify=True)
            return
        except (ExchangeError, requests.RequestException) as e:
            self.warn_once("chk", f"⚠ 주문 조회 실패: {e}", every=120)
            return
        with self.lock:
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
        if side == "buy":
            net = qty - (o["fee"] if o["fee_coin"] else 0)
            cost = o["funds"] + (0 if o["fee_coin"] else o["fee"])
            fee_krw = o["fee"] * o["avg"] if o["fee_coin"] else o["fee"]
            c.update(st="hold", cid="", ok=False, qty=c["qty"] + net, cost=c["cost"] + cost, at=now())
            self.state["stats"]["fees"] += fee_krw
            self.log_trade("매수", c, o["avg"], net, cost, fee_krw)
            self.log(f"🟢 {c['i'] + 1}칸 매수 체결 {cost:,.0f}원 @ {o['avg']:,.2f} → {c['sell']:,.2f}에 매도 대기")
        else:
            fee_krw = o["fee"] * o["avg"] if o["fee_coin"] else o["fee"]
            proceeds = o["funds"] - fee_krw
            part = min(1.0, qty / c["qty"]) if c["qty"] > 0 else 1.0
            cost_part = c["cost"] * part
            profit = proceeds - cost_part
            st = self.state["stats"]
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

    def _cancel_all(self, ex):
        cells = [c for c in self.state["cells"] if c["st"] in ("buy", "sell")]
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
                with self.lock:
                    if not c["ok"]:
                        c.update(st="idle" if c["st"] == "buy" else "hold", cid="", ok=False)
                continue
            except Exception as e:
                self.log(f"⚠ {c['i'] + 1}칸 주문 상태 확인 실패: {e}")
                continue
            with self.lock:
                if o["state"] == "open":
                    self.log(f"⚠ {c['i'] + 1}칸 주문이 아직 살아 있습니다 — 거래소 앱에서 확인하세요.")
                    continue
                self._absorb(c, o)
        with self.lock:
            self.save()

    def _stop_loss(self, ex, price):
        self.log(f"🛑 손절가 {self.cfg['stop_loss_price']:,.0f} 이탈 (현재 {price:,.0f}) — 주문 취소 후 보유분 전량 매도", notify=True)
        self.running = False
        self._set_flag(False)
        self._cancel_all(ex)
        self._sell_holdings(ex, "손절")
        with self.lock:
            self.state["paused"] = f"손절가 이탈로 정리 후 중지했습니다 ({now()}). 범위를 다시 정하고 [정리 → 기록 비우기] 후 시작하세요."
            self.save()

    def _sell_holdings(self, ex, reason):
        hold = [c for c in self.state["cells"] if c["st"] == "hold" and c["qty"] > 0]
        vol, cost = sum(c["qty"] for c in hold), sum(c["cost"] for c in hold)
        if vol <= 0:
            return None
        fill = ex.market_sell(self.market, vol)
        profit = fill["krw"] - cost
        with self.lock:
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

    def clear(self, action):
        """중지 상태에서 정리. sell = 보유분 시장가 매도 후 격자 초기화 / forget = 기록만 비움(거래소 잔고는 그대로)"""
        if self.running or self.stopping:
            return ["먼저 그리드를 중지하세요."]
        if action == "sell" and any(c["st"] in ("buy", "sell") for c in self.state["cells"]):
            return ["아직 취소 확인이 안 된 주문이 있습니다. 거래소 앱에서 그리드 미체결 주문을 직접 취소한 뒤 [기록 비우기]를 쓰세요."]
        if action == "sell":
            try:
                ex = self.make_exchange()
                self._sell_holdings(ex, "정리")
            except Exception as e:
                return [f"정리 매도 실패: {e}"]
            return self._forget()
        if action == "forget":
            return self._forget()
        return ["알 수 없는 동작입니다."]

    def _forget(self):
        with self.lock:
            stats = self.state["stats"]
            self.state = self._empty_state()
            self.state["stats"] = stats
            self.save()
        self.log("그리드 기록(칸·주문)을 비웠습니다. 누적 손익 기록은 유지됩니다. 다음 시작 때 현재 설정으로 격자를 새로 만듭니다.")
        return []

    # ---------- 현황 ----------
    def status(self):
        with self.lock:
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
                "running": self.running, "stopping": self.stopping, "mode": cfg["mode"], "exchange": cfg["exchange"],
                "coin": cfg["coin"], "price": price, "paused": st.get("paused", ""),
                "cells": [{k: c.get(k) for k in ("i", "buy", "sell", "st", "qty", "cost", "at")} for c in cells],
                "stats": st["stats"],
                "total": {"hold_qty": qty, "hold_cost": cost, "value": value, "unrealized": unreal, "buy_orders_krw": buy_krw,
                          "budget": cfg["krw_per_grid"] * len(cells) if cells else cfg["krw_per_grid"] * cfg["grids"],
                          "in_range": bool(cells and price and cells[0]["buy"] <= price <= cells[-1]["sell"])},
                "summary": summary(cfg, tick), "minfo": st.get("minfo", {}),
                "logs": list(self.logs)[:150], "last_error": self.last_error,
            }
