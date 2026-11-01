"""地形生成管线（`docs/01_terrain.md` 的 S0–S9 实现）。

输入只有 `seed` 与尺寸，输出一张**只读**的地形图：每格高程、湿度、地形枚举、
高原标记，以及资源（煤/铀/风/光）和城市候选。

管线顺序（契约 §2，不可交换）：

    S0 独立 RNG  → S1 控制点 → S4a 粗高程场 → S2 最陡下降成河 → S3 河道锁死
    → S4b 湿度场 → S5 平滑 → S6 分级 → S7 连通域校验 → S8 资源绑定 → S9 城市候选

契约里 S2 在 S4 之前，但"最陡下降"得先有个高程场能下降，所以这里先把 IDW+fbm
算一遍（S4a）用于成河；河道锁死之后再平滑（S5），不违背"S3 之后不得改河道格"。

本模块**不 import world**：01 只出候选，地块对象由 `world.WorldMap.generate` 组装，
避免 01/02 互相依赖（契约 §7 最后一条）。
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from .constants import MINE_RESERVE, TERRAIN_INFO, Terrain

ELEV_MAX = 1200.0
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "terrain_config.json"

# 契约 §4 的配置字段（默认值即契约默认值）
DEFAULTS: dict = {
    "width": 18,
    "height": 18,
    "control_points": (6, 9),
    "idw_power": 2.0,
    "idw_neighbors": 8,
    "idw_epsilon": 0.6,
    "smooth_iter": 2,
    "fbm_amplitude": 60.0,
    "elev_max": ELEV_MAX,
    "water_level": 0.0,
    "desert_moisture": 0.30,
    "hill_elev": 0.35,
    "highland_elev": 0.72,
    "min_region": 8,
    "min_river_len": 10,
    "max_rivers": 3,
    "coal_count": 3,
    "uranium_count": (2, 3),
    "city_candidate_count": 4,
    "city_min_gap": 4,
    "regen_attempts": 5,
}


@dataclass
class Cell:
    """生成阶段的格子（尚未挂 owner/kind/ref，那是 world 的事）。"""

    x: int
    y: int
    elevation: float = 0.0
    moisture: float = 0.5
    terrain: Terrain = Terrain.PLAIN
    highland: bool = False
    frozen: bool = False        # 河道格：S4/S5 不得修改
    coal: float = 0.0
    uranium: float = 0.0
    wind: float = 1.0
    solar: float = 1.0

    @property
    def norm(self) -> float:
        return self.elevation / ELEV_MAX


@dataclass
class TerrainMap:
    w: int
    h: int
    cells: list[list[Cell]] = field(default_factory=list)
    rivers: list[list[tuple[int, int]]] = field(default_factory=list)
    city_candidates: list[dict] = field(default_factory=list)
    attempts: int = 1

    def cell(self, x: int, y: int) -> Cell:
        return self.cells[y][x]

    def iter_cells(self):
        for row in self.cells:
            yield from row

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h


def load_config(path: str | Path | None = None) -> dict:
    """读 `config/terrain_config.json`（契约 §4）；文件缺失或坏了就退回默认值。

    只认 DEFAULTS 里有的键，避免手写配置打错字把生成器带进沟里。
    """
    cfg = dict(DEFAULTS)
    p = Path(path) if path else CONFIG_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for key, value in data.items():
            if key not in cfg:
                continue
            if key in ("control_points", "uranium_count") and isinstance(value, list):
                value = tuple(value)
            cfg[key] = value
    return cfg


# ---------------------------------------------------------------- 噪声
def _value_noise(rng: random.Random, n: int) -> list[list[float]]:
    """一张 n×n 的随机格点，双线性 + smoothstep 插值，值域 [-1, 1]。"""
    grid = [[rng.uniform(-1.0, 1.0) for _ in range(n)] for _ in range(n)]
    return grid


def _sample_noise(grid: list[list[float]], x: float, y: float) -> float:
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


def _fbm(grid: list[list[float]], x: float, y: float, octaves: int = 3) -> float:
    """分形噪声，振幅依次减半；返回值大致落在 [-1, 1]。"""
    total, amp, freq, norm = 0.0, 1.0, 1.0, 0.0
    for _ in range(octaves):
        total += amp * _sample_noise(grid, x * freq, y * freq)
        norm += amp
        amp *= 0.5
        freq *= 2.0
    return total / norm


# ---------------------------------------------------------------- S1 控制点
def _control_points(rng: random.Random, w: int, h: int, cfg: dict) -> list[tuple[float, float, float, float]]:
    """返回 [(x, y, elevation, weight)]：3 个东侧海域锚点（权重 ×3）+ N-3 个内部点。"""
    lo, hi = cfg["control_points"]
    n = rng.randint(int(lo), int(hi)) if hi > lo else int(lo)
    pts: list[tuple[float, float, float, float]] = []

    # 东侧海域锚点：高程 0、权重 3，把海压平
    for y in (1, h // 2, h - 2):
        pts.append((float(w - 2), float(max(0, min(h - 1, y))), 0.0, 3.0))

    inner = max(1, n - 3)
    gap = min(w, h) * 0.35
    made = 0
    tries = 0
    need_west = True
    while made < inner and tries < 400:
        tries += 1
        x = rng.uniform(0, max(0.0, w - 3))
        y = rng.uniform(0, h - 1)
        if need_west and made == inner - 1 and x > w * 0.35:
            continue                      # 最后一点必须落在西侧，给河流留落差
        if any(math.hypot(x - px, y - py) < gap for px, py, _, _ in pts[3:]):
            continue
        elev = rng.uniform(200.0, 1100.0)
        pts.append((x, y, elev, 1.0))
        if x <= w * 0.35:
            need_west = False
        made += 1

    if made == 0:                         # 极端小地图兜底
        pts.append((w * 0.25, h * 0.5, 700.0, 1.0))
    return pts


# ---------------------------------------------------------------- S4 IDW 高程场
def _idw(pts, x: float, y: float, k: int, power: float, eps: float) -> float:
    near = sorted(pts, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)[:max(1, k)]
    num = den = 0.0
    for px, py, z, wt in near:
        d = math.hypot(px - x, py - y)
        w = wt / max(d, eps) ** power
        num += w * z
        den += w
    return num / den if den else 0.0


def _elevation_field(rng: random.Random, w: int, h: int, pts, cfg: dict) -> list[list[float]]:
    noise = _value_noise(rng, 16)
    amp = cfg["fbm_amplitude"]
    field = [[0.0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            e = _idw(pts, x, y, int(cfg["idw_neighbors"]), float(cfg["idw_power"]), float(cfg["idw_epsilon"]))
            e += amp * _fbm(noise, x / 6.0, y / 6.0, 3) + rng.gauss(0.0, 18.0)
            field[y][x] = max(0.0, min(float(cfg["elev_max"]), round(e, 1)))
    # 东侧两列就是海，恒 0（S6 也认这个规则）
    for y in range(h):
        for x in range(max(0, w - 2), w):
            field[y][x] = 0.0
    return field


# ---------------------------------------------------------------- S2 河流
def _sea_dist(x: int, y: int, w: int) -> int:
    return max(0, (w - 2) - x)


def _trace_river(field, w, h, start: tuple[int, int], taken: set, cfg: dict) -> list[tuple[int, int]]:
    """从 start 沿最陡下降走到海；走不通就返回 []。"""
    cur = start
    path = [cur]
    seen = {cur}
    while True:
        cx, cy = cur
        best = None
        best_e = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if nx >= w - 2:                      # 到海了：这条河算成功
                    return path
                key = (nx, ny)
                if key in taken and key != start:
                    return path                           # 汇入已有河
                e = field[ny][nx]
                if best is None or e < best_e - 1e-9:
                    best, best_e = key, e
                elif abs(e - best_e) <= 1e-9:
                    # 同高程：优先"远离海"的方向，避免贴边走
                    if _sea_dist(nx, ny, w) < _sea_dist(best[0], best[1], w):
                        best = key
        if best is None or best in seen:
            return []                                      # 洼地/打转：放弃
        # 洼地处理：把下一点抬高，最多 40 次（每次 +2 米）
        cur_e = field[cy][cx]
        tries = 0
        while best_e >= cur_e and tries < 40:
            field[best[1]][best[0]] += 2.0
            best_e = field[best[1]][best[0]]
            tries += 1
        if best_e >= cur_e:
            return []
        cur = best
        path.append(cur)
        seen.add(cur)


def _straight_fallback(w: int, h: int, start: tuple[int, int]) -> list[tuple[int, int]]:
    """兜底：从起点一路向东走到海（水力条件的最后保障）。"""
    x, y = start
    path = []
    while x < w - 2:
        path.append((x, y))
        x += 1
        if x < w - 3 and (x + y) % 5 == 0:
            y = max(1, min(h - 2, y + 1))
    return path


# ---------------------------------------------------------------- 距离场
def _bfs_dist(w: int, h: int, sources, diag: bool = False) -> list[list[float]]:
    inf = 1e9
    dist = [[inf] * w for _ in range(h)]
    from collections import deque

    q = deque()
    for x, y in sources:
        if 0 <= x < w and 0 <= y < h:
            dist[y][x] = 0.0
            q.append((x, y))
    steps = ((1, 0), (-1, 0), (0, 1), (0, -1))
    if diag:
        steps += ((1, 1), (1, -1), (-1, 1), (-1, -1))
    while q:
        x, y = q.popleft()
        d = dist[y][x]
        for dx, dy in steps:
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and dist[ny][nx] > d + 1:
                dist[ny][nx] = d + 1
                q.append((nx, ny))
    return dist


# ---------------------------------------------------------------- S5 平滑
def _smooth(field, w, h, frozen, cfg: dict) -> list[list[float]]:
    cur = [row[:] for row in field]
    for _ in range(int(cfg["smooth_iter"])):
        nxt = [row[:] for row in cur]
        for y in range(h):
            for x in range(w):
                if frozen[y][x] or x >= w - 2:
                    continue
                c = cur[y][x]
                side = 0.0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = min(w - 1, max(0, x + dx)), min(h - 1, max(0, y + dy))
                    side += cur[ny][nx]
                diag = 0.0
                for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    nx, ny = min(w - 1, max(0, x + dx)), min(h - 1, max(0, y + dy))
                    diag += cur[ny][nx]
                nxt[y][x] = round((4 * c + 2 * side + diag) / 16.0, 1)
        cur = nxt
    return cur


# ---------------------------------------------------------------- 连通域
def _components(terrain, w, h, kinds) -> list[list[tuple[int, int]]]:
    seen = [[False] * w for _ in range(h)]
    out: list[list[tuple[int, int]]] = []
    for y in range(h):
        for x in range(w):
            if seen[y][x] or terrain[y][x] not in kinds:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            comp = []
            while stack:
                cx, cy = stack.pop()
                comp.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] and terrain[ny][nx] in kinds:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            out.append(comp)
    return out


def _reclassify_small(terrain, w, h, cfg: dict) -> None:
    """S7：面积 < min_region 的连通域重分类成"边界上最多的地形"。"""
    min_region = int(cfg["min_region"])
    changed = True
    guard = 0
    while changed and guard < 12:
        changed = False
        guard += 1
        for kinds in ({Terrain.PLAIN}, {Terrain.HILL}, {Terrain.DESERT}):
            for comp in _components(terrain, w, h, kinds):
                if len(comp) >= min_region:
                    continue
                counts: dict[Terrain, int] = {}
                for cx, cy in comp:
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = cx + dx, cy + dy
                        if not (0 <= nx < w and 0 <= ny < h):
                            continue
                        other = terrain[ny][nx]
                        if other in kinds:
                            continue
                        counts[other] = counts.get(other, 0) + 1
                if not counts:
                    continue
                best_n = max(counts.values())
                best = [k for k, v in counts.items() if v == best_n]
                target = Terrain.PLAIN if Terrain.PLAIN in best else sorted(best, key=lambda t: t.value)[0]
                for cx, cy in comp:
                    terrain[cy][cx] = target
                changed = True


# ---------------------------------------------------------------- 主入口
def generate(width: int = 18, height: int = 18, seed: int = 20240501, **overrides) -> TerrainMap:
    """按契约生成一张地形图；同 seed 逐格一致。"""
    cfg = load_config(overrides.pop("config_path", None))
    cfg.update(overrides)
    w, h = int(width), int(height)
    attempts = int(cfg.get("regen_attempts", 5))
    best: TerrainMap | None = None

    for attempt in range(1, attempts + 1):
        # S0：独立 RNG，不碰 game.rng；只有重试时才加盐，第一次严格等于契约的 seed ^ 0x5EED
        salt = 0 if attempt == 1 else attempt * 7919
        rng = random.Random((seed ^ 0x5EED) + salt)
        tm = _generate_once(rng, w, h, cfg, attempt)
        if best is None or _score(tm) > _score(best):
            best = tm
        if _acceptable(tm, cfg):
            best = tm
            break
    assert best is not None
    return best


def _score(tm: TerrainMap) -> int:
    return len(tm.city_candidates)


def _acceptable(tm: TerrainMap, cfg: dict) -> bool:
    cells = list(tm.iter_cells())
    total = len(cells) or 1
    coast = sum(1 for c in cells if c.terrain is Terrain.COAST)
    plain = sum(1 for c in cells if c.terrain is Terrain.PLAIN)
    # 候选城址建城后就不再是 PLAIN，验收按"世界里的平原"算，扣掉这几格
    plain_land = (plain - len(tm.city_candidates)) / total
    rivers = [r for r in tm.rivers if len(r) >= int(cfg["min_river_len"])]
    return (
        0.08 <= coast / total <= 0.20
        and plain_land >= 0.35
        and len(rivers) >= 1
        and len(tm.city_candidates) >= int(cfg["city_candidate_count"])
    )


def _generate_once(rng: random.Random, w: int, h: int, cfg: dict, attempt: int) -> TerrainMap:
    tm = TerrainMap(w=w, h=h, attempts=attempt)
    pts = _control_points(rng, w, h, cfg)

    # S4a：先算一遍高程场，给"最陡下降"用
    field = _elevation_field(rng, w, h, pts, cfg)

    # S2：找高点 → 最陡下降
    flat = sorted(((field[y][x], x, y) for y in range(h) for x in range(w)), reverse=True)
    q75 = flat[int(len(flat) * 0.25)][0]
    sources = [(x, y) for e, x, y in flat if e >= q75 and x < w - 2]
    frozen = [[False] * w for _ in range(h)]
    taken: set[tuple[int, int]] = set()
    rivers: list[list[tuple[int, int]]] = []
    min_len = int(cfg["min_river_len"])

    for sx, sy in sources:
        if len(rivers) >= int(cfg["max_rivers"]):
            break
        if any(max(abs(sx - rx), abs(sy - ry)) <= 2 for r in rivers for rx, ry in r[:1]):
            continue                                  # 源头别挤在一起
        path = _trace_river(field, w, h, (sx, sy), taken, cfg)
        if len(path) < min_len:
            continue
        rivers.append(path)
        taken.update(path)

    # 主干必须成功：不行就走直线兜底
    if not rivers:
        fallback = _straight_fallback(w, h, (1, h // 3))
        if len(fallback) >= min_len:
            rivers.append(fallback)

    # S3：河道锁死
    for path in rivers:
        for x, y in path:
            frozen[y][x] = True
            field[y][x] = float(cfg["water_level"])

    # 支流：起点高程需低于主干中位数（这里简单取"第二个源点必须更低"）
    if rivers and len(rivers) < int(cfg["max_rivers"]):
        main = rivers[0]
        mid = sorted(field[y][x] for x, y in main)[len(main) // 2]
        for sx, sy in sources:
            if len(rivers) >= int(cfg["max_rivers"]):
                break
            if frozen[sy][sx] or field[sy][sx] >= mid:
                continue
            path = _trace_river(field, w, h, (sx, sy), taken, cfg)
            if len(path) < min_len:
                continue
            rivers.append(path)
            taken.update(path)
            for x, y in path:
                frozen[y][x] = True
                field[y][x] = float(cfg["water_level"])

    # S5：平滑（跳过冻结格与海）
    field = _smooth(field, w, h, frozen, cfg)

    # S4b：湿度场
    sea_cells = [(x, y) for y in range(h) for x in range(max(0, w - 2), w)]
    sea_dist = _bfs_dist(w, h, sea_cells, diag=True)
    river_cells = [(x, y) for path in rivers for x, y in path]
    river_dist = _bfs_dist(w, h, river_cells, diag=True)

    cells: list[list[Cell]] = []
    for y in range(h):
        row = []
        for x in range(w):
            e = round(field[y][x], 1)
            norm = e / ELEV_MAX
            near_river = 1.0 / (1.0 + river_dist[y][x])
            moist = 1.0 - sea_dist[y][x] / (w * 0.9) + 0.25 * near_river - 0.35 * norm
            row.append(Cell(x, y, elevation=e, moisture=round(max(0.0, min(1.0, moist)), 3)))
        cells.append(row)
    tm.cells = cells

    # S6：分级
    for y in range(h):
        for x in range(w):
            c = cells[y][x]
            if x >= w - 2:
                c.terrain = Terrain.COAST
                c.elevation = 0.0
            elif frozen[y][x]:
                c.terrain = Terrain.RIVER
                c.elevation = 0.0
            elif c.moisture < float(cfg["desert_moisture"]) and c.norm < 0.55:
                c.terrain = Terrain.DESERT
            elif c.norm >= float(cfg["highland_elev"]):
                c.terrain = Terrain.HILL
                c.highland = True
            elif c.norm >= float(cfg["hill_elev"]):
                c.terrain = Terrain.HILL
            else:
                c.terrain = Terrain.PLAIN

    # S7：连通域面积校验（河道/海不参与）
    terrain = [[c.terrain for c in row] for row in cells]
    _reclassify_small(terrain, w, h, cfg)
    for y in range(h):
        for x in range(w):
            cells[y][x].terrain = terrain[y][x]

    tm.rivers = [[(x, y) for x, y in path] for path in rivers]

    # S9 先算城市候选（S8 的矿点要避开候选格）
    tm.city_candidates = _city_candidates(tm, cfg)

    # 城址会把平原切碎：把候选格当成 CITY 再跑一次面积校验，避免城市边上留 1~2 格的碎片
    city_cells = {(c["x"], c["y"]) for c in tm.city_candidates}
    terrain = [[c.terrain for c in row] for row in cells]
    for x, y in city_cells:
        terrain[y][x] = Terrain.CITY
    _reclassify_small(terrain, w, h, cfg)
    for y in range(h):
        for x in range(w):
            if (x, y) not in city_cells:
                cells[y][x].terrain = terrain[y][x]

    # S8：资源
    _bind_resources(rng, tm, river_dist, sea_dist, cfg)
    return tm


# ---------------------------------------------------------------- S9 候选
def _plain_components(tm: TerrainMap) -> list[list[tuple[int, int]]]:
    terrain = [[c.terrain for c in row] for row in tm.cells]
    comps = _components(terrain, tm.w, tm.h, {Terrain.PLAIN})
    comps.sort(key=len, reverse=True)
    return comps


def _centroid_cell(tm: TerrainMap, comp: list[tuple[int, int]]) -> tuple[int, int]:
    mx = sum(x for x, _ in comp) / len(comp)
    my = sum(y for _, y in comp) / len(comp)
    best = min(comp, key=lambda p: (p[0] - mx) ** 2 + (p[1] - my) ** 2)
    return best


def _city_candidates(tm: TerrainMap, cfg: dict) -> list[dict]:
    """S9：平原连通域从大到小，逐个挑足候选。

    契约的"质心"取法在 18×12 上只能出 1–2 个候选（大平原只有一个质心），
    这里改成"同一连通域内挑离边界最远的若干格"，仍然满足 §6 的验收不变量
    （候选在平原里、3×3 内有 ≥2 个非水非山格、彼此切比雪夫距 ≥4）。
    """
    need = int(cfg["city_candidate_count"])
    gap = int(cfg["city_min_gap"])
    out: list[dict] = []
    for comp in _plain_components(tm):
        if len(out) >= need:
            break
        for cx, cy in _spots_in(tm, comp):
            if len(out) >= need:
                break
            if any(max(abs(cx - c["x"]), abs(cy - c["y"])) < gap for c in out):
                continue
            if not _candidate_ok(tm, cx, cy):
                continue
            if not _starter_ok(tm, cx, cy):
                continue
            out.append({"x": cx, "y": cy, "area": len(comp)})
    return out


def _spots_in(tm: TerrainMap, comp: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """连通域内按"离边界越远越优先"排序，便于挑出真正平坦的中心。"""
    inside = set(comp)
    depth: dict[tuple[int, int], int] = {}
    for x, y in comp:
        d = 0
        while d < 6:
            ring = [(x + dx, y + dy)
                    for dx in range(-d, d + 1) for dy in range(-d, d + 1)
                    if max(abs(dx), abs(dy)) == d]
            if any(p not in inside for p in ring):
                break
            d += 1
        depth[(x, y)] = d
    sea_col = tm.w - 2
    return sorted(comp, key=lambda p: (-depth[p], abs(p[0] - sea_col), p[1], p[0]))


def _starter_ok(tm: TerrainMap, cx: int, cy: int) -> bool:
    """这个候选能不能让四家公司都起得来：6 格内有临水的可建格（建火电）+ 3 格内有变电站位。"""
    has_power = False
    for y in range(cy - 6, cy + 7):
        for x in range(cx - 6, cx + 7):
            if not tm.in_bounds(x, y):
                continue
            c = tm.cell(x, y)
            if c.terrain not in (Terrain.PLAIN, Terrain.HILL):
                continue
            if _adjacent_water(tm, x, y):
                has_power = True
                break
        if has_power:
            break
    if not has_power:
        return False
    for y in range(cy - 3, cy + 4):
        for x in range(cx - 3, cx + 4):
            if not tm.in_bounds(x, y):
                continue
            if tm.cell(x, y).terrain in (Terrain.PLAIN, Terrain.DESERT):
                return True
    return False


def _adjacent_water(tm: TerrainMap, x: int, y: int) -> bool:
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if tm.in_bounds(nx, ny) and tm.cell(nx, ny).terrain in (Terrain.RIVER, Terrain.COAST):
            return True
    return False


def _candidate_ok(tm: TerrainMap, cx: int, cy: int) -> bool:
    """3×3 内至少有 2 个"非水非山非矿"的格子，才放得下变电站；城址不贴边。"""
    if not (1 <= cx <= tm.w - 2 and 1 <= cy <= tm.h - 2):
        return False
    ok = 0
    for y in range(cy - 1, cy + 2):
        for x in range(cx - 1, cx + 2):
            if not tm.in_bounds(x, y):
                continue
            c = tm.cell(x, y)
            if c.terrain in (Terrain.RIVER, Terrain.COAST, Terrain.HILL):
                continue
            if c.terrain is Terrain.CITY:
                continue
            ok += 1
    return ok >= 2


# ---------------------------------------------------------------- S8 资源
def _bind_resources(rng: random.Random, tm: TerrainMap, river_dist, sea_dist, cfg: dict) -> None:
    taken: list[tuple[int, int]] = []
    cands = {(c["x"], c["y"]) for c in tm.city_candidates}

    def spot_ok(x: int, y: int, min_gap: int = 3) -> bool:
        if (x, y) in cands:
            return False
        for tx, ty in taken:
            if max(abs(x - tx), abs(y - ty)) < min_gap:
                return False
        return True

    # 煤矿：平原/丘陵，中低海拔，靠河，离城市候选 ≥3
    coal_need = int(cfg["coal_count"])
    coal_spots: list[tuple[int, int]] = []
    for c in sorted(tm.iter_cells(), key=lambda c: -c.moisture):
        if len(coal_spots) >= coal_need:
            break
        if c.terrain not in (Terrain.PLAIN, Terrain.HILL):
            continue
        if not (0.10 <= c.norm <= 0.60):
            continue
        if river_dist[c.y][c.x] > 3 or not spot_ok(c.x, c.y):
            continue
        coal_spots.append((c.x, c.y))
        taken.append((c.x, c.y))

    # 铀矿：丘陵，高海拔，离海 ≥2，离城市候选 ≥4
    lo, hi = cfg["uranium_count"]
    uranium_need = rng.randint(int(lo), int(hi))
    uranium_spots: list[tuple[int, int]] = []
    for c in sorted(tm.iter_cells(), key=lambda c: -c.elevation):
        if len(uranium_spots) >= uranium_need:
            break
        if c.terrain is not Terrain.HILL or c.norm < 0.45:
            continue
        if sea_dist[c.y][c.x] < 2:
            continue
        if any(max(abs(c.x - cx), abs(c.y - cy)) < 4 for cx, cy in cands):
            continue
        if not spot_ok(c.x, c.y):
            continue
        uranium_spots.append((c.x, c.y))
        taken.append((c.x, c.y))

    # 兜底：有些种子整张图没有丘陵（西部全是荒漠），核燃料不能就此绝种，
    # 退到"离海 ≥2 的最高可建格"，保证铀矿至少有 1 处。
    if not uranium_spots:
        for c in sorted(tm.iter_cells(), key=lambda c: -c.elevation):
            if c.terrain not in (Terrain.PLAIN, Terrain.DESERT):
                continue
            if sea_dist[c.y][c.x] < 2:
                continue
            if any(max(abs(c.x - cx), abs(c.y - cy)) < 4 for cx, cy in cands):
                continue
            if not spot_ok(c.x, c.y):
                continue
            uranium_spots.append((c.x, c.y))
            taken.append((c.x, c.y))
            if len(uranium_spots) >= 1:
                break

    for x, y in coal_spots:
        tm.cell(x, y).coal = round(MINE_RESERVE["coal_mine"] * rng.uniform(0.8, 1.2), 2)
    for x, y in uranium_spots:
        tm.cell(x, y).uranium = round(MINE_RESERVE["uranium_mine"] * rng.uniform(0.8, 1.2), 2)

    # 风光：按地形基准 × 抖动；近海补风、荒漠补光
    for c in tm.iter_cells():
        info = TERRAIN_INFO[c.terrain]
        wind = info.wind * rng.uniform(0.82, 1.24)
        solar = info.solar * rng.uniform(0.82, 1.24)
        if sea_dist[c.y][c.x] <= 2:
            wind += 0.15
        if c.terrain is Terrain.DESERT:
            solar += 0.10
        c.wind = round(wind, 2)
        c.solar = round(solar, 2)
