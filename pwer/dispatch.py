"""需求分配、机组调度与经营结算（利润表的前半段）。

顺序：
    1 城市可交付容量（电网连通 + 接口容量）
    2 客户按"电价 + 可靠性"分流，但每家能拿到的份额**不超过它实际能送出的容量**
      （超出部分顺延给别家 → 想抢份额就必须先建产能）
    3 机组按边际成本排序调度；自有矿先供料；不足部分外购电（现货市场有深度上限）
    4 现货也补不上的部分 → 限电停电：丢收入 + 罚款 + 掉声望
这里刻意不做：潮流、机组组合优化、逐小时仿真。一个季度一个平均工况。
"""
from __future__ import annotations

from statistics import mean

from .company import Company, IncomeStatement
from .constants import (
    HOURS_PER_TURN,
    OUTAGE_FINE_MULT,
    OUTAGE_REP,
    PEAK_RATIO,
    PRICE_ELASTICITY,
    RES_RATIO,
    RES_SENSITIVITY,
    SEASON_DEMAND,
    SEASON_RAIN,
    SEASON_SOLAR,
    SEASON_WIND,
    SPOT_IMPORT_PRICE,
    STAFF_SHORT_CF,
)
from .grid import all_city_supply

SPOT_DEPTH = 0.25          # 现货市场最多补足 25% 的缺口，再多就只能限电


def season_index(turn: int) -> int:
    return (turn - 1) % 4


def plant_cf(game, co: Company, plant) -> float:
    """本季度该机组的容量因子（已含季节、资源、缺人惩罚）。"""
    spec = plant.spec
    season = season_index(game.turn)
    w = game.world_for_city(plant.city) if plant.city is not None else game.world
    tile = w.tile(plant.x, plant.y) if (tile_ok := (w is not None and plant.x >= 0 and plant.y >= 0)) else None
    f = spec.base_cf
    if plant.kind == "wind":
        f *= (tile.wind if tile is not None else 1.0) * SEASON_WIND[season] * game.weather.get("wind", 1.0)
    elif plant.kind == "solar":
        f *= (tile.solar if tile is not None else 1.0) * SEASON_SOLAR[season] * game.weather.get("solar", 1.0)
    elif plant.kind == "hydro":
        f *= SEASON_RAIN[season] * game.weather.get("rain", 1.0)
    elif plant.kind in ("coal", "gas", "nuclear"):
        f *= max(0.85, 1.0 - 0.04 * max(0.0, game.weather.get("heat", 1.0) - 1.0))
    sr = co.staff_ratio()
    if sr < 1.0:
        f *= 1.0 - (1.0 - sr) * (1.0 - STAFF_SHORT_CF)
    if not plant.online:
        return 0.0
    return max(0.0, min(1.0, f))


def _score(co: Company, price: float, ref_price: float, elasticity: float,
           cap: float, demand: float) -> float:
    rel = 0.45 + 0.55 * (co.reputation / 100.0)
    price_term = (ref_price / max(price, 1.0)) ** elasticity
    adequacy = min(1.0, cap / max(1.0, demand))
    return rel * price_term * (0.65 + 0.35 * adequacy)


def _clamped_split(active: list[Company], weights: dict[int, tuple[float, float]],
                   demand: float, seg: int, caps: dict[int, float],
                   alloc_res: dict[int, float], alloc_ind: dict[int, float]) -> None:
    """按权重分流，并受各家可交付容量约束；装不下的需求顺延给其他公司。"""
    pool = list(active)
    remaining = demand
    for _ in range(6):
        if remaining <= 0.5 or not pool:
            return
        tot_w = sum(weights[c.idx][seg] for c in pool)
        if tot_w <= 0:
            return
        overflow = 0.0
        for c in pool:
            give = remaining * weights[c.idx][seg] / tot_w
            used = alloc_res[c.idx] + alloc_ind[c.idx]
            room = max(0.0, caps[c.idx] / PEAK_RATIO - used)
            add = min(give, room)
            if seg == 0:
                alloc_res[c.idx] += add
            else:
                alloc_ind[c.idx] += add
            overflow += give - add
        remaining = overflow
        pool = [c for c in pool
                if alloc_res[c.idx] + alloc_ind[c.idx] < caps[c.idx] / PEAK_RATIO - 0.5]


def settle_production(game) -> None:
    season = season_index(game.turn)
    dmul = SEASON_DEMAND[season]
    cos = [c for c in game.companies if c.alive and not c.delisted]

    # 0. 需求随季节与增长波动（含 AI 离屏城市）
    for city in game.cities:
        city.demand_mw = city.base_mw * dmul * game.demand_shock
        city.refresh_split(RES_RATIO)

    # 1. 各自的电网可交付容量：真实地图 + 离屏机组
    caps: dict[int, dict[int, float]] = {
        co.idx: {city.idx: 0.0 for city in game.cities} for co in cos
    }
    for co in cos:
        for cid in (0, 1):
            w = game.world_for_city(cid)
            if w is None:
                continue
            supply = all_city_supply(w, co.idx)
            caps[co.idx][cid] = supply.get(cid, (0.0, 0.0))[0]
        for p in co.plants:
            if p.online and p.x < 0 and p.y < 0:
                caps[co.idx][p.city] = (caps[co.idx].get(p.city, 0.0)
                                        + p.cap * plant_cf(game, co, p))

    # 跨城线：只替玩家做自动潮流，盈余城送往缺口城
    player = game.player
    pcaps = caps.get(player.idx, {})
    for ln in game.links:
        ca, cb = pcaps.get(ln.a, 0.0), pcaps.get(ln.b, 0.0)
        da = game.city_state(ln.a).demand_mw if game.city_state(ln.a) else 0.0
        db = game.city_state(ln.b).demand_mw if game.city_state(ln.b) else 0.0
        sa, sb = ca - da, cb - db
        if sa > 0 and sb < 0:
            flow = min(ln.capacity, sa, -sb / max(1e-6, 1.0 - ln.loss))
            pcaps[ln.a] = ca - flow
            pcaps[ln.b] = cb + flow * (1.0 - ln.loss)
        elif sb > 0 and sa < 0:
            flow = min(ln.capacity, sb, -sa / max(1e-6, 1.0 - ln.loss))
            pcaps[ln.b] = cb - flow
            pcaps[ln.a] = ca + flow * (1.0 - ln.loss)

    for co in cos:
        co.grid_capacity = sum(caps[co.idx].values())
    game.city_cap = caps

    for co in cos:
        co.city_served = {}
        co.served_mw = 0.0
        co.last_income = IncomeStatement()

    # 2. 客户分流
    for city in game.cities:
        active = [c for c in cos if caps[c.idx].get(city.idx, 0.0) > 1.0]
        if not active:
            city.served = {}
            city.share = {}
            city.outage_mw = city.demand_mw
            continue
        caps_city = {c.idx: caps[c.idx][city.idx] for c in active}
        avg_res = mean(c.res_price for c in active) or 1.0
        avg_ind = mean(c.ind_price for c in active) or 1.0
        weights = {
            c.idx: (
                _score(c, c.res_price, avg_res, PRICE_ELASTICITY * RES_SENSITIVITY,
                       caps_city[c.idx], city.residential),
                _score(c, c.ind_price, avg_ind, PRICE_ELASTICITY,
                       caps_city[c.idx], city.industrial),
            )
            for c in active
        }
        alloc_res = {c.idx: 0.0 for c in active}
        alloc_ind = {c.idx: 0.0 for c in active}
        _clamped_split(active, weights, city.residential, 0, caps_city, alloc_res, alloc_ind)
        _clamped_split(active, weights, city.industrial, 1, caps_city, alloc_res, alloc_ind)

        served: dict[int, float] = {}
        for c in active:
            a_res = alloc_res[c.idx]
            a_ind = alloc_ind[c.idx]
            st = c.last_income
            st.revenue_res += a_res * HOURS_PER_TURN * c.res_price / 10_000.0
            st.revenue_ind += a_ind * HOURS_PER_TURN * c.ind_price / 10_000.0
            st.served_mwh += (a_res + a_ind) * HOURS_PER_TURN
            c.city_served[city.idx] = (a_res, a_ind, 0.0)
            served[c.idx] = a_res + a_ind
            c.served_mw += a_res + a_ind

        city.served = served
        tot_served = sum(served.values())
        city.share = {cid: v / max(1.0, city.demand_mw) for cid, v in served.items()}
        city.outage_mw = max(0.0, city.demand_mw - tot_served)

    # 3. 每家公司按边际成本调度自有电厂
    for co in cos:
        _dispatch(game, co)


def _dispatch(game, co: Company) -> None:
    st = co.last_income
    need_mwh = st.served_mwh
    if need_mwh <= 0:
        st.spot = 0.0
    plants = sorted([p for p in co.plants if p.online], key=lambda p: p.spec.var_cost)
    remaining = need_mwh
    for p in plants:
        cf = plant_cf(game, co, p)
        avail = p.cap * cf * HOURS_PER_TURN
        p.cf_actual = cf
        p.available_mwh = avail
        take = min(avail, remaining)
        p.dispatched_mwh = max(0.0, take)
        remaining -= take
        if remaining <= 0:
            remaining = 0.0
    for p in co.plants:
        if not p.online:
            p.available_mwh = 0.0
            p.dispatched_mwh = 0.0

    own_gen = need_mwh - remaining
    st.generated_mwh = own_gen

    # 现货补缺：市场深度有限，补不上的就是限电
    shortfall = max(0.0, remaining)
    spot = min(shortfall, need_mwh * SPOT_DEPTH)
    unserved = shortfall - spot
    st.spot = spot * SPOT_IMPORT_PRICE / 10_000.0
    if unserved > 0.5:
        ratio = unserved / max(1.0, need_mwh)
        avg_price = (st.revenue_res + st.revenue_ind) * 10_000.0 / need_mwh
        # 少发的电 = 少卖的钱
        st.revenue_res *= 1.0 - ratio
        st.revenue_ind *= 1.0 - ratio
        st.served_mwh *= 1.0 - ratio
        st.outage_mwh += unserved
        st.fines += unserved * avg_price * OUTAGE_FINE_MULT / 10_000.0
        co.reputation = max(0.0, co.reputation - OUTAGE_REP * min(1.0, ratio * 4))
        if co.is_player:
            game.log(f"⚡ 供电缺口 {ratio:.1%}，被迫限电，罚款与收入损失已计入财报")

    # 燃料：自有矿先供，缺口按市场价外购
    fuel_cost = 0.0
    for fuel in ("coal", "uranium"):
        demand_t = sum(p.dispatched_mwh * p.spec.fuel_rate
                       for p in co.plants if p.spec.fuel == fuel)
        if demand_t <= 0:
            continue
        mine_kind = "coal_mine" if fuel == "coal" else "uranium_mine"
        price = game.market["coal"] if fuel == "coal" else game.market["uranium"]
        left = demand_t
        for m in co.mines:
            if m.kind != mine_kind or left <= 0:
                continue
            give = min(m.output, m.reserve, left)
            m.reserve -= give
            m.produced_last = give
            left -= give
            fuel_cost += give * m.cost_per_t / 10_000.0
        fuel_cost += left * price / 10_000.0
    st.fuel = fuel_cost

    st.emissions = sum(p.dispatched_mwh * p.spec.co2 for p in co.plants)
    st.renewable_mwh = sum(p.dispatched_mwh for p in co.plants if p.spec.renewable)
    # 注意：fixed 只含运维，折旧在 engine.settle_finance 里单独计入，避免重复
    st.fixed = (sum(p.oam_q for p in co.plants)
                + sum(max(0.0, m.fixed_q - m.dep_q) for m in co.mines))
    st.labor = co.labor_cost_q()
    st.grid = co.grid_maintenance_q()
    st.carbon = st.emissions * game.policy.carbon_tax / 10_000.0
