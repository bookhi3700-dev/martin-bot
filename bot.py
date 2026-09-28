"""자동매매 실행기 — 설정/상태 저장, 주기적 시세 확인, 주문 실행, 알림"""
import csv
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime

import requests

from exchanges import make_exchange, ExchangeError
from strategy import (DEFAULT_CONFIG, Position, decide, apply_buy, close_position,
                      next_buy_trigger, take_profit_price, stop_loss_price, ladder, MIN_ORDER_KRW)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
os.makedirs(DATA_DIR, exist_ok=True)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def validate_config(cfg):
    errs = []
    if cfg["base_amount"] < MIN_ORDER_KRW:
        errs.append(f"기본 매수금액은 최소 {MIN_ORDER_KRW:,}원 이상이어야 합니다.")
    if cfg["martin_multiplier"] < 1:
        errs.append("마틴 배수는 1 이상이어야 합니다.")
    if not (0 < cfg["drop_pct"] < 100):
        errs.append("추가 매수 하락폭은 0~100% 사이여야 합니다.")
    if not (0 < cfg["take_profit_pct"] < 1000):
        errs.append("익절 수익률이 올바르지 않습니다.")
    if not (1 <= int(cfg["max_steps"]) <= 15):
        errs.append("최대 단계는 1~15 사이여야 합니다.")
    if cfg["check_interval_sec"] < 3:
        errs.append("시세 확인 주기는 3초 이상이어야 합니다.")
    if cfg["mode"] == "live" and (not cfg["access_key"] or not cfg["secret_key"]):
        errs.append("실전 모드는 API Access Key / Secret Key 가 필요합니다.")
    return errs


class Bot:
    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.Lock()
        self.thread = None
        self.running = False
        self.logs = deque(maxlen=300)
        self.last_price = None
        self.last_error = ""
        self.pos = Position()
        self.stats = {"cycles": 0, "realized": 0, "history": []}
        self._load_state()

    # ---------- 파일 ----------
    @property
    def key(self):
        c = self.cfg
        return f"{c['mode']}_{c['exchange']}_{c['coin']}"

    @property
    def market(self):
        return f"KRW-{self.cfg['coin']}"

    def _state_path(self):
        return os.path.join(DATA_DIR, f"state_{self.key}.json")

    def _load_state(self):
        self.pos, self.stats = Position(), {"cycles": 0, "realized": 0, "history": []}
        p = self._state_path()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            self.pos = Position.from_dict(d.get("position"))
            self.stats.update(d.get("stats", {}))

    def _save_state(self):
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"position": self.pos.to_dict(), "stats": self.stats}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path())

    def _log_trade(self, side, reason, fill):
        path = os.path.join(DATA_DIR, f"trades_{self.key}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각", "구분", "사유", "체결가", "수량", "원화", "수수료", "단계", "평단가"])
            w.writerow([now(), side, reason, round(fill["price"], 4), f"{fill['volume']:.8f}",
                        round(fill["krw"]), round(fill["fee"], 2), self.pos.step, round(self.pos.avg_price, 4)])

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
            requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                          json={"chat_id": c, "text": f"[마틴봇 {self.key}]\n{msg}"}, timeout=5)
        except Exception:
            pass

    # ---------- 설정 ----------
    def update_config(self, new_cfg):
        with self.lock:
            merged = dict(self.cfg)
            for k, v in new_cfg.items():
                if k in DEFAULT_CONFIG:
                    if k in ("access_key", "secret_key", "telegram_token") and (v == "" or (isinstance(v, str) and "•" in v)):
                        continue  # 빈칸/마스킹 값이면 기존 키 유지
                    typ = type(DEFAULT_CONFIG[k])
                    if typ is bool:
                        merged[k] = v in (True, "true", "on", 1, "1")
                    elif typ is int:
                        merged[k] = int(float(v))
                    else:
                        merged[k] = typ(v)
            errs = validate_config(merged)
            if errs:
                return errs
            identity_changed = any(merged[k] != self.cfg[k] for k in ("mode", "exchange", "coin"))
            if identity_changed and self.running:
                return ["실행 중에는 거래소/코인/모드를 바꿀 수 없습니다. 먼저 중지해 주세요."]
            self.cfg = merged
            save_config(self.cfg)
            if identity_changed:
                self._load_state()
            self.log("설정이 저장되었습니다.")
            return []

    def public_config(self):
        c = dict(self.cfg)
        for k in ("access_key", "secret_key", "telegram_token"):
            if c.get(k):
                c[k] = c[k][:4] + "••••••••"
        return c

    # ---------- 실행 ----------
    def start(self):
        with self.lock:
            if self.running:
                return ["이미 실행 중입니다."]
            errs = validate_config(self.cfg)
            if errs:
                return errs
            self.running = True
            self._set_run_flag(True)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        mode = "실전" if self.cfg["mode"] == "live" else "모의투자"
        self.log(f"▶ 시작 — {mode} / {self.cfg['exchange']} / {self.market}", notify=True)
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
            except ExchangeError as e:
                self.last_error = str(e)
                self.log(f"⚠ {e}", notify=True)
                time.sleep(max(30, self.cfg["check_interval_sec"]))
            except Exception as e:
                self.last_error = str(e)
                self.log(f"⚠ 예기치 않은 오류: {e}")
                traceback.print_exc()
                time.sleep(30)
            for _ in range(int(self.cfg["check_interval_sec"] * 10)):
                if not self.running:
                    break
                time.sleep(0.1)

    def tick(self, ex):
        price = ex.price(self.market)
        self.last_price = price
        with self.lock:
            act = decide(self.pos, price, self.cfg)
        if act.kind == "hold":
            return
        if act.kind == "buy":
            fill = ex.market_buy(self.market, act.amount_krw)
            with self.lock:
                apply_buy(self.pos, fill["krw"], fill["volume"], fill["price"], now())
                self._save_state()
                self._log_trade("매수", act.reason, fill)
            self.log(f"🟢 {act.reason}: {fill['krw']:,.0f}원 @ {fill['price']:,.2f} / "
                     f"평단 {self.pos.avg_price:,.2f} / 누적 {self.pos.cost:,.0f}원", notify=True)
            if self.pos.step >= int(self.cfg["max_steps"]):
                self.log(f"⚠ 최대 {self.pos.step}단계 도달 — 더 이상 추가 매수하지 않습니다.", notify=True)
        elif act.kind == "sell":
            fill = ex.market_sell(self.market, act.volume)
            with self.lock:
                self._log_trade("매도", act.reason, fill)
                res = close_position(self.pos, fill["krw"])
                res["ended_at"] = now()
                res["reason"] = act.reason
                self.stats["cycles"] += 1
                self.stats["realized"] += res["profit"]
                self.stats["history"] = ([res] + self.stats["history"])[:200]
                self._save_state()
            self.log(f"🔴 {act.reason}: {res['steps']}단계 사이클 종료, 손익 {res['profit']:+,}원 "
                     f"({res['profit_pct']:+.2f}%) / 누적 실현손익 {self.stats['realized']:+,}원", notify=True)
            if not self.cfg["auto_restart"]:
                self.running = False
                self._set_run_flag(False)
                self.log("자동 재시작이 꺼져 있어 봇을 멈췄습니다.", notify=True)

    # ---------- 재부팅 후 자동 이어하기 ----------
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
            self.log("재시작 감지 — 중지 전 상태였던 자동매매를 이어서 실행합니다.")
            self.start()

    def reset_position(self):
        """보유 기록 초기화 (거래소 잔고는 건드리지 않음)"""
        with self.lock:
            if self.running:
                return ["실행 중에는 초기화할 수 없습니다."]
            self.pos = Position()
            self._save_state()
        self.log("포지션 기록을 초기화했습니다. (거래소 잔고는 그대로)")
        return []

    def status(self):
        with self.lock:
            p, c = self.pos, self.cfg
            price = self.last_price
            value = p.volume * price if price else None
            return {
                "running": self.running,
                "key": self.key,
                "market": self.market,
                "price": price,
                "position": {
                    **p.to_dict(),
                    "avg_price": p.avg_price,
                    "value": value,
                    "pnl": (value * (1 - c["fee_pct"] / 100) - p.cost) if value is not None and p.step else None,
                    "next_buy_price": next_buy_trigger(p, c),
                    "next_buy_amount": round(c["base_amount"] * c["martin_multiplier"] ** p.step) if p.step < c["max_steps"] else None,
                    "take_profit_price": take_profit_price(p, c),
                    "stop_loss_price": stop_loss_price(p, c),
                },
                "stats": self.stats,
                "ladder": ladder(c),
                "logs": list(self.logs)[:100],
                "last_error": self.last_error,
            }
