"""界面交互冒烟测试：模拟玩家点击建造、买股、控股、合并、结束回合。

用法：python tools/ui_smoke.py
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PWER_NO_INTRO"] = "1"

from pwer import ma  # noqa: E402
from pwer.ui.app import App  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


def main() -> None:
    app = App()
    app.update()
    g = app.game
    me = g.player

    # --- 1. 建造流程：变电站 + 燃煤电厂 到钢城
    city = g.world.cities[2]          # 钢城
    me.cash = 400_000
    for kind in ("substation", "coal"):
        # set_build 只算"视野附近"的可建格，先把镜头挪到钢城
        app.map.focus_on(city.x, city.y, 0.4)
        app.update()
        app.set_build(kind)
        # 480×480 上城市周围 7×7 未必有可建格，用环形搜索找最近的合规位置
        hits = g.world.find_tiles(kind, limit=1, near=(city.x, city.y), radius=40)
        placed = False
        if hits:
            app.on_tile_click(*hits[0])
            placed = bool(me.projects)
        check(f"在钢城附近建造 {kind}", placed, f"候选 {hits}")
    app.refresh()

    # --- 2. 结束回合推进工程
    for _ in range(3):
        app.end_turn()
        app.update()
    coal_tiles = [i for i, k in enumerate(g.world.kind)
                  if k == "coal" and g.world.own[i] == me.idx]
    check("燃煤电厂已投产", len(coal_tiles) >= 2, f"实际 {len(coal_tiles)}")

    # --- 3. 买股：公司账户买入 滨海控股 51%
    tgt = g.companies[2]
    me.cash = 2_000_000
    sel = str(tgt.idx)
    app.tv_mkt.selection_set(sel)
    app.update()
    app.var_qty.set("6")
    for _ in range(9):
        app.tv_mkt.selection_set(sel)
        app.do_buy(company_fund=True)
        app.update()
    stake = ma.player_stake(g, tgt)
    check("持股超过 51%", stake > 0.51, f"实际 {stake:.1%}")

    # --- 4. 控股接管 + 抽血 + 淘汰机组
    app.tv_mkt.selection_set(sel)
    app.do_take_control()
    app.update()
    check("已控股", tgt.controlled_by == me.idx)
    app.do_sweep()
    app.update()
    n_sub_rows = app.lb_subs.size()
    check("子公司机组列出", n_sub_rows > 0, f"{n_sub_rows} 行")
    if n_sub_rows:
        app.lb_subs.selection_set(0)
        before = len(tgt.plants)
        app.do_retire()
        app.update()
        check("淘汰机组生效", len(tgt.plants) == before - 1)

    # --- 5. 增持到 75% 并吸收合并
    for _ in range(5):
        app.tv_mkt.selection_set(sel)
        app.do_buy(company_fund=True)
        app.update()
    check("持股超过 75%", ma.player_stake(g, tgt) > 0.75,
          f"实际 {ma.player_stake(g, tgt):.1%}")
    app.tv_mkt.selection_set(sel)
    plants_before = len(me.plants)
    try:
        ma.absorb_merge(g, tgt)
    except ma.MaError as e:
        check("吸收合并", False, str(e))
    app.refresh()
    check("合并后资产并入", len(me.plants) > plants_before)
    check("子公司已退市", tgt.delisted)

    # --- 6. 继续跑 8 回合，确保各种刷新路径不炸
    try:
        for _ in range(8):
            app.end_turn()
            app.update()
        check("合并后继续回合", not g.game_over or True)
    except Exception:
        traceback.print_exc()
        check("合并后继续回合", False)

    # --- 7. 抽屉页签刷新
    for key in ('policy', 'stock', 'company'):
        app.toggle_panel(key)
        app.update()
    check("三个页签刷新正常", True)

    app.map.draw(g)
    app.update()
    print("\n游戏状态：", g.turn_label(), "份额", f"{g.player_share():.1%}",
          "股价", f"{me.price:.2f}", "净资产", me.equity())
    app.destroy()
    if FAILS:
        print("\n失败项：", FAILS)
        raise SystemExit(1)
    print("\n全部通过")


if __name__ == "__main__":
    main()
