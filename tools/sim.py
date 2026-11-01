"""无头模拟：不启动界面，直接跑 N 回合，用来验证数值与经济闭环。

用法：
    python tools/sim.py            # 默认 30 回合
    python tools/sim.py 48         # 自定义回合数
    python tools/sim.py 30 noisy   # 打印每一家公司的季度明细
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pwer.constants import money  # noqa: E402
from pwer.engine import Game  # noqa: E402
from pwer.ma import MaError, buy_shares  # noqa: E402
from pwer.world import CITY_RADIUS, chebyshev  # noqa: E402


def _free_near(g, co, kind, city, radius=CITY_RADIUS):
    out = []
    for t in g.world.iter_tiles():
        if chebyshev(t.x, t.y, city.x, city.y) > radius:
            continue
        if g.can_build(co, kind, t.x, t.y)[0]:
            out.append(t)
    return out


def _adjacent_free(g, co, kind, city, radius=CITY_RADIUS + 1):
    """贴着自家电网、且在城市附近的可建地块。"""
    out = []
    for t in g.world.iter_tiles():
        if t.owner != co.idx:
            continue
        if chebyshev(t.x, t.y, city.x, city.y) > radius:
            continue
        for nx, ny in g.world.neighbors4(t.x, t.y):
            if g.can_build(co, kind, nx, ny)[0]:
                out.append(g.world.tile(nx, ny))
    return out


def auto_play_player(g: Game) -> None:
    """一个"中庸玩家"基准线：补人力 → 按成本定价 → 补电网 → 有钱就买对手。"""
    me = g.player

    # 0) 人力：缺人就招
    need = me.staff_need()
    if me.ops_staff < need and me.cash > 12_000:
        g.hire(me, ops=min(need - me.ops_staff, 6))
    if len(me.projects) > me.eng_teams and me.cash > 30_000:
        g.hire(me, eng=min(len(me.projects) - me.eng_teams, 2))

    # 1) 定价：全成本 +15%，但不低于可变成本 1.45 倍
    st = me.last_income
    mwh = max(1.0, st.served_mwh)
    unit = st.total_cost * 10_000.0 / mwh if mwh > 1000 else 460.0
    var = st.fuel * 10_000.0 / mwh if st.fuel > 0 else 150.0
    price = min(g.policy.price_cap * 0.95, max(var * 1.45, unit * 1.15, 330.0))
    me.res_price = round(price * 1.03, 1)
    me.ind_price = round(price * 0.96, 1)

    # 2) 建设：找"我方覆盖缺口最大"的城市
    best_city, best_gap = None, 0.0
    for city in g.world.cities:
        cap = g.city_cap.get(me.idx, {}).get(city.idx, 0.0)
        gap = city.demand_mw * 0.8 - cap
        if gap > best_gap:
            best_city, best_gap = city, gap
    if best_city is not None and len(me.projects) < 2 and me.cash > 25_000:
        my_subs = [t for t in g.world.iter_tiles() if t.owner == me.idx
                   and t.kind == "substation"
                   and chebyshev(t.x, t.y, best_city.x, best_city.y) <= CITY_RADIUS]
        if not my_subs:
            cands = _free_near(g, me, "substation", best_city)
            if cands:
                g.build(me, "substation", cands[0].x, cands[0].y)
        else:
            # 缺新能源先补光伏/风电，否则补火电
            low_re = me.renewable_share() < g.policy.renewable_quota + 0.02
            kinds = ("solar", "wind", "coal", "gas") if low_re else ("coal", "gas", "wind", "solar")
            built = False
            for kind in kinds:
                cands = _adjacent_free(g, me, kind, best_city)
                if not cands:
                    continue
                if me.cash < g.total_build_cost(me, kind, cands[0].x, cands[0].y) * 1.1:
                    continue
                if g.build(me, kind, cands[0].x, cands[0].y):
                    built = True
                    break
            if not built:
                for t in _free_near(g, me, "line", best_city):
                    if g.build(me, "line", t.x, t.y):
                        break

    # 3) 借款
    if me.cash < 20_000:
        try:
            g.borrow(me, 25_000)
        except ValueError:
            pass

    # 4) 股票：现金充裕时买最便宜的对手
    rivals = [c for c in g.listed() if c.idx != me.idx]
    if me.cash > 80_000 and rivals:
        tgt = min(rivals, key=lambda c: c.market_cap())
        try:
            buy_shares(g, me.idx, tgt, tgt.shares * 0.03)
        except MaError:
            pass


def main() -> None:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    noisy = len(sys.argv) > 2 and sys.argv[2] == "noisy"
    g = Game.new_game()
    print("种子：", g.seed, "（想复现就把这个数传给 Game.new_game）")
    print("初始：", " | ".join(c.summary_line() for c in g.companies))
    print("城市需求：", ", ".join(f"{c.name}{c.demand_mw:.0f}MW" for c in g.world.cities))
    print("-" * 118)
    for _ in range(turns):
        if g.game_over:
            break
        auto_play_player(g)
        msgs = g.end_turn()
        me = g.player
        st = me.last_income
        print(f"T{g.turn:02d} {me.name} 现金{money(me.cash):>9} 负债{money(me.debt):>9} "
              f"净资产{money(me.equity()):>9} 营收{money(st.revenue):>9} 净利{money(st.net_profit):>9} "
              f"股价{me.price:6.2f} 份额{g.player_share():5.1%} 装机{me.gen_capacity():4.0f}MW "
              f"人力{me.staff_ratio():4.0%} 电价{me.ind_price:5.0f} 燃料{money(st.fuel):>7}")
        if noisy:
            for name, val, _ in st.rows():
                print(f"       {name:<16}{money(val):>12}")
        if g.turn % 8 == 0:
            for c in g.companies:
                print(f"     · {c.name:<6} 股价{c.price:6.2f} 市值{money(c.market_cap()):>9} "
                      f"净资产{money(c.equity()):>9} 份额{c.market_share:5.1%} "
                      f"负债率{c.debt_ratio():5.0%} 声望{c.reputation:3.0f} "
                      f"装机{c.gen_capacity():4.0f}MW")
        for m in msgs:
            print("     >", m)
    print("-" * 118)
    print("结束：", g.result, g.result_detail)
    print("日志尾部：")
    for line in g.log_lines[-12:]:
        print("  ", line)


if __name__ == "__main__":
    main()
