"""粗网格 → 细网格：把 `terrain.py` 生成的 120×120 契约地图扩成 960×960 的可玩棋盘。

为什么不在 960×960 上直接跑契约管线：IDW + 平滑 + BFS + 连通域在 92 万格上
纯 Python 要十几秒到一分钟，还要重试；改成"粗网格定骨架、细网格补细节"：

* 高程 / 湿度 / 风光：粗格中心双线性插值 + 细尺度噪声抖动
* 海岸线：粗格海陆边界换成一条带噪声的曲线（不再是 8 格台阶）
* 河流：粗河道路径用 Bresenham 连成 1 格宽的细河道
* 资源 / 城址：粗格里的矿点、城址落到对应细块中心

输出的都是按行优先展开的数组（`bytearray` / `array`），92 万格也只占十几 MB，
不用给每格建 Python 对象。
"""
from __future__ import annotations

import math
import random
from array import array

from . import terrain as terrain_mod
from .constants import Terrain

COARSE = 60           # 粗网格边长（契约管线的实际运行尺寸）
FINE = 480            # 细网格边长（真正的游戏棋盘）
K = FINE // COARSE    # 8：一个粗格 = 8×8 细格

_ORDER = (Terrain.PLAIN, Terrain.HILL, Terrain.RIVER, Terrain.DESERT,
          Terrain.COAST, Terrain.CITY)
T_IDX = {t: i for i, t in enumerate(_ORDER)}
T_BY_IDX = _ORDER

FLAG_HIGHLAND = 1
FLAG_WATER = 2


# ---------------------------------------------------------------- 噪声
def _noise_grid(rng: random.Random, n: int = 64) -> list[list[float]]:
    return [[rng.uniform(-1.0, 1.0) for _ in range(n)] for _ in range(n)]


def _sample(grid: list[list[float]], x: float, y: float) -> float:
    n = len(grid)
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    fx, fy = x - x0, y - y0
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    x1, y1 = (x0 + 1) % n, (y0 + 1) % n
    x0 %= n
    y0 %= n
    top = grid[y0][x0] * (1 - fx) + grid[y0][x1] * fx
    bot = grid[y1][x0] * (1 - fx) + grid[y1][x1] * fx
    return top * (1 - fy) + bot * fy


# ---------------------------------------------------------------- 主入口
def _cleanup_small(terr: bytearray, w: int, h: int, ctab, k: int,
                   min_region: int = 8) -> int:
    """细网格上的孤岛清理：面积 < min_region 的同地形连通块，回退成所在粗块的地形。

    找到 8 格就提前收工，所以大块只花常数时间；整图大约 0.5 秒。
    """
    from collections import deque

    n = w * h
    seen = bytearray(n)
    fixed = 0
    water = (T_IDX[Terrain.RIVER], T_IDX[Terrain.COAST], T_IDX[Terrain.CITY])
    for start in range(n):
        if seen[start]:
            continue
        ti = terr[start]
        if ti in water:
            seen[start] = 1
            continue
        seen[start] = 1
        cells = [start]
        q = deque([start])
        big = False
        while q:
            i = q.popleft()
            x, y = i % w, i // w
            for j, ok in ((i - 1, x > 0), (i + 1, x + 1 < w),
                          (i - w, y > 0), (i + w, y + 1 < h)):
                if not ok or seen[j] or terr[j] != ti:
                    continue
                seen[j] = 1
                cells.append(j)
                q.append(j)
                if len(cells) >= min_region:
                    big = True
                    q.clear()
                    break
        if big or len(cells) >= min_region:
            continue
        # 改成"边界上最多的地形"，比回退粗块更不容易留下新的碎块
        votes: dict[int, int] = {}
        for i in cells:
            x, y = i % w, i // w
            for j, ok in ((i - 1, x > 0), (i + 1, x + 1 < w),
                          (i - w, y > 0), (i + w, y + 1 < h)):
                if not ok:
                    continue
                tj = terr[j]
                if tj == ti:
                    continue
                votes[tj] = votes.get(tj, 0) + 1
        if votes:
            target = max(votes.items(), key=lambda kv: kv[1])[0]
        else:
            i = cells[0]
            target = ctab[(i // w) // k][(i % w) // k]
        for i in cells:
            terr[i] = target
            fixed += 1
    return fixed


def expand(tm, size: int = FINE, seed: int = 0) -> dict:
    """把粗网格 `tm` 扩成 size×size 的数组集合。"""
    k = size // tm.w
    w = h = size
    n = w * h
    rng = random.Random((seed or 1) ^ 0xA5A5A5)

    elev = array("f", bytes(4 * n))
    moist = array("f", bytes(4 * n))
    wind = array("f", bytes(4 * n))
    solar = array("f", bytes(4 * n))
    coal = array("f", bytes(4 * n))
    uranium = array("f", bytes(4 * n))
    terr = bytearray(n)
    flags = bytearray(n)

    cw, ch = tm.w, tm.h
    noise = _noise_grid(rng)
    # 细尺度抖动查表：每 4 格一个值，比每格调用一次噪声函数快一个数量级
    jn = 120
    j_e = [[rng.uniform(-26.0, 26.0) for _ in range(jn)] for _ in range(jn)]
    j_m = [[rng.uniform(-0.05, 0.05) for _ in range(jn)] for _ in range(jn)]
    j_w = [[rng.uniform(-0.10, 0.10) for _ in range(jn)] for _ in range(jn)]
    j_s = [[rng.uniform(-0.10, 0.10) for _ in range(jn)] for _ in range(jn)]

    # 粗格字段先摊平成列表：内层循环只做下标和四则运算，避免 92 万次属性访问
    ge = [[tm.cell(x, y).elevation for x in range(cw)] for y in range(ch)]
    gm = [[tm.cell(x, y).moisture for x in range(cw)] for y in range(ch)]
    gw = [[tm.cell(x, y).wind for x in range(cw)] for y in range(ch)]
    gs = [[tm.cell(x, y).solar for x in range(cw)] for y in range(ch)]

    # 粗格地形表：细格只允许在"隔壁粗块就有这个地形"时翻转，
    # 否则噪声会在粗块的腹地凭空长出 1~4 格的孤岛（契约 §6 要求无孤岛）
    ctab = [[T_IDX[c.terrain] for c in row] for row in tm.cells]

    def bilinear(grid: list[list[float]], fx: float, fy: float) -> float:
        gx = fx / k - 0.5
        gy = fy / k - 0.5
        x0 = int(math.floor(gx))
        y0 = int(math.floor(gy))
        tx = gx - x0
        ty = gy - y0
        x1 = x0 + 1 if x0 + 1 < cw else cw - 1
        y1 = y0 + 1 if y0 + 1 < ch else ch - 1
        x0 = 0 if x0 < 0 else x0
        y0 = 0 if y0 < 0 else y0
        r0 = grid[y0]
        r1 = grid[y1]
        return ((r0[x0] * (1 - tx) + r0[x1] * tx) * (1 - ty)
                + (r1[x0] * (1 - tx) + r1[x1] * tx) * ty)

    # 海岸线：粗图第一个海格换算成细格，再加一条正弦噪声让岸线不是刀切
    coast_start = cw
    for x in range(cw):
        if tm.cell(x, 0).terrain is Terrain.COAST:
            coast_start = x
            break
    shore_base = coast_start * k
    shore_amp = max(2.0, k * 1.2)
    river_cells: set[int] = set()

    cfg_norm = 1200.0
    hill_elev = 0.40
    highland_elev = 0.72
    desert_moist = 0.30

    for y in range(h):
        # 岸线随纬度轻微摆动
        shore = shore_base + shore_amp * math.sin(y / (h * 0.11)) \
            + 2.4 * _sample(noise, y / 37.0, 11.3)
        row = y * w
        fy = float(y)
        jy = y >> 2
        je_row = j_e[jy]
        jm_row = j_m[jy]
        jw_row = j_w[jy]
        js_row = j_s[jy]
        for x in range(w):
            fx = float(x)
            i = row + x
            jx = x >> 2

            e = bilinear(ge, fx, fy) + je_row[jx]
            m = bilinear(gm, fx, fy) + jm_row[jx]
            wnd = bilinear(gw, fx, fy) + jw_row[jx]
            sol = bilinear(gs, fx, fy) + js_row[jx]

            # 细尺度抖动：让色块有毛边，但不动大格局
            e = 0.0 if e < 0.0 else (cfg_norm if e > cfg_norm else e)
            m = 0.0 if m < 0.0 else (1.0 if m > 1.0 else m)
            wnd = 0.0 if wnd < 0.0 else wnd
            sol = 0.0 if sol < 0.0 else sol

            elev[i] = e
            moist[i] = m
            wind[i] = wnd
            solar[i] = sol

            if x >= shore:
                terr[i] = T_IDX[Terrain.COAST]
                flags[i] = FLAG_WATER
                elev[i] = 0.0
                continue

            norm = e / cfg_norm
            if m < desert_moist and norm < 0.55:
                cls = T_IDX[Terrain.DESERT]
            elif norm >= highland_elev:
                cls = T_IDX[Terrain.HILL]
            elif norm >= hill_elev:
                cls = T_IDX[Terrain.HILL]
            else:
                cls = T_IDX[Terrain.PLAIN]
            bx, by = x // k, y // k
            ct = ctab[by][bx]
            if cls != ct:
                near = ((bx > 0 and ctab[by][bx - 1] == cls)
                        or (bx + 1 < cw and ctab[by][bx + 1] == cls)
                        or (by > 0 and ctab[by - 1][bx] == cls)
                        or (by + 1 < ch and ctab[by + 1][bx] == cls))
                if not near:
                    cls = ct
            terr[i] = cls
            if cls == T_IDX[Terrain.HILL] and norm >= highland_elev:
                flags[i] = FLAG_HIGHLAND

    # 河流：粗河道 → 1 格宽的细折线
    for path in tm.rivers:
        pts = [((x * k) + k // 2, (y * k) + k // 2) for x, y in path]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            for px, py in _line(x0, y0, x1, y1):
                if 0 <= px < w and 0 <= py < h:
                    i = py * w + px
                    river_cells.add(i)
                    terr[i] = T_IDX[Terrain.RIVER]
                    flags[i] |= FLAG_WATER
                    elev[i] = 0.0

    # 资源：粗格矿点落到细块中心（细图上仍然是稀疏几个点）
    coal_spots: list[tuple[int, int]] = []
    uranium_spots: list[tuple[int, int]] = []
    for c in tm.iter_cells():
        if c.coal > 0 or c.uranium > 0:
            fx = c.x * k + k // 2
            fy = c.y * k + k // 2
            i = fy * w + fx
            coal[i] = c.coal + rng.uniform(-0.05, 0.05) * c.coal
            uranium[i] = c.uranium + rng.uniform(-0.05, 0.05) * c.uranium
            if c.coal > 0:
                coal_spots.append((fx, fy))
            if c.uranium > 0:
                uranium_spots.append((fx, fy))

    # 孤岛清理：噪声可能在粗块腹地长出 1~4 格的碎块，多跑两轮
    for _ in range(3):
        if _cleanup_small(terr, w, h, ctab, k) == 0:
            break

    return {
        "w": w, "h": h, "k": k,
        "terrain": terr, "flags": flags,
        "elevation": elev, "moisture": moist,
        "wind": wind, "solar": solar,
        "coal": coal, "uranium": uranium,
        "rivers": [[(x * k + k // 2, y * k + k // 2) for x, y in path]
                   for path in tm.rivers],
        "coal_spots": coal_spots,
        "uranium_spots": uranium_spots,
        "city_candidates": [{"x": c["x"] * k + k // 2, "y": c["y"] * k + k // 2,
                             "area": c.get("area", 0) * k * k}
                            for c in tm.city_candidates],
    }


def _line(x0: int, y0: int, x1: int, y1: int):
    """Bresenham：把两个粗格中心连成细格折线。"""
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        yield x0, y0
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def generate_fine(width: int = FINE, height: int = FINE, seed: int = 20240501,
                  coarse: int = COARSE) -> dict:
    """一步到位：粗网格（契约管线）→ 细网格数组。"""
    tm = terrain_mod.generate(coarse, coarse, seed)
    data = expand(tm, size=width, seed=seed)
    data["coarse"] = tm
    return data
