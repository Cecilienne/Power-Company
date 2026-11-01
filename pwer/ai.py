"""AI 对手：会建厂、会报价、会抄底、会反收购。

目标不是"最强 AI"，而是"能被读懂、又会惩罚失误的对手"：
- 你亏损、负债率高的时候，它会用低价抢你的客户，同时扫你的股票；
- 你开始举牌它，它会回购+提价（把收购成本顶上去）；
- 它也会自己扩张、也会债务紧张、也会缺人。
"""
from __future__ import annotations

from .company import Company
from .constants import (
    HIRE_COST,
    LOAN_LIMIT_RATIO,
    STAKE_ALERT,
)
from .grid import city_supply
from .ma import MaError, buy_shares, buyback, player_stake
from .world import CITY_RADIUS, chebyshev


def _own_tiles(game, co: Company):
    return list(game.world.owned_tiles(co.idx))


def _has_sub_near(game, co: Company, city) -> bool:
    return any(t.kind == "substation" and chebyshev(t.x, t.y, city.x, city.y) <= CITY_RADIUS
               for t in _own_tiles(game, co))


def _adjacent_candidates(game, co: Company, kind: str):
    out = []
    for t in _own_tiles(game, co):
        for nx, ny in game.world.neighbors4(t.x, t.y):
            ok, _ = game.world.check_build(kind, nx, ny)
            if ok:
                out.append((nx, ny))
    return list(dict.fromkeys(out))


def _near_city_candidates(game, co: Company, kind: str, city):
    out = []
    for y in range(city.y - CITY_RADIUS, city.y + CITY_RADIUS + 1):
        for x in range(city.x - CITY_RADIUS, city.x + CITY_RADIUS + 1):
            ok, _ = game.world.check_build(kind, x, y)
            if ok:
                out.append((x, y))
    return out


def _pick_kind(game, co: Company) -> list[str]:
    phase = game.turn
    if game.policy.nuclear_approved and phase >= 12 and co.cash > 600_000:
        return ["nuclear", "coal"]
    if phase < 8:
        return ["coal", "gas"] if co.cash > 120_000 else ["gas", "coal"]
    if phase < 14:
        return ["wind", "solar", "gas"]
    return ["solar", "wind", "nuclear"]


def ai_turn(game, co: Company) -> None:
    _ai_finance(game, co)
    _ai_pricing(game, co)
    _ai_build(game, co)
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
    for city in game.world.cities:
        cap = city_supply(game.world, co.idx, city)[0]
        srv = co.city_served.get(city.idx, (0.0, 0.0, 0.0))
        idle += max(0.0, cap - (srv[0] + srv[1]) * 1.35)
    if idle > 80 and game.turn % 3 == 0:
        base *= 0.93
    co.res_price = round(base * 1.02, 1)
    co.ind_price = round(base * 0.95, 1)


# ---------------------------------------------------------------- 建造
def _ensure_cash(game, co: Company, need: float) -> bool:
    """钱不够就借，借不到就放弃这次投资。"""
    if co.cash >= need:
        return True
    limit = co.assets() * LOAN_LIMIT_RATIO - co.debt
    gap = need - co.cash
    if limit >= gap and gap > 0:
        co.debt += gap
        co.cash += gap
        return True
    return False


def _ai_build(game, co: Company) -> None:
    if len(co.projects) >= 2:
        return

    # 找到"需求大、我方覆盖最小"的城市
    best_city, best_gap = None, 0.0
    for city in game.world.cities:
        cap = city_supply(game.world, co.idx, city)[0]
        gap = city.demand_mw * 0.75 - cap
        if gap > best_gap:
            best_city, best_gap = city, gap
    if best_city is None:
        return

    kinds = _pick_kind(game, co)

    # 1) 该城市没有变电站 → 先建变电站
    if not _has_sub_near(game, co, best_city):
        for (x, y) in _near_city_candidates(game, co, "substation", best_city):
            if not game.can_build(co, "substation", x, y)[0]:
                continue
            if not _ensure_cash(game, co, game.total_build_cost(co, "substation", x, y) * 1.3):
                continue
            if game.build(co, "substation", x, y):
                return
        return

    # 2) 变电站旁边还没有电厂 → 贴着"该城市的变电站"建电厂，保证连通
    subs_near = [t for t in _own_tiles(game, co)
                 if t.kind == "substation" and chebyshev(t.x, t.y, best_city.x, best_city.y) <= CITY_RADIUS]
    cands_all: list[tuple[str, int, int]] = []
    for kind in kinds:
        spots = []
        for st in subs_near:
            for nx, ny in game.world.neighbors4(st.x, st.y):
                if game.world.check_build(kind, nx, ny)[0]:
                    spots.append((nx, ny))
        if not spots:
            continue
        spots = list(dict.fromkeys(spots))
        if kind in ("wind", "solar"):
            key = (lambda p: game.world.tile(*p).wind) if kind == "wind" else (lambda p: game.world.tile(*p).solar)
            spots.sort(key=key, reverse=True)
        cands_all.extend([(kind, x, y) for (x, y) in spots])
        break
    for kind, x, y in cands_all:
        if not _ensure_cash(game, co, game.total_build_cost(co, kind, x, y) * 1.2):
            continue
        if game.build(co, kind, x, y):
            return

    # 3) 容量够但接口（线路）不够 → 在城市半径内加线路扩容
    if co.cash < 6_000:
        return
    for (x, y) in _near_city_candidates(game, co, "line", best_city):
        if game.build(co, "line", x, y):
            return


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
