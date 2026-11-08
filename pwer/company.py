"""公司、资产与财务报表。

三张表的关系：
    1 生产结算 → IncomeStatement（利润表）
    2 资产折旧 + 现金流 → 资产负债表（简化：资产账面 - 负债 = 净资产）
    3 净资产/利润 → 股价（valuation.py）
所有"重资产压力"都来自这里：fixed_cost 不看发电量，投产即扣。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from .constants import (
    INFRA_SPECS,
    MINE_FIXED,
    PLANT_SPECS,
    WAGE_ENG,
    WAGE_OPS,
    money,
)


@dataclass
class Plant:
    id: int
    kind: str
    x: int
    y: int
    owner: int
    cap: float
    capex: float
    city: int = 0
    build_turn: int = 0
    book_value: float = 0.0
    life_q: float = 60.0
    # 运行时状态
    online: bool = True
    outage_turns: int = 0
    cf_actual: float = 0.0
    available_mwh: float = 0.0
    dispatched_mwh: float = 0.0

    @property
    def spec(self):
        return PLANT_SPECS[self.kind]

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def dep_q(self) -> float:
        return self.capex / max(1.0, self.life_q)

    @property
    def fixed_q(self) -> float:
        return self.spec.fixed_per_mw * self.cap

    @property
    def oam_q(self) -> float:
        return max(0.0, self.fixed_q - self.dep_q)


@dataclass
class Mine:
    id: int
    kind: str
    x: int
    y: int
    owner: int
    capex: float
    book_value: float
    reserve: float
    output: float
    cost_per_t: float
    city: int = 0
    build_turn: int = 0
    life_q: float = 80.0
    produced_last: float = 0.0

    @property
    def spec(self):
        return INFRA_SPECS[self.kind]

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def dep_q(self) -> float:
        return self.capex / max(1.0, self.life_q)

    @property
    def fixed_q(self) -> float:
        return MINE_FIXED[self.kind]


@dataclass
class Project:
    """在建工程：每回合按 capex/工期 付款，工程队不足会拖工期。"""

    id: int
    kind: str
    x: int
    y: int
    owner: int
    capex: float
    turns_total: int
    turns_left: int
    spent: float = 0.0
    teams: int = 1
    progress: float = 0.0
    stalled_turns: int = 0
    city: int = 0

    @property
    def name(self) -> str:
        if self.kind in PLANT_SPECS:
            return PLANT_SPECS[self.kind].name
        if self.kind in INFRA_SPECS:
            return INFRA_SPECS[self.kind].name
        return self.kind

    @property
    def per_turn(self) -> float:
        return self.capex / max(1, self.turns_total)


@dataclass
class IncomeStatement:
    revenue_res: float = 0.0
    revenue_ind: float = 0.0
    investment_income: float = 0.0
    fuel: float = 0.0
    spot: float = 0.0
    fixed: float = 0.0
    depreciation: float = 0.0
    labor: float = 0.0
    grid: float = 0.0
    carbon: float = 0.0
    fines: float = 0.0
    interest: float = 0.0
    tax: float = 0.0
    # 结算辅助数据（不参与利润计算，供 UI 展示）
    served_mwh: float = 0.0
    generated_mwh: float = 0.0
    emissions: float = 0.0
    renewable_mwh: float = 0.0
    outage_mwh: float = 0.0
    unserved_fine: float = 0.0

    @property
    def revenue(self) -> float:
        return self.revenue_res + self.revenue_ind + self.investment_income

    @property
    def operating_cost(self) -> float:
        return self.fuel + self.spot + self.fixed + self.labor + self.grid + self.carbon

    @property
    def total_cost(self) -> float:
        return self.operating_cost + self.depreciation + self.fines + self.interest + self.tax

    @property
    def net_profit(self) -> float:
        return self.revenue - self.total_cost

    @property
    def ebitda(self) -> float:
        return self.revenue - self.operating_cost

    def rows(self) -> list[tuple[str, float, bool]]:
        """给 UI 用的行：(名称, 值, 是否为成本行)。"""
        return [
            ("居民售电收入", self.revenue_res, False),
            ("工业售电收入", self.revenue_ind, False),
            ("投资收益/股利", self.investment_income, False),
            ("燃料成本", -self.fuel, True),
            ("外购电成本", -self.spot, True),
            ("固定成本(运维)", -self.fixed, True),
            ("折旧", -self.depreciation, True),
            ("人力工资", -self.labor, True),
            ("电网维护", -self.grid, True),
            ("碳税", -self.carbon, True),
            ("罚款/罚款", -self.fines, True),
            ("利息", -self.interest, True),
            ("所得税", -self.tax, True),
        ]


@dataclass
class Company:
    idx: int
    name: str
    account_name: str = ""      # 玩家账户名（AI 公司为空；创建页写入）
    is_player: bool = False
    cash: float = 0.0
    debt: float = 0.0
    plants: list[Plant] = field(default_factory=list)
    mines: list[Mine] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    ops_staff: int = 0
    eng_teams: int = 0
    shares: float = 0.0
    price: float = 12.0
    price_prev: float = 12.0
    price_history: list[float] = field(default_factory=list)
    # 行情：每回合一根 K 线 (开, 高, 低, 收)。开盘=上回合收盘，影线来自本回合内的所有价格变动
    candles: list[tuple[float, float, float, float]] = field(default_factory=list)
    turn_open: float = 0.0
    turn_high: float = 0.0
    turn_low: float = 0.0
    fair_value: float = 12.0
    reputation: float = 60.0
    payout: float = 0.35
    alive: bool = True
    delisted: bool = False
    controlled_by: int = -1        # 被哪家公司控股（>=51%）
    res_price: float = 520.0
    ind_price: float = 470.0
    profit_history: list[float] = field(default_factory=list)
    last_income: IncomeStatement = field(default_factory=IncomeStatement)
    holdings: dict[int, float] = field(default_factory=dict)   # 本公司持有的他公司股数
    ai_aggression: float = 0.3
    ai_target_price: float = 0.0
    dividends_paid: float = 0.0
    total_fines: float = 0.0
    grid_capacity: float = 0.0     # 上一回合并网可交付容量（UI 展示）
    served_mw: float = 0.0
    market_share: float = 0.0
    risk_flags: list[str] = field(default_factory=list)
    ai_alerted: bool = False
    banned_acq_turns: int = 0
    city_served: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    home_city: int = -1           # AI 公司的离屏城市；玩家为 -1

    # ------------------------------------------------------------ 行情（K 线）
    def note_price(self, p: float | None = None) -> None:
        """记录本回合内的价格极值——K 线上下影线的唯一来源。"""
        p = self.price if p is None else p
        if self.turn_high <= 0:
            self.turn_high = p
        if self.turn_low <= 0:
            self.turn_low = p
        self.turn_high = max(self.turn_high, p)
        self.turn_low = min(self.turn_low, p)

    def open_candle(self) -> None:
        """回合开始：以当前价为开盘价，重置本回合高低。"""
        self.turn_open = self.price
        self.turn_high = self.price
        self.turn_low = self.price

    def close_candle(self) -> None:
        """回合结束：结算成一根 K 线。"""
        self.note_price()
        o = self.turn_open or self.price
        hi = max(self.turn_high, o, self.price)
        lo = min(self.turn_low or self.price, o, self.price)

        # 影线：引擎每回合只结算一次价格，若不补影线，所有 K 线都会退化成"光头光脚"的柱子。
        # 这里按本回合的实体幅度确定性地补一段上下影（不消耗随机数、不参与结算），
        # 玩家买卖造成的真实冲击已经在 turn_high/turn_low 里，优先级更高。
        body = abs(self.price - o)
        if body > 1e-9:
            r = random.Random(f"wick-{self.idx}-{o:.4f}-{self.price:.4f}")
            hi = max(hi, max(o, self.price) + body * (0.10 + 0.45 * r.random()))
            lo = min(lo, min(o, self.price) - body * (0.10 + 0.45 * r.random()))

        self.candles.append((o, hi, lo, self.price))
        if len(self.candles) > 40:
            del self.candles[:-40]

    def __setstate__(self, state) -> None:
        """旧存档兼容：补齐后来才加的行情字段，避免读档后 AttributeError。"""
        self.__dict__.update(state)
        self.__dict__.setdefault("candles", [])
        self.__dict__.setdefault("account_name", "")
        self.__dict__.setdefault("home_city", -1)
        if getattr(self, "turn_open", 0.0) <= 0:
            self.turn_open = self.price
        if getattr(self, "turn_high", 0.0) <= 0:
            self.turn_high = self.price
        if getattr(self, "turn_low", 0.0) <= 0:
            self.turn_low = self.price

    # ------------------------------------------------------------ 资产
    def assets(self) -> float:
        return (
            self.cash
            + sum(p.book_value for p in self.plants)
            + sum(m.book_value for m in self.mines)
            + sum(pr.spent for pr in self.projects)
        )

    def equity(self) -> float:
        return self.assets() - self.debt

    def market_cap(self) -> float:
        return self.shares * self.price

    def book_per_share(self) -> float:
        return self.equity() / max(1.0, self.shares)

    def debt_ratio(self) -> float:
        a = self.assets()
        return self.debt / a if a > 0 else 0.0

    # ------------------------------------------------------------ 人力
    def staff_need(self) -> int:
        n = sum(p.spec.staff for p in self.plants if p.online)
        n += sum(m.spec.staff for m in self.mines)
        return n

    def staff_ratio(self) -> float:
        need = self.staff_need()
        if need <= 0:
            return 1.0
        return min(1.0, self.ops_staff / need)

    # ------------------------------------------------------------ 成本
    def fixed_cost_q(self) -> float:
        return sum(p.fixed_q for p in self.plants) + sum(m.fixed_q for m in self.mines)

    def depreciation_q(self) -> float:
        return sum(p.dep_q for p in self.plants) + sum(m.dep_q for m in self.mines)

    def labor_cost_q(self) -> float:
        return self.ops_staff * WAGE_OPS + self.eng_teams * WAGE_ENG

    def grid_maintenance_q(self) -> float:
        return 40.0 + 6.0 * sum(1 for p in self.plants)

    def gen_capacity(self) -> float:
        return sum(p.cap for p in self.plants if p.online)

    def carbon_intensity(self) -> float:
        g = self.last_income.generated_mwh
        if g <= 0:
            return 0.0
        return self.last_income.emissions / g

    def renewable_share(self) -> float:
        g = self.last_income.generated_mwh
        if g <= 0:
            return 0.0
        return self.last_income.renewable_mwh / g

    # ------------------------------------------------------------ 持股
    def stake_in(self, other: "Company") -> float:
        return self.holdings.get(other.idx, 0.0) / max(1.0, other.shares)

    def summary_line(self) -> str:
        return (
            f"{self.name} 现金{money(self.cash)} 负债{money(self.debt)} "
            f"股{self.price:.2f} 净资产{money(self.equity())}"
        )

    def add_project(self, proj: Project) -> None:
        self.projects.append(proj)

    def tick_depreciation(self, q: float = 1.0) -> float:
        """按季度折旧并返回本期折旧额。"""
        total = 0.0
        for p in self.plants:
            d = p.dep_q * q
            total += d
            p.book_value = max(0.0, p.book_value - d)
        for m in self.mines:
            d = m.dep_q * q
            total += d
            m.book_value = max(0.0, m.book_value - d)
        return total

    def best_plant_by(self, key) -> Optional[Plant]:
        plants = [p for p in self.plants if p.online]
        if not plants:
            return None
        return max(plants, key=key)
