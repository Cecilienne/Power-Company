# 01 · 地形生成模块（数据契约）

> 状态：**已实现 v1.1（`pwer/terrain.py`，替换掉了手搓地图；差异见 §9）**
> 开局：`Game.new_game()` 不带种子时随机取（1 ~ 2^31-1），种子写进 `Game.seed`，
> 显示在「开局提示」与菜单【帮助 → 玩法与规则】末尾；想复现同一张图就把种子记下来。
> 依赖：无（最底层，只被 `world` / `city` 读取）
> 上游：`pwer/world.py::WorldMap.generate`
> 本文定义：生成流水线、每格输出契约、资源绑定规则、配置字段、验收不变量。

---

## 0. 定位与红线

地形只回答三个问题：**能不能建、造价乘多少、出力打几折**。
不做流体、不做气候、不做逐格侵蚀动画。

**红线（别做）**：
- ❌ 真实水文学（流量/汇流累积/河道宽度）——只用"最陡下降"生成骨架线。
- ❌ 多图层材质/噪声贴图——2D 色块即可。
- ❌ 运行时生成——地图在开局一次性生成，之后只读（城市扩张写的是城市对象，不是地形）。

---

## 1. 输出契约

`WorldMap.generate(seed) -> WorldMap`，对外字段**只增不减**，保证 `world/grid/dispatch/ui` 四处调用点不破。

### 1.1 每格 `Tile`（`pwer/world.py`）

| 字段 | 类型 | 现状 | 契约 | 说明 |
|---|---|---|---|---|
| `x` `y` | int | ✅ | 不变 | |
| `terrain` | `Terrain` | ✅ | 不变 | 只允许 6 个枚举值，**不新增枚举** |
| `elevation` | float | ✅ 已加 | 0~1200（米） | 高程；河道格恒为 0 |
| `moisture` | float | ✅ 已加 | 0~1 | 干湿场，用于荒漠分类 |
| `highland` | bool | ✅ 已加 | | 高原标记（仍是 `HILL`，只是造价更贵） |
| `water` | bool | ✅ 已加 | | 海或河，等价于 `terrain in (RIVER, COAST)`，做成冗余字段方便查询 |
| `wind` | float | ✅ | 0.6~1.6 | 风资源系数 |
| `solar` | float | ✅ | 0.6~1.6 | 光资源系数 |
| `coal` | float | ✅ | ≥0 | 剩余可采储量（吨），只有矿点格 > 0 |
| `uranium` | float | ✅ | ≥0 | 同上 |
| `owner` `kind` `ref` | int/str/int | ✅ | 不变 | 运行时占用信息，生成阶段恒为 `-1 / "" / -1` |

> `water` 与 `highland` 是**派生冗余字段**：可以在 `__post_init__` 里算，但落盘要存，避免存档后规则变化。

### 1.2 地图级 `WorldMap`

| 字段 | 契约 |
|---|---|
| `w` `h` | 默认 18×18（契约尺寸，可配置）；游戏内实跑 60×60 粗网格，再上采样到 480×480（见 §9） |
| `tiles` | `list[list[Tile]]`，`tiles[y][x]` |
| `cities` | 由 02 选址写入；01 只负责产出**候选中心集** `city_candidates` |

### 1.3 落盘 JSON（调试/编辑器用，不是运行时依赖）

```json
{
  "version": 1,
  "seed": 20240501,
  "width": 18,
  "height": 12,
  "tiles": [
    {"x": 0, "y": 0, "elevation": 412.5, "moisture": 0.62,
     "terrain": "hill", "highland": false, "water": false,
     "wind": 1.28, "solar": 0.81, "coal": 0, "uranium": 0}
  ],
  "rivers": [[[3, 1], [4, 1], [5, 2]]],
  "city_candidates": [{"x": 4, "y": 3, "area": 63}]
}
```

---

## 2. 生成流水线（9 步，顺序不可交换）

### S0 · 确定性种子

```python
rng = random.Random(seed ^ 0x5EED)   # 独立 RNG，禁止消耗 game.rng
```

同一 `seed` 必须产出逐格相同的图；生成过程**不得**读 `game` 任何状态。

### S1 · 控制点采样

- 控制点数 `N = 7 + rng.randint(-1, +2)`（6~9 个）。
- 东侧海域锚点 3 个：`x = w-2`，`y ∈ {1, h/2, h-2}`，高程 **0**，权重 ×3（保证 IDW 把海压平）。
- 内部点 `N-3` 个：`x ∈ [0, w-3]`，最小间距 `min(w,h)*0.35`，高程 `200~1100`。
- 高地偏置：至少 1 个内部点落在 `x ≤ w*0.35`（西侧），保证河流有落差可走。

### S2 · 最陡下降 → 河流骨架

对每个"高点"（`elevation ≥ 分位数 0.75` 且非海）：

```
cur = 高点
while cur 不是海/湖:
    下一点 = 8 邻接中 elevation 最小的格（同高程时优先"远离海"的方向，避免贴边走）
    if 下一点 已存在河流: 汇入，结束
    if 下一点 elevation ≥ cur:      # 洼地
        对 [cur, 下一点] 这段做 priority-flood 抬升（+2 米/次，最多 40 次）
        若仍无法下降 → 丢弃这条河
    cur = 下一点
```

- 最短长度 `MIN_RIVER_LEN = 10`，不足则丢弃（避免一格"河"变成免费水电点）。
- 主干 1 条必须成功；支流最多 2 条，起点高程需低于主干中位数。
- **不得**允许河流在原地 2×2 打转；连续 3 步同方向允许，第 4 步必须换向。

### S3 · 河道锁死

河流格 `elevation = 0`、`water = True`、`terrain = RIVER`，并标记 `frozen = True`。
后续 S4/S5 **不得**修改冻结格（这是本模块唯一的硬约束）。

### S4 · IDW 高程插值

```
elev(x,y) = Σ(k=8) w_i·z_i / Σ w_i ,   w_i = 1 / max(d_i, 0.6)^2
```

- `z_i` 为控制点高程，`d_i` 为欧氏距离；取最近的 8 个控制点。
- 结果再叠加两段噪声：
  `elev += 60·fbm(x/6, y/6, octaves=3) + N(0, 18)`，`fbm` 用 `rng` 生成的值噪声双线性插值。
- `clamp(0, 1200)`，保留 1 位小数。
- **蒸发/湿润场**：`moisture = clamp(1.0 - 距海叶氏距离/(w*0.9) + 0.25·近河度 - 0.35·高程归一, 0, 1)`。
  近河度 = `1 / (1 + 到最近河流格的格距)`。

### S5 · 平滑（河道锁死）

```
elev' = (4·c + 2·(上+下+左+右) + 1·(四对角)) / 16
```

迭代 `SMOOTH_ITER = 2` 轮，每轮跳过 `frozen` 格。

### S6 · 高程分级 → 地形枚举

| 条件（按顺序判定，先命中先返回） | terrain |
|---|---|
| `x ≥ w-2`（东侧 2 列） | `COAST` |
| 河流骨架格 | `RIVER` |
| `moisture < 0.30` 且 `elev < 0.55·1200` | `DESERT` |
| `elev_norm ≥ 0.72` | `HILL` + `highland=True` |
| `0.35 ≤ elev_norm < 0.72` | `HILL` |
| 其余 | `PLAIN` |

`elev_norm = elevation / 1200`。

> **不新增 `Terrain` 枚举**。高原、湖泊、湿地都映射到现有 6 类，靠 `highland` / `water` 这类布尔位区分，
> 避免 `TERRAIN_INFO`、`check_build`、UI 配色三处同时改。

### S7 · 连通域面积校验与重分类

对 `PLAIN / HILL / DESERT` 分别做 4 邻连通域：

```
for 每个连通域:
    if 面积 < MIN_REGION(8):
        重分类为该域边界上出现次数最多的地形
        若并列 → 重分类为 PLAIN
    if 该域是 HILL 且 highland 比例 > 0.8:
        标记为"高原区"（仅调试用）
```

- 河流不参与面积校验，但必须校验**连通到海**（下游终点必须是 `COAST` 或已存在水体）。
- 校验失败的河流整条回滚（S2 重来），最多 3 次；仍失败则退化为"从西侧高点直连最近海格"的直线河。

### S8 · 资源绑地形

| 资源 | 允许地形 | 额外约束 | 数量 | 数值 |
|---|---|---|---|---|
| 煤矿 | `PLAIN` / `HILL` | `elev_norm ∈ [0.10, 0.60]`；距河 ≤ 3 格；距城 ≥ 3 | 3 处 | `MINE_RESERVE["coal_mine"] × U(0.8, 1.2)` |
| 铀矿 | `HILL` | `elev_norm ≥ 0.45`；距城 ≥ 4；距海 ≥ 2 | 2~3 处 | `MINE_RESERVE["uranium_mine"] × U(0.8, 1.2)` |
| 风 | 全局 | 近海 `+0.15`，丘陵 `×1.0` | —— | `wind = TERRAIN_INFO.wind × U(0.82, 1.24)` |
| 光 | 全局 | 荒漠 `+0.10` | —— | `solar = TERRAIN_INFO.solar × U(0.82, 1.24)` |
| 水 | `RIVER` | 河道格天然可建水电 | —— | —— |

- 矿点之间最小间距 3 格，且**不得落在城市候选格**。
- 分配完成后把数值保留 2 位小数写入 `Tile`。

### S9 · 后处理

1. **城市候选**：对每个 `PLAIN` 连通域算"连片面积"，取前 4 大域，输出各自质心（若质心被占则取域内距质心最近的格）。
2. **候选互斥**：候选中心 `chebyshev ≥ 4`。
3. **变电站可用性**：每个候选中心 3×3 范围内至少有 2 个非水、非山、非矿格（否则该候选作废，顺延下一个域）。
4. **起始地块保证**：至少 2 个候选中心的 6 格内存在 `PLAIN` 空位可建火电（现有开局 `_starter_site` 依赖）。
5. 写出 `city_candidates`，供 02 建城。

---

## 3. 与现有代码的接口

| 调用方 | 依赖点 | 契约 |
|---|---|---|
| `world.check_build` | `terrain`、`has_water_adjacent`、`coal/uranium`、`wind/solar` 阈值 | 字段名与语义不变 |
| `world.build_cost` | `TERRAIN_INFO[terrain].cost_mult` | 不变（`highland` 的额外造价见 §4 可选） |
| `grid.all_city_supply` | `tiles[].owner/kind`、`cities[].x/y` | 不变 |
| `dispatch.plant_cf` | `tile.wind`、`tile.solar` | 不变 |
| `ui/mapview.py` | `terrain → 颜色` | 不变（不新增枚举，配色表不用改） |
| `02_city.md` | `city_candidates` | 新增只读字段 |

**替换方式**：只重写 `WorldMap.generate` 的函数体，签名与返回类型不变。
`Game.new_game` 里 `WorldMap.generate(seed)` 一行不动。

---

## 4. 配置字段（`config/terrain_config.json`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `width` / `height` | 18 / 12 | 地图尺寸 |
| `control_points` | `[6, 9]` | 控制点数区间 |
| `idw_power` | 2.0 | 距离幂次 |
| `idw_neighbors` | 8 | 参与插值的最近点数 |
| `idw_epsilon` | 0.6 | 距离下限，防除零 |
| `smooth_iter` | 2 | 平滑轮数 |
| `fbm_amplitude` | 60.0 | 分形噪声振幅（米） |
| `elev_max` | 1200.0 | 高程上限 |
| `water_level` | 0.0 | 水面高程（海/河恒 0） |
| `desert_moisture` | 0.30 | 荒漠阈值 |
| `hill_elev` | 0.35 | 丘陵下界（归一化） |
| `highland_elev` | 0.72 | 高原下界 |
| `min_region` | 8 | 连通域最小面积（格） |
| `min_river_len` | 10 | 河流最短长度（格） |
| `max_rivers` | 3 | 主干+支流上限 |
| `coal_count` / `uranium_count` | 3 / `[2,3]` | 矿点数量 |
| `city_candidate_count` | 4 | 城市候选数 |
| `city_min_gap` | 4 | 候选中心最小间距（切比雪夫） |
| `regen_attempts` | 5 | 平原不足时的重生成上限 |

`highland` 的可选造价规则（本期**不实现**，仅预留）：
`cost_mult × (1 + 0.15 × max(0, elev_norm - 0.5))`，写在 `WorldMap.build_cost` 里，配置开关 `highland_cost=True`。

---

## 5. 现状 vs 目标

| 项 | 现状（`world.generate`） | 目标 |
|---|---|---|
| 尺寸 | 硬编码 18×12 | 配置（现默认 18×18） |
| 海 | 东侧固定 2 列 | 同（控制点锚定） |
| 荒漠 | 右上角固定矩形 | moisture 场判定 |
| 丘陵 | 3 个圆形贴片 | 高程分级 |
| 河流 | 从 (3,1) 随机游走到东 + 一条支流 | 最陡下降骨架，可多条 |
| 高程 | 无 | IDW + fbm，0~1200 |
| 面积校验 | 无 | 连通域重分类 |
| 城市 | 4 个硬编码坐标 | `city_candidates` + 02 选址 |
| 矿 | 6 个硬编码坐标 | 地形绑定 + 间距约束 |
| 确定性 | `Random(seed)` | `Random(seed ^ 0x5EED)`（与战斗 RNG 解耦） |

**迁移步骤**（每步都可独立发布）：
1. 加 `elevation/moisture/water/highland` 四个字段（默认值 0），先不改生成逻辑——验证存档兼容。
2. 用流水线替换 `generate`，但**保留旧地图作为 `generate_legacy`**，`--seed legacy` 可回退对比。
3. 城市候选接入 02；旧坐标作为 fallback。
4. 面积校验与重生成打开。

---

## 6. 验收不变量

1. **确定性**：同 seed 两次生成，逐格 `terrain/elevation/coal/uranium/wind/solar` 完全一致。
2. **河道封闭**：每条河的上游起点高程 ≥ 200，下游终点必须是 `COAST`；河道格 `elevation == 0` 且 `water == True`。
3. **无孤立域**：任何 `PLAIN/HILL/DESERT` 连通域面积 ≥ 8。
4. **城市可建**：≥ 3 个候选，且每个候选 3×3 内非水非山格 ≥ 2。
5. **开局可玩**：至少 2 个候选 6 格内有 `PLAIN` 空位可建火电；至少 1 条河长度 ≥ 10 可建水电。
6. **海陆比**：`COAST` 占比 ∈ [8%, 20%]；`PLAIN` 占比 ≥ 35%。
7. **资源不重叠**：矿点互不 8 邻接，且不在城市候选格上。
8. **不越界**：所有坐标 `0 ≤ x < w, 0 ≤ y < h`，`tiles` 为 `h×w` 且无 `None`。

---

## 7. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| 小地图上 IDW 太糊 | 整图一个坡，地形没有层次 | fbm 振幅 60 米 + 分位数分级；不够就把 `idw_neighbors` 降到 4 |
| 河流立刻入海 | 河长只有 2~3 格，水电点位消失 | 强制西侧高点、`MIN_RIVER_LEN=10`、失败重试 3 次后走直线兜底 |
| 面积校验反复重生成 | 开局卡顿 | `regen_attempts=5` 上限，超限放宽 `min_region` 到 5 |
| 城市候选落进山里 | 玩家开局无法建变电站 | S9 第 3/4 条硬校验，候选作废顺延 |
| 新增字段破坏存档 | 旧 `.pkl` 读取报 `AttributeError` | 新字段一律给默认值；`Tile` 用 `dataclass`，`water` 用 `__post_init__` 推导 |
| 与 `city` 模块的循环依赖 | 生成时就要城市，城市又要地形 | 严格分层：01 只出候选，02 只读候选，01 不 import 02 |

---

## 8. 实测（实现后回填）

43 个种子（`1..40` + `99` + `20240501` + `4242`）全部通过 §6 的 8 条不变量：

| 指标 | 实测范围 | 契约要求 |
|---|---|---|
| `COAST` 占比 | 11.1%（恒等于 `2/W`） | 8%~20% |
| `PLAIN` 占比 | 36.1%~53.2%（建城后） | ≥35% |
| 河流条数 / 长度 | 1~2 条，10~15 格 | ≥1 条 ≥10 格 |
| 城市候选 | 4 个，切比雪夫距 ≥4，离边 1 格 | ≥3 |
| 矿 | 煤恒 3 处；铀 1~3 处 | 不重叠、不在城址上 |
| 生成耗时 | 单次 ≤ 8 次重试、合计 < 0.15 s | 开局不卡 |

单元测试 `tests/test_engine.py::TestTerrain` 把上面几条锁成了回归用例
（确定性、海陆比、平原占比、候选 3×3、河道入海、无孤立域、资源存在）。

## 9. 实现说明（与 v1.0 契约的差异）

1. **S4 拆成 S4a / S4b**：S2 的"最陡下降"得先有个能下降的高程场，所以先算一遍
   IDW+fbm（S4a）用于成河，河道锁死（S3）后再平滑（S5），湿度场（S4b）最后算。
   顺序调整不违背"S3 之后不得改河道格"。
2. **城市候选不取质心**：当时的地图（18×12）上一个平原连通域只有一个质心，照搬只能出 1~2 个候选；现在 18×18 同样沿用这条。
   改成"同一连通域内挑离边界最远的格"，仍然满足 §6 第 4 条与 §9 的互斥要求。
3. **建城后再跑一次面积校验**：候选格建城会把平原切开，1~2 格的碎片会冒出来；
   生成时把候选格临时当成 `CITY` 再执行一次 S7，避免这类碎片。
4. **铀矿兜底**：有些种子整张图没有丘陵（西部全是荒漠），铀矿退到"离海 ≥2 的最高
   可建格"，保证至少 1 处，不让核电燃料绝种。
5. **重试加盐**：第 1 次严格用契约的 `Random(seed ^ 0x5EED)`，第 n 次用
   `Random((seed ^ 0x5EED) + n * 7919)`，保证"同 seed 同结果"的同时能让重生成换一张图。
6. **配置实调**：`config/terrain_config.json` 里 `hill_elev` 用 **0.40**（0.35 在 18×18 上
   经常压不出 35% 平原）、`regen_attempts` 用 **8**。其余键与契约默认值一致。

## 10. 代码地图

| 文件 | 作用 |
|---|---|
| `pwer/terrain.py` | S0–S9 全部实现（`DEFAULTS` / `load_config` / `Cell` / `TerrainMap` / `generate`） |
| `pwer/world.py` | `Tile` 接收四个新字段；`WorldMap.generate` 只负责"01 产物 → Tile/City" |
| `config/terrain_config.json` | §4 的配置字段（删掉即回落到 `DEFAULTS`） |
| `tests/test_engine.py::TestTerrain` | §6 不变量的回归测试 |
