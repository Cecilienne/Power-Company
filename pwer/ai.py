"""AI 对手：会报价、会抄底、会反收购。

目标不是"最强 AI"，而是"能被读懂、又会惩罚失误的对手"：
- 你亏损、负债率高的时候，它会用低价抢你的客户，同时扫你的股票；
- 你开始举牌它，它会回购+提价（把收购成本顶上去）；
- 它也会抢客户、也会债务紧张、也会缺人；建造只发生在自己的离屏城市，不在玩家的地图上占地。
"""
from __future__ import annotations

from .company import Company
from .constants import (
    HIRE_COST,
    LOAN_LIMIT_RATIO,
    STAKE_ALERT,
)
from .ma import MaError, buy_shares, buyback, player_stake


def ai_turn(game, co: Company) -> None:
    _ai_finance(game, co)
    _ai_pricing(game, co)
    _ai_defense(game, co)
    _ai_stock(game, co)


# ---------------------------------------------------------------- 财务与人力
def _ai_finance(game, co: Company) -> None:
    # 缺人 → 招人（AI 不会永远省这笔钱）
    need = co.staff_need()
    if co.ops_staff < need and co.cash > HIRE_COST * (need - co.ops_staff) * 3:
        n = need - co.ops_staff
        co.cash -= n * HIRE_COST
        co.ops_staff += n
    if len(co.projects) > co.eng_teams and co.cash > 40_000:
        n = len(co.projects) - co.eng_teams
        co.cash -= n * HIRE_COST
        co.eng_teams += n

    need_cash = (co.fixed_cost_q() + co.labor_cost_q() + co.debt * 0.018
                 + sum(p.per_turn for p in co.projects))
    if co.cash < need_cash * 1.5:
        limit = co.assets() * LOAN_LIMIT_RATIO - co.debt
        want = min(max(need_cash * 3, 5_000.0), max(0.0, limit))
        if want > 500:
            co.debt += want
            co.cash += want
            game.log(f"{co.name} 向银行借款 {want:,.0f}万元补充流动性")


# ---------------------------------------------------------------- 定价
def _ai_pricing(game, co: Company) -> None:
    st = co.last_income
    mwh = max(1.0, st.served_mwh)
    unit = st.total_cost * 10_000.0 / mwh
    var = st.fuel * 10_000.0 / mwh if st.fuel > 0 else 120.0
    if unit <= 0 or mwh < 1000:
        unit, var = 460.0, 200.0
    player = game.player
    weak = player.debt_ratio() > 0.55 or (player.profit_history and player.profit_history[-1] < 0)
    base = unit * (1.22 if weak else 1.12)
    base = max(base, var * 1.35)
    if co.ai_alerted:
        base *= 1.05
    base *= 1.0 + game.rng.uniform(-0.03, 0.03)
    base = min(base, game.policy.price_cap * 0.96)
    # 抢份额：如果自家有明显闲置产能，就小幅低于市场均价
    idle = 0.0
    for city in game.cities:
        cap = game.city_capacity(co, city.idx)
        srv = co.city_served.get(city.idx, (0.0, 0.0, 0.0))
        idle += max(0.0, cap - (srv[0] + srv[1]) * 1.35)
    if idle > 80 and game.turn % 3 == 0:
        base *= 0.93
    co.res_price = round(base * 1.02, 1)
    co.ind_price = round(base * 0.95, 1)


# ---------------------------------------------------------------- 反收购
def _ai_defense(game, co: Company) -> None:
    st = player_stake(game, co)
    if st < STAKE_ALERT:
        return
    if co.cash > 20_000:
        try:
            buyback(game, co, co.shares * 0.012)
        except MaError:
            pass
    co.payout = min(0.6, co.payout + 0.05)
    if st > 0.25:
        co.cash = max(0.0, co.cash - 200.0)
        game.log(f"☠ {co.name} 启动毒丸防御：提高分红、四处游说白衣骑士")


# ---------------------------------------------------------------- 股票
def _ai_stock(game, co: Company) -> None:
    player = game.player
    if (player.price < player.fair_value * 0.85 and co.cash > 30_000
            and player_stake(game, player) < 0.35 and player.debt_ratio() > 0.4):
        qty = player.shares * 0.015
        try:
            buy_shares(game, co.idx, player, qty)
            game.log(f"🚨 {co.name} 正在市场吸纳 {player.name} 的股票！")
        except MaError:
            pass
    if co.price < co.fair_value * 0.78 and co.cash > 15_000:
        try:
            buyback(game, co, co.shares * 0.008)
        except MaError:
            pass
