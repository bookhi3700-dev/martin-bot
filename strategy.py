"""마틴게일 물타기 전략 (순수 계산 로직 — 거래소와 무관)

코인 한 개의 규칙
 1) 포지션이 없으면 1단계 매수 (base_amount)
 2) 기준가(평단가 또는 직전 매수가)보다 '이번 단계 하락폭'% 이상 떨어지면
    직전 매수금액 x martin_multiplier 로 추가 매수 (max_steps 까지)
    - 단계별 하락폭 drop_steps 예: "5,5,7,10,15" → 2단계 5%, 3단계 5%, 4단계 7%, 5단계 10%, 6단계 15%
      (단계 수보다 짧으면 마지막 값을 반복)
 3) 익절: 평단가(수수료 포함) 대비 take_profit_pct% 도달
    - 추적 익절 끔: 즉시 전량 매도
    - 추적 익절 켬: 그때부터 고점을 따라가다 고점 대비 trailing_pct% 빠지면 매도
      (단, 목표 익절가 밑으로는 기다리지 않고 바로 매도 → 최소 목표 수익은 확보)
 4) (선택) 최대 단계 이후 평단가 대비 stop_loss_pct% 하락 시 전량 손절
 5) auto_restart 켜져 있으면 다음 사이클 자동 시작
"""
from dataclasses import dataclass, field, asdict

GLOBAL_DEFAULTS = {
    "exchange": "upbit",          # upbit | bithumb
    "mode": "paper",              # paper(모의투자) | live(실전)
    "access_key": "",
    "secret_key": "",
    "total_budget": 0,            # 전체 투입 한도(원) — 0 이면 제한 없음
    "total_limit_action": "hold", # hold(추가 매수만 보류) | stop_bot(봇 전체 중지)
    "check_interval_sec": 10,
    "fee_pct": 0.05,              # 모의투자/백테스트 수수료(%)
    "slippage_pct": 0.1,          # 백테스트 슬리피지(%) — 시장가 주문이 불리하게 체결되는 정도
    "telegram_token": "",
    "telegram_chat_id": "",
}

COIN_DEFAULTS = {
    "coin": "BTC",
    "enabled": True,
    "base_amount": 10000,
    "martin_multiplier": 2.0,
    "drop_steps": "5",            # 단계별 하락폭(%) 목록, 쉼표 구분
    "drop_basis": "avg",          # avg(평단가) | last(직전 매수가)
    "max_steps": 6,
    "take_profit_pct": 10.0,
    "trailing_enabled": False,
    "trailing_pct": 2.0,
    "stop_loss_enabled": False,
    "stop_loss_pct": 15.0,
    "auto_restart": True,
    "coin_budget": 0,             # 이 코인 투입 한도(원) — 0 이면 제한 없음
    "limit_action": "hold",       # hold(추가 매수만 중지, 익절 대기) | pause(코인 매매 중지) | sell(전량 매도 후 중지)
}

MIN_ORDER_KRW = 5000


def parse_drops(s):
    try:
        vals = [float(x) for x in str(s).replace(" ", "").split(",") if x != ""]
    except ValueError:
        return []
    return vals


def drop_for_step(cfg, next_step):
    """next_step: 다음에 살 단계 번호 (2부터)"""
    vals = parse_drops(cfg["drop_steps"]) or [5.0]
    i = next_step - 2
    return vals[i] if i < len(vals) else vals[-1]


def step_amount(cfg, step_index):
    """step_index: 0부터 (0 = 1단계)"""
    return int(round(cfg["base_amount"] * (cfg["martin_multiplier"] ** step_index)))


def ladder(cfg):
    """단계별 매수액·누적액·하락폭 표"""
    rows, total = [], 0
    for i in range(int(cfg["max_steps"])):
        a = step_amount(cfg, i)
        total += a
        rows.append({"step": i + 1, "amount": a, "cumulative": total,
                     "drop": None if i == 0 else drop_for_step(cfg, i + 1)})
    return rows


def max_budget(cfg):
    return sum(step_amount(cfg, i) for i in range(int(cfg["max_steps"])))


@dataclass
class Position:
    step: int = 0
    cost: float = 0.0             # 총 투입 원화 (수수료 포함)
    volume: float = 0.0
    last_buy_price: float = 0.0
    cycle_started_at: str = ""
    trail_armed: bool = False
    trail_peak: float = 0.0
    buys: list = field(default_factory=list)

    @property
    def avg_price(self):
        return self.cost / self.volume if self.volume > 0 else 0.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k):
                setattr(p, k, v)
        return p


@dataclass
class Action:
    kind: str            # buy | sell | hold
    reason: str = ""
    amount_krw: float = 0.0
    volume: float = 0.0
    trigger_price: float = 0.0


def next_buy_trigger(pos, cfg):
    if pos.step == 0 or pos.step >= int(cfg["max_steps"]):
        return None
    ref = pos.avg_price if cfg["drop_basis"] == "avg" else pos.last_buy_price
    return ref * (1 - drop_for_step(cfg, pos.step + 1) / 100)


def take_profit_price(pos, cfg):
    if pos.step == 0:
        return None
    # 매도 수수료까지 빼고도 목표 수익률이 남도록 보정
    return pos.avg_price * (1 + cfg["take_profit_pct"] / 100) / (1 - cfg.get("fee_pct", 0) / 100)


def trailing_stop_price(pos, cfg):
    """추적 익절 중일 때 이 가격 이하로 내려오면 매도"""
    if not pos.trail_armed:
        return None
    return max(pos.trail_peak * (1 - cfg["trailing_pct"] / 100), take_profit_price(pos, cfg))


def stop_loss_price(pos, cfg):
    if pos.step == 0 or not cfg["stop_loss_enabled"] or pos.step < int(cfg["max_steps"]):
        return None
    return pos.avg_price * (1 - cfg["stop_loss_pct"] / 100)


def update_trailing(pos, price, cfg):
    """추적 익절 상태 갱신. 새로 시작되면 True"""
    if pos.step == 0 or not cfg["trailing_enabled"]:
        return False
    if pos.trail_armed:
        pos.trail_peak = max(pos.trail_peak, price)
        return False
    if price >= take_profit_price(pos, cfg):
        pos.trail_armed, pos.trail_peak = True, price
        return True
    return False


def decide(pos, price, cfg):
    if pos.step == 0:
        return Action("buy", "사이클 시작 (1단계)", amount_krw=step_amount(cfg, 0), trigger_price=price)

    tp = take_profit_price(pos, cfg)
    if cfg["trailing_enabled"]:
        ts = trailing_stop_price(pos, cfg)
        if ts is not None and price <= ts:
            gain = (price * (1 - cfg.get("fee_pct", 0) / 100) / pos.avg_price - 1) * 100
            return Action("sell", f"추적 익절 (고점 대비 -{cfg['trailing_pct']}%, 수익 약 {gain:+.1f}%)",
                          volume=pos.volume, trigger_price=ts)
    elif price >= tp:
        return Action("sell", f"익절 +{cfg['take_profit_pct']}% 도달", volume=pos.volume, trigger_price=tp)

    trig = next_buy_trigger(pos, cfg)
    if trig is not None and price <= trig and not pos.trail_armed:
        n = pos.step + 1
        return Action("buy", f"{drop_for_step(cfg, n)}% 하락 → {n}단계 추가 매수",
                      amount_krw=step_amount(cfg, pos.step), trigger_price=trig)

    sl = stop_loss_price(pos, cfg)
    if sl is not None and price <= sl:
        return Action("sell", f"손절 -{cfg['stop_loss_pct']}% 도달", volume=pos.volume, trigger_price=sl)

    return Action("hold")


def apply_buy(pos, krw_spent_with_fee, volume, price, when=""):
    if pos.step == 0:
        pos.cycle_started_at = when
    pos.step += 1
    pos.cost += krw_spent_with_fee
    pos.volume += volume
    pos.last_buy_price = price
    pos.buys.append({"step": pos.step, "price": price, "krw": round(krw_spent_with_fee), "volume": volume, "at": when})


def close_position(pos, krw_received_net):
    profit = krw_received_net - pos.cost
    result = {
        "steps": pos.step,
        "cost": round(pos.cost),
        "proceeds": round(krw_received_net),
        "profit": round(profit),
        "profit_pct": round(profit / pos.cost * 100, 2) if pos.cost else 0,
        "started_at": pos.cycle_started_at,
    }
    pos.__dict__.update(Position().__dict__)
    return result


def validate_coin(c):
    errs, name = [], c.get("coin", "?")
    if c["base_amount"] < MIN_ORDER_KRW:
        errs.append(f"[{name}] 1단계 매수금액은 최소 {MIN_ORDER_KRW:,}원입니다.")
    if c["martin_multiplier"] < 1:
        errs.append(f"[{name}] 마틴 배수는 1 이상이어야 합니다.")
    d = parse_drops(c["drop_steps"])
    if not d or any(not (0 < x < 100) for x in d):
        errs.append(f"[{name}] 단계별 하락폭은 0~100 사이 숫자를 쉼표로 적어주세요. 예: 5,5,7,10")
    if not (0 < c["take_profit_pct"] < 1000):
        errs.append(f"[{name}] 익절 수익률이 올바르지 않습니다.")
    if not (1 <= int(c["max_steps"]) <= 15):
        errs.append(f"[{name}] 최대 단계는 1~15 사이여야 합니다.")
    if c["coin_budget"] < 0:
        errs.append(f"[{name}] 코인 투입 한도는 0 이상이어야 합니다 (0 = 제한 없음).")
    elif 0 < c["coin_budget"] < c["base_amount"]:
        errs.append(f"[{name}] 코인 투입 한도가 1단계 매수금액보다 작습니다.")
    if c["limit_action"] not in ("hold", "pause", "sell"):
        errs.append(f"[{name}] 한도 도달 시 동작 값이 올바르지 않습니다.")
    if c["trailing_enabled"] and not (0 < c["trailing_pct"] < 50):
        errs.append(f"[{name}] 추적 익절 폭은 0~50% 사이여야 합니다.")
    return errs
