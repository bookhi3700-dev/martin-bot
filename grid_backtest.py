"""과거 시세로 그리드 전략을 시뮬레이션합니다.

캔들 안의 가격 순서는 알 수 없으므로 보수적으로 처리합니다.
 - 양봉은 시가→저가→고가→종가, 음봉은 시가→고가→저가→종가 순서로 움직였다고 봅니다
 - 캔들 안에서 새로 건 주문(매수 체결 후 건 매도, 매도 체결 후 다시 건 매수)은 다음 캔들부터 체결될 수 있습니다
 - 지정가는 주문 가격 그대로 체결, 매수·매도 모두 수수료를 뺍니다
"""
from grid_strategy import build_levels, new_cells, can_place_buy, cell_profit_pct, apply_range_mode


def run_grid_backtest(candles, cfg, tick=0.0, detail=True):
    fee = cfg["fee_pct"] / 100
    krw = float(cfg["krw_per_grid"])
    levels = build_levels(cfg, tick)
    cells = new_cells(levels)
    n = len(cells)
    if n < 1 or not candles:
        raise ValueError("격자 또는 시세 데이터가 없습니다.")
    for c in cells:
        c["pk"] = -1          # 주문을 건 캔들 번호
    realized = fees = 0.0
    trips = 0
    worst_unreal = 0.0
    max_locked = 0.0
    stop_hit = None
    sl = cfg["stop_loss_price"] if cfg.get("stop_loss_enabled") else 0
    daily, last_day = [], None
    trips_by_cell = [0] * n

    def place(k, close):
        for c in cells:
            if c["st"] == "hold":
                c["st"], c["pk"] = "sell", k
        for i in range(n - 1, -1, -1):
            if can_place_buy(cells, i, close, tick):
                cells[i]["st"], cells[i]["pk"] = "buy", k

    place(-1, candles[0]["o"])
    in_range = 0
    for k, cd in enumerate(candles):
        o, h, l, cl = cd["o"], cd["h"], cd["l"], cd["c"]
        path = [o, l, h, cl] if cl >= o else [o, h, l, cl]
        for a, b in zip(path, path[1:]):
            if b < a:   # 하락 구간: 매수 체결
                for c in cells:
                    if c["st"] == "buy" and c["pk"] < k and c["buy"] >= b:
                        qty = krw / c["buy"]
                        cost = qty * c["buy"] * (1 + fee)
                        fees += qty * c["buy"] * fee
                        c.update(st="hold", qty=qty, cost=cost, pk=k)
                if sl and b <= sl:
                    stop_hit = cd["kst"]
                    break
            elif b > a:  # 상승 구간: 매도 체결
                for i, c in enumerate(cells):
                    if c["st"] == "sell" and c["pk"] < k and c["sell"] <= b:
                        proceeds = c["qty"] * c["sell"] * (1 - fee)
                        fees += c["qty"] * c["sell"] * fee
                        realized += proceeds - c["cost"]
                        trips += 1
                        trips_by_cell[i] += 1
                        c.update(st="idle", qty=0.0, cost=0.0, pk=k)
        if stop_hit:
            hold = [c for c in cells if c["st"] in ("hold", "sell")]
            px = sl * 0.995
            for c in hold:
                realized += c["qty"] * px * (1 - fee) - c["cost"]
                fees += c["qty"] * px * fee
                c.update(st="idle", qty=0.0, cost=0.0)
            for c in cells:
                c["st"] = "idle"
            last_close = sl
            break
        place(k, cl)
        last_close = cl
        if levels[0] <= cl <= levels[-1]:
            in_range += 1
        hold = [c for c in cells if c["st"] in ("hold", "sell")]
        hq, hc = sum(c["qty"] for c in hold), sum(c["cost"] for c in hold)
        unreal = hq * cl * (1 - fee) - hc
        worst_unreal = min(worst_unreal, unreal)
        locked = hc + sum(krw for c in cells if c["st"] == "buy")
        max_locked = max(max_locked, locked)
        day = cd["kst"][:10]
        if detail and day != last_day:
            daily.append([day, round(realized), round(realized + unreal), cl])
            last_day = day

    hold = [c for c in cells if c["st"] in ("hold", "sell")]
    hq, hc = sum(c["qty"] for c in hold), sum(c["cost"] for c in hold)
    unreal = hq * last_close * (1 - fee) - hc
    budget = krw * n
    total = realized + unreal
    first, last = candles[0], candles[-1] if not stop_hit else candles[k]
    days = max(1, len(candles) / 24)
    bh_pct = (last_close / first["o"] - 1) * 100
    res = {
        "period": f"{first['kst'][:10]} ~ {last['kst'][:10]}",
        "grids": n, "krw_per_grid": round(krw), "budget": round(budget),
        "lower": levels[0], "upper": levels[-1],
        "cell_profit_pct": round(min(cell_profit_pct(c["buy"], c["sell"], cfg["fee_pct"]) for c in cells), 3),
        "round_trips": trips, "trips_per_day": round(trips / days, 2),
        "realized": round(realized), "fees": round(fees),
        "end_hold_qty": hq, "end_hold_cost": round(hc), "end_unrealized": round(unreal),
        "end_cells_holding": len(hold),
        "total_pnl": round(total), "return_pct": round(total / budget * 100, 2),
        "annual_pct": round(total / budget * 100 * 365 / days, 1),
        "buy_hold_pct": round(bh_pct, 2), "buy_hold_pnl": round(budget * bh_pct / 100),
        "worst_unrealized": round(worst_unreal), "worst_unrealized_pct": round(worst_unreal / budget * 100, 1),
        "max_locked": round(max_locked),
        "in_range_pct": round(in_range / max(1, k + 1) * 100, 1),
        "stopped_at": stop_hit,
    }
    if detail:
        step = max(1, len(candles) // 1500)
        res["price_series"] = [[c["kst"][:16], c["c"]] for c in candles[::step]]
        res["daily"] = daily
        res["trips_by_cell"] = [{"cell": i + 1, "buy": cells[i]["buy"], "sell": cells[i]["sell"], "trips": t}
                                for i, t in enumerate(trips_by_cell)]
    return res


def run_grid_sweep(candles, cfg, counts, tick=0.0, min_krw=5000):
    """같은 총 예산으로 칸 수만 바꿔 비교"""
    budget = cfg["krw_per_grid"] * cfg["grids"]
    rows = []
    for n in sorted({int(x) for x in counts if 2 <= int(x) <= 100} | {int(cfg["grids"])}):
        c = {**cfg, "grids": n, "krw_per_grid": budget / n}
        lv = build_levels(c, tick)
        if len(set(lv)) != len(lv):
            continue
        note = ""
        if c["krw_per_grid"] < min_krw * 1.1:
            note = "칸당 금액이 최소 주문금액 미만"
        elif min(cell_profit_pct(a, b, c["fee_pct"]) for a, b in zip(lv, lv[1:])) < 0.1:
            note = "간격이 수수료보다 좁음"
        r = run_grid_backtest(candles, c, tick, detail=False)
        r["note"] = note
        r["current"] = n == int(cfg["grids"])
        rows.append(r)
    rows.sort(key=lambda r: (r["note"] != "", -r["total_pnl"]))
    return rows


def run_gap_sweep(candles, cfg, gaps, tick=0.0, min_krw=5000):
    """간격 % 방식: 하단·칸당 금액·칸 수는 그대로 두고 칸 간격(%)만 바꿔 비교 (간격이 넓을수록 범위도 넓어짐)"""
    rows = []
    for g in sorted({round(float(x), 3) for x in gaps if 0.2 <= float(x) <= 50} | {round(cfg["gap_pct"], 3)}):
        c = apply_range_mode({**cfg, "gap_pct": g}, tick)
        lv = build_levels(c, tick)
        if len(set(lv)) != len(lv):
            continue
        note = "간격이 수수료보다 좁음" if min(cell_profit_pct(a, b, c["fee_pct"]) for a, b in zip(lv, lv[1:])) < 0.1 else ""
        r = run_grid_backtest(candles, c, tick, detail=False)
        r.update(note=note, gap_pct=g, current=abs(g - cfg["gap_pct"]) < 1e-9)
        rows.append(r)
    rows.sort(key=lambda r: (r["note"] != "", -r["total_pnl"]))
    return rows


def suggest_range(candles, days):
    """최근 days 일 고가·저가로 범위 제안"""
    if not candles:
        return None
    sub = candles[-days * 24:]
    lo, hi = min(c["l"] for c in sub), max(c["h"] for c in sub)
    return {"low": lo, "high": hi, "price": candles[-1]["c"], "days": days}
