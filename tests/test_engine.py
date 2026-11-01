"""引擎单元测试：锁住"经营→财报→股价→股权"这条链上的关键不变量。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pwer import ma  # noqa: E402
from pwer.constants import HOURS_PER_TURN, PLANT_SPECS  # noqa: E402
from pwer.engine import Game  # noqa: E402
from pwer.grid import city_supply, components  # noqa: E402
from pwer.world import Terrain, WorldMap, chebyshev  # noqa: E402


def new_game(seed: int = 1234) -> Game:
    return Game.new_game(seed)


class TestWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.w = WorldMap.generate(99)

    def test_map_has_four_cities(self) -> None:
        self.assertEqual(len(self.w.cities), 4)

    def test_hydro_only_on_river(self) -> None:
        for t in self.w.iter_tiles():
            ok, why = self.w.check_build("hydro", t.x, t.y)
            if ok:
                self.assertIs(t.terrain, Terrain.RIVER)
            elif t.terrain is not Terrain.CITY and not t.occupied:
                self.assertIn("河流", why)

    def test_nuclear_away_from_city(self) -> None:
        for t in self.w.iter_tiles():
            ok, _ = self.w.check_build("nuclear", t.x, t.y)
            if ok:
                for c in self.w.cities:
                    self.assertGreater(chebyshev(t.x, t.y, c.x, c.y), 2)

    def test_coal_needs_water(self) -> None:
        for t in self.w.iter_tiles():
            ok, why = self.w.check_build("coal", t.x, t.y)
            if ok:
                self.assertTrue(self.w.has_water_adjacent(t.x, t.y))

    def test_every_kind_has_buildable_tile(self) -> None:
        for kind in PLANT_SPECS:
            tiles = self.w.find_tiles(kind, limit=5)
            self.assertGreater(len(tiles), 0, f"{kind} 没有可建地块，地图资源分布有问题")

    def test_occupied_tile_rejected(self) -> None:
        t = self.w.find_tiles("gas", 1)[0]
        tile = self.w.tile(*t)
        tile.owner, tile.kind = 0, "gas"
        ok, why = self.w.check_build("gas", *t)
        self.assertFalse(ok)


class TestGrid(unittest.TestCase):
    def setUp(self) -> None:
        self.g = new_game()
        self.city = self.g.world.cities[0]
        self.me = self.g.player

    def test_city_supply_limit(self) -> None:
        cap, loss = city_supply(self.g.world, self.me.idx, self.city)
        total_plant = sum(p.cap for p in self.me.plants)
        self.assertGreater(cap, 0)
        self.assertLessEqual(cap, total_plant + 1e-6)
        self.assertGreaterEqual(loss, 0.0)

    def test_isolated_plant_cannot_deliver(self) -> None:
        co = self.g.companies[1]
        cap, _ = city_supply(self.g.world, co.idx, self.city)
        self.assertEqual(cap, 0.0)

    def test_components_are_connected(self) -> None:
        comps = components(self.g.world, self.me.idx)
        self.assertTrue(comps)
        for comp in comps:
            start = next(iter(comp))
            seen = {start}
            stack = [start]
            while stack:
                x, y = stack.pop()
                for n in self.g.world.neighbors4(x, y):
                    if n in comp and n not in seen:
                        seen.add(n)
                        stack.append(n)
            self.assertEqual(seen, comp)


class TestDispatch(unittest.TestCase):
    def test_served_never_exceeds_capacity(self) -> None:
        g = new_game()
        g.end_turn()
        for city in g.world.cities:
            for cid, mw in city.served.items():
                co = g.companies[cid]
                cap = g.city_cap[cid].get(city.idx, 0.0)
                self.assertLessEqual(mw, cap + 1.0)

    def test_revenue_matches_energy(self) -> None:
        g = new_game()
        g.end_turn()
        co = g.player
        st = co.last_income
        e_res = st.revenue_res * 10_000.0 / co.res_price
        e_ind = st.revenue_ind * 10_000.0 / co.ind_price
        self.assertAlmostEqual(e_res + e_ind, st.served_mwh, delta=1.0)
        self.assertAlmostEqual(st.served_mwh, (co.served_mw) * HOURS_PER_TURN, delta=1.0)

    def test_no_fixed_double_count(self) -> None:
        g = new_game()
        g.end_turn()
        co = g.player
        st = co.last_income
        expect_dep = sum(p.dep_q for p in co.plants) + sum(m.dep_q for m in co.mines)
        self.assertAlmostEqual(st.depreciation, expect_dep, delta=1.0)
        self.assertAlmostEqual(st.fixed, sum(p.oam_q for p in co.plants), delta=1.0)


class TestFinance(unittest.TestCase):
    def test_balanced_bookkeeping(self) -> None:
        """现金变动 = 净利润 + 折旧 - 股利支付 - 资本开支（本回合没有新建设）"""
        g = new_game()
        co = g.player
        cash0 = co.cash
        equity0 = co.equity()
        g.end_turn()
        st = co.last_income
        div = co.dividends_paid
        delta = co.cash - cash0
        # 现金 = 净利润 + 折旧 - 股利（股利在净利润之外单独支付）
        self.assertAlmostEqual(delta, st.net_profit + st.depreciation - div
                               - sum(p.per_turn for p in []), delta=50)
        self.assertGreater(co.equity(), equity0 - 10_000)

    def test_depreciation_reduces_book_value(self) -> None:
        g = new_game()
        p = g.player.plants[0]
        bv0 = p.book_value
        g.end_turn()
        self.assertLess(p.book_value, bv0)

    def test_bankruptcy_when_insolvent(self) -> None:
        g = new_game()
        co = g.player
        co.debt = co.assets() * 3
        co.cash = -1.0
        g.settle_finance()
        # 现金为负且无处借款 → 破产
        self.assertFalse(co.alive)
        self.assertTrue(g.game_over)

    def test_borrow_limit(self) -> None:
        g = new_game()
        co = g.player
        limit = co.assets() * 0.65 - co.debt
        got = g.borrow(co, limit * 10)
        self.assertLessEqual(got, limit + 1e-6)
        with self.assertRaises(ValueError):
            g.borrow(co, 1.0)


class TestValuation(unittest.TestCase):
    def test_price_follows_profit(self) -> None:
        g = new_game()
        co = g.player
        for _ in range(6):
            g.end_turn()
        p1 = co.price
        # 人为制造两个季度的暴利
        co.profit_history = [20_000.0, 25_000.0, 30_000.0, 35_000.0]
        from pwer.valuation import update_price
        update_price(co, g.rng)
        self.assertGreater(co.price, p1)

    def test_loss_makes_price_fall(self) -> None:
        g = new_game()
        co = g.player
        g.end_turn()
        p0 = co.price
        co.profit_history = [-8_000.0] * 4
        from pwer.valuation import update_price
        for _ in range(4):
            update_price(co, g.rng)
        self.assertLess(co.price, p0)


class TestMA(unittest.TestCase):
    def setUp(self) -> None:
        self.g = new_game()
        self.me = self.g.player
        self.tgt = self.g.companies[1]
        self.me.cash = 1_000_000.0

    def test_buy_shares_updates_stake(self) -> None:
        qty = self.tgt.shares * 0.10
        cost = ma.buy_shares(self.g, self.me.idx, self.tgt, qty)
        self.assertGreater(cost, 0)
        self.assertAlmostEqual(ma.player_stake(self.g, self.tgt), 0.10, places=3)

    def test_cannot_buy_without_cash(self) -> None:
        self.me.cash = 1.0
        with self.assertRaises(ma.MaError):
            ma.buy_shares(self.g, self.me.idx, self.tgt, self.tgt.shares * 0.1)

    def test_control_requires_51(self) -> None:
        ma.buy_shares(self.g, self.me.idx, self.tgt, self.tgt.shares * 0.30)
        with self.assertRaises(ma.MaError):
            ma.take_control(self.g, self.tgt)
        ma.buy_shares(self.g, self.me.idx, self.tgt, self.tgt.shares * 0.25)
        ma.take_control(self.g, self.tgt)
        self.assertEqual(self.tgt.controlled_by, self.me.idx)

    def test_merge_transfers_assets(self) -> None:
        n_plants = len(self.me.plants)
        target_plants = len(self.tgt.plants)
        target_debt = self.tgt.debt
        ma.buy_shares(self.g, self.me.idx, self.tgt, self.tgt.shares * 0.80)
        ma.absorb_merge(self.g, self.tgt)
        self.assertEqual(len(self.me.plants), n_plants + target_plants)
        self.assertGreaterEqual(self.me.debt, target_debt)
        self.assertTrue(self.tgt.delisted)
        self.assertNotIn(self.tgt, [c for c in self.g.listed()])

    def test_buyback_reduces_shares(self) -> None:
        sh0 = self.me.shares
        ma.buyback(self.g, self.me, self.me.shares * 0.02)
        self.assertLess(self.me.shares, sh0)

    def test_issue_shares_dilutes(self) -> None:
        sh0 = self.me.shares
        cash0 = self.me.cash
        ma.issue_shares(self.g, self.me, self.me.shares * 0.05)
        self.assertGreater(self.me.shares, sh0)
        self.assertGreater(self.me.cash, cash0)


class TestEngine(unittest.TestCase):
    def test_twenty_turns_no_crash(self) -> None:
        g = new_game(7)
        for _ in range(20):
            g.end_turn()
            for co in g.companies:
                self.assertGreaterEqual(co.cash, -1e-6)
                self.assertGreater(co.shares, 0)
                self.assertGreaterEqual(co.price, 0.3)
                for p in co.plants:
                    self.assertGreaterEqual(p.book_value, 0.0)
                    self.assertLessEqual(p.dispatched_mwh, p.available_mwh + 1e-6)

    def test_build_spends_cash_and_creates_project(self) -> None:
        g = new_game()
        me = g.player
        me.cash = 500_000.0
        spot = None
        for t in g.world.iter_tiles():
            ok, _ = g.can_build(me, "solar", t.x, t.y)
            if ok:
                spot = (t.x, t.y)
                break
        self.assertIsNotNone(spot)
        cash0 = me.cash
        self.assertTrue(g.build(me, "solar", *spot))
        self.assertLess(me.cash, cash0)
        self.assertEqual(len(me.projects), 1)
        # 工期到了就投产
        for _ in range(3):
            g.advance_projects()
        self.assertEqual(len(me.projects), 0)
        self.assertTrue(any(p.kind == "solar" for p in me.plants))

    def test_project_blocked_by_engine_team_shortage(self) -> None:
        g = new_game()
        me = g.player
        me.cash = 500_000.0
        me.eng_teams = 0
        spot = None
        for t in g.world.iter_tiles():
            ok, _ = g.can_build(me, "coal", t.x, t.y)
            if ok:
                spot = (t.x, t.y)
                break
        g.build(me, "coal", *spot)
        g.advance_projects()
        self.assertEqual(len(me.projects), 1)   # 没有工程队，工期无法推进

    def test_antitrust_triggers(self) -> None:
        g = new_game()
        me = g.player
        me.market_share = 0.9
        me.last_income.served_mwh = 1000.0
        me.last_income.revenue_res = 10.0
        me.last_income.fixed = 100.0
        me.banned_acq_turns = 0
        for _ in range(3):
            msgs = __import__("pwer.policy", fromlist=["check_antitrust"]).check_antitrust(g)
        self.assertTrue(any("反垄断" in m for m in msgs))
        self.assertGreater(me.banned_acq_turns, 0)

    def test_events_change_state(self) -> None:
        g = new_game()
        from pwer import events
        for ev in events.EVENTS:
            g2 = new_game()
            msg = ev.apply(g2)
            self.assertIsInstance(msg, str)


class TestStockCandles(unittest.TestCase):
    """股市页的 K 线：每回合一根，开=上回合收，高/低包住实体。"""

    def setUp(self) -> None:
        self.g = new_game()
        self.co = self.g.player

    def test_one_candle_per_turn(self) -> None:
        for _ in range(5):
            self.g.end_turn()
        self.assertEqual(len(self.co.candles), 5)

    def test_ohlc_consistent(self) -> None:
        for _ in range(8):
            self.g.end_turn()
        prev_close = 12.0
        for o, hi, lo, c in self.co.candles:
            self.assertAlmostEqual(o, prev_close, places=6)   # 开盘 = 上回合收盘
            self.assertLessEqual(lo, min(o, c) + 1e-9)
            self.assertGreaterEqual(hi, max(o, c) - 1e-9)
            self.assertGreater(lo, 0.0)
            prev_close = c

    def test_trade_shows_in_wick(self) -> None:
        """玩家买入会把价格顶上去，这根 K 线的最高价要能看见。"""
        tgt = next(c for c in self.g.listed() if c.idx != self.g.player_idx)
        before = tgt.price
        qty = max(1.0, tgt.shares * 0.02)
        ma.buy_shares(self.g, self.g.player_idx, tgt, qty)
        self.assertGreater(tgt.price, before)
        self.assertGreaterEqual(tgt.turn_high, tgt.price - 1e-9)

    def test_old_save_without_candle_fields(self) -> None:
        """旧存档里没有行情字段，读档后必须自动补齐而不是崩。"""
        from pwer.company import Company
        co = Company(idx=0, name="旧档")
        state = {k: v for k, v in co.__dict__.items()
                 if k not in ("candles", "turn_open", "turn_high", "turn_low")}
        old = object.__new__(Company)          # 模拟 pickle 反序列化：不跑 __init__
        old.__setstate__(state)
        self.assertEqual(old.candles, [])
        self.assertAlmostEqual(old.turn_open, old.price)


class TestIssueShares(unittest.TestCase):
    """增发是折价配售：拿现金、摊股本，但市值不能被印出来。"""

    def test_issue_does_not_inflate_market_cap(self) -> None:
        g = new_game()
        co = g.player
        cap0, price0, cash0, shares0 = co.market_cap(), co.price, co.cash, co.shares
        cash = ma.issue_shares(g, co, co.shares * 0.06)
        self.assertGreater(cash, 0.0)                # 募到钱
        self.assertGreater(co.shares, shares0)       # 股本变大（稀释）
        self.assertGreater(co.cash, cash0)           # 现金变多
        self.assertLess(co.price, price0)            # 股价被砸下来
        self.assertLess(co.market_cap(), cap0)       # 市值不能变大

    def test_issue_is_capped(self) -> None:
        g = new_game()
        co = g.player
        shares0 = co.shares
        ma.issue_shares(g, co, co.shares * 5)        # 想发 500%
        self.assertLessEqual(co.shares, shares0 * 1.15 + 1e-6)


class TestTerrain(unittest.TestCase):
    """锁住 docs/01_terrain.md §6 的验收不变量（生成器回归时第一时间发现）。"""

    def test_same_seed_is_identical(self) -> None:
        a = WorldMap.generate(20240501)
        b = WorldMap.generate(20240501)

        def sig(wm):
            return [(t.x, t.y, t.terrain, t.elevation, t.moisture,
                     t.wind, t.solar, t.coal, t.uranium) for t in wm.iter_tiles()]

        self.assertEqual(sig(a), sig(b))

    def test_terrain_mix_and_candidates(self) -> None:
        for seed in (99, 20240501):
            wm = WorldMap.generate(seed)
            tiles = list(wm.iter_tiles())
            total = len(tiles)
            coast = sum(1 for t in tiles if t.terrain is Terrain.COAST)
            plain = sum(1 for t in tiles if t.terrain is Terrain.PLAIN)
            self.assertTrue(0.08 <= coast / total <= 0.20, f"seed {seed} COAST {coast/total:.1%}")
            self.assertGreaterEqual(plain / total, 0.35, f"seed {seed} PLAIN {plain/total:.1%}")
            self.assertGreaterEqual(len(wm.city_candidates), 4)
            for cand in wm.city_candidates:
                x, y = cand["x"], cand["y"]
                land = sum(1 for yy in range(y - 1, y + 2) for xx in range(x - 1, x + 2)
                           if wm.in_bounds(xx, yy)
                           and wm.tile(xx, yy).terrain in (Terrain.PLAIN, Terrain.DESERT))
                self.assertGreaterEqual(land, 2, f"seed {seed} 城址 {x},{y} 3×3 内可建格不足")

    def test_rivers_reach_sea_and_are_long(self) -> None:
        """每条河要么自己入海，要么汇进的河入了海（水网整体连通到海）。"""
        wm = WorldMap.generate(20240501)
        self.assertTrue(wm.rivers)
        paths = [[(x, y) for x, y in p] for p in wm.rivers]
        for i, path in enumerate(paths):
            self.assertGreaterEqual(len(path), 10, f"第 {i} 条河只有 {len(path)} 格")

        def neighbours(x: int, y: int):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        nx, ny = x + dx, y + dy
                        if wm.in_bounds(nx, ny):
                            yield nx, ny

        # 河道之间用"隔壁就是另一条河"认连通，再从任一入海口回溯整片水网
        links: dict[int, set[int]] = {i: set() for i in range(len(paths))}
        for i, path in enumerate(paths):
            for x, y in path:
                for nx, ny in neighbours(x, y):
                    for j, other in enumerate(paths):
                        if j != i and (nx, ny) in other:
                            links[i].add(j)
                            links[j].add(i)

        def touches_sea(i: int) -> bool:
            for x, y in paths[i]:
                for nx, ny in neighbours(x, y):
                    if wm.tile(nx, ny).terrain is Terrain.COAST:
                        return True
            return False

        for i in range(len(paths)):
            seen, stack, ok = {i}, [i], False
            while stack:
                cur = stack.pop()
                if touches_sea(cur):
                    ok = True
                    break
                for j in links[cur] - seen:
                    seen.add(j)
                    stack.append(j)
            self.assertTrue(ok, f"第 {i} 条河（{len(paths[i])} 格）连不到海")

    def test_no_tiny_isolated_region(self) -> None:
        wm = WorldMap.generate(20240501)
        seen = [[False] * wm.w for _ in range(wm.h)]
        for ty in range(wm.h):
            for tx in range(wm.w):
                if seen[ty][tx] or wm.tile(tx, ty).terrain in (Terrain.RIVER, Terrain.COAST, Terrain.CITY):
                    continue
                kind = wm.tile(tx, ty).terrain
                stack, seen[ty][tx], size = [(tx, ty)], True, 0
                while stack:
                    cx, cy = stack.pop()
                    size += 1
                    for nx, ny in wm.neighbors4(cx, cy):
                        if not seen[ny][nx] and wm.tile(nx, ny).terrain is kind:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
                self.assertGreaterEqual(size, 8, f"({tx},{ty}) 附近有 {size} 格的孤岛")

    def test_resources_exist(self) -> None:
        for seed in (99, 20240501):
            wm = WorldMap.generate(seed)
            self.assertEqual(len(wm.find_tiles("coal_mine")), 3, f"seed {seed} 煤矿数量")
            self.assertGreaterEqual(len(wm.find_tiles("uranium_mine")), 1, f"seed {seed} 铀矿缺失")


if __name__ == "__main__":
    unittest.main(verbosity=2)
