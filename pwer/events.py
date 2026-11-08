"""随机事件：外部冲击，也是给 AI 偷袭你的窗口期。

每个事件只改"全局系数"或"某家公司的一个资产"，不做复杂连锁，保证可读可预期。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from .constants import money


@dataclass
class EventDef:
    key: str
    name: str
    weight: float
    min_turn: int
    apply: Callable[["object"], str]


def _pick_company(game):
    live = [c for c in game.companies if c.alive and not c.delisted]
    return game.rng.choice(live)


def _ev_coal_spike(game) -> str:
    game.commodity_mod["coal"] = max(game.commodity_mod["coal"], 1.38)
    return "国际煤价暴涨 38%，火电燃料成本大幅上升"


def _ev_coal_crash(game) -> str:
    game.commodity_mod["coal"] = min(game.commodity_mod["coal"], 0.72)
    return "煤炭产能过剩，煤价下跌 28%，火电毛利改善"


def _ev_uranium_spike(game) -> str:
    game.commodity_mod["uranium"] = max(game.commodity_mod["uranium"], 1.45)
    return "铀矿供应紧张，铀价暴涨 45%"


def _ev_drought(game) -> str:
    game.weather["rain"] = 0.55
    return "流域来水偏枯，水电出力大幅下降"


def _ev_heatwave(game) -> str:
    game.weather["heat"] = 1.18
    game.demand_shock *= 1.06
    return "持续高温：负荷上涨 6%，火电核电冷却效率下降"


def _ev_coldsnap(game) -> str:
    game.weather["heat"] = 0.92
    game.demand_shock *= 1.08
    return "寒潮来袭：采暖负荷上涨 8%"


def _ev_storm(game) -> str:
    co = _pick_company(game)
    online = [p for p in co.plants if p.online]
    if not online:
        return "风暴过境，但当地没有可受损的机组"
    p = game.rng.choice(online)
    p.online = False
    p.outage_days = game.rng.randint(30, 60)
    co.reputation = max(0.0, co.reputation - 4.0)
    game.demand_shock *= 1.02
    return f"风暴摧毁 {co.name} 的一处线路，{p.name} 停机 {p.outage_days} 天"


def _ev_strike(game) -> str:
    co = _pick_company(game)
    co.reputation = max(0.0, co.reputation - 5.0)
    lost = min(co.ops_staff, 4)
    co.ops_staff -= lost
    return f"{co.name} 爆发罢工，{lost} 个班组停工，出力与可靠性下降"


def _ev_mine_accident(game) -> str:
    mines = [m for c in game.companies for m in c.mines]
    if not mines:
        return "矿井安全检查，但全市场没有在产矿山"
    m = game.rng.choice(mines)
    m.reserve *= 0.72
    owner = game.companies[m.owner]
    owner.reputation = max(0.0, owner.reputation - 4.0)
    owner.last_income.fines += 1_800.0
    return f"{owner.name} 的{m.name}发生事故，储量减少 28% 并罚款 1800万"


def _ev_nuclear_approved(game) -> str:
    game.policy.nuclear_approved = True
    return "国家放开核电核准，你现在可以投资核电站（需临水且远离城市）"


def _ev_subsidy(game) -> str:
    got = []
    for co in game.companies:
        if co.alive and not co.delisted and co.renewable_share() > 0.25:
            grant = 2_500.0
            co.cash += grant
            got.append(co.name)
    if not got:
        return "新能源补贴开始申报，但在场公司新能源占比都太低，无人受益"
    return "新能源补贴到账： " + "、".join(f"{n}+2500万" for n in got)


def _ev_recession(game) -> str:
    game.demand_shock *= 0.93
    return "经济下行：全社会用电需求下降 7%，电价承压"


def _ev_boom(game) -> str:
    game.demand_shock *= 1.07
    return "制造业扩张：用电需求上涨 7%"


def _ev_env_fine(game) -> str:
    co = _pick_company(game)
    fine = 3_000.0
    co.last_income.fines += fine
    co.cash -= fine
    co.reputation = max(0.0, co.reputation - 5.0)
    return f"{co.name} 因超标排放被处罚 {money(fine)}，并登上环保黑名单"


def _ev_tech(game) -> str:
    game.capex_mod["wind"] = min(game.capex_mod.get("wind", 1.0), 0.82)
    game.capex_mod["solar"] = min(game.capex_mod.get("solar", 1.0), 0.82)
    return "厂商降价：风电与光伏单位造价永久下降 18%"


EVENTS: tuple[EventDef, ...] = (
    EventDef("coal_spike", "煤价暴涨", 10, 1, _ev_coal_spike),
    EventDef("coal_crash", "煤价下跌", 8, 1, _ev_coal_crash),
    EventDef("uranium_spike", "铀价暴涨", 5, 6, _ev_uranium_spike),
    EventDef("drought", "流域来水偏枯", 8, 1, _ev_drought),
    EventDef("heatwave", "极端高温", 9, 1, _ev_heatwave),
    EventDef("coldsnap", "寒潮", 8, 1, _ev_coldsnap),
    EventDef("storm", "风暴袭击", 10, 2, _ev_storm),
    EventDef("strike", "工人罢工", 7, 3, _ev_strike),
    EventDef("mine_accident", "矿难", 5, 4, _ev_mine_accident),
    EventDef("nuclear_approved", "核电核准放开", 3, 8, _ev_nuclear_approved),
    EventDef("subsidy", "新能源补贴", 7, 5, _ev_subsidy),
    EventDef("recession", "经济下行", 7, 6, _ev_recession),
    EventDef("boom", "需求爆发", 7, 3, _ev_boom),
    EventDef("env_fine", "环保处罚", 7, 4, _ev_env_fine),
    EventDef("tech", "新能源降价", 5, 6, _ev_tech),
)


EVENT_BY_KEY = {e.key: e for e in EVENTS}

# 触发时会自动暂停时钟的负面事件
MAJOR_NEGATIVE_KEYS = frozenset({
    "coal_spike", "uranium_spike", "drought", "storm", "strike",
    "mine_accident", "recession", "env_fine",
})


def roll_event(game) -> EventDef | None:
    turn = game.turn
    chance = 0.38 + min(0.22, turn * 0.006)
    if game.rng.random() > chance:
        return None
    pool = [e for e in EVENTS if turn >= e.min_turn]
    weights = [e.weight for e in pool]
    return game.rng.choices(pool, weights=weights, k=1)[0]
