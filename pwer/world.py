"""地图、地形、地块与建造权限。

设计意图（对应设计文档"地形与地块系统"）：
格子只承载 4 个属性 —— 地形类型、水系标记、风光系数、矿产资源。
它们只影响三件事：能不能建、建造成本乘数、出力效率。不做潮流/流体模拟。
"限制即乐趣"：没有河流不能造水电，荒漠建不了大型火电，城市附近不能上核电。

**存储方式**：960×960 = 921,600 格，一格一个 Python 对象要 ~300MB，所以改成
按行优先展开的数组（`bytearray` / `array`）。`Tile` 变成一个轻量"视图"对象，
读写直接落到数组里，调用方写法不变（`t.owner = 1` 照样能用）。
"""
from __future__ import annotations

import math
from array import array
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

GRID_W = 480
GRID_H = 480           # 方形棋盘：480×480 格 = 230,400 格
MAP_PX = 1080          # 旧口径（像素），保留给老代码引用
TILE_PX = 60           # 100% 缩放时一个格子的像素边长
CITY_RADIUS = 2        # 城市供电接入半径（切比雪夫距离）
NUCLEAR_CITY_GAP = 2   # 核电站与城市的最小距离

_T_ORDER = (Terrain.PLAIN, Terrain.HILL, Terrain.RIVER, Terrain.DESERT,
            Terrain.COAST, Terrain.CITY)
_T_IDX = {t: i for i, t in enumerate(_T_ORDER)}
FLAG_HIGHLAND = 1
FLAG_WATER = 2

TERRAIN_ORDER = _T_ORDER          # 索引 → Terrain（渲染层直接读数组用）
TERRAIN_INDEX = dict(_T_IDX)
CITY_TERRAIN_IDX = _T_IDX[Terrain.CITY]


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


class Tile:
    """地块视图：不持有数据，读写都转发给 `WorldMap` 的数组。

    这样调用方仍然可以 `t = world.tile(x, y); t.owner = 2`，但存 92 万格
    只花十几 MB，而且不会每格一个对象。
    """

    __slots__ = ("_w", "x", "y")

    def __init__(self, world: "WorldMap", x: int, y: int) -> None:
        self._w = world
        self.x = x
        self.y = y

    # -- 基础字段
    @property
    def _i(self) -> int:
        return self.y * self._w.w + self.x

    @property
    def terrain(self) -> Terrain:
        return _T_ORDER[self._w.terr[self._i]]

    @terrain.setter
    def terrain(self, value: Terrain) -> None:
        self._w.terr[self._i] = _T_IDX[value]

    @property
    def owner(self) -> int:
        return self._w.own[self._i]

    @owner.setter
    def owner(self, value: int) -> None:
        self._w.set_owner(self._i, int(value))

    @property
    def kind(self) -> str:
        return self._w.kind[self._i]

    @kind.setter
    def kind(self, value: str) -> None:
        self._w.kind[self._i] = value or ""

    @property
    def ref(self) -> int:
        return self._w.ref[self._i]

    @ref.setter
    def ref(self, value: int) -> None:
        self._w.ref[self._i] = int(value)

    @property
    def wind(self) -> float:
        return self._w.wind[self._i]

    @wind.setter
    def wind(self, value: float) -> None:
        self._w.wind[self._i] = float(value)

    @property
    def solar(self) -> float:
        return self._w.solar[self._i]

    @solar.setter
    def solar(self, value: float) -> None:
        self._w.solar[self._i] = float(value)

    @property
    def coal(self) -> float:
        return self._w.coal[self._i]

    @coal.setter
    def coal(self, value: float) -> None:
        self._w.coal[self._i] = float(value)

    @property
    def uranium(self) -> float:
        return self._w.uranium[self._i]

    @uranium.setter
    def uranium(self, value: float) -> None:
        self._w.uranium[self._i] = float(value)

    @property
    def elevation(self) -> float:
        return self._w.elev[self._i]

    @elevation.setter
    def elevation(self, value: float) -> None:
        self._w.elev[self._i] = float(value)

    @property
    def moisture(self) -> float:
        return self._w.moist[self._i]

    @moisture.setter
    def moisture(self, value: float) -> None:
        self._w.moist[self._i] = float(value)

    @property
    def highland(self) -> bool:
        return bool(self._w.flags[self._i] & FLAG_HIGHLAND)

    @highland.setter
    def highland(self, value: bool) -> None:
        if value:
            self._w.flags[self._i] |= FLAG_HIGHLAND
        else:
            self._w.flags[self._i] &= ~FLAG_HIGHLAND & 0xFF

    @property
    def water(self) -> bool:
        return bool(self._w.flags[self._i] & FLAG_WATER)

    @water.setter
    def water(self, value: bool) -> None:
        if value:
            self._w.flags[self._i] |= FLAG_WATER
        else:
            self._w.flags[self._i] &= ~FLAG_WATER & 0xFF

    # -- 派生
    @property
    def occupied(self) -> bool:
        return bool(self._w.kind[self._i])

    @property
    def buildable_land(self) -> bool:
        return self.terrain is not Terrain.CITY

    def __repr__(self) -> str:      # 调试用
        return f"Tile({self.x},{self.y},{self.terrain.value},{self.kind or '-'})"


class WorldMap:
    def __init__(self, w: int = GRID_W, h: int = GRID_H, arrays: bool = True):
        self.w = w
        self.h = h
        self.cities: list[City] = []
        self.city_candidates: list[dict] = []
        self.rivers: list[list[tuple[int, int]]] = []
        self.terrain_seed: int = 0
        if arrays:
            self._init_arrays()

    # ------------------------------------------------------------ 数组
    def _init_arrays(self) -> None:
        n = self.w * self.h
        self.terr = bytearray(n)
        self.flags = bytearray(n)
        self.own = array("h", [-1]) * n
        self.ref = array("i", [-1]) * n
        self.kind: list[str] = [""] * n
        self.coal = array("f", bytes(4 * n))
        self.uranium = array("f", bytes(4 * n))
        self.wind = array("f", bytes(4 * n))
        self.solar = array("f", bytes(4 * n))
        self.elev = array("f", bytes(4 * n))
        self.moist = array("f", bytes(4 * n))
        self._owned: dict[int, set[int]] = {}
        self._buildable_cache: dict[str, int] = {}
        self.coal_spots: list[tuple[int, int]] = []
        self.uranium_spots: list[tuple[int, int]] = []
        self._counts_ready = False

    def set_owner(self, i: int, value: int) -> None:
        old = self.own[i]
        if old == value:
            return
        if old >= 0:
            s = self._owned.get(old)
            if s is not None:
                s.discard(i)
        self.own[i] = value
        if value >= 0:
            self._owned.setdefault(value, set()).add(i)

    def owned_index(self, owner: int) -> set[int]:
        return self._owned.get(owner, set())

    def all_owned(self) -> Iterable[tuple[int, int, int]]:
        """(owner, x, y)：所有被占用的格子（每个公司几十到几百格，遍历很便宜）。"""
        w = self.w
        for owner, idxs in self._owned.items():
            for i in idxs:
                yield owner, i % w, i // w

    def owned_tiles(self, owner: int) -> Iterable[Tile]:
        w = self.w
        for i in sorted(self._owned.get(owner, ())):
            yield Tile(self, i % w, i // w)

    # ------------------------------------------------------------ 基础访问
    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def tile(self, x: int, y: int) -> Tile:
        return Tile(self, x, y)

    def terrain_at(self, x: int, y: int) -> Terrain:
        return _T_ORDER[self.terr[y * self.w + x]]

    def kind_at(self, x: int, y: int) -> str:
        return self.kind[y * self.w + x]

    def iter_tiles(self) -> Iterable[Tile]:
        """逐格产出视图（92 万格时很慢，只在工具/测试里用；热路径请用数组接口）。"""
        w = self.w
        for y in range(self.h):
            for x in range(w):
                yield Tile(self, x, y)

    def iter_rect(self, x0: int, y0: int, x1: int, y1: int) -> Iterable[Tile]:
        """矩形区域（含端点，自动裁剪到地图内）——渲染与局部搜索用。"""
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w - 1, x1), min(self.h - 1, y1)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                yield Tile(self, x, y)

    def neighbors4(self, x: int, y: int) -> Iterable[tuple[int, int]]:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if self.in_bounds(nx, ny):
                yield nx, ny

    def has_water_adjacent(self, x: int, y: int) -> bool:
        """本格或相邻格是河流/近海 —— 冷却水判定。"""
        if not self.in_bounds(x, y):
            return False
        w = self.w
        i = y * w + x
        if self.flags[i] & FLAG_WATER:
            return True
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < self.h:
                if self.flags[ny * w + nx] & FLAG_WATER:
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
        """粗网格（契约管线）→ 细网格：地形、矿、风光、城址都从这里来。"""
        from . import worldgen

        data = worldgen.generate_fine(GRID_W, GRID_H, seed)
        wm = cls(data["w"], data["h"])
        wm.terr[:] = data["terrain"]
        wm.flags[:] = data["flags"]
        wm.elev[:] = data["elevation"]
        wm.moist[:] = data["moisture"]
        wm.wind[:] = data["wind"]
        wm.solar[:] = data["solar"]
        wm.coal[:] = data["coal"]
        wm.uranium[:] = data["uranium"]
        wm.coal_spots = list(data.get("coal_spots", ()))
        wm.uranium_spots = list(data.get("uranium_spots", ()))
        wm.terrain_seed = seed
        wm.city_candidates = [dict(c) for c in data["city_candidates"]]
        wm.rivers = [list(p) for p in data["rivers"]]

        # 城址：候选从大到小，越大的平原给越大的用电基数
        names = (("临江", 700.0), ("北岭", 560.0), ("钢城", 420.0), ("滨海", 300.0))
        for idx, (name, base_mw) in enumerate(names):
            if idx >= len(wm.city_candidates):
                break
            cand = wm.city_candidates[idx]
            cx, cy = int(cand["x"]), int(cand["y"])
            i = cy * wm.w + cx
            wm.terr[i] = _T_IDX[Terrain.CITY]
            wm.flags[i] &= ~FLAG_WATER & 0xFF
            wm.kind[i] = "city"
            c = City(idx, name, cx, cy, base_mw)
            c.demand_mw = base_mw
            wm.cities.append(c)

        for c in wm.cities:
            c.refresh_split(0.42)
        wm._precompute_buildable()
        return wm

    def _precompute_buildable(self) -> None:
        """生成后一次性数出"每种建筑有多少格能建"。

        960×960 上逐格调用 `check_build` 要好几秒，而 UI 只需要知道按钮能不能点；
        这里用数组做一遍扫描（水邻接表 + 地形位掩码），总耗时约 1 秒。
        """
        w, h, n = self.w, self.h, self.w * self.h
        terr, flags, kind = self.terr, self.flags, self.kind
        water_adj = bytearray(n)
        for y in range(h):
            base = y * w
            for x in range(w):
                i = base + x
                if flags[i] & FLAG_WATER:
                    water_adj[i] = 1
                    continue
                if x + 1 < w and flags[i + 1] & FLAG_WATER:
                    water_adj[i] = 1
                elif x > 0 and flags[i - 1] & FLAG_WATER:
                    water_adj[i] = 1
                elif y + 1 < h and flags[i + w] & FLAG_WATER:
                    water_adj[i] = 1
                elif y > 0 and flags[i - w] & FLAG_WATER:
                    water_adj[i] = 1

        kinds = list(PLANT_SPECS.items())
        mask_by_terr = [0] * len(_T_ORDER)
        for j, (_, spec) in enumerate(kinds):
            for t in spec.terrains:
                mask_by_terr[_T_IDX[t]] |= 1 << j
        need_water = [PLANT_SPECS[k].water for k, _ in kinds]
        counts = [0] * len(kinds)
        for i in range(n):
            if kind[i]:
                continue
            m = mask_by_terr[terr[i]]
            if not m:
                continue
            has_w = water_adj[i] or (flags[i] & FLAG_WATER)
            j = 0
            while m:
                if m & 1:
                    if not need_water[j] or has_w:
                        counts[j] += 1
                m >>= 1
                j += 1
        for j, (k, _) in enumerate(kinds):
            self._buildable_cache[k] = counts[j]

        self._buildable_cache["coal_mine"] = sum(
            1 for x, y in self.coal_spots if not self.kind[y * w + x])
        self._buildable_cache["uranium_mine"] = sum(
            1 for x, y in self.uranium_spots if not self.kind[y * w + x])
        # 线路哪儿都能铺（除了被占的格子）
        self._buildable_cache["line"] = n - sum(1 for k in kind if k)
        # 变电站只在城圈里；四座城 × 25 格足够，恒 >0
        self._buildable_cache["substation"] = sum(
            1 for c in self.cities for _ in range(CITY_RADIUS * 2 + 1)) or 1
        self._counts_ready = True

    # ------------------------------------------------------------ 建造权限
    def check_build(self, kind: str, x: int, y: int) -> tuple[bool, str]:
        """返回 (是否可建, 不可建原因)。这里只管"地形/占位"，不管钱和审批。"""
        if not self.in_bounds(x, y):
            return False, "超出地图"
        w = self.w
        i = y * w + x
        ti = self.terr[i]
        if ti == _T_IDX[Terrain.CITY]:
            return False, "城市地块不可建设"
        if self.kind[i]:
            return False, "该地块已被占用"

        terrain = _T_ORDER[ti]
        if kind in PLANT_SPECS:
            spec = PLANT_SPECS[kind]
            if terrain not in spec.terrains:
                names = "/".join(TERRAIN_INFO[tt].name for tt in spec.terrains)
                return False, f"{spec.name}只能建在：{names}"
            if spec.water and not self.has_water_adjacent(x, y):
                return False, "缺少冷却水源（需临河或近海）"
            if kind == "hydro" and terrain is not Terrain.RIVER:
                return False, "水电站必须建在河流地块"
            if kind == "nuclear":
                if self.near_city(x, y, NUCLEAR_CITY_GAP):
                    return False, f"核电站需距城市 {NUCLEAR_CITY_GAP + 1} 格以上"
            if kind == "wind" and self.wind[i] < 0.8:
                return False, "该处风资源不足"
            if kind == "solar" and self.solar[i] < 0.8:
                return False, "该处光照资源不足"
            return True, ""

        if kind in INFRA_SPECS:
            if kind == "coal_mine":
                if self.coal[i] <= 0:
                    return False, "此处没有可采煤矿"
                return True, ""
            if kind == "uranium_mine":
                if self.uranium[i] <= 0:
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
        mult = TERRAIN_INFO[self.terrain_at(x, y)].cost_mult
        if kind in PLANT_SPECS:
            spec = PLANT_SPECS[kind]
            return spec.capex_per_mw * spec.cap * mult
        if kind in INFRA_SPECS:
            return INFRA_SPECS[kind].capex * mult
        return 0.0

    def land_cost(self, x: int, y: int) -> float:
        return LAND_COST * TERRAIN_INFO[self.terrain_at(x, y)].cost_mult

    def find_tiles(self, kind: str, limit: int = 9999,
                   near: Optional[tuple[int, int]] = None,
                   radius: Optional[int] = None) -> list[tuple[int, int]]:
        """找出可建 `kind` 的地块；给了 near/radius 就只搜那一圈（大图上必备）。"""
        out: list[tuple[int, int]] = []
        if near is not None and radius is not None:
            cx, cy = near
            for r in range(0, radius + 1):
                for y in range(cy - r, cy + r + 1):
                    for x in range(cx - r, cx + r + 1):
                        if max(abs(x - cx), abs(y - cy)) != r:
                            continue        # 只走第 r 圈，近的先找到
                        ok, _ = self.check_build(kind, x, y)
                        if ok:
                            out.append((x, y))
                            if len(out) >= limit:
                                return out
            return out
        for y in range(self.h):
            for x in range(self.w):
                ok, _ = self.check_build(kind, x, y)
                if ok:
                    out.append((x, y))
                    if len(out) >= limit:
                        return out
        return out

    def count_buildable(self, kind: str, sample: int = 4000) -> tuple[int, int]:
        """快速估算：在固定抽样上返回 (可建格数, 抽样总格数)。

        大图上逐格检查要一秒多，UI 只需要知道"有没有地方能建"，
        所以用固定步长的抽样，结果缓存到 `_buildable_cache`。
        """
        cached = self._buildable_cache.get(kind)
        if cached is not None:
            return cached, max(1, cached)
        total = self.w * self.h
        step = max(1, total // sample)
        n = 0
        i = 0
        while i < total and n < sample:
            x, y = i % self.w, i // self.w
            ok, _ = self.check_build(kind, x, y)
            if ok:
                n += 1
            i += step
        self._buildable_cache[kind] = n
        return n, sample
