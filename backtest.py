"""과거 시세로 마틴 전략을 시뮬레이션합니다.

캔들(시가/고가/저가/종가) 안에서의 가격 순서는 알 수 없으므로 보수적으로 처리합니다.
 - 같은 캔들에서는 '익절 확인 → 추가 매수 → 손절' 순서
 - 추가 매수로 평단이 낮아진 그 캔들에서는 바로 익절하지 않음 (다음 캔들부터)
"""
import json
import os
import time
from datetime import datetime, timedelta

from strategy import Position, decide, apply_buy, close_position, step_amount, next_buy_trigger, take_profit_price, stop_loss_price

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


def run_backtest(candles, cfg):
    fee = cfg.get("fee_pct", 0.05) / 100
    pos = Position()
    cycles, events = [], []
    realized = 0.0
    peak_cost = 0.0
    worst_unrealized = 0.0
    max_step_hits = 0
    stopped = False

    def buy(amount, price, when, reason):
        vol = amount * (1 - fee) / price
        apply_buy(pos, amount, vol, price, when)
        events.append({"t": when, "type": "buy", "price": price, "step": pos.step, "krw": amount, "reason": reason})

    def sell(price, when, reason):
        nonlocal realized
        net = pos.volume * price * (1 - fee)
        res = close_position(pos, net)
        res.update({"ended_at": when, "reason": reason})
        realized += res["profit"]
        cycles.append(res)
        events.append({"t": when, "type": "sell", "price": price, "profit": res["profit"], "reason": reason})

    for c in candles:
        when = c["kst"]
        if stopped:
            break
        if pos.step == 0:
            buy(step_amount(cfg, 0), c["o"], when, "사이클 시작")

        # 1) 익절 (이 캔들 시작 시점 평단 기준)
        tp = take_profit_price(pos, cfg)
        if c["o"] >= tp:
            sell(c["o"], when, "익절")
        elif c["h"] >= tp:
            sell(tp, when, "익절")
        if pos.step == 0:
            if not cfg.get("auto_restart", True):
                stopped = True
            continue

        # 2) 추가 매수 (한 캔들에서 여러 단계 가능)
        while True:
            trig = next_buy_trigger(pos, cfg)
            if trig is None or c["l"] > trig:
                break
            buy(step_amount(cfg, pos.step), min(c["o"], trig), when, f"{pos.step + 1}단계")
            if pos.step >= int(cfg["max_steps"]):
                max_step_hits += 1

        # 3) 손절
        sl = stop_loss_price(pos, cfg)
        if sl is not None and c["l"] <= sl:
            sell(min(c["o"], sl), when, "손절")
            if not cfg.get("auto_restart", True):
                stopped = True
            continue

        peak_cost = max(peak_cost, pos.cost)
        unreal = pos.volume * c["l"] * (1 - fee) - pos.cost
        worst_unrealized = min(worst_unrealized, unreal)

    last = candles[-1]["c"] if candles else 0
    open_pnl = pos.volume * last * (1 - fee) - pos.cost if pos.step else 0
    durations = []
    for r in cycles:
        try:
            d = datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(r["started_at"])
            durations.append(d.total_seconds() / 86400)
        except Exception:
            pass
    first = candles[0]["o"] if candles else 0
    return {
        "period": f"{candles[0]['kst'][:10]} ~ {candles[-1]['kst'][:10]}" if candles else "",
        "candles": len(candles),
        "price_change_pct": round((last / first - 1) * 100, 2) if first else 0,
        "cycles": len(cycles),
        "wins": sum(1 for r in cycles if r["profit"] > 0),
        "losses": sum(1 for r in cycles if r["profit"] <= 0),
        "realized": round(realized),
        "open_position": {"step": pos.step, "cost": round(pos.cost), "pnl": round(open_pnl),
                          "avg_price": pos.avg_price} if pos.step else None,
        "total_pnl": round(realized + open_pnl),
        "peak_capital": round(peak_cost),
        "return_on_peak_pct": round((realized + open_pnl) / peak_cost * 100, 2) if peak_cost else 0,
        "worst_unrealized": round(worst_unrealized),
        "max_step_hits": max_step_hits,
        "avg_cycle_days": round(sum(durations) / len(durations), 1) if durations else None,
        "longest_cycle_days": round(max(durations), 1) if durations else None,
        "step_distribution": {str(s): sum(1 for r in cycles if r["steps"] == s) for s in range(1, int(cfg["max_steps"]) + 1)},
        "history": cycles[-100:][::-1],
        "price_series": [[r["kst"][:16], r["c"]] for r in candles[:: max(1, len(candles) // 1500)]],
        "events": events[-400:],
    }


if __name__ == "__main__":
    import argparse
    from bot import load_config
    from exchanges import Upbit, Bithumb

    ap = argparse.ArgumentParser(description="마틴 전략 백테스트")
    ap.add_argument("--coin", default=None)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    cfg = load_config()
    if args.coin:
        cfg["coin"] = args.coin
    ex = Bithumb() if cfg["exchange"] == "bithumb" else Upbit()
    cs = fetch_candles(ex, f"KRW-{cfg['coin']}", args.days, progress=lambda n: print(f"\r캔들 {n}개 수집", end=""))
    print()
    r = run_backtest(cs, cfg)
    for k in ("period", "price_change_pct", "cycles", "wins", "losses", "realized", "open_position",
              "total_pnl", "peak_capital", "return_on_peak_pct", "worst_unrealized", "max_step_hits",
              "avg_cycle_days", "longest_cycle_days", "step_distribution"):
        print(f"{k:20}: {r[k]}")
