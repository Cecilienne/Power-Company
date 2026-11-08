"""《电力公司》集中数值表 —— 所有可调参数都在这里。

单位约定（全工程统一）：
    货币   : 万元
    功率   : MW
    电量   : MWh
    电价   : 元/MWh
    燃料   : 吨；煤 元/吨，铀 元/吨
    时间   : 1 回合 = 1 季度 = 2190 小时
设计原则：任何"看起来像玄学"的数字都必须能追溯到本文件。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------- 时间 / 规模
HOURS_PER_TURN = 2190.0        # 一季度小时数
PEAK_RATIO = 1.35              # 峰值负荷 / 平均负荷
PEAK_HOURS = 420.0             # 一个季度里处于高峰的小时数
START_TURN = 1
MAX_TURNS = 48                 # 12 年，用来兜底触发"任期结束"结算

# ---------------------------------------------------------------- 多城市
SAVE_VERSION = 2
# 两座可玩城市：名字、基础用电需求。第一座开局归玩家，第二座要买。
CITY_DEFS = (("临江", 700.0), ("北岭", 560.0))
# AI 离屏城市：名字、基础需求、初始归属公司 idx。它们只参与全市场结算，没有地图。
AI_CITY_DEFS = (("钢城", 420.0, 1), ("滨海", 300.0, 2))
CITY_LICENSE_COST = 12_000.0   # 1.2 亿，买断第二座城市
CITY_LICENSE_REP = 55.0        # 声望门槛
INTERCITY_LINK_COST = 6_000.0  # 6000 万
INTERCITY_LINK_CAP = 300.0     # MW
INTERCITY_LINK_LOSS = 0.05     # 跨城线损 5%

# ---------------------------------------------------------------- 货币展示
WAN = 1.0
YI = 10_000.0                  # 1 亿 = 10000 万元


def money(v: float) -> str:
    """把万元格式化成中文可读字符串。"""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= YI:
        return f"{sign}{v / YI:,.2f}亿"
    return f"{sign}{v:,.0f}万"


# ---------------------------------------------------------------- 地形
class Terrain(str, Enum):
    PLAIN = "plain"
    HILL = "hill"
    RIVER = "river"
    DESERT = "desert"
    COAST = "coast"
    CITY = "city"


class TerrainInfo:
    __slots__ = ("name", "color", "cost_mult", "wind", "solar")

    def __init__(self, name: str, color: str, cost_mult: float, wind: float, solar: float):
        self.name = name
        self.color = color
        self.cost_mult = cost_mult
        self.wind = wind
        self.solar = solar


TERRAIN_INFO: dict[Terrain, TerrainInfo] = {
    Terrain.PLAIN: TerrainInfo("平原", "#7fa36b", 1.00, 0.95, 1.00),
    Terrain.HILL: TerrainInfo("山地", "#8d7f63", 1.25, 1.25, 0.80),
    Terrain.RIVER: TerrainInfo("河流", "#5d9bc4", 1.05, 0.90, 0.95),
    Terrain.DESERT: TerrainInfo("荒漠", "#d9c489", 1.15, 0.85, 1.35),
    Terrain.COAST: TerrainInfo("近海", "#4f86a8", 1.10, 1.30, 0.85),
    Terrain.CITY: TerrainInfo("城市", "#b9a48f", 9.99, 0.0, 0.0),
}

# 允许水电：只有河流
# 允许核电：需要冷却水（河流/近海）且远离城市

# ---------------------------------------------------------------- 电源类型
@dataclass(frozen=True)
class PlantSpec:
    key: str
    name: str
    short: str
    cap: float              # 单机/单厂容量 MW
    capex_per_mw: float     # 万元/MW
    fixed_per_mw: float     # 万元/MW/季度（折旧 + 运维，投产即扣，不看发电量）
    var_cost: float         # 元/MWh（含燃料，不含碳税）
    co2: float              # t/MWh
    build_turns: int
    base_cf: float          # 基础容量因子
    cf_jitter: float        # 出力波动
    staff: int              # 需要运营班组
    teams: int              # 需要工程队
    water: bool             # 需要冷却水
    terrains: tuple[Terrain, ...]
    fuel: str               # "" / "coal" / "uranium"
    fuel_rate: float        # t/MWh
    color: str
    renewable: bool = False
    life_q: int = 60        # 折旧年限（季度）
    approval: bool = False  # 是否需要审批（核电）


PLANT_SPECS: dict[str, PlantSpec] = {
    "coal": PlantSpec(
        "coal", "燃煤电厂", "煤", 300, 300, 7.0, 224, 0.85, 2, 0.78, 0.05, 8, 2,
        True, (Terrain.PLAIN, Terrain.RIVER, Terrain.COAST),
        "coal", 0.32, "#6B625A",
    ),
    "gas": PlantSpec(
        "gas", "燃气电站", "气", 100, 185, 5.0, 380, 0.40, 1, 0.55, 0.10, 3, 1,
        False, (Terrain.PLAIN, Terrain.DESERT, Terrain.RIVER, Terrain.COAST),
        "", 0.0, "#7D7590",
    ),
    "hydro": PlantSpec(
        "hydro", "水电站", "水", 150, 600, 6.5, 10, 0.0, 3, 0.50, 0.18, 5, 2,
        True, (Terrain.RIVER,), "", 0.0, "#4E7896", renewable=True, life_q=120,
    ),
    "wind": PlantSpec(
        "wind", "风电场", "风", 100, 300, 6.5, 2, 0.0, 2, 0.30, 0.22, 2, 1,
        False, (Terrain.COAST, Terrain.PLAIN), "", 0.0, "#6C9692",
        renewable=True, life_q=60,
    ),
    "solar": PlantSpec(
        "solar", "光伏电站", "光", 80, 300, 6.5, 1, 0.0, 1, 0.19, 0.12, 1, 1,
        False, (Terrain.DESERT, Terrain.PLAIN), "", 0.0, "#B5A36A", renewable=True, life_q=60,
    ),
    "nuclear": PlantSpec(
        "nuclear", "核电站", "核", 600, 750, 12.0, 60, 0.02, 5, 0.90, 0.02, 20, 4,
        True, (Terrain.RIVER, Terrain.COAST), "uranium", 0.02, "#9A6A88",
        life_q=120, approval=True,
    ),
}

# ---------------------------------------------------------------- 基础设施
@dataclass(frozen=True)
class InfraSpec:
    key: str
    name: str
    short: str
    capex: float          # 万元
    build_turns: int
    teams: int
    staff: int
    gateway_mw: float     # 变电容量（线路容量亦用此）
    color: str


INFRA_SPECS: dict[str, InfraSpec] = {
    "substation": InfraSpec("substation", "变电站", "变", 8_000, 1, 1, 2, 180, "#D8D5CB"),
    "line": InfraSpec("line", "输电线路", "线", 1_200, 1, 1, 1, 220, "#8FB0C8"),
    "coal_mine": InfraSpec("coal_mine", "煤矿", "煤", 55_000, 2, 2, 4, 0, "#2B2B2B"),
    "uranium_mine": InfraSpec("uranium_mine", "铀矿", "铀", 42_000, 2, 2, 3, 0, "#7FA08A"),
}

# 矿区单季度产出上限与开采成本
MINE_OUTPUT = {"coal_mine": 150_000.0, "uranium_mine": 25_000.0}   # 吨/季度
MINE_COST_PER_T = {"coal_mine": 430.0, "uranium_mine": 1_900.0}    # 元/吨
MINE_FIXED = {"coal_mine": 1_600.0, "uranium_mine": 1_400.0}       # 万元/季度
MINE_RESERVE = {"coal_mine": 2_400_000.0, "uranium_mine": 400_000.0}

# 地形对建造成本的乘数已在 TERRAIN_INFO.cost_mult

# ---------------------------------------------------------------- 大宗商品
COMMODITY_BASE = {"coal": 700.0, "uranium": 3_000.0}
COMMODITY_VOL = {"coal": 0.11, "uranium": 0.07}
COMMODITY_MIN = {"coal": 380.0, "uranium": 1_600.0}
COMMODITY_MAX = {"coal": 1_600.0, "uranium": 6_000.0}
SPOT_IMPORT_PRICE = 880.0     # 缺口外购电，元/MWh（比自建边际成本贵得多）

# ---------------------------------------------------------------- 人力
WAGE_OPS = 95.0        # 万元/班组/季度
WAGE_ENG = 70.0
HIRE_COST = 380.0      # 万元/人
FIRE_COST = 520.0      # 万元/人（含补偿与舆论成本）
FIRE_REP = 4.0         # 裁员掉声望
STAFF_SHORT_CF = 0.55  # 缺人时出力打最低折扣系数（配合缺口比例）
LAND_COST = 600.0      # 万元/格，再乘地形系数

# ---------------------------------------------------------------- 财务
LOAN_RATE = 0.018          # 季度利率
LOAN_LIMIT_RATIO = 0.65    # 负债 / 总资产 上限
CORP_TAX = 0.25
START_CEO_CASH = 6_000.0
CEO_SALARY = 220.0
INTEGRATION_COST_RATIO = 0.03   # 吸收合并时按目标净资产收 3% 整合费
DIVIDEND_MIN = 0.0
DIVIDEND_MAX = 0.6

# ---------------------------------------------------------------- 评分 / 股价
PE_BASE = 8.0
PE_MIN = 4.0
PE_MAX = 22.0
PRICE_SMOOTH = 0.45
PRICE_NOISE = 0.018
BOOK_ANCHOR = 0.35         # 市净率锚定权重
ACQUISITION_PREMIUM = 0.15  # 现价收购溢价
SHARE_IMPACT = 0.30        # 买入占股本 1% 时的价格冲击

# ---------------------------------------------------------------- 客户竞争
PRICE_ELASTICITY = 2.6     # 价格敏感度指数
RELIABILITY_WEIGHT = 0.55  # 可靠性在客户评分里的权重
OUTAGE_FINE_MULT = 0.35    # 停电罚款 = 未供电量 × 电价 × 该系数
OUTAGE_REP = 6.0
RES_RATIO = 0.42           # 居民负荷占比（对价格与停电更敏感）
RES_SENSITIVITY = 1.25     # 居民的价格敏感度额外乘数

# 季节系数（回合 = 季度，一年循环）：需求、水电来水、风电、光伏
SEASON_DEMAND = (1.02, 1.12, 0.98, 0.94)
SEASON_RAIN = (1.25, 1.10, 0.80, 0.70)
SEASON_WIND = (1.10, 1.00, 0.90, 0.95)
SEASON_SOLAR = (0.85, 1.05, 1.10, 0.90)
SEASON_NAMES = ("春", "夏", "秋", "冬")

# ---------------------------------------------------------------- 政策
@dataclass(frozen=True)
class PolicyStep:
    turn: int
    carbon_tax: float          # 元/t CO2
    renewable_quota: float     # 新能源配额 0~1
    price_cap: float           # 电价上限 元/MWh
    approval_fee: float = 0.0  # 核电审批费用 万元


POLICY_SCHEDULE: tuple[PolicyStep, ...] = (
    PolicyStep(1, 0, 0.00, 1_200),
    PolicyStep(5, 40, 0.05, 1_150),
    PolicyStep(9, 90, 0.12, 1_100),
    PolicyStep(14, 160, 0.20, 1_050),
    PolicyStep(20, 240, 0.30, 1_000),
    PolicyStep(27, 320, 0.42, 960),
)

QUOTA_PENALTY = 250.0      # 万元/每个百分点缺口
QUOTA_FINE_CAP = 0.25      # 配额罚款不超过当期营收的 25%
QUOTA_REP = 1.5
ANTITRUST_SHARE = 0.55     # 市占率红线
ANTITRUST_PRICE_RATIO = 0.98  # 低于全成本 98% 且份额超标 → 触发
ANTITRUST_FINE = 6_000.0
ANTITRUST_TURNS = 3        # 连续触发回合数

# ---------------------------------------------------------------- 收购阶梯
STAKE_ALERT = 0.05
STAKE_BOARD = 0.20
STAKE_CONTROL = 0.51
STAKE_MERGE = 0.75

# ---------------------------------------------------------------- 胜负
WIN_SHARE = 0.50
GREEN_CARBON_INTENSITY = 0.18   # t/MWh
GREEN_RENEWABLE = 0.55

# ---------------------------------------------------------------- 颜色（UI）
# 单一主题：黑底 + 低饱和深蓝 UI。所有界面共用这一套，不再做主题切换。
# 蓝色负责结构/选中/主行动，红色只保留给危险与"跌"，绿色只保留给正常/盈利。
UI_BG = "#1F1F1F"          # 画布底 / 可建区
UI_PANEL = "#101720"       # 面板 / 抽屉
UI_PANEL2 = "#16202B"      # 面板内的二级块（标题条、列表底）
UI_HOVER = "#1D2A38"       # 悬停底
UI_RULE = "#26323F"        # 1px 分隔线
UI_TEXT = "#E7E4DC"
UI_DIM = "#98A1AB"
UI_DIM2 = "#545E69"
UI_BLUE = "#0B3C74"        # 深蓝：主行动 / 时间块 / 选中
UI_BLUE_HI = "#0B3C74"     # 深蓝的悬停档
UI_RED = "#C43E32"         # 低饱和砖红：危险 / 下跌
UI_YELLOW = "#FFD557"      # 低饱和黄铜：警告
UI_SEL = "#29445C"         # 列表选中行底色
UI_ACCENT = "#52B5FC"      # 低饱和浅蓝：数据标题 / 强调
UI_GOOD = "#4EFA93"        # 低饱和灰绿：正常 / 盈利
UI_BAD = UI_RED
UI_WARN = UI_YELLOW
COMPANY_COLORS = ("#6F93B3", "#C43E32", "#4EFA93", "#B3A067")

# ---------------------------------------------------------------- 地图（单一主题）
MAP_BUILD = "#1F1F1F"          # 可建区 = 黑底
MAP_MOUNTAIN = "#C43E32"       # 山地：低饱和砖红，不可建
MAP_MOUNTAIN_DARK = "#5E3631"
MAP_WATER = "#3C7195"          # 水域：低饱和深蓝
MAP_WATER_DARK = "#24455C"
MAP_GRID = "#5E7894"
MAP_SELECT = "#C9DCEA"

# ---------------------------------------------------------------- 版本 / 启动页
VERSION = "1.1.1"
BUILD = "2026.11.01"
MULTIPLAYER_READY = False      # 联机模块尚未接入；启动页状态灯按这个值说话，不写死"可联机"

# 启动页色板（只属于 UI，不参与游戏数值）
UI_START_BG = "#1F1F1F"
UI_START_INK = "#E7E4DC"
UI_START_DIM1 = "#98A1AB"
UI_START_DIM2 = "#545E69"
UI_START_RULE = "#26323F"
UI_START_BLUE = "#0B3C74"
UI_START_BLUE_HI = "#0B3C74"
UI_START_PANEL = "#0B3C74"
UI_START_PANEL2 = "#0B3C74"
UI_START_PANEL_RULE = "#0B3C74"
UI_START_ACCENT = "#52B5FC"
UI_START_RED = "#C43E32"
UI_START_YELLOW = "#FFD557"
UI_START_GREEN = "#4EFA93"
