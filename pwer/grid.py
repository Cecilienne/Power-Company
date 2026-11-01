"""电网连通与供电覆盖。

刻意做的简化（设计文档"红线取舍"）：
- 不做潮流/短路/电压，只算三件事：连通性、线路容量、线损系数。
- 连通 = 4 邻接的"自有电网地块"（电厂 / 变电站 / 输电线路）。
- 城市可交付容量 = min(电源容量, 城市接口容量) * (1 - 线损)
  · 城市接口容量 = 城市半径内的变电站/线路容量之和
  · 因此必须"从电厂拉线到城市变电站"，这是电网建造的核心玩法。
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

from .constants import PLANT_SPECS
from .world import CITY_RADIUS, City, Tile, WorldMap, chebyshev

GRID_NODE_KINDS = ({"substation", "line"} | set(PLANT_SPECS.keys()))


def is_grid_node(t: Tile) -> bool:
    return t.kind in GRID_NODE_KINDS


def grid_tiles(world: WorldMap, owner: int) -> Iterable[Tile]:
    """自有电网地块（走占用索引，960×960 也不用扫全图）。"""
    for t in world.owned_tiles(owner):
        if is_grid_node(t):
            yield t


def components(world: WorldMap, owner: int) -> list[set[tuple[int, int]]]:
    """按 4 邻接把自有电网地块切成若干连通块。"""
    nodes = {(t.x, t.y) for t in grid_tiles(world, owner)}
    seen: set[tuple[int, int]] = set()
    comps: list[set[tuple[int, int]]] = []
    for start in nodes:
        if start in seen:
            continue
        comp: set[tuple[int, int]] = set()
        q = deque([start])
        seen.add(start)
        while q:
            x, y = q.popleft()
            comp.add((x, y))
            for nx, ny in world.neighbors4(x, y):
                n = (nx, ny)
                if n in nodes and n not in seen:
                    seen.add(n)
                    q.append(n)
        comps.append(comp)
    return comps


def _plant_cap_in(world: WorldMap, owner: int, comp: set[tuple[int, int]]) -> float:
    cap = 0.0
    for (x, y) in comp:
        t = world.tile(x, y)
        if t.kind in PLANT_SPECS:
            cap += PLANT_SPECS[t.kind].cap
    return cap


def _gateway_in(world: WorldMap, owner: int, city: City) -> float:
    gw = 0.0
    for t in world.owned_tiles(owner):
        if chebyshev(t.x, t.y, city.x, city.y) > CITY_RADIUS:
            continue
        if t.kind == "substation":
            gw += 180.0
        elif t.kind == "line":
            gw += 120.0
        elif t.kind in PLANT_SPECS:
            gw += 60.0   # 电厂直接接入城市，容量贡献较小
    return gw


def city_supply(world: WorldMap, owner: int, city: City,
                comps: list[set[tuple[int, int]]] | None = None) -> tuple[float, float]:
    """返回 (可交付 MW, 线损系数 0~1)。"""
    if comps is None:
        comps = components(world, owner)
    if not comps:
        return 0.0, 0.0
    # 城市半径内有变电站才算接入
    has_sub = any(
        t.kind == "substation" and chebyshev(t.x, t.y, city.x, city.y) <= CITY_RADIUS
        for t in world.owned_tiles(owner)
    )
    if not has_sub:
        return 0.0, 0.0

    best_cap = 0.0
    best_dist = 99
    for comp in comps:
        touching = [(x, y) for (x, y) in comp
                    if chebyshev(x, y, city.x, city.y) <= CITY_RADIUS]
        if not touching:
            continue
        cap = _plant_cap_in(world, owner, comp)
        if cap <= 0:
            continue
        # 简化：不做多区潮流，取容量最大的那一块电网供电
        if cap > best_cap:
            best_cap = cap
            best_dist = min(chebyshev(x, y, city.x, city.y) for (x, y) in touching)

    if best_cap <= 0:
        return 0.0, 0.0
    gateway = _gateway_in(world, owner, city)
    deliver = min(best_cap, gateway)
    loss = min(0.10, 0.012 * max(0, best_dist))
    return deliver * (1.0 - loss), loss


def all_city_supply(world: WorldMap, owner: int) -> dict[int, tuple[float, float]]:
    comps = components(world, owner)
    return {c.idx: city_supply(world, owner, c, comps) for c in world.cities}
