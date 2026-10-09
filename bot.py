"""자동매매 실행기 — 여러 코인을 한 루프에서 순서대로 관리"""
import csv
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime

import requests

from exchanges import make_exchange, ExchangeError, OrderUnconfirmed
from strategy import (GLOBAL_DEFAULTS, COIN_DEFAULTS, Position, Action, decide, apply_buy, close_position,
                      update_trailing, next_buy_trigger, take_profit_price, trailing_stop_price,
                      stop_loss_price, ladder, max_budget, step_amount, validate_coin, drop_for_step)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
os.makedirs(DATA_DIR, exist_ok=True)
SECRET_KEYS = ("access_key", "secret_key", "telegram_token")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cast(default, v):
    if isinstance(default, bool):
        return v in (True, "true", "on", 1, "1")
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    return str(v)


def normalize_coin(c):
    out = dict(COIN_DEFAULTS)
    for k, v in (c or {}).items():
        if k in COIN_DEFAULTS and v is not None and v != "":
            out[k] = _cast(COIN_DEFAULTS[k], v)
    out["coin"] = out["coin"].strip().upper()
    return out


def load_config():
    raw = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    cfg = dict(GLOBAL_DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k in GLOBAL_DEFAULTS})
    if "coins" in raw:
        coins = raw["coins"]
    elif "coin" in raw:  # 이전 버전(코인 1개) 설정 이전
        old = dict(raw)
        old["drop_steps"] = str(raw.get("drop_pct", 5))
        coins = [old]
    else:
        coins = [dict(COIN_DEFAULTS)]
    cfg["coins"] = [normalize_coin(c) for c in coins]
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def validate_config(cfg):
    errs = []
    if cfg["check_interval_sec"] < 3:
        errs.append("시세 확인 주기는 3초 이상이어야 합니다.")
    if cfg["total_budget"] < 0:
        errs.append("전체 투입 한도는 0 이상이어야 합니다 (0 = 제한 없음).")
    if cfg["total_limit_action"] not in ("hold", "stop_bot"):
        errs.append("전체 한도 도달 시 동작 값이 올바르지 않습니다.")
    if cfg["mode"] == "live" and (not cfg["access_key"] or not cfg["secret_key"]):
        errs.append("실전 모드는 API Access Key / Secret Key 가 필요합니다.")
    names = [c["coin"] for c in cfg["coins"]]
    if len(names) != len(set(names)):
        errs.append("같은 코인이 두 번 등록되어 있습니다.")
    if not names:
        errs.append("코인을 한 개 이상 등록하세요.")
    for c in cfg["coins"]:
        errs += validate_coin(c)
    return errs


class Slot:
    """코인 하나의 상태"""

    def __init__(self, bot, coin):
        self.bot, self.coin = bot, coin
        self.pos = Position()
        self.stats = {"cycles": 0, "realized": 0, "history": []}
        self.paused = ""          # 사유가 있으면 일시정지 (미확인 주문 등)
        self.last_price = None
        self.budget_blocked = False
        self.load()

    @property
    def key(self):
        g = self.bot.cfg
        return f"{g['mode']}_{g['exchange']}_{self.coin}"

    @property
    def market(self):
        return f"KRW-{self.coin}"

    def path(self):
        return os.path.join(DATA_DIR, f"state_{self.key}.json")

    def load(self):
        self.pos, self.stats, self.paused = Position(), {"cycles": 0, "realized": 0, "history": []}, ""
        if os.path.exists(self.path()):
            with open(self.path(), encoding="utf-8") as f:
                d = json.load(f)
            self.pos = Position.from_dict(d.get("position"))
            self.stats.update(d.get("stats", {}))
            self.paused = d.get("paused", "")

    def save(self):
        tmp = self.path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"position": self.pos.to_dict(), "stats": self.stats, "paused": self.paused},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path())

    def log_trade(self, side, reason, fill):
        path = os.path.join(DATA_DIR, f"trades_{self.key}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각", "구분", "사유", "체결가", "수량", "원화", "수수료", "단계", "평단가"])
            w.writerow([now(), side, reason, round(fill["price"], 4), f"{fill['volume']:.8f}",
                        round(fill["krw"]), round(fill["fee"], 2), self.pos.step, round(self.pos.avg_price, 4)])


class Bot:
    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.running = False
        self.thread = None
        self.logs = deque(maxlen=400)
        self.last_error = ""
        self.slots = {}
        self.extra_validate = None   # 그리드 봇과 코인이 겹치는지 확인하는 함수 (app.py 에서 연결)
        self._sync_slots()

    # ---------- 도우미 ----------
    def coin_cfg(self, coin):
        c = next(c for c in self.cfg["coins"] if c["coin"] == coin)
        return {**self.cfg, **c}

    def _sync_slots(self, reload_all=False):
        names = [c["coin"] for c in self.cfg["coins"]]
        for n in list(self.slots):
            if n not in names:
                del self.slots[n]
        for n in names:
            if n not in self.slots:
                self.slots[n] = Slot(self, n)
            elif reload_all:
                self.slots[n].load()

    def log(self, msg, notify=False):
        line = f"[{now()}] {msg}"
        self.logs.appendleft(line)
        print(line, flush=True)
        if notify:
            self._telegram(msg)

    def _telegram(self, msg):
        t, c = self.cfg.get("telegram_token"), self.cfg.get("telegram_chat_id")
        if not t or not c:
            return
        try:
            mode = "실전" if self.cfg["mode"] == "live" else "모의"
            requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                          json={"chat_id": c, "text": f"[마틴봇·{mode}] {msg}"}, timeout=5)
        except Exception:
            pass

    def total_cost(self):
        return sum(s.pos.cost for s in self.slots.values())

    # ---------- 설정 ----------
    def update_config(self, new):
        with self.lock:
            merged = dict(self.cfg)
            for k, v in new.items():
                if k in GLOBAL_DEFAULTS:
                    if k in SECRET_KEYS and (v == "" or (isinstance(v, str) and "•" in v)):
                        continue
                    try:
                        merged[k] = _cast(GLOBAL_DEFAULTS[k], v)
                    except (TypeError, ValueError):
                        return [f"'{k}' 값이 올바르지 않습니다."]
            if "coins" in new:
                try:
                    merged["coins"] = [normalize_coin(c) for c in new["coins"]]
                except (TypeError, ValueError):
                    return ["코인 설정에 숫자가 아닌 값이 있습니다."]
            errs = validate_config(merged)
            if not errs and self.extra_validate:
                errs = self.extra_validate(merged)
            if errs:
                return errs
            identity = any(merged[k] != self.cfg[k] for k in ("mode", "exchange"))
            if identity and self.running:
                return ["실행 중에는 거래소/모드를 바꿀 수 없습니다. 먼저 중지해 주세요."]
            old_names = {c["coin"] for c in self.cfg["coins"]}
            new_names = {c["coin"] for c in merged["coins"]}
            for gone in old_names - new_names:
                s = self.slots.get(gone)
                if s and s.pos.step and not identity:
                    return [f"{gone} 은(는) 보유 중인 포지션이 있어 삭제할 수 없습니다. 끄기만 하거나, 정리 후 삭제하세요."]
            self.cfg = merged
            save_config(self.cfg)
            self._sync_slots(reload_all=identity)
            self.log("설정이 저장되었습니다.")
            return []

    def public_config(self):
        c = json.loads(json.dumps(self.cfg))
        for k in SECRET_KEYS:
            if c.get(k):
                c[k] = c[k][:4] + "••••••••"
        return c

    # ---------- 실행 ----------
    RUN_FLAG = os.path.join(DATA_DIR, "running.flag")

    def _set_run_flag(self, on):
        try:
            if on:
                open(self.RUN_FLAG, "w").close()
            elif os.path.exists(self.RUN_FLAG):
                os.remove(self.RUN_FLAG)
        except OSError:
            pass

    def resume_if_needed(self):
        if os.path.exists(self.RUN_FLAG) and not self.running:
            self.log("재시작 감지 — 중지 전 실행 중이던 자동매매를 이어서 진행합니다.", notify=True)
            self.start()

    def start(self):
        with self.lock:
            if self.running:
                return ["이미 실행 중입니다."]
            errs = validate_config(self.cfg)
            if not errs and self.extra_validate:
                errs = self.extra_validate(self.cfg)
            if errs:
                return errs
            self.running = True
            self._set_run_flag(True)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        on = [c["coin"] for c in self.cfg["coins"] if c["enabled"]]
        mode = "실전" if self.cfg["mode"] == "live" else "모의투자"
        self.log(f"▶ 시작 — {mode} / {self.cfg['exchange']} / {', '.join(on) or '켜진 코인 없음'}", notify=True)
        return []

    def stop(self):
        self.running = False
        self._set_run_flag(False)
        self.log("■ 중지했습니다. (보유 코인은 그대로 유지됩니다)", notify=True)

    def _loop(self):
        ex = make_exchange(self.cfg)
        while self.running:
            try:
                self.tick(ex)
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)
                self.log(f"⚠ 시세 조회 오류: {e}", notify=isinstance(e, ExchangeError))
                traceback.print_exc()
                time.sleep(20)
            for _ in range(int(self.cfg["check_interval_sec"] * 10)):
                if not self.running:
                    break
                time.sleep(0.1)

    def tick(self, ex):
        with self.lock:
            active = [c["coin"] for c in self.cfg["coins"] if c["enabled"]]
        if not active:
            return
        prices = ex.prices([f"KRW-{c}" for c in active])
        for coin in active:
            if not self.running:
                break
            slot = self.slots.get(coin)
            price = prices.get(f"KRW-{coin}")
            if slot is None or price is None:
                continue
            slot.last_price = price
            if slot.paused:
                continue
            try:
                self._tick_coin(ex, slot, price)
            except OrderUnconfirmed as e:
                with self.lock:
                    slot.paused = str(e)
                    slot.save()
                self.log(f"⛔ {coin} 일시정지 — {e} 확인 후 현황 화면에서 '재개'를 눌러주세요.", notify=True)
            except ExchangeError as e:
                self.log(f"⚠ {coin}: {e}", notify=True)
            except Exception as e:
                self.log(f"⚠ {coin} 처리 중 오류: {e}")
                traceback.print_exc()

    def _tick_coin(self, ex, slot, price):
        with self.lock:
            cfg = self.coin_cfg(slot.coin)
            if update_trailing(slot.pos, price, cfg):
                slot.save()
                self.log(f"📈 {slot.coin} 목표 수익 도달 — 추적 익절 시작 (고점 {price:,.2f}에서 "
                         f"{cfg['trailing_pct']}% 빠지면 매도)", notify=True)
            elif slot.pos.trail_armed:
                slot.save()
            act = decide(slot.pos, price, cfg)
            if act.kind == "hold":
                return
            if act.kind == "buy" and slot.pos.step == 0 and not cfg["auto_restart"] and slot.stats["cycles"] > 0:
                return
            stop_after = False
            # ① 코인별 투입 한도
            if act.kind == "buy" and cfg["coin_budget"] > 0 and slot.pos.cost + act.amount_krw > cfg["coin_budget"]:
                la = cfg["limit_action"]
                info = (f"{slot.coin} 코인 투입 한도 {cfg['coin_budget']:,}원 도달 "
                        f"(현재 {slot.pos.cost:,.0f}원, 다음 매수 {act.amount_krw:,.0f}원)")
                if la == "hold":
                    if not slot.budget_blocked:
                        slot.budget_blocked = True
                        self.log(f"🚫 {info} — 추가 매수 중지, 익절가 도달을 기다립니다.", notify=True)
                    return
                if la == "pause":
                    slot.paused = f"{info} — 매매를 중지했습니다 (보유 코인은 그대로). 재개하면 다시 진행합니다."
                    slot.save()
                    self.log(f"⛔ {slot.paused}", notify=True)
                    return
                # sell: 전량 매도 후 중지
                act = Action("sell", "투입 한도 도달 — 전량 매도 후 중지", volume=slot.pos.volume, trigger_price=price)
                stop_after = True
                self.log(f"🛑 {info} — 설정에 따라 전량 매도 후 이 코인을 중지합니다.", notify=True)
            # ② 전체 투입 한도
            if act.kind == "buy" and self.cfg["total_budget"] > 0 and self.total_cost() + act.amount_krw > self.cfg["total_budget"]:
                info = (f"전체 투입 한도 {self.cfg['total_budget']:,}원 도달 "
                        f"(현재 {self.total_cost():,.0f}원 + {slot.coin} {act.amount_krw:,.0f}원)")
                if self.cfg["total_limit_action"] == "stop_bot":
                    self.running = False
                    self._set_run_flag(False)
                    self.log(f"🛑 {info} — 설정에 따라 봇 전체를 중지했습니다. 보유 코인은 그대로입니다.", notify=True)
                    return
                if not slot.budget_blocked:
                    slot.budget_blocked = True
                    self.log(f"🚫 {slot.coin} {act.reason} 보류 — {info}", notify=True)
                return
            slot.budget_blocked = False

        if act.kind == "buy":
            fill = ex.market_buy(slot.market, act.amount_krw)
            with self.lock:
                apply_buy(slot.pos, fill["krw"], fill["volume"], fill["price"], now())
                slot.save()
                slot.log_trade("매수", act.reason, fill)
            self.log(f"🟢 {slot.coin} {act.reason}: {fill['krw']:,.0f}원 @ {fill['price']:,.2f} / "
                     f"평단 {slot.pos.avg_price:,.2f} / 누적 {slot.pos.cost:,.0f}원", notify=True)
            if slot.pos.step >= int(cfg["max_steps"]):
                self.log(f"⚠ {slot.coin} 최대 {slot.pos.step}단계 도달 — 더 이상 추가 매수하지 않습니다.", notify=True)
        else:
            fill = ex.market_sell(slot.market, act.volume)
            with self.lock:
                slot.log_trade("매도", act.reason, fill)
                res = close_position(slot.pos, fill["krw"])
                res.update(ended_at=now(), reason=act.reason)
                slot.stats["cycles"] += 1
                slot.stats["realized"] += res["profit"]
                slot.stats["history"] = ([res] + slot.stats["history"])[:200]
                slot.save()
            self.log(f"🔴 {slot.coin} {act.reason}: {res['steps']}단계 사이클 종료, 손익 {res['profit']:+,}원 "
                     f"({res['profit_pct']:+.2f}%)", notify=True)
            if stop_after:
                with self.lock:
                    slot.paused = "투입 한도 도달로 전량 매도 후 중지했습니다. 재개하면 1단계부터 새로 시작합니다."
                    slot.save()
                return
            if not cfg["auto_restart"]:
                self.log(f"{slot.coin} 자동 재시작이 꺼져 있어 새 사이클을 시작하지 않습니다.", notify=True)

    # ---------- 코인별 조작 ----------
    def reset_position(self, coin):
        with self.lock:
            s = self.slots.get(coin)
            if not s:
                return ["없는 코인입니다."]
            s.pos = Position()
            s.paused = ""
            s.save()
        self.log(f"{coin} 포지션 기록을 초기화했습니다. (거래소 잔고는 그대로)")
        return []

    def resume_coin(self, coin):
        with self.lock:
            s = self.slots.get(coin)
            if not s:
                return ["없는 코인입니다."]
            s.paused = ""
            s.save()
        self.log(f"{coin} 일시정지를 해제했습니다.")
        return []

    # ---------- 현황 ----------
    def status(self):
        with self.lock:
            coins = []
            for c in self.cfg["coins"]:
                s = self.slots[c["coin"]]
                cfg, p, price = self.coin_cfg(c["coin"]), s.pos, s.last_price
                value = p.volume * price if price else None
                pnl = (value * (1 - cfg["fee_pct"] / 100) - p.cost) if value is not None and p.step else None
                nb = next_buy_trigger(p, cfg)
                coins.append({
                    "coin": c["coin"], "enabled": c["enabled"], "market": s.market, "price": price,
                    "paused": s.paused, "budget_blocked": s.budget_blocked,
                    "max_steps": c["max_steps"], "trailing_enabled": c["trailing_enabled"],
                    "coin_budget": c["coin_budget"], "limit_action": c["limit_action"],
                    "position": {**p.to_dict(), "avg_price": p.avg_price, "value": value, "pnl": pnl,
                                 "next_buy_price": nb,
                                 "next_buy_amount": step_amount(cfg, p.step) if nb else None,
                                 "next_drop": drop_for_step(cfg, p.step + 1) if nb else None,
                                 "take_profit_price": take_profit_price(p, cfg),
                                 "trailing_stop_price": trailing_stop_price(p, cfg),
                                 "stop_loss_price": stop_loss_price(p, cfg)},
                    "stats": s.stats, "ladder": ladder(cfg), "max_budget": max_budget(cfg),
                })
            tot_cost = sum(x["position"]["cost"] for x in coins)
            tot_pnl = sum(x["position"]["pnl"] or 0 for x in coins)
            return {
                "running": self.running, "mode": self.cfg["mode"], "exchange": self.cfg["exchange"],
                "total": {"cost": tot_cost, "pnl": tot_pnl,
                          "realized": sum(x["stats"]["realized"] for x in coins),
                          "cycles": sum(x["stats"]["cycles"] for x in coins),
                          "budget": self.cfg["total_budget"], "limit_action": self.cfg["total_limit_action"],
                          "max_need": sum(x["max_budget"] for x in coins if x["enabled"])},
                "coins": coins,
                "logs": list(self.logs)[:150],
                "last_error": self.last_error,
            }
