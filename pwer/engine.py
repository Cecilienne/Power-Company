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
    AI_CITY_DEFS,
    CITY_DEFS,
    CITY_LICENSE_COST,
    CITY_LICENSE_REP,
    COMMODITY_BASE,
    COMMODITY_MAX,
    COMMODITY_MIN,
    COMMODITY_VOL,
    CORP_TAX,
    DAYS_PER_QUARTER,
    CEO_SALARY,
    GREEN_CARBON_INTENSITY,
    GREEN_RENEWABLE,
    HIRE_COST,
    INFRA_SPECS,
    INTERCITY_LINK_CAP,
    INTERCITY_LINK_COST,
    INTERCITY_LINK_LOSS,
    LOAN_LIMIT_RATIO,
    LOAN_RATE,
    MAX_TURNS,
    MINE_COST_PER_T,
    MINE_OUTPUT,
    PLANT_SPECS,
    SAVE_VERSION,
    SEASON_NAMES,
    START_CEO_CASH,
    WIN_SHARE,
    FIRE_COST,
    FIRE_REP,
    money,
)
from .dispatch import season_index, settle_production
from .events import EVENT_BY_KEY, MAJOR_NEGATIVE_KEYS, roll_event
from .ma import CEO, player_stake
from .policy import PolicyState, apply_quota_penalty, check_antitrust, policy_for_turn
from .valuation import update_all_prices
from .world import CITY_RADIUS, City, WorldMap, chebyshev


def _find_tile(world: WorldMap, kind: str, cx: int, cy: int, max_r: int = 14):
    """在 (cx,cy) 周围的环上找第一个可建格（大图上不能扫全图）。"""
    hit = world.find_tiles(kind, limit=1, near=(cx, cy), radius=max_r)
    return hit[0] if hit else None


@dataclass
class InterCityLink:
    a: int
    b: int
    capacity: float = INTERCITY_LINK_CAP
    loss: float = INTERCITY_LINK_LOSS


@dataclass
class Game:
    seed: int = 20240501
    rng: random.Random = field(default_factory=random.Random)
    world: WorldMap = field(default_factory=WorldMap)
    cities: list[City] = field(default_factory=list)
    worlds: list[WorldMap | None] = field(default_factory=list)
    current_city: int = 0
    links: list[InterCityLink] = field(default_factory=list)
    version: int = SAVE_VERSION
    companies: list[Company] = field(default_factory=list)
    player_idx: int = 0
    turn: int = 1
    day: int = 1
    clock_paused: bool = True
    pause_reason: str = "准备阶段"
    cash_warned: bool = False
    event_day: int = 0
    event_key: str = ""
    event_seq: int = 0
    last_event_name: str = ""
    quarter_report: str = ""
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
    def new_game(cls, seed: int | None = None, world: "WorldMap | None" = None,
                 company_name: str | None = None,
                 account_name: str | None = None) -> "Game":
        """开一局新游戏。seed 为空就随机取一个（想复现就把种子记下来）。

        `world` 可以塞一张现成的地图（测试里复用，省掉每次 1.5s 的生成）。
        公司名/账户名来自启动页的命名页；为空时沿用默认的“盛阳电力 / CEO”。
        """
        if seed is None:
            seed = random.SystemRandom().randrange(1, 2 ** 31 - 1)
        g = cls(seed=seed)
        g.rng = random.Random(seed)
        w0 = world if world is not None else WorldMap.generate(seed, city_idx=0)
        g.worlds = [w0, None]
        g.world = w0
        g.current_city = 0
        c0 = w0.cities[0] if w0.cities else City(0, CITY_DEFS[0][0], -1, -1, CITY_DEFS[0][1])
        c0.owner = 0
        c0.unlocked = True
        c0.base_mw = c0.base_mw or CITY_DEFS[0][1]
        c0.demand_mw = c0.base_mw
        c1 = City(1, CITY_DEFS[1][0], -1, -1, CITY_DEFS[1][1])
        c1.demand_mw = c1.base_mw
        g.cities = [c0, c1]
        for i, (name, base, owner) in enumerate(AI_CITY_DEFS):
            c = City(100 + i, name, -1, -1, base, ai=True, owner=owner, unlocked=True)
            c.demand_mw = base
            g.cities.append(c)
        for c in g.cities:
            c.refresh_split(0.42)
        g.policy = policy_for_turn(1)
        g.market_raw = dict(COMMODITY_BASE)
        g.commodity_mod = {"coal": 1.0, "uranium": 1.0}
        g.capex_mod = {}
        g.weather = {"rain": 1.0, "wind": 1.0, "solar": 1.0, "heat": 1.0}
        g._refresh_market()
        g.log_lines = []
        g.messages = []
        g._build_companies(company_name=company_name, account_name=account_name)
        g.log(f"=== 《电力公司》开局：{g.player.name} 开始经营 ===")
        g.log(f"现金 {money(g.player.cash)} · 负债 {money(g.player.debt)} · "
              f"装机 {g.player.gen_capacity():.0f}MW · 员工 {g.player.ops_staff} 班组")
        g.day = 1
        g.clock_paused = True
        g.pause_reason = "准备阶段"
        g._schedule_event()
        return g

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @property
    def player(self) -> Company:
        return self.companies[self.player_idx]

    def total_demand(self) -> float:
        return sum(c.demand_mw for c in self.cities)

    # ------------------------------------------------------------ 多城市
    def city_state(self, city_id: int) -> City | None:
        for c in self.cities:
            if c.idx == city_id:
                return c
        return None

    def world_for_city(self, city_id: int) -> WorldMap | None:
        if city_id in (0, 1):
            worlds = getattr(self, "worlds", None) or []
            if city_id < len(worlds) and worlds[city_id] is not None:
                return worlds[city_id]
            if city_id == self.current_city:
                return self.world
        return None

    def ensure_city_world(self, idx: int) -> WorldMap | None:
        """懒生成第二座可玩城市；AI 城市没有地图。"""
        if idx not in (0, 1):
            return None
        if not getattr(self, "worlds", None):
            self.worlds = [self.world, None]
        w = self.world_for_city(idx)
        if w is not None:
            return w
        old = self.city_state(idx)
        w = WorldMap.generate(self.seed + idx * 7919, city_idx=idx)
        if old is not None and w.cities:
            c = w.cities[0]
            c.owner = old.owner
            c.unlocked = old.unlocked
            c.base_mw = old.base_mw
            c.demand_mw = old.demand_mw
            c.served = dict(old.served)
            c.share = dict(old.share)
            c.outage_mw = old.outage_mw
            c.growth = old.growth
            self.cities[idx] = c
        self.worlds[idx] = w
        if idx == self.current_city:
            self.world = w
        return w

    def switch_city(self, idx: int) -> tuple[bool, str]:
        c = self.city_state(idx)
        if c is None or c.ai:
            return False, "这座城市没有可进入的地图"
        if c.owner != self.player_idx:
            return False, "尚未取得该城市经营权"
        if idx == self.current_city:
            return True, ""
        w = self.ensure_city_world(idx)
        if w is None:
            return False, "城市地图生成失败"
        self.world = w
        self.current_city = idx
        self.city_cap = {}
        self.log(f"镜头进入 {c.name}，开始经营这座城市")
        return True, ""

    def buy_city(self, idx: int) -> tuple[bool, str]:
        c = self.city_state(idx)
        if c is None or c.ai:
            return False, "这座城市不可购买"
        if c.owner == self.player_idx:
            return False, "你已经拥有这座城市的经营权"
        if c.owner not in (-1,):
            return False, "这座城市已被其他公司买断"
        if self.player.reputation < CITY_LICENSE_REP:
            return False, f"声望不足（需 {CITY_LICENSE_REP:.0f}）"
        if self.player.cash < CITY_LICENSE_COST:
            return False, f"现金不足（需 {money(CITY_LICENSE_COST)}）"
        self.player.cash -= CITY_LICENSE_COST
        c.owner = self.player_idx
        c.unlocked = True
        self.log(f"{self.player.name} 以 {money(CITY_LICENSE_COST)} 取得 {c.name} 的经营权")
        return True, ""

    def has_substation(self, city_id: int, owner: int) -> bool:
        w = self.world_for_city(city_id)
        if w is None:
            w = self.ensure_city_world(city_id)
        c = self.city_state(city_id)
        if w is None or c is None or c.x < 0:
            return False
        for y in range(max(0, c.y - CITY_RADIUS - 2), min(w.h, c.y + CITY_RADIUS + 3)):
            for x in range(max(0, c.x - CITY_RADIUS - 2), min(w.w, c.x + CITY_RADIUS + 3)):
                t = w.tile(x, y)
                if t.kind == "substation" and t.owner == owner:
                    return True
        return False

    def build_intercity_link(self, a: int, b: int) -> tuple[bool, str]:
        if a == b:
            return False, "不能连接同一座城市"
        for ln in self.links:
            if {ln.a, ln.b} == {a, b}:
                return False, "这两座城市之间已有跨城线路"
        for cid in (a, b):
            c = self.city_state(cid)
            if c is None or c.ai or c.owner != self.player_idx:
                return False, "两座城市都必须由你拥有"
            if not self.has_substation(cid, self.player_idx):
                return False, f"{c.name} 还没有变电站"
        if self.player.cash < INTERCITY_LINK_COST:
            return False, f"现金不足（需 {money(INTERCITY_LINK_COST)}）"
        self.player.cash -= INTERCITY_LINK_COST
        self.links.append(InterCityLink(a, b))
        self.log(f"{self.player.name} 建成跨城线路（{self.city_state(a).name}—{self.city_state(b).name}），"
                 f"容量 {INTERCITY_LINK_CAP:.0f}MW，线损 {INTERCITY_LINK_LOSS:.0%}")
        return True, ""

    def city_capacity(self, co: Company, city_id: int) -> float:
        caps = self.city_cap.get(co.idx)
        if caps is not None:
            return caps.get(city_id, 0.0)
        w = self.world_for_city(city_id)
        if w is not None:
            return sum(p.cap for p in co.plants if p.online and p.city == city_id and p.x >= 0)
        return sum(p.cap for p in co.plants if p.online and p.city == city_id and p.x < 0)

    def year_of(self, turn: int | None = None) -> int:
        t = self.turn if turn is None else turn
        return 2025 + (t - 1) // 4

    def turn_label(self) -> str:
        s = SEASON_NAMES[season_index(self.turn)]
        return f"第 {self.turn} 季度 · {self.year_of()}年{s}季"

    def date_label(self) -> str:
        s = SEASON_NAMES[season_index(self.turn)]
        return f"{self.year_of()}年{s}季 · 第 {self.day} 天"

    def _schedule_event(self) -> None:
        """每季度抽一次事件，随机安排在季中某天触发。"""
        self.event_day = 0
        self.event_key = ""
        ev = roll_event(self)
        if ev is not None:
            self.event_key = ev.key
            self.event_day = self.rng.randint(12, 78)

    def _fire_event(self) -> None:
        ev = EVENT_BY_KEY.get(self.event_key)
        self.event_day = 0
        self.event_key = ""
        if ev is None or self.turn < ev.min_turn:
            return
        msg = ev.apply(self)
        self.event_seq += 1
        self.last_event_name = ev.name
        self.log(f"【{ev.name}】{msg}")
        if ev.key in MAJOR_NEGATIVE_KEYS:
            self.clock_paused = True
            self.pause_reason = f"事件：{ev.name}"

    # ------------------------------------------------------------ 初始化公司
    def _build_companies(self, company_name: str | None = None,
                         account_name: str | None = None) -> None:
        player_name = (company_name or "").strip() or "盛阳电力"
        player_account = (account_name or "").strip() or "CEO"
        defs = [
            (player_name, 0, "#4fa3ff", True, self.cities[0]),
            ("北岭能源", 1, "#e0605e", False, None),
            ("滨海控股", 2, "#57c785", False, None),
        ]
        for name, idx, _color, is_player, city in defs:
            co = Company(idx=idx, name=name, is_player=is_player,
                         account_name=player_account if is_player else "")
            co.cash = 80_000.0 if is_player else 46_000.0
            co.debt = 20_000.0 if is_player else 12_000.0
            co.ops_staff = 16 if is_player else 10
            co.eng_teams = 4 if is_player else 2
            co.reputation = 62.0 if is_player else 58.0
            co.payout = 0.35
            co.res_price = 500.0
            co.ind_price = 460.0
            self.companies.append(co)
        # 起始资产：玩家在第一座城市有真实地块；AI 在离屏城市有抽象机组
        self._starter_site(self.companies[0], self.cities[0], ("coal", "gas"))
        for co, city_id in ((self.companies[1], 100), (self.companies[2], 101)):
            co.home_city = city_id
            self._seed_ai_fleet(co, self.city_state(city_id))
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
            spot = _find_tile(self.world, kind, city.x, city.y, max_r=8)
            if spot is None:
                spot = _find_tile(self.world, kind, city.x, city.y, max_r=40)
            if spot is None:
                spot = _find_tile(self.world, kind, city.x, city.y, max_r=160)
            if spot is None:
                continue
            spec = PLANT_SPECS[kind]
            pid = self.next_id()
            p = Plant(pid, kind, spot[0], spot[1], co.idx, spec.cap,
                      capex=spec.capex_per_mw * spec.cap * 1.0,
                      city=city.idx,
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

    def _seed_ai_fleet(self, co: Company, city: City | None) -> None:
        """AI 的机组不在任何地图上，只作为离屏容量参与全市场结算。"""
        if city is None:
            return
        kinds = ("coal", "gas") if co.idx == 1 else ("gas", "coal")
        for kind in kinds:
            spec = PLANT_SPECS[kind]
            pid = self.next_id()
            capex = spec.capex_per_mw * spec.cap
            p = Plant(pid, kind, -1, -1, co.idx, spec.cap, capex=capex,
                      city=city.idx, book_value=capex, life_q=spec.life_q)
            co.plants.append(p)

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
        city = self.city_state(self.current_city)
        if city is None or city.owner != co.idx:
            return False, "尚未取得该城市经营权"
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
                     turns_left=turns, spent=capex, teams=teams, city=self.current_city)
        co.projects.append(pr)
        t = self.world.tile(x, y)
        t.owner, t.kind, t.ref = co.idx, kind, -pid - 1
        self.log(f"{co.name} 开工 {spec.name}（{x},{y}），投资 {money(total)}，"
                 f"工期 {turns * DAYS_PER_QUARTER} 天")
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
    def advance_projects(self, days: float = 1.0) -> None:
        """按天推进工程；一个季度 = DAYS_PER_QUARTER 天。"""
        for co in self.companies:
            if not co.alive or co.delisted or not co.projects:
                continue
            need = sum(p.teams for p in co.projects)
            ratio = min(1.0, co.eng_teams / need) if need else 1.0
            for pr in list(co.projects):
                pr.progress += ratio * days / DAYS_PER_QUARTER
                pr.turns_left = max(0, pr.turns_total - int(pr.progress))
                if ratio < 1.0:
                    pr.stalled_turns += 1
                if pr.progress + 1e-9 >= 1.0:
                    self._complete(co, pr)

    def _complete(self, co: Company, pr: Project) -> None:
        w = self.world_for_city(pr.city) or self.world
        tile = w.tile(pr.x, pr.y)
        co.projects.remove(pr)
        if pr.kind in PLANT_SPECS:
            spec = PLANT_SPECS[pr.kind]
            pid = self.next_id()
            p = Plant(pid, pr.kind, pr.x, pr.y, co.idx, spec.cap, pr.capex,
                      city=pr.city, build_turn=self.turn, book_value=pr.capex, life_q=spec.life_q)
            co.plants.append(p)
            tile.ref = p.id
            self.log(f"✔ {co.name} 的{spec.name}（{spec.cap:.0f}MW）投产，"
                     f"每季度固定成本 {p.fixed_q:,.0f}万")
        elif pr.kind in ("coal_mine", "uranium_mine"):
            mid = self.next_id()
            reserve = tile.coal if pr.kind == "coal_mine" else tile.uranium
            m = Mine(mid, pr.kind, pr.x, pr.y, co.idx, pr.capex, pr.capex,
                     reserve=reserve, output=MINE_OUTPUT[pr.kind],
                     cost_per_t=MINE_COST_PER_T[pr.kind], city=pr.city, build_turn=self.turn)
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
    def advance_day(self) -> list[str]:
        """推进一天：工程/停机/禁令按天走，季度末自动结算。"""
        if self.game_over:
            return []
        self.messages = []
        self.advance_projects(1.0)

        for co in self.companies:
            for p in co.plants:
                if not p.online and p.outage_days > 0:
                    p.outage_days -= 1
                    if p.outage_days <= 0:
                        p.online = True
                        self.log(f"{co.name} 的{p.name}恢复并网")
            if co.banned_acq_days > 0:
                co.banned_acq_days -= 1

        self.day += 1
        if self.day > DAYS_PER_QUARTER:
            self.day = 1
            self.close_quarter()
            return self.messages

        if self.event_day and self.day >= self.event_day:
            self._fire_event()

        if self.player.cash < 0:
            if not self.cash_warned:
                self.clock_paused = True
                self.pause_reason = "现金为负"
                self.cash_warned = True
        else:
            self.cash_warned = False
        return self.messages

    def close_quarter(self) -> list[str]:
        """季度结算：AI/经济/政策/股价/胜负，事件已在日中触发。"""
        if self.game_over:
            return []
        self.messages = []
        self.turn_counter += 1
        closed_turn = self.turn

        # A. AI 行动（只在季末跑，避免每天重复借款/定价）
        for co in self.companies:
            if co.is_player or not co.alive or co.delisted:
                continue
            if co.controlled_by == self.player_idx:
                continue
            ai_mod.ai_turn(self, co)

        # B. 生产结算
        settle_production(self)

        # C. 政策
        for co in self.companies:
            if co.alive and not co.delisted:
                apply_quota_penalty(self, co)
        for msg in check_antitrust(self):
            self.log(msg)

        # D. 股利（按上季度利润）
        self.collect_dividends()

        # E. 财务
        self.settle_finance()

        # F. 股价
        update_all_prices(self)

        # G. 控股/被收购
        self.check_control()
        if self.game_over:
            self.day = 1
            self.clock_paused = True
            self.pause_reason = "游戏结束"
            return self.messages

        # H. 进入下一回合
        self._advance_turn()
        self.day = 1

        st = self.player.last_income
        self.quarter_report = (f"第 {closed_turn} 季度结算：营收 {money(st.revenue)} · "
                               f"净利 {money(st.net_profit)} · 现金 {money(self.player.cash)}")
        self.clock_paused = True
        self.pause_reason = "季度结算"

        # I. 胜负
        self._check_end()
        return self.messages

    def skip_to_quarter_end(self) -> list[str]:
        """把当前季度剩余天数一次跑完，停在季末结算。"""
        if self.game_over:
            return []
        start = self.turn
        guard = 0
        while not self.game_over and self.turn == start and guard <= DAYS_PER_QUARTER:
            self.advance_day()
            guard += 1
        self.clock_paused = True
        self.pause_reason = "已跳到季度末"
        return self.messages

    def end_turn(self) -> list[str]:
        """兼容旧调用：直接跳到季度末。"""
        return self.skip_to_quarter_end()

    def _advance_turn(self) -> None:
        self.turn += 1
        nuclear = self.policy.nuclear_approved
        self.policy = policy_for_turn(self.turn)
        self.policy.nuclear_approved = nuclear
        self.demand_shock = 1.0
        self.weather = {"rain": 1.0, "wind": 1.0, "solar": 1.0, "heat": 1.0}
        for city in self.cities:
            city.base_mw *= 1.0 + city.growth
        self._walk_market()
        for co in self.companies:
            co.open_candle()             # 新回合：以上回合收盘价为开盘价
            # 声望向 60 回归
            co.reputation += (60.0 - co.reputation) * 0.06
            co.risk_flags = [f for f in co.risk_flags if f]
        self.ceo_cash += CEO_SALARY
        self._schedule_event()

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
