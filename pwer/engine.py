"""回合引擎：把地形、资产、人力、电价、财报、股价、股权串成一个闭环。

一个回合（=一个季度）的执行顺序：
    A AI 行动（建厂/报价/抄底/反收购）
    B 在建工程推进（工程队不足 → 拖工期）
    C 生产结算：需求分流 → 调度 → 收入成本（dispatch.py）
    D 政策结算：新能源配额、反垄断
    E 股利：按上季度利润与分红比例分给股东（含 CEO 个人账户）
    F 财务结算：折旧、利息、所得税、现金流、破产判定
    G 股价刷新（valuation.py）
    H 控股/被收购判定（ma.py）
    I 随机事件
    J 进入下一回合：政策台阶、天气、商品价格、需求增长
    K 胜负判定
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import ai as ai_mod
from .company import Company, Mine, Plant, Project
from .constants import (
    COMMODITY_BASE,
    COMMODITY_MAX,
    COMMODITY_MIN,
    COMMODITY_VOL,
    CORP_TAX,
    CEO_SALARY,
    GREEN_CARBON_INTENSITY,
    GREEN_RENEWABLE,
    HIRE_COST,
    INFRA_SPECS,
    LOAN_LIMIT_RATIO,
    LOAN_RATE,
    MAX_TURNS,
    MINE_COST_PER_T,
    MINE_OUTPUT,
    PLANT_SPECS,
    SEASON_NAMES,
    START_CEO_CASH,
    WIN_SHARE,
    FIRE_COST,
    FIRE_REP,
    money,
)
from .dispatch import season_index, settle_production
from .events import roll_event
from .ma import CEO, player_stake
from .policy import PolicyState, apply_quota_penalty, check_antitrust, policy_for_turn
from .valuation import update_all_prices
from .world import CITY_RADIUS, City, WorldMap, chebyshev


def _find_tile(world: WorldMap, kind: str, cx: int, cy: int, max_r: int = 14):
    best = None
    best_d = 99
    for t in world.iter_tiles():
        ok, _ = world.check_build(kind, t.x, t.y)
        if not ok:
            continue
        d = chebyshev(t.x, t.y, cx, cy)
        if d < best_d and d <= max_r:
            best, best_d = (t.x, t.y), d
    return best


@dataclass
class Game:
    seed: int = 20240501
    rng: random.Random = field(default_factory=random.Random)
    world: WorldMap = field(default_factory=WorldMap)
    companies: list[Company] = field(default_factory=list)
    player_idx: int = 0
    turn: int = 1
    policy: PolicyState = field(default_factory=PolicyState)
    market: dict[str, float] = field(default_factory=dict)
    market_raw: dict[str, float] = field(default_factory=dict)
    commodity_mod: dict[str, float] = field(default_factory=dict)
    capex_mod: dict[str, float] = field(default_factory=dict)
    weather: dict[str, float] = field(default_factory=dict)
    demand_shock: float = 1.0
    ceo_cash: float = START_CEO_CASH
    ceo_holdings: dict[int, float] = field(default_factory=dict)
    log_lines: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    game_over: bool = False
    result: str = ""
    result_detail: str = ""
    city_cap: dict[int, dict[int, float]] = field(default_factory=dict)
    _next_id: int = 1
    turn_counter: int = 0

    # ------------------------------------------------------------ 构造
    @classmethod
    def new_game(cls, seed: int | None = None) -> "Game":
        """开一局新游戏。seed 为空就随机取一个（想复现就把种子记下来）。"""
        if seed is None:
            seed = random.SystemRandom().randrange(1, 2 ** 31 - 1)
        g = cls(seed=seed)
        g.rng = random.Random(seed)
        g.world = WorldMap.generate(seed)
        g.policy = policy_for_turn(1)
        g.market_raw = dict(COMMODITY_BASE)
        g.commodity_mod = {"coal": 1.0, "uranium": 1.0}
        g.capex_mod = {}
        g.weather = {"rain": 1.0, "wind": 1.0, "solar": 1.0, "heat": 1.0}
        g._refresh_market()
        g.log_lines = []
        g.messages = []
        g._build_companies()
        g.log("=== 《电力公司》开局：你接手了一家地方电力公司 ===")
        g.log(f"现金 {money(g.player.cash)} · 负债 {money(g.player.debt)} · "
              f"装机 {g.player.gen_capacity():.0f}MW · 员工 {g.player.ops_staff} 班组")
        return g

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @property
    def player(self) -> Company:
        return self.companies[self.player_idx]

    def total_demand(self) -> float:
        return sum(c.demand_mw for c in self.world.cities)

    def year_of(self, turn: int | None = None) -> int:
        t = self.turn if turn is None else turn
        return 2025 + (t - 1) // 4

    def turn_label(self) -> str:
        s = SEASON_NAMES[season_index(self.turn)]
        return f"第 {self.turn} 回合 · {self.year_of()}年{s}季"

    # ------------------------------------------------------------ 初始化公司
    def _build_companies(self) -> None:
        defs = [
            ("盛阳电力", 0, "#4fa3ff", True, self.world.cities[0]),
            ("北岭能源", 1, "#e0605e", False, self.world.cities[1]),
            ("滨海控股", 2, "#57c785", False, self.world.cities[3]),
        ]
        for name, idx, _color, is_player, city in defs:
            co = Company(idx=idx, name=name, is_player=is_player)
            co.cash = 80_000.0 if is_player else 46_000.0
            co.debt = 20_000.0 if is_player else 12_000.0
            co.ops_staff = 16 if is_player else 10
            co.eng_teams = 4 if is_player else 2
            co.reputation = 62.0 if is_player else 58.0
            co.payout = 0.35
            co.res_price = 500.0
            co.ind_price = 460.0
            self.companies.append(co)
        # 起始资产
        self._starter_site(self.companies[0], self.world.cities[0], ("coal", "gas"))
        self._starter_site(self.companies[1], self.world.cities[1], ("coal",))
        self._starter_site(self.companies[2], self.world.cities[3], ("coal", "gas"))
        for co in self.companies:
            equity = max(1.0, co.equity())
            co.shares = round(equity / 12.0, 1)
            co.price = 12.0
            co.price_prev = 12.0
            co.price_history = [12.0]
            co.open_candle()

    def _starter_site(self, co: Company, city: City, kinds: tuple[str, ...]) -> None:
        """给一家公司在城市附近配好"电厂 + 变电站 + 输电线"。"""
        for kind in kinds:
            spot = _find_tile(self.world, kind, city.x, city.y, max_r=6)
            if spot is None:
                spot = _find_tile(self.world, kind, city.x, city.y, max_r=14)
            if spot is None:
                continue
            spec = PLANT_SPECS[kind]
            pid = self.next_id()
            p = Plant(pid, kind, spot[0], spot[1], co.idx, spec.cap,
                      capex=spec.capex_per_mw * spec.cap * 1.0,
                      book_value=spec.capex_per_mw * spec.cap, life_q=spec.life_q)
            co.plants.append(p)
            t = self.world.tile(*spot)
            t.owner, t.kind, t.ref = co.idx, kind, p.id

        sub = _find_tile(self.world, "substation", city.x, city.y, max_r=CITY_RADIUS)
        if sub is None:
            sub = _find_tile(self.world, "substation", city.x, city.y, max_r=CITY_RADIUS + 2)
        if sub is None:
            return
        t = self.world.tile(*sub)
        t.owner, t.kind, t.ref = co.idx, "substation", self.next_id()
        self._connect(co, co.plants[0].x, co.plants[0].y, sub[0], sub[1])

    def _connect(self, co: Company, ax: int, ay: int, bx: int, by: int) -> None:
        """沿 L 形路径铺输电线路，保证电厂与变电站连通。"""
        paths = []
        p1 = [(x, ay) for x in range(min(ax, bx), max(ax, bx) + 1)]
        p1 += [(bx, y) for y in range(min(ay, by), max(ay, by) + 1)]
        paths.append(p1)
        p2 = [(ax, y) for y in range(min(ay, by), max(ay, by) + 1)]
        p2 += [(x, by) for x in range(min(ax, bx), max(ax, bx) + 1)]
        paths.append(p2)
        for path in paths:
            if all(self.world.tile(x, y).kind in ("", "line") for x, y in path):
                for x, y in path:
                    t = self.world.tile(x, y)
                    if t.kind == "":
                        t.owner, t.kind, t.ref = co.idx, "line", self.next_id()
                return
        for path in paths:
            if all(self.world.tile(x, y).kind in ("", "line", "city") for x, y in path):
                for x, y in path:
                    t = self.world.tile(x, y)
                    if t.kind == "":
                        t.owner, t.kind, t.ref = co.idx, "line", self.next_id()
                return

    # ------------------------------------------------------------ 日志
    def log(self, msg: str) -> None:
        self.log_lines.append(f"[{self.turn:02d}] {msg}")
        if len(self.log_lines) > 400:
            self.log_lines = self.log_lines[-400:]
        self.messages.append(msg)

    # ------------------------------------------------------------ 建造
    def build_cost(self, co: Company, kind: str, x: int, y: int) -> float:
        c = self.world.build_cost(kind, x, y)
        if kind in self.capex_mod:
            c *= self.capex_mod[kind]
        return c

    def total_build_cost(self, co: Company, kind: str, x: int, y: int) -> float:
        return self.build_cost(co, kind, x, y) + self.world.land_cost(x, y)

    def can_build(self, co: Company, kind: str, x: int, y: int) -> tuple[bool, str]:
        ok, why = self.world.check_build(kind, x, y)
        if not ok:
            return False, why
        if kind == "nuclear" and not self.policy.nuclear_approved:
            return False, "核电尚未核准（等政策放开）"
        need = self.total_build_cost(co, kind, x, y)
        if co.cash < need:
            return False, f"现金不足（需 {money(need)}）"
        if kind == "nuclear" and co.cash < need * 1.3:
            return False, "核电项目要求充足的资金储备"
        return True, ""

    def build(self, co: Company, kind: str, x: int, y: int) -> bool:
        ok, why = self.can_build(co, kind, x, y)
        if not ok:
            self.messages.append(f"无法建造：{why}")
            return False
        capex = self.build_cost(co, kind, x, y)
        land = self.world.land_cost(x, y)
        total = capex + land
        co.cash -= total
        if kind in PLANT_SPECS:
            spec = PLANT_SPECS[kind]
            turns = spec.build_turns
            teams = spec.teams
        else:
            spec = INFRA_SPECS[kind]
            turns = spec.build_turns
            teams = spec.teams
        pid = self.next_id()
        pr = Project(pid, kind, x, y, co.idx, capex=capex, turns_total=turns,
                     turns_left=turns, spent=capex, teams=teams)
        co.projects.append(pr)
        t = self.world.tile(x, y)
        t.owner, t.kind, t.ref = co.idx, kind, -pid - 1
        self.log(f"{co.name} 开工 {spec.name}（{x},{y}），投资 {money(total)}，工期 {turns} 回合")
        return True

    # ------------------------------------------------------------ 人力
    def hire(self, co: Company, ops: int = 0, eng: int = 0) -> float:
        cost = (ops + eng) * HIRE_COST
        if co.cash < cost:
            raise ValueError(f"现金不足，招聘需要 {money(cost)}")
        co.cash -= cost
        co.ops_staff += ops
        co.eng_teams += eng
        self.log(f"{co.name} 新招 {ops} 个运营班组 / {eng} 支工程队，支付 {money(cost)}")
        return cost

    def fire(self, co: Company, ops: int = 0, eng: int = 0) -> float:
        ops = min(ops, co.ops_staff)
        eng = min(eng, co.eng_teams)
        cost = (ops + eng) * FIRE_COST
        co.cash -= cost
        co.ops_staff -= ops
        co.eng_teams -= eng
        co.reputation = max(0.0, co.reputation - FIRE_REP * (ops + eng) / 4.0)
        self.log(f"{co.name} 裁撤 {ops} 班组 / {eng} 工程队，补偿 {money(cost)}，声望受损")
        return cost

    # ------------------------------------------------------------ 借款
    def borrow(self, co: Company, amount: float) -> float:
        limit = co.assets() * LOAN_LIMIT_RATIO - co.debt
        amount = max(0.0, min(amount, limit))
        if amount < 100:
            raise ValueError("已达负债上限或金额过小")
        co.debt += amount
        co.cash += amount
        self.log(f"{co.name} 借款 {money(amount)}，负债率升至 {co.debt_ratio():.0%}")
        return amount

    def repay(self, co: Company, amount: float) -> float:
        amount = max(0.0, min(amount, co.debt, co.cash))
        if amount < 100:
            raise ValueError("可还款金额不足")
        co.debt -= amount
        co.cash -= amount
        self.log(f"{co.name} 还款 {money(amount)}")
        return amount

    # ------------------------------------------------------------ 工程推进
    def advance_projects(self) -> None:
        for co in self.companies:
            if not co.alive or co.delisted or not co.projects:
                continue
            need = sum(p.teams for p in co.projects)
            ratio = min(1.0, co.eng_teams / need) if need else 1.0
            for pr in list(co.projects):
                pr.progress += ratio
                pr.turns_left = max(0, pr.turns_total - int(pr.progress))
                if ratio < 1.0:
                    pr.stalled_turns += 1
                if pr.progress + 1e-9 >= pr.turns_total:
                    self._complete(co, pr)

    def _complete(self, co: Company, pr: Project) -> None:
        tile = self.world.tile(pr.x, pr.y)
        co.projects.remove(pr)
        if pr.kind in PLANT_SPECS:
            spec = PLANT_SPECS[pr.kind]
            pid = self.next_id()
            p = Plant(pid, pr.kind, pr.x, pr.y, co.idx, spec.cap, pr.capex,
                      build_turn=self.turn, book_value=pr.capex, life_q=spec.life_q)
            co.plants.append(p)
            tile.ref = p.id
            self.log(f"✔ {co.name} 的{spec.name}（{spec.cap:.0f}MW）投产，"
                     f"每季度固定成本 {p.fixed_q:,.0f}万")
        elif pr.kind in ("coal_mine", "uranium_mine"):
            mid = self.next_id()
            reserve = tile.coal if pr.kind == "coal_mine" else tile.uranium
            m = Mine(mid, pr.kind, pr.x, pr.y, co.idx, pr.capex, pr.capex,
                     reserve=reserve, output=MINE_OUTPUT[pr.kind],
                     cost_per_t=MINE_COST_PER_T[pr.kind], build_turn=self.turn)
            co.mines.append(m)
            tile.ref = mid
            self.log(f"✔ {co.name} 的{INFRA_SPECS[pr.kind].name}投产，"
                     f"储量 {reserve:,.0f} 吨")
        else:
            tile.ref = self.next_id()
            self.log(f"✔ {co.name} 的{INFRA_SPECS[pr.kind].name}完工（{pr.x},{pr.y}）")

    # ------------------------------------------------------------ 市场价格
    def _refresh_market(self) -> None:
        self.market["coal"] = self.market_raw["coal"] * self.commodity_mod["coal"]
        self.market["uranium"] = self.market_raw["uranium"] * self.commodity_mod["uranium"]

    def _walk_market(self) -> None:
        for k in ("coal", "uranium"):
            raw = self.market_raw[k]
            drift = self.rng.gauss(0.0, COMMODITY_VOL[k])
            revert = (COMMODITY_BASE[k] - raw) / COMMODITY_BASE[k] * 0.18
            raw *= 1.0 + drift + revert
            raw = max(COMMODITY_MIN[k], min(COMMODITY_MAX[k], raw))
            self.market_raw[k] = raw
        for k in self.commodity_mod:
            mod = self.commodity_mod[k]
            if abs(mod - 1.0) > 1e-6:
                self.commodity_mod[k] = 1.0 + (mod - 1.0) * 0.55
        self._refresh_market()

    # ------------------------------------------------------------ 股利
    def collect_dividends(self) -> None:
        for co in self.companies:
            if not co.alive or co.delisted:
                continue
            last_profit = co.profit_history[-1] if co.profit_history else 0.0
            if last_profit <= 0 or co.payout <= 0:
                continue
            amount = last_profit * co.payout
            amount = min(amount, max(0.0, co.cash) * 0.6)
            if amount < 10:
                continue
            co.cash -= amount
            co.dividends_paid = amount
            for holder_co in self.companies:
                if holder_co.idx == co.idx:
                    continue
                frac = holder_co.holdings.get(co.idx, 0.0) / max(1.0, co.shares)
                if frac <= 0:
                    continue
                got = amount * frac
                holder_co.cash += got
                holder_co.last_income.investment_income += got
            frac = self.ceo_holdings.get(co.idx, 0.0) / max(1.0, co.shares)
            if frac > 0:
                self.ceo_cash += amount * frac
            if co.is_player:
                self.log(f"你宣布分红 {money(amount)}（分红率 {co.payout:.0%}）")

    # ------------------------------------------------------------ 财务结算
    def settle_finance(self) -> None:
        for co in self.companies:
            if not co.alive or co.delisted:
                continue
            st = co.last_income
            st.depreciation = co.tick_depreciation()
            st.interest = co.debt * LOAN_RATE
            pretax = (st.revenue - st.operating_cost - st.depreciation
                      - st.fines - st.interest)
            st.tax = max(0.0, pretax) * CORP_TAX
            np_ = st.net_profit
            co.cash += np_ + st.depreciation
            co.profit_history.append(np_)
            if len(co.profit_history) > 24:
                co.profit_history = co.profit_history[-24:]
            # 现金窟窿 → 紧急借款；借不到就破产
            if co.cash < 0:
                limit = co.assets() * LOAN_LIMIT_RATIO - co.debt
                if limit >= -co.cash:
                    co.debt += -co.cash
                    co.cash = 0.0
                    if co.is_player:
                        self.log("⚠ 现金流为负，被迫紧急借款，利息负担上升")
                else:
                    self._bankrupt(co)

    def _bankrupt(self, co: Company) -> None:
        co.alive = False
        co.delisted = True
        self.log(f"💀 {co.name} 资金链断裂，资不抵债，宣告破产")
        if co.is_player:
            self.game_over = True
            self.result = "defeat"
            self.result_detail = "你的公司资不抵债，宣告破产。"

    # ------------------------------------------------------------ 控股检查
    def check_control(self) -> None:
        player = self.player
        for co in self.companies:
            if not co.alive or co.delisted or co.idx == player.idx:
                continue
            st = player_stake(self, co)
            if st >= 0.51 and co.controlled_by != player.idx:
                co.controlled_by = player.idx
                co.payout = 1.0
                self.log(f"★ 你已控股 {co.name}（{st:.1%}），可强制分红抽血、淘汰其落后机组")
            # AI 反向收购玩家
            ai_stake = co.holdings.get(player.idx, 0.0) / max(1.0, player.shares)
            if ai_stake >= 0.51:
                self.game_over = True
                self.result = "defeat"
                self.result_detail = f"{co.name} 已持有你 {ai_stake:.1%} 的股权，你被收购，公司沦为子公司。"
                self.log("💀 " + self.result_detail)
                return

    # ------------------------------------------------------------ 主循环
    def end_turn(self) -> list[str]:
        """结束本回合，返回本回合产生的消息。"""
        if self.game_over:
            return []
        self.messages = []
        self.turn_counter += 1

        # A. AI 行动
        for co in self.companies:
            if co.is_player or not co.alive or co.delisted:
                continue
            if co.controlled_by == self.player_idx:
                continue
            ai_mod.ai_turn(self, co)

        # B. 工程推进
        self.advance_projects()

        # C. 生产结算
        settle_production(self)

        # D. 政策
        for co in self.companies:
            if co.alive and not co.delisted:
                apply_quota_penalty(self, co)
        for msg in check_antitrust(self):
            self.log(msg)

        # E. 股利（按上季度利润）
        self.collect_dividends()

        # F. 财务
        self.settle_finance()

        # G. 股价
        update_all_prices(self)

        # H. 控股/被收购
        self.check_control()
        if self.game_over:
            return self.messages

        # I. 随机事件
        ev = roll_event(self)
        if ev is not None:
            msg = ev.apply(self)
            self.log(f"【{ev.name}】{msg}")

        # J. 进入下一回合
        self._advance_turn()

        # K. 胜负
        self._check_end()
        return self.messages

    def _advance_turn(self) -> None:
        self.turn += 1
        self.policy = policy_for_turn(self.turn)
        self.demand_shock = 1.0
        self.weather = {"rain": 1.0, "wind": 1.0, "solar": 1.0, "heat": 1.0}
        for city in self.world.cities:
            city.base_mw *= 1.0 + city.growth
        self._walk_market()
        for co in self.companies:
            co.open_candle()             # 新回合：以上回合收盘价为开盘价
            for p in co.plants:
                if not p.online:
                    p.outage_turns -= 1
                    if p.outage_turns <= 0:
                        p.online = True
                        self.log(f"{co.name} 的{p.name}恢复并网")
            # 声望向 60 回归
            co.reputation += (60.0 - co.reputation) * 0.06
            if co.banned_acq_turns > 0:
                co.banned_acq_turns -= 1
            co.risk_flags = [f for f in co.risk_flags if f]
        self.ceo_cash += CEO_SALARY

    def _check_end(self) -> None:
        player = self.player
        if not player.alive:
            return
        total = self.total_demand() or 1.0
        share = self.player_group_mw() / total
        listed = [c for c in self.companies if c.alive and not c.delisted]
        rank = sorted(listed, key=lambda c: c.market_cap(), reverse=True)
        top = rank[0] if rank else player
        if share >= WIN_SHARE and top.idx == player.idx:
            self.game_over = True
            self.result = "victory_dominance"
            self.result_detail = (f"你在全市场占有 {share:.1%} 的供电份额，市值 {money(player.market_cap())}，"
                                  f"成为区域能源垄断巨头。")
        elif (player.renewable_share() >= GREEN_RENEWABLE
              and player.carbon_intensity() <= GREEN_CARBON_INTENSITY
              and self.policy.carbon_tax >= 240):
            self.game_over = True
            self.result = "victory_green"
            self.result_detail = (f"新能源占比 {player.renewable_share():.0%}，"
                                  f"碳强度 {player.carbon_intensity() * 1000:.0f}g/kWh，完成绿色转型。")
        elif self.turn > MAX_TURNS:
            self.game_over = True
            self.result = "timeout"
            self.result_detail = "任期结束，未能达成垄断或绿色转型目标。"

    # ------------------------------------------------------------ 统计
    def player_group_mw(self) -> float:
        total = 0.0
        for co in self.companies:
            if not co.alive or co.delisted:
                continue
            if co.idx == self.player_idx or co.controlled_by == self.player_idx:
                total += co.served_mw
        return total

    def player_share(self) -> float:
        return self.player_group_mw() / max(1.0, self.total_demand())

    def listed(self) -> list[Company]:
        return [c for c in self.companies if c.alive and not c.delisted]

    def score(self) -> float:
        return self.player.market_cap() + self.player.equity()
