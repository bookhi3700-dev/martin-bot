"""과거 시세로 마틴 전략을 시뮬레이션합니다.

캔들(시가/고가/저가/종가) 안에서의 가격 순서는 알 수 없으므로 보수적으로 처리합니다.
 - 같은 캔들에서는 '익절 확인 → 추가 매수 → 손절' 순서
 - 추가 매수로 평단이 낮아진 그 캔들에서는 바로 익절하지 않음 (다음 캔들부터)
 - 추적 익절: 이전 캔들까지의 고점으로 매도 여부를 먼저 판단한 뒤 고점을 갱신
 - 모든 체결에 수수료 + 슬리피지(불리한 체결)를 반영
"""
import json
import os
import time
from datetime import datetime, timedelta

import itertools

from strategy import (Position, apply_buy, close_position, step_amount, next_buy_trigger, take_profit_price,
                      trailing_stop_price, stop_loss_price, parse_drops, validate_coin)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def fetch_candles(ex, market, days=365, unit=60, progress=None):
    """최근 days 일치 분봉(기본 60분봉)을 오래된 순서로 반환 (파일 캐시 사용)"""
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = os.path.join(DATA_DIR, f"candles_{type(ex).__name__}_{market}_{unit}m.json")
    need_from = datetime.utcnow() - timedelta(days=days)
    rows = {}
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            rows = {r["t"]: r for r in json.load(f)}
    to = None
    while True:
        batch = ex.candles(market, unit, 200, to)
        if not batch:
            break
        new = 0
        for c in batch:
            t = c["candle_date_time_utc"]
            if t not in rows:
                new += 1
            rows[t] = {"t": t, "kst": c["candle_date_time_kst"], "o": float(c["opening_price"]),
                       "h": float(c["high_price"]), "l": float(c["low_price"]), "c": float(c["trade_price"])}
        oldest = min(c["candle_date_time_utc"] for c in batch)
        if progress:
            progress(len(rows))
        if datetime.fromisoformat(oldest) <= need_from:
            break
        # 캐시가 이미 그 이전 구간을 갖고 있으면 건너뜀
        older_cached = [t for t in rows if t < oldest]
        if new == 0 and older_cached and datetime.fromisoformat(min(older_cached)) <= need_from:
            break
        to = oldest.replace("T", " ")
        time.sleep(0.15)
    data = sorted(rows.values(), key=lambda r: r["t"])
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(data, f)
    cutoff = need_from.isoformat(timespec="seconds")
    return [r for r in data if r["t"] >= cutoff]


def run_backtest(candles, cfg, detail=True):
    fee = cfg.get("fee_pct", 0.05) / 100
    slip = cfg.get("slippage_pct", 0.0) / 100
    maxs = int(cfg["max_steps"])
    pos = Position()
    cycles, events = [], []
    realized = 0.0
    peak_cost = worst_unrealized = 0.0
    max_step_hits = 0
    limit_hits = 0
    limit_flag = False   # 이번 사이클에 이미 한도에 걸렸는지
    stopped = False

    def buy(amount, price, when, reason):
        fill = price * (1 + slip)
        apply_buy(pos, amount, amount * (1 - fee) / fill, fill, when)
        if detail:
            events.append({"t": when, "type": "buy", "price": price, "step": pos.step, "krw": amount, "reason": reason})

    def sell(price, when, reason):
        nonlocal realized
        fill = price * (1 - slip)
        res = close_position(pos, pos.volume * fill * (1 - fee))
        res.update({"ended_at": when, "reason": reason})
        realized += res["profit"]
        cycles.append(res)
        if detail:
            events.append({"t": when, "type": "sell", "price": price, "profit": res["profit"], "reason": reason})

    for c in candles:
        when = c["kst"]
        if stopped:
            break
        if pos.step == 0:
            limit_flag = False
            buy(step_amount(cfg, 0), c["o"], when, "1단계")

        # 1) 익절
        tp = take_profit_price(pos, cfg)
        if cfg.get("trailing_enabled"):
            if pos.trail_armed:
                ts = trailing_stop_price(pos, cfg)
                if c["l"] <= ts:
                    sell(min(c["o"], ts), when, "추적 익절")
                else:
                    pos.trail_peak = max(pos.trail_peak, c["h"])
            elif c["h"] >= tp:
                pos.trail_armed, pos.trail_peak = True, c["h"]
        else:
            if c["o"] >= tp:
                sell(c["o"], when, "익절")
            elif c["h"] >= tp:
                sell(tp, when, "익절")
        if pos.step == 0:
            if not cfg.get("auto_restart", True):
                stopped = True
            continue

        # 2) 추가 매수 (한 캔들에서 여러 단계 가능)
        cb, la = cfg.get("coin_budget", 0), cfg.get("limit_action", "hold")
        while not pos.trail_armed:
            trig = next_buy_trigger(pos, cfg)
            if trig is None or c["l"] > trig:
                break
            amt = step_amount(cfg, pos.step)
            if cb > 0 and pos.cost + amt > cb:
                if not limit_flag:
                    limit_hits += 1
                    limit_flag = True
                if la == "pause":
                    stopped = True
                elif la == "sell":
                    sell(min(c["o"], trig), when, "한도 도달 매도")
                    stopped = True
                break
            buy(amt, min(c["o"], trig), when, f"{pos.step + 1}단계")
            if pos.step >= maxs:
                max_step_hits += 1
        if stopped:
            continue

        # 3) 손절
        sl = stop_loss_price(pos, cfg)
        if sl is not None and c["l"] <= sl:
            sell(min(c["o"], sl), when, "손절")
            if not cfg.get("auto_restart", True):
                stopped = True
            continue

        peak_cost = max(peak_cost, pos.cost)
        worst_unrealized = min(worst_unrealized, pos.volume * c["l"] * (1 - fee) - pos.cost)

    last = candles[-1]["c"] if candles else 0
    open_pnl = pos.volume * last * (1 - fee) - pos.cost if pos.step else 0
    durations = []
    for r in cycles:
        try:
            durations.append((datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds() / 86400)
        except Exception:
            pass
    first = candles[0]["o"] if candles else 0
    total = realized + open_pnl
    res = {
        "period": f"{candles[0]['kst'][:10]} ~ {candles[-1]['kst'][:10]}" if candles else "",
        "candles": len(candles),
        "buy_hold_pct": round((last / first - 1) * 100, 2) if first else 0,
        "cycles": len(cycles),
        "wins": sum(1 for r in cycles if r["profit"] > 0),
        "losses": sum(1 for r in cycles if r["profit"] <= 0),
        "realized": round(realized),
        "open_position": {"step": pos.step, "cost": round(pos.cost), "pnl": round(open_pnl),
                          "avg_price": pos.avg_price} if pos.step else None,
        "total_pnl": round(total),
        "peak_capital": round(peak_cost),
        "return_on_peak_pct": round(total / peak_cost * 100, 2) if peak_cost else 0,
        "worst_unrealized": round(worst_unrealized),
        "worst_unrealized_pct": round(worst_unrealized / peak_cost * 100, 1) if peak_cost else 0,
        "max_step_hits": max_step_hits,
        "limit_hits": limit_hits,
        "stopped_by_limit": stopped and limit_hits > 0,
        "avg_cycle_days": round(sum(durations) / len(durations), 1) if durations else None,
        "longest_cycle_days": round(max(durations), 1) if durations else None,
        "step_distribution": {str(s): sum(1 for r in cycles if r["steps"] == s) for s in range(1, maxs + 1)},
    }
    if detail:
        res["history"] = cycles[-100:][::-1]
        res["price_series"] = [[r["kst"][:16], r["c"]] for r in candles[:: max(1, len(candles) // 1500)]]
        res["events"] = events[-400:]
    return res


def _list(s, cast=float):
    return [cast(x) for x in str(s).replace(" ", "").split(",") if x != ""]


def run_sweep(candles, cfg, grid, limit=300):
    """grid 예: {"drop_steps": ["5", "5,5,7,10,15"], "take_profit_pct": "5,10,15", "max_steps": "5,6,7",
                 "martin_multiplier": "1.5,2", "trailing_pct": "0,2"}   (trailing_pct 0 = 추적 익절 끔)"""
    axes = {
        "drop_steps": [d.strip() for d in grid.get("drop_steps", []) if d.strip()] or [cfg["drop_steps"]],
        "take_profit_pct": _list(grid.get("take_profit_pct", "")) or [cfg["take_profit_pct"]],
        "max_steps": _list(grid.get("max_steps", ""), lambda x: int(float(x))) or [cfg["max_steps"]],
        "martin_multiplier": _list(grid.get("martin_multiplier", "")) or [cfg["martin_multiplier"]],
        "trailing_pct": _list(grid.get("trailing_pct", "")) or [cfg["trailing_pct"] if cfg["trailing_enabled"] else 0],
    }
    combos = list(itertools.product(*axes.values()))
    if len(combos) > limit:
        raise ValueError(f"조합이 {len(combos)}개입니다. {limit}개 이하로 줄여주세요.")
    out = []
    for vals in combos:
        c = dict(cfg)
        c.update(dict(zip(axes.keys(), vals)))
        c["trailing_enabled"] = c["trailing_pct"] > 0
        if c["trailing_pct"] <= 0:
            c["trailing_pct"] = 2.0
        if validate_coin(c):
            continue
        r = run_backtest(candles, c, detail=False)
        out.append({"drop_steps": c["drop_steps"], "take_profit_pct": c["take_profit_pct"],
                    "max_steps": c["max_steps"], "martin_multiplier": c["martin_multiplier"],
                    "trailing": c["trailing_pct"] if c["trailing_enabled"] else 0,
                    **{k: r[k] for k in ("cycles", "realized", "total_pnl", "peak_capital", "return_on_peak_pct",
                                         "worst_unrealized", "worst_unrealized_pct", "max_step_hits", "limit_hits", "open_position")}})
    out.sort(key=lambda r: r["total_pnl"], reverse=True)
    return {"period": f"{candles[0]['kst'][:10]} ~ {candles[-1]['kst'][:10]}",
            "buy_hold_pct": round((candles[-1]["c"] / candles[0]["o"] - 1) * 100, 2), "rows": out}


if __name__ == "__main__":
    import argparse
    from bot import load_config
    from exchanges import exchange_class

    ap = argparse.ArgumentParser(description="마틴 전략 백테스트")
    ap.add_argument("--coin", default=None)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    g = load_config()
    coin = args.coin or g["coins"][0]["coin"]
    cc = next((c for c in g["coins"] if c["coin"] == coin), g["coins"][0])
    cfg = {**g, **cc, "coin": coin}
    ex = exchange_class(g["exchange"])()
    cs = fetch_candles(ex, f"KRW-{coin}", args.days, progress=lambda n: print(f"\r캔들 {n}개 수집", end=""))
    print()
    r = run_backtest(cs, cfg, detail=False)
    for k, v in r.items():
        print(f"{k:22}: {v}")
