"""政策与监管：碳税、新能源配额、电价上限、核电审批、反垄断。

作用（设计文档 §三.6）：防止"一套火电玩到底"，把前期并购发动机变成中后期税负，
迫使玩家在某个时点做战略转型。所有参数来自 constants.POLICY_SCHEDULE。
"""
from __future__ import annotations

from dataclasses import dataclass

from .company import Company
from .constants import (
    ANTITRUST_FINE,
    ANTITRUST_PRICE_RATIO,
    ANTITRUST_SHARE,
    ANTITRUST_TURNS,
    POLICY_SCHEDULE,
    QUOTA_FINE_CAP,
    QUOTA_PENALTY,
    QUOTA_REP,
)


@dataclass
class PolicyState:
    turn: int = 1
    carbon_tax: float = 0.0
    renewable_quota: float = 0.0
    price_cap: float = 1200.0
    approval_fee: float = 0.0
    nuclear_approved: bool = False
    antitrust: dict[int, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.antitrust is None:
            self.antitrust = {}

    def stage(self) -> str:
        if self.carbon_tax >= 240:
            return "深度脱碳期"
        if self.carbon_tax >= 90:
            return "转型期"
        if self.carbon_tax > 0:
            return "过渡期"
        return "扩张期"

    def summary(self) -> str:
        return (f"碳税 {self.carbon_tax:.0f}元/t · 新能源配额 {self.renewable_quota:.0%} · "
                f"电价上限 {self.price_cap:.0f}元/MWh")


def policy_for_turn(turn: int) -> PolicyState:
    step = POLICY_SCHEDULE[0]
    for s in POLICY_SCHEDULE:
        if turn >= s.turn:
            step = s
    st = PolicyState(
        turn=turn,
        carbon_tax=step.carbon_tax,
        renewable_quota=step.renewable_quota,
        price_cap=step.price_cap,
        approval_fee=step.approval_fee,
    )
    return st


def apply_quota_penalty(game, co: Company) -> float:
    quota = game.policy.renewable_quota
    if quota <= 0 or co.last_income.generated_mwh <= 0:
        return 0.0
    short = quota - co.renewable_share()
    if short <= 0.001:
        return 0.0
    fine = short * 100.0 * QUOTA_PENALTY
    fine = min(fine, co.last_income.revenue * QUOTA_FINE_CAP)
    co.last_income.fines += fine
    co.reputation = max(0.0, co.reputation - QUOTA_REP * min(1.0, short * 4))
    if co.is_player:
        game.log(f"⚠ 新能源配额未达标（差 {short:.1%}），罚款 {fine:,.0f}万元，声望下降")
    return fine


def check_antitrust(game) -> list[str]:
    """低于全成本定价 + 市占率超红线 → 连续 3 回合触发反垄断处罚。"""
    msgs: list[str] = []
    for co in game.companies:
        if not co.alive or co.delisted:
            continue
        st = co.last_income
        mwh = max(1.0, st.served_mwh)
        unit_cost = st.total_cost * 10_000.0 / mwh
        avg_price = ((st.revenue_res + st.revenue_ind) * 10_000.0) / mwh
        share = getattr(co, "market_share", 0.0)
        bad = share > ANTITRUST_SHARE and avg_price < unit_cost * ANTITRUST_PRICE_RATIO
        if bad:
            game.policy.antitrust[co.idx] = game.policy.antitrust.get(co.idx, 0) + 1
            n = game.policy.antitrust[co.idx]
            if co.is_player and n < ANTITRUST_TURNS:
                msgs.append(f"⚠ 反垄断预警：你以低于成本价抢占 {share:.0%} 市场份额（{n}/{ANTITRUST_TURNS}）")
            if n >= ANTITRUST_TURNS:
                st.fines += ANTITRUST_FINE
                co.reputation = max(0.0, co.reputation - 8.0)
                co.banned_acq_days = 180
                game.policy.antitrust[co.idx] = 0
                msgs.append(f"⛔ {co.name} 因掠夺定价被反垄断处罚：罚款 {ANTITRUST_FINE:,.0f}万元，"
                            f"180 天内禁止收购")
        else:
            game.policy.antitrust[co.idx] = 0
    return msgs
