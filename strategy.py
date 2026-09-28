"""마틴게일 물타기 전략 (순수 계산 로직 — 거래소와 무관)

규칙
 1) 포지션이 없으면 기본금액(base_amount)으로 1단계 매수
 2) 기준가(평단가 또는 직전 매수가)보다 drop_pct% 이상 떨어지면
    이전 매수금액 x martin_multiplier 로 추가 매수 (max_steps 까지)
 3) 현재가가 평단가(수수료 포함) 대비 take_profit_pct% 이상이면 전량 매도 → 1사이클 종료
 4) (선택) 최대 단계 도달 후 평단가 대비 stop_loss_pct% 이상 떨어지면 전량 손절
 5) auto_restart 가 켜져 있으면 다음 사이클을 기본금액으로 다시 시작
"""
from dataclasses import dataclass, field, asdict

DEFAULT_CONFIG = {
    "exchange": "upbit",          # upbit | bithumb
    "coin": "BTC",                # BTC | ETH | XRP | SOL ...
    "mode": "paper",              # paper(모의투자) | live(실전)
    "access_key": "",
    "secret_key": "",
    "base_amount": 10000,         # 1단계 매수 금액(원)
    "martin_multiplier": 2.0,     # 추가 매수 배수
    "drop_pct": 5.0,              # 추가 매수 하락폭(%)
    "drop_basis": "avg",          # avg(평단가 기준) | last(직전 매수가 기준)
    "max_steps": 6,               # 최대 매수 단계
    "take_profit_pct": 10.0,      # 익절 수익률(%) — 평단가(수수료 포함) 기준
    "stop_loss_enabled": False,   # 최대 단계 이후 손절 사용
    "stop_loss_pct": 15.0,        # 손절 기준(%) — 평단가 기준
    "auto_restart": True,         # 익절/손절 후 자동으로 새 사이클 시작
    "check_interval_sec": 10,     # 시세 확인 주기(초)
    "fee_pct": 0.05,              # 모의투자/백테스트용 수수료(%)
    "telegram_token": "",
    "telegram_chat_id": "",
}

MIN_ORDER_KRW = 5000


def step_amount(cfg, step_index):
    """step_index: 0부터 시작 (0 = 1단계)"""
    return int(round(cfg["base_amount"] * (cfg["martin_multiplier"] ** step_index)))


def ladder(cfg):
    """단계별 (매수액, 누적액) 표"""
    rows, total = [], 0
    for i in range(int(cfg["max_steps"])):
        a = step_amount(cfg, i)
        total += a
        rows.append({"step": i + 1, "amount": a, "cumulative": total})
    return rows


@dataclass
class Position:
    step: int = 0                 # 현재 몇 단계까지 매수했는지 (0 = 포지션 없음)
    cost: float = 0.0             # 총 투입 원화 (수수료 포함)
    volume: float = 0.0           # 보유 수량
    last_buy_price: float = 0.0
    cycle_started_at: str = ""
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
    kind: str            # "buy" | "sell" | "hold"
    reason: str = ""
    amount_krw: float = 0.0   # buy 일 때 원화 금액
    volume: float = 0.0       # sell 일 때 수량
    trigger_price: float = 0.0


def next_buy_trigger(pos, cfg):
    """다음 추가 매수가 발동되는 가격 (없으면 None)"""
    if pos.step == 0 or pos.step >= int(cfg["max_steps"]):
        return None
    ref = pos.avg_price if cfg["drop_basis"] == "avg" else pos.last_buy_price
    return ref * (1 - cfg["drop_pct"] / 100)


def take_profit_price(pos, cfg):
    if pos.step == 0:
        return None
    # 매도 수수료까지 빼고도 목표 수익률이 남도록 보정
    return pos.avg_price * (1 + cfg["take_profit_pct"] / 100) / (1 - cfg.get("fee_pct", 0) / 100)


def stop_loss_price(pos, cfg):
    if pos.step == 0 or not cfg["stop_loss_enabled"] or pos.step < int(cfg["max_steps"]):
        return None
    return pos.avg_price * (1 - cfg["stop_loss_pct"] / 100)


def decide(pos, price, cfg, can_start_new=True):
    """현재가로 다음 행동 결정"""
    if pos.step == 0:
        if not can_start_new:
            return Action("hold", "자동 재시작 꺼짐 — 대기")
        return Action("buy", "사이클 시작 (1단계)", amount_krw=step_amount(cfg, 0), trigger_price=price)

    tp = take_profit_price(pos, cfg)
    if price >= tp:
        return Action("sell", f"익절 +{cfg['take_profit_pct']}% 도달", volume=pos.volume, trigger_price=tp)

    trig = next_buy_trigger(pos, cfg)
    if trig is not None and price <= trig:
        n = pos.step + 1
        return Action("buy", f"{cfg['drop_pct']}% 하락 → {n}단계 추가 매수",
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
    """전량 매도 후 실현손익 반환, 포지션 초기화"""
    profit = krw_received_net - pos.cost
    result = {
        "steps": pos.step,
        "cost": round(pos.cost),
        "proceeds": round(krw_received_net),
        "profit": round(profit),
        "profit_pct": round(profit / pos.cost * 100, 2) if pos.cost else 0,
        "started_at": pos.cycle_started_at,
    }
    fresh = Position()
    pos.__dict__.update(fresh.__dict__)
    return result
