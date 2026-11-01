# 02 · 城市模块（数据契约）

> 状态：**设计契约 v1.0（现状为 4 座定点单格城市，待升级为多区块）**
> 依赖：01（`city_candidates`）
> 上游：`pwer/world.py::City`、`pwer/engine.py::_advance_turn`
> 本文定义：区块模型、选址硬边界、扩张意愿与两层扩张、衰退、负荷派生、配置与验收。

---

## 0. 定位与红线

城市是**需求的容器**，也是**信誉的受体**：供电拉胯 → 城市萎缩 → 营收掉 → 股价跌 → 被偷袭。

**红线**：
- ❌ 不做逐小时负荷曲线——一个季度一个平均负荷 + 季节系数（沿用 `SEASON_DEMAND`）。
- ❌ 不做居民个体/交通/地价模拟——区块只有"类型 + 负荷"两个属性。
- ❌ 不让扩张无限加速——每回合最多 1 区块 + 每 2 回合 1 格，防止需求爆炸。

---

## 1. 城市对象与区块模型

```
City
 ├─ center (x, y)          城市中心格（地图上恒为 Terrain.CITY）
 ├─ tiles  [(x,y)...]      城市已占的地图格（初始 1 格，扩张后可到 3 格）
 └─ blocks [Block...]      区块（纯数据，不占地图格；每格最多 6 个区块）
```

> **为什么区块不占地图格**：18×18 的地图也不够 4 座城 × 10 个区块铺开。
> 地图上城市仍是 1~3 格色块，区块只在"城市面板"里以列表呈现。
> 这样既保留了"城市在长"的观感（格子变多），又不吃掉建造用地。

### 1.1 `Block`

| 字段 | 类型 | 说明 |
|---|---|---|
| `bid` | int | 城市内唯一 |
| `type` | str | `res` / `ind` / `com` / `special` / `vacant` |
| `load_mw` | float | 基准负荷（类型定值，见 §1.2） |
| `tile` | `(x,y)` | 归属的城市格 |
| `age` | int | 存在回合数（撤空判定用） |
| `client_tag` | str | `""` / `"ai_datacenter"` / `"green_direct"`，供 04 合同切片 |

### 1.2 区块类型与负荷

| 类型 | 名称 | 基准负荷 | 扩张抽签权重 | 对电价 | 对断电 |
|---|---|---|---|---|---|
| `res` | 居民区 | **55 MW** | 0.45 | 极敏感（弹性 ×1.25） | 极敏感 |
| `ind` | 工业区 | **85 MW** | 0.35 | 不敏感 | 可容忍 |
| `com` | 商业区 | **30 MW** | 0.15 | 中等 | 敏感 |
| `special` | 特殊区 | **130 MW** | 0.05 | 不敏感 | 极敏感 |
| `vacant` | 空地 | 0 | —— | —— | —— |

> `special` 是"AI 数据中心 / 医院 / 算力园"的占位，签约门槛见 04 §3。

### 1.3 初始城市（沿用现有 4 城，负荷校准到 ±10% 以内）

| 城市 | 规模 | 初始区块组合 | 派生负荷 | 现有 `base_mw` |
|---|---|---|---|---|
| 临江 | town | 2 res + 2 ind + 1 com | 310 MW | 300 |
| 北岭 | town | 4 res + 2 ind + 1 com | 420 MW | 420 |
| 钢城 | city | 3 res + 4 ind + 1 com | 535 MW | 560 |
| 滨海 | metropolis | 4 res + 4 ind + 1 com + 1 special | 720 MW | 700 |

规模只影响：扩张速率、特殊区解锁、UI 图标，**不乘负荷**（负荷由区块决定，避免两套倍率）。

---

## 2. 选址（由 01 供候选，02 裁决）

```
候选集 = WorldMap.city_candidates            # 按连片平原面积降序
逐候选检查:
  ① 硬边界：候选中心必须是 PLAIN；3×3 内无 RIVER/COAST/HILL
  ② 间距：与其他城市中心 chebyshev ≥ 4
  ③ 可用性：3×3 内非水非山空位 ≥ 2（能放变电站）
  ④ 开采冲突：不在矿点 8 邻接内
通过 → 建城；不足 3 座 → 放宽 ③ 为 ≥1 并记 warning
```

### 2.1 硬边界（扩张也遵守）

- `HILL` / `highland` / `RIVER` / `COAST` **永不可扩**。
- 主河道（长度 ≥ 20 的河）两侧 1 格内不可扩。
- 城市格之间 `chebyshev ≥ 2`（不能贴脸）。
- 不可扩到已被任何公司占用的格（`owner >= 0`）。
- 城市总格数上限 **3 格**（metropolis 可 4 格）。

---

## 3. 扩张意愿

### 3.1 供电率（近 3 回合加权）

```
supply_rate(t) = 本回合 Σ_公司 served_mw(city) / demand_mw(city)     # 已有字段可算
S = 0.5·supply_rate(t) + 0.3·supply_rate(t-1) + 0.2·supply_rate(t-2)
```

`supply_rate` 由 `settle_production()` 在分流结束后算出并写入 `City.supply_history`（保留 4 个）。
**这是唯一写入点**，城市、信誉、合同都读它。

### 3.2 意愿公式

```
willingness = S × (0.80 + 0.20 × trust_coef) × policy_mult
```

| 项 | 取值 |
|---|---|
| `trust_coef` | 信誉档位映射：优秀 1.0 / 良好 0.8 / 一般 0.6 / 较差 0.4 / 极差 0.2（信誉定义见 04 §7） |
| `policy_mult` | 扩张期 1.03 / 过渡期 1.00 / 转型期 0.98 / 深度脱碳期 1.00 |

### 3.3 四档

| 档位 | 条件 | 效果 |
|---|---|---|
| **衰退** | `willingness < 0.60` | 每回合 1 个区块 → `vacant`；连续 3 回合 < 0.50 → 失去 1 格（保底 1 格） |
| **停滞** | `0.60 ≤ w < 0.85` | 无变化 |
| **稳定扩张** | `0.85 ≤ w < 0.95` | 每 **2** 回合 +1 区块（内部填充优先） |
| **高速扩张** | `w ≥ 0.95` | 每回合 +1 区块；每 **2** 回合 +1 城市格（向外拓界） |

### 3.4 两层扩张

**第一层：向外拓界**（新格）

```
候选格 = 现有城市格的 4 邻接格
过滤：PLAIN、无主、非矿、距其他城市 ≥ 2、未超格数上限
优先：距当前中心"背离已有城市"的方向（让城市朝外长而不是贴脸）
```

**第二层：内部填充**（新区块）

```
if 存在 vacant 区块:  vacant → 按权重抽类型（res 45% / ind 35% / com 15% / special 5%）
else:                  在"区块数最少的城市格"新增 1 个区块
```

- 每回合**最多** 1 次拓界 + 1 次填充；同一回合不得连续拓界。
- `special` 区块不是随机出的：只有满足解锁条件（§3.5）才进入抽签池，否则权重转移给 `ind`。

### 3.5 特殊区解锁（AI 数据中心 / 绿电直供）

```
turn ≥ 18
AND 城市规模 ∈ {city, metropolis}
AND 该城近 4 回合 supply_rate ≥ 0.98
AND 该公司（拥有该城供电份额最高者）trust ≥ 70
```

满足后该城出现 1 个 `special` 区块，`client_tag = "ai_datacenter"`，并在合同面板生成一份可签合同（04 §2）。
**不满足就永远不出现**——这是后期目标，不是开局福利。

---

## 4. 衰退

| 阶段 | 触发 | 表现 |
|---|---|---|
| 轻度 | `w < 0.60` 连续 1 回合 | 1 个区块 → `vacant`（先 `com` → `ind` → `res`，`special` 最后） |
| 中度 | `w < 0.50` 连续 3 回合 | 失去 1 格（该格上区块全部转 `vacant`，格归还地图） |
| 重度 | `demand_mw` 低于初始 40% | 城市"空心化"：UI 显示灰色，`supply_rate` 上限锁 0.85，禁止拓界 |

- 撤空区块保留 `2 回合`（给玩家抢修窗口），之后从 `blocks` 移除。
- 衰退**不影响**已建成的电厂/变电站，只影响需求与合同客户。
- 衰退会通过 §3.1 的 `S` 自我强化——**这就是"供电拉胯 → 城市萎缩"的传导链**，不做额外惩罚。

---

## 5. 负荷派生

```
base_mw   = Σ block.load_mw                      # 派生缓存，不再是独立输入
demand_mw = base_mw × SEASON_DEMAND[season] × game.demand_shock
res_ratio = Σ(load_mw of res blocks) / base_mw   # 替代全局 RES_RATIO
residential = demand_mw × res_ratio
industrial  = demand_mw × (1 - res_ratio)
```

- `base_mw` 每回合由区块重算（`recompute_base()`），**禁止**再写 `base_mw *= 1 + growth`。
- `growth` 字段保留为"上回合相对增速"，只给 UI 显示，不参与结算。
- 区块负荷不再单独乘季节；季节只作用在 `demand_mw`，避免双乘。
- **`RES_RATIO` 降级为兜底常量**：旧存档 / 无区块城市仍用 0.42；
  有区块时居民占比由区块构成派生（例：临江 35.5%、北岭 52.4%、钢城 30.8%、滨海 30.6%）。
  调用点只改一行：`city.refresh_split(city.res_ratio())`。

---

## 6. 数据结构

```python
@dataclass
class Block:
    bid: int
    type: str                # res / ind / com / special / vacant
    load_mw: float           # 基准负荷（类型定值）
    tile: tuple[int, int]
    age: int = 0
    client_tag: str = ""

@dataclass
class City:
    idx: int
    name: str
    x: int
    y: int
    scale: str = "town"                       # village / town / city / metropolis
    blocks: list[Block] = field(default_factory=list)      ★新
    tiles: list[tuple[int, int]] = field(default_factory=list)  ★新（含中心）
    base_mw: float = 0.0                      # 派生缓存（保留，兼容）
    growth: float = 0.0                       # 仅 UI（保留，兼容）
    demand_mw: float = 0.0                    # 派生
    residential: float = 0.0
    industrial: float = 0.0
    served: dict[int, float] = field(default_factory=dict) # 现有
    share: dict[int, float] = field(default_factory=dict)  # 现有
    outage_mw: float = 0.0                    # 现有
    supply_history: list[float] = field(default_factory=list)  ★新（4 个）
    outage_hours: float = 0.0                 ★新（断电时长唯一源之一，见 04 §9）
    last_expand_turn: int = 0                 ★新
    vacant_rounds: int = 0                    ★新
```

**兼容性**：`base_mw / demand_mw / residential / industrial / served / share / outage_mw` 七个字段签名不变，
`dispatch.py`、`ui/app.py` 无需改动即可运行。

---

## 7. 与主循环的接口

| 时点 | 动作 | 写入 |
|---|---|---|
| `settle_production()` C 步末 | 计算并记录 `supply_rate` | `City.supply_history`、`City.outage_hours` |
| `_advance_turn()` J 步 | 跑一次扩张/衰退 | `City.blocks`、`City.tiles`、`base_mw`、`demand_mw` |
| 事件 I 步 | 灾害写 `disaster_result` | 不改城市，只改 `outage_hours` 的输入 |
| 合同 S 步 | 读区块切片 | 只读 `Block.client_tag` / `load_mw` |
| UI | 城市面板 | 只读 |

**顺序约束**：先算 `supply_rate`（C）→ 再结算合同/信誉（S）→ 最后扩张（J）。
否则本回合的供电率会被下一回合的扩张结果污染。

---

## 8. 配置字段（`config/city_config.json`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `block_load` | `{"res":55,"ind":85,"com":30,"special":130}` | 区块基准负荷 MW |
| `block_weight` | `{"res":0.45,"ind":0.35,"com":0.15,"special":0.05}` | 抽签权重 |
| `max_blocks_per_tile` | 6 | 单格区块上限 |
| `max_tiles` | 3 | 城市格上限（metropolis 4） |
| `city_min_gap` | 4 | 城市中心最小间距 |
| `expand_threshold` | `{"decay":0.60,"stable":0.85,"fast":0.95}` | 四档阈值 |
| `stable_interval` | 2 | 稳定扩张每 N 回合 +1 区块 |
| `fast_expand_cap` | 1 | 高速扩张每回合最多新格数（2 回合 1 格 → 用计数器） |
| `trust_coef` | `{"优秀":1.0,"良好":0.8,"一般":0.6,"较差":0.4,"极差":0.2}` | 信誉对意愿的影响 |
| `policy_mult` | `{"扩张期":1.03,"过渡期":1.0,"转型期":0.98,"深度脱碳期":1.0}` | 政策期修正 |
| `decay_floor` | 0.40 | 负重下限（低于则空心化） |
| `vacant_keep_rounds` | 2 | 撤空区块保留回合数 |
| `special_unlock` | `{"turn":18,"supply_rate":0.98,"trust":70}` | 特殊区解锁门槛 |

---

## 9. 现状 vs 目标

| 项 | 现状 | 目标 |
|---|---|---|
| 城市结构 | 4 座，各 1 格，无区块 | 中心 + ≤3 格，区块列表 |
| 需求增长 | `base_mw *= 1 + growth`（0.012/回合，无条件） | 区块驱动的条件增长 |
| 居民/工业拆分 | 全局 `RES_RATIO = 0.42` | 由区块构成派生 `res_ratio()`，`RES_RATIO` 仅作旧档兜底 |
| 供电率反馈 | 无 | `supply_history` → 意愿 → 扩张/衰退 |
| 衰退 | 无 | 三级衰退 |
| 特殊区 | 无 | 满足门槛后出现 |
| 硬边界 | 无（城市不会长） | 山地/水/间距/占用四重约束 |

**迁移步骤**：
1. 给 `City` 加 `blocks/tiles/supply_history` 字段，`base_mw` 仍按旧公式增长（先跑通读写）。
2. `recompute_base()` 上线，`base_mw` 改为派生；此时可能有个位数百分比的数值漂移，用 `tools/sim.py 48` 对比。
3. 打开 `supply_rate` 记录与意愿计算，但仍只影响 `growth` 显示。
4. 打开扩张/衰退；`MAX_TURNS=48` 内城市最多长到 3 格，需重点回归"需求是否爆掉"。

---

## 10. 验收不变量

1. **初始负荷校准**：4 城初始 `base_mw` 与现值（300/420/560/700）偏差 ≤ 10%。
2. **区块守恒**：`Σ block.load_mw == base_mw`（浮点容差 1e-6）。
3. **需求公式唯一**：`demand_mw` 只由 §5 一条公式产生，不存在第二处写入。
4. **扩张上限**：任一 10 回合窗口内，新格数 ≤ 5，新块数 ≤ 10。
5. **不越边界**：所有城市格 `terrain == PLAIN` 且 `owner == -1`；城市中心恒为 `CITY`。
6. **衰退保底**：任何城市 ≥ 1 格、≥ 2 个非 `vacant` 区块、负荷 ≥ 初始 40%。
7. **特殊区不早现**：`turn < 18` 时全图 `special` 区块数 == 0。
8. **存档兼容**：旧档读入时 `blocks` 为空 → 按 `base_mw` 反推生成等价区块（保证 §2 校验通过）。

---

## 11. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| 需求爆炸 | 高速扩张 + 季节峰值，装机永远追不上 | 每回合 1 区块上限 + `fast_expand_cap` 计数器 |
| 衰退螺旋 | 一次限电 → 需求掉 → 收入掉 → 更没钱修 | `decay_floor=0.40` 硬底 + 应急组抢修（04）打断螺旋 |
| 区块数量失控 | 后期一座城 30+ 区块，城市面板刷屏 | 单格 6 块 × 3 格 = 18 块上限；面板分页或折叠 |
| 与合同口径打架 | 合同要求 800MW，城市只有 700MW | 合同 `required_capacity ≤ 该城 demand_mw × 0.6` 硬校验（04 §3） |
| 与旧数值表冲突 | `DESIGN.md` 写的是固定 base_mw | 在 `DESIGN.md` §4 增加一行"城市负荷由区块派生"，不删旧表 |
| 扩张改地形 | 城市格占掉唯一平原，玩家没地方建厂 | 拓界前预检"该格是否在玩家常用建造区"→ 不做，靠玩家自己抢地，保留博弈 |
