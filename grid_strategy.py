"""그리드(격자) 매매 규칙 — 순수 계산 로직 (거래소와 무관)

원화로 시작하는 롱(현물) 그리드
 - 하단~상단 가격 범위를 N칸으로 나눕니다. 칸 i = '아래 가격 p[i]에서 사서 위 가격 p[i+1]에서 판다'
 - 현재가보다 아래에 있는 칸마다 지정가 매수 주문을 깔아 둡니다 (칸당 같은 원화 금액)
 - 매수가 체결되면 바로 위 가격에 지정가 매도를 겁니다
 - 매도가 체결되면 그 칸의 수익이 확정되고, 같은 자리에 다시 매수를 겁니다
 - 시작할 때 코인을 미리 사지 않으므로, 현재가 위쪽 칸은 가격이 올라가 그 칸 위로 올라선 뒤부터 매수를 겁니다
 - 같은 가격에 '아래 칸의 매도'와 '위 칸의 매수'가 동시에 걸리지 않도록, 아래 칸이 코인을 들고 있으면 위 칸 매수는 쉽니다
"""
import math

GRID_DEFAULTS = {
    "exchange": "coinone",        # upbit | bithumb | coinone
    "mode": "paper",              # paper(모의투자) | live(실전)
    "access_key": "",             # 비우면 Martin Bot 설정의 키를 사용 (같은 거래소일 때)
    "secret_key": "",
    "coin": "BTC",
    "range_mode": "gap",          # gap(하단 + 칸 간격% + 칸 수 → 상단 자동) | range(하단·상단 직접 입력)
    "gap_pct": 1.0,               # 칸 간격(%) — 이만큼 오르면 판다
    "lower": 0.0,                 # 하단 가격
    "upper": 0.0,                 # 상단 가격
    "grids": 20,                  # 칸 수
    "spacing": "geom",            # geom(등비: 칸마다 같은 %) | arith(등간격: 칸마다 같은 원)
    "krw_per_grid": 10000,        # 칸당 매수 금액(원)
    "fee_pct": 0.2,               # 수수료(%) — 검증·모의투자·백테스트 계산용
    "check_interval_sec": 5,
    "stop_loss_enabled": False,   # 손절: 이 가격 아래로 내려가면 그리드 주문 취소 + 보유분 시장가 매도 + 중지
    "stop_loss_price": 0.0,
    "max_buy_orders": 0,          # 미리 걸어둘 매수 주문 수 (현재가에 가까운 칸부터). 0 = 아래 칸 전부
}

# 공통 설정(거래소·계좌 단위) / 코인별 설정 구분
GLOBAL_KEYS = ("exchange", "mode", "access_key", "secret_key", "fee_pct", "check_interval_sec")
GRID_GLOBAL_DEFAULTS = {k: GRID_DEFAULTS[k] for k in GLOBAL_KEYS}
GRID_COIN_DEFAULTS = {"enabled": True, **{k: v for k, v in GRID_DEFAULTS.items() if k not in GLOBAL_KEYS}}

MAX_GRIDS = 100


def apply_range_mode(cfg, tick=0.0):
    """간격 % 방식이면 상단 가격을 계산해서 채움 (등비 고정). cfg 를 직접 바꾸고 돌려줌"""
    if cfg.get("range_mode") == "gap" and cfg.get("lower", 0) > 0 and cfg.get("gap_pct", 0) > 0 and int(cfg.get("grids", 0)) >= 1:
        cfg["spacing"] = "geom"
        cfg["upper"] = round_tick(cfg["lower"] * (1 + cfg["gap_pct"] / 100) ** int(cfg["grids"]), tick)
    return cfg


def round_tick(p, tick):
    if tick <= 0:
        return p
    return round(round(p / tick) * tick, 10)


def build_levels(cfg, tick=0.0):
    """하단~상단을 칸 수만큼 나눈 가격 목록 (칸 수 + 1개). 호가 단위로 반올림"""
    lo, hi, n = float(cfg["lower"]), float(cfg["upper"]), int(cfg["grids"])
    if n < 1 or lo <= 0 or hi <= lo:
        return []
    if cfg.get("spacing") == "arith":
        raw = [lo + (hi - lo) * i / n for i in range(n + 1)]
    else:
        r = (hi / lo) ** (1 / n)
        raw = [lo * r ** i for i in range(n + 1)]
    return [round_tick(p, tick) for p in raw]


def cell_profit_pct(buy, sell, fee_pct):
    """한 칸을 사고팔았을 때 수수료를 뺀 수익률(%)"""
    f = fee_pct / 100
    return (sell * (1 - f) / (buy * (1 + f)) - 1) * 100


def summary(cfg, tick=0.0):
    lv = build_levels(cfg, tick)
    if len(lv) < 2:
        return None
    profits = [cell_profit_pct(a, b, cfg["fee_pct"]) for a, b in zip(lv, lv[1:])]
    n = len(lv) - 1
    return {"levels": lv, "min_profit_pct": round(min(profits), 3), "max_profit_pct": round(max(profits), 3),
            "budget": int(cfg["krw_per_grid"]) * n, "cells": n,
            "per_cell_profit_krw": round(cfg["krw_per_grid"] * min(profits) / 100)}


def validate(cfg, tick=0.0, min_krw=5000):
    errs = []
    lo, hi, n = float(cfg["lower"]), float(cfg["upper"]), int(cfg["grids"])
    if cfg["mode"] not in ("paper", "live"):
        errs.append("실행 모드 값이 올바르지 않습니다.")
    if cfg.get("range_mode") not in ("gap", "range"):
        errs.append("범위 정하는 방식 값이 올바르지 않습니다.")
    if cfg.get("range_mode") == "gap" and not (0.2 <= cfg.get("gap_pct", 0) <= 50):
        errs.append("칸 간격은 0.2% ~ 50% 사이로 입력하세요.")
    if not cfg["coin"] or not cfg["coin"].isalnum():
        errs.append("코인 심볼을 영문으로 입력하세요. 예: BTC")
    if lo <= 0 or hi <= 0:
        errs.append("하단·상단 가격을 입력하세요.")
    elif hi <= lo * 1.005:
        errs.append("상단 가격은 하단 가격보다 충분히 커야 합니다.")
    if not (2 <= n <= MAX_GRIDS):
        errs.append(f"칸 수는 2~{MAX_GRIDS} 사이여야 합니다.")
    if cfg["spacing"] not in ("geom", "arith"):
        errs.append("간격 방식 값이 올바르지 않습니다.")
    if cfg["krw_per_grid"] < min_krw * 1.1:
        errs.append(f"칸당 매수 금액은 최소 {math.ceil(min_krw * 1.1):,}원 이상으로 해주세요 "
                    f"(거래소 최소 주문 {min_krw:,.0f}원 + 수수료·단위 절사 여유).")
    if not (0 <= cfg["fee_pct"] < 2):
        errs.append("수수료(%) 값이 올바르지 않습니다.")
    if cfg["check_interval_sec"] < 3:
        errs.append("시세 확인 주기는 3초 이상이어야 합니다.")
    if not (0 <= int(cfg.get("max_buy_orders", 0)) <= MAX_GRIDS):
        errs.append("미리 걸어둘 매수 주문 수는 0~100 사이로 입력하세요 (0 = 전부).")
    if cfg["stop_loss_enabled"]:
        if cfg["stop_loss_price"] <= 0:
            errs.append("손절 가격을 입력하세요.")
        elif lo > 0 and cfg["stop_loss_price"] >= lo:
            errs.append("손절 가격은 하단 가격보다 낮아야 합니다.")
    if errs:
        return errs
    lv = build_levels(cfg, tick)
    if len(set(lv)) != len(lv):
        errs.append("칸이 너무 촘촘해서 호가 단위로 반올림하면 같은 가격이 생깁니다. 칸 수를 줄이거나 범위를 넓히세요.")
        return errs
    s = summary(cfg, tick)
    if s["min_profit_pct"] < 0.1:
        errs.append(f"칸 간격이 너무 좁습니다: 수수료({cfg['fee_pct']}% × 2)를 빼면 한 칸 수익이 "
                    f"{s['min_profit_pct']}%로 0.1%보다 작습니다. 칸 수를 줄이거나 범위를 넓히세요.")
    return errs


def conflict_with_martin(grid_cfg, martin_cfg, from_martin=False):
    """같은 거래소에서 Martin Bot과 Grid가 같은 코인을 실전 매매할 때 보여줄 경고 (막지는 않음)"""
    if grid_cfg.get("mode") != "live" or martin_cfg.get("mode") != "live":
        return None
    if grid_cfg.get("exchange") != martin_cfg.get("exchange"):
        return None
    coin = (grid_cfg.get("coin") or "").upper()
    if coin in {c["coin"] for c in martin_cfg.get("coins", [])}:
        return (f"{coin}: Martin과 Grid가 같은 거래소에서 함께 실전 매매합니다. 각자 자기가 산 수량만 기록하고 팔지만, "
                f"거래소 앱의 {coin} 보유 수량은 두 봇 합계입니다. 거래소 앱에서 직접 사고팔지 마세요.")
    return None


def new_cells(levels):
    return [{"i": i, "buy": levels[i], "sell": levels[i + 1], "st": "idle", "cid": "", "ok": False,
             "qty": 0.0, "cost": 0.0, "at": ""} for i in range(len(levels) - 1)]


def buy_window(cells, price, tick, max_orders):
    """매수 주문을 걸어도 되는 칸(place)과, 이미 걸린 매수를 유지할 칸(keep).
    현재가 바로 아래부터 비어 있거나 매수 대기인 칸을 차례로 셉니다. max_orders=0 이면 전부.
    유지 범위는 1칸 더 넓게 둬서, 가격이 칸 경계에서 오르내릴 때 주문을 걸었다 취소했다 반복하지 않게 합니다."""
    gap = max(tick, price * 1e-6)
    below = [i for i in range(len(cells) - 1, -1, -1)
             if cells[i]["buy"] <= price - gap and cells[i]["st"] in ("idle", "buy")]
    if max_orders <= 0:
        return set(below), set(below)
    return set(below[:max_orders]), set(below[:max_orders + 1])


def can_place_buy(cells, i, price, tick):
    """칸 i 에 매수를 걸어도 되는지: 현재가보다 아래(대기 주문이 되도록)이고, 아래 칸이 같은 가격에 매도를 걸고 있지 않아야 함"""
    c = cells[i]
    if c["st"] != "idle":
        return False
    if not (c["buy"] <= price - max(tick, price * 1e-6)):
        return False
    if i > 0 and cells[i - 1]["st"] in ("hold", "sell"):
        return False
    return True
