"""地图、地形、地块与建造权限。

设计意图（对应设计文档"地形与地块系统"）：
格子只承载 4 个属性 —— 地形类型、水系标记、风光系数、矿产资源。
它们只影响三件事：能不能建、建造成本乘数、出力效率。不做潮流/流体模拟。
"限制即乐趣"：没有河流不能造水电，荒漠建不了大型火电，城市附近不能上核电。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .constants import (
    INFRA_SPECS,
    LAND_COST,
    MINE_RESERVE,
    PLANT_SPECS,
    TERRAIN_INFO,
    Terrain,
)

GRID_W = 18
GRID_H = 18            # 方形棋盘：18×18 格 = 1080×1080 像素（每格 60）
MAP_PX = 1080          # 地图在设计稿里的边长（像素，1x）
TILE_PX = MAP_PX // GRID_W   # 60：格子铺满 1080 时的边长
CITY_RADIUS = 2          # 城市供电接入半径（切比雪夫距离）
NUCLEAR_CITY_GAP = 2     # 核电站与城市的最小距离


@dataclass
class Tile:
    x: int
    y: int
    terrain: Terrain
    wind: float = 1.0
    solar: float = 1.0
    coal: float = 0.0        # 剩余可采储量（吨）
    uranium: float = 0.0
    owner: int = -1          # 占用地块的公司下标
    kind: str = ""           # 占用建筑类型键（电源 key / 设施 key）
    ref: int = -1            # 对应对象 id（plant.id / mine.id / project.id）
    # 01_terrain 生成的四件"读数"，只影响能不能建 / 造价乘数 / 出力，不参与模拟
    elevation: float = 0.0   # 0~1200 米
    moisture: float = 0.5    # 0~1
    highland: bool = False
    water: bool = False      # 本格是不是水系（河流 / 近海）

    @property
    def occupied(self) -> bool:
        return bool(self.kind)

    @property
    def buildable_land(self) -> bool:
        return self.terrain is not Terrain.CITY

    def __setstate__(self, state: dict) -> None:
        """兼容 01_terrain 之前的存档：缺的字段补默认值。"""
        self.__dict__.update(state)
        self.__dict__.setdefault("elevation", 0.0)
        self.__dict__.setdefault("moisture", 0.5)
        self.__dict__.setdefault("highland", False)
        self.__dict__.setdefault("water", self.terrain in (Terrain.RIVER, Terrain.COAST))


@dataclass
class City:
    idx: int
    name: str
    x: int
    y: int
    base_mw: float
    growth: float = 0.012
    demand_mw: float = 0.0
    residential: float = 0.0
    industrial: float = 0.0
    served: dict[int, float] = field(default_factory=dict)   # 公司 -> 实际供电 MW
    share: dict[int, float] = field(default_factory=dict)    # 公司 -> 份额
    outage_mw: float = 0.0

    def refresh_split(self, res_ratio: float) -> None:
        self.residential = self.demand_mw * res_ratio
        self.industrial = self.demand_mw * (1.0 - res_ratio)


def chebyshev(x1: int, y1: int, x2: int, y2: int) -> int:
    return max(abs(x1 - x2), abs(y1 - y2))


class WorldMap:
    def __init__(self, w: int = GRID_W, h: int = GRID_H):
        self.w = w
        self.h = h
        self.tiles: list[list[Tile]] = []
        self.cities: list[City] = []
        # 01_terrain 的产物：候选城址 + 河道（存档里带一份，方便调试/复盘）
        self.city_candidates: list[dict] = []
        self.rivers: list[list[tuple[int, int]]] = []
        self.terrain_seed: int = 0

    # ------------------------------------------------------------ 基础访问
    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def tile(self, x: int, y: int) -> Tile:
        return self.tiles[y][x]

    def iter_tiles(self) -> Iterable[Tile]:
        for row in self.tiles:
            yield from row

    def neighbors4(self, x: int, y: int) -> Iterable[tuple[int, int]]:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if self.in_bounds(nx, ny):
                yield nx, ny

    def has_water_adjacent(self, x: int, y: int) -> bool:
        """本格或相邻格是河流/近海 —— 冷却水判定。"""
        t = self.tile(x, y)
        if t.terrain in (Terrain.RIVER, Terrain.COAST):
            return True
        for nx, ny in self.neighbors4(x, y):
            if self.tile(nx, ny).terrain in (Terrain.RIVER, Terrain.COAST):
                return True
        return False

    def near_city(self, x: int, y: int, radius: int) -> bool:
        return any(chebyshev(x, y, c.x, c.y) <= radius for c in self.cities)

    def city_at(self, x: int, y: int) -> Optional[City]:
        for c in self.cities:
            if c.x == x and c.y == y:
                return c
        return None

    # ------------------------------------------------------------ 生成
    @classmethod
    def generate(cls, seed: int = 20240501) -> "WorldMap":
        """按 `docs/01_terrain.md` 的契约生成地图：地形、矿、风光、城址全部来自 01。"""
        from . import terrain as terrain_mod

        wm = cls()
        w, h = wm.w, wm.h
        tm = terrain_mod.generate(w, h, seed)
        wm.terrain_seed = seed
        wm.city_candidates = [dict(c) for c in tm.city_candidates]
        wm.rivers = [list(path) for path in tm.rivers]

        wm.tiles = []
        for y in range(h):
            row: list[Tile] = []
            for x in range(w):
                c = tm.cell(x, y)
                row.append(Tile(
                    x, y, c.terrain,
                    wind=c.wind, solar=c.solar, coal=c.coal, uranium=c.uranium,
                    elevation=c.elevation, moisture=c.moisture, highland=c.highland,
                    water=c.terrain in (Terrain.RIVER, Terrain.COAST),
                ))
            wm.tiles.append(row)

        # 城址：候选从大到小，越大的平原给越大的用电基数
        names = (("临江", 700.0), ("北岭", 560.0), ("钢城", 420.0), ("滨海", 300.0))
        for idx, (name, base_mw) in enumerate(names):
            if idx >= len(wm.city_candidates):
                break
            cand = wm.city_candidates[idx]
            cx, cy = int(cand["x"]), int(cand["y"])
            t = wm.tiles[cy][cx]
            t.terrain = Terrain.CITY
            t.water = False
            t.owner = -1
            t.kind = "city"
            c = City(idx, name, cx, cy, base_mw)
            c.demand_mw = base_mw
            wm.cities.append(c)

        for c in wm.cities:
            c.refresh_split(0.42)
        return wm

    # ------------------------------------------------------------ 建造权限
    def check_build(self, kind: str, x: int, y: int) -> tuple[bool, str]:
        """返回 (是否可建, 不可建原因)。这里只管"地形/占位"，不管钱和审批。"""
        if not self.in_bounds(x, y):
            return False, "超出地图"
        t = self.tile(x, y)
        if t.terrain is Terrain.CITY:
            return False, "城市地块不可建设"
        if t.occupied:
            return False, "该地块已被占用"

        if kind in PLANT_SPECS:
            spec = PLANT_SPECS[kind]
            if t.terrain not in spec.terrains:
                names = "/".join(TERRAIN_INFO[tt].name for tt in spec.terrains)
                return False, f"{spec.name}只能建在：{names}"
            if spec.water and not self.has_water_adjacent(x, y):
                return False, "缺少冷却水源（需临河或近海）"
            if kind == "hydro" and t.terrain is not Terrain.RIVER:
                return False, "水电站必须建在河流地块"
            if kind == "nuclear":
                if self.near_city(x, y, NUCLEAR_CITY_GAP):
                    return False, f"核电站需距城市 {NUCLEAR_CITY_GAP + 1} 格以上"
                if t.wind < 0:  # 占位，保持分支一致
                    return False, "不可建"
            if kind == "wind" and t.wind < 0.8:
                return False, "该处风资源不足"
            if kind == "solar" and t.solar < 0.8:
                return False, "该处光照资源不足"
            return True, ""

        if kind in INFRA_SPECS:
            if kind == "coal_mine":
                if t.coal <= 0:
                    return False, "此处没有可采煤矿"
                return True, ""
            if kind == "uranium_mine":
                if t.uranium <= 0:
                    return False, "此处没有可采铀矿"
                return True, ""
            if kind == "substation":
                if not self.near_city(x, y, CITY_RADIUS):
                    return False, f"变电站必须建在城市 {CITY_RADIUS} 格内才能接网"
                return True, ""
            if kind == "line":
                return True, ""
        return False, "未知建筑类型"

    # ------------------------------------------------------------ 造价
    def build_cost(self, kind: str, x: int, y: int) -> float:
        t = self.tile(x, y)
        mult = TERRAIN_INFO[t.terrain].cost_mult
        if kind in PLANT_SPECS:
            spec = PLANT_SPECS[kind]
            return spec.capex_per_mw * spec.cap * mult
        if kind in INFRA_SPECS:
            return INFRA_SPECS[kind].capex * mult
        return 0.0

    def land_cost(self, x: int, y: int) -> float:
        t = self.tile(x, y)
        return LAND_COST * TERRAIN_INFO[t.terrain].cost_mult

    def find_tiles(self, kind: str, limit: int = 9999) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for t in self.iter_tiles():
            ok, _ = self.check_build(kind, t.x, t.y)
            if ok:
                out.append((t.x, t.y))
                if len(out) >= limit:
                    break
        return out
