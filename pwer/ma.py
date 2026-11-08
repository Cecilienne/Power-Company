"""股权、收购与反收购。

三个资金主体：
    公司账户  —— 经营赚的钱，做现金收购/换股，也能回购自家股票
    CEO 账户  —— 个人资金（工资 + 股利），贴身护盘、悄悄吸筹
    对手 AI   —— 同样两套手段，且会盯着你的财报下手
股权阶梯（设计文档 §二.5）：
    5%  举牌，AI 警觉
    20% 董事席位，参与分红
    51% 控股：可以抽血、处置资产
    75% 吸收合并：资产/负债/矿权/人力全部并入，子公司退市
"""
from __future__ import annotations

from dataclasses import dataclass

from .company import Company
from .constants import (
    ACQUISITION_PREMIUM,
    INTEGRATION_COST_RATIO,
    SHARE_IMPACT,
    STAKE_ALERT,
    STAKE_BOARD,
    STAKE_CONTROL,
    STAKE_MERGE,
)
from .constants import money

CEO = "CEO"


class MaError(Exception):
    pass


def holder_cash(game, holder) -> float:
    if holder == CEO:
        return game.ceo_cash
    return game.companies[holder].cash


def add_cash(game, holder, amount: float) -> None:
    if holder == CEO:
        game.ceo_cash += amount
    else:
        game.companies[holder].cash += amount


def holder_holdings(game, holder) -> dict[int, float]:
    if holder == CEO:
        return game.ceo_holdings
    return game.companies[holder].holdings


def player_stake(game, target: Company) -> float:
    """玩家（公司 + CEO 个人）对目标公司的合计持股比例。"""
    if target.idx == game.player_idx:
        # 自家公司：CEO 持股 + 公司库存股（视为自己人）
        sh = game.ceo_holdings.get(target.idx, 0.0)
        sh += game.companies[target.idx].holdings.get(target.idx, 0.0)
        return sh / max(1.0, target.shares)
    sh = game.ceo_holdings.get(target.idx, 0.0)
    sh += game.companies[game.player_idx].holdings.get(target.idx, 0.0)
    return sh / max(1.0, target.shares)


def stake_of(game, holder, target: Company) -> float:
    return holder_holdings(game, holder).get(target.idx, 0.0) / max(1.0, target.shares)


def ownership(game, target: Company) -> dict[str, float]:
    """全部持股结构：键为 '公司名' 或 'CEO'。"""
    out: dict[str, float] = {}
    if game.ceo_holdings.get(target.idx, 0.0) > 0:
        out["CEO"] = game.ceo_holdings[target.idx] / max(1.0, target.shares)
    for co in game.companies:
        sh = co.holdings.get(target.idx, 0.0)
        if sh > 0:
            out[co.name] = sh / max(1.0, target.shares)
    out["散户"] = max(0.0, 1.0 - sum(out.values()))
    return out


def buy_cost(target: Company, qty: float) -> float:
    """含收购溢价与冲击成本的总价（万元）。"""
    if qty <= 0:
        return 0.0
    impact = SHARE_IMPACT * qty / max(1.0, target.shares)
    unit = target.price * (1.0 + ACQUISITION_PREMIUM * 0.5 + impact * 0.5)
    return unit * qty


def buy_shares(game, holder, target: Company, qty: float) -> float:
    """holder 以市价买入 target 的股票，返回花费（万元）。"""
    if target.delisted:
        raise MaError("该公司已退市，无法交易")
    if holder in (game.player_idx, CEO) and target.idx != game.player_idx:
        ban = game.player.banned_acq_days
        if ban > 0:
            raise MaError(f"反垄断禁令：{ban} 天内不得收购其他公司股票")
    if holder == target.idx:
        raise MaError("回购自家股票请使用【回购】操作")
    qty = min(qty, target.shares * 0.95)
    if qty <= 0:
        raise MaError("数量无效")
    cost = buy_cost(target, qty)
    if holder_cash(game, holder) < cost:
        raise MaError(f"资金不足，需要 {money(cost)}")
    add_cash(game, holder, -cost)
    hh = holder_holdings(game, holder)
    hh[target.idx] = hh.get(target.idx, 0.0) + qty
    impact = SHARE_IMPACT * qty / max(1.0, target.shares)
    target.price *= 1.0 + impact * 0.5
    target.note_price()            # 买入冲击计入本回合 K 线的上影
    if holder == game.player_idx or holder == CEO:
        prev = (hh.get(target.idx, 0.0) - qty) / max(1.0, target.shares)
        now = hh[target.idx] / max(1.0, target.shares)
        for th in (STAKE_ALERT, STAKE_BOARD, STAKE_CONTROL, STAKE_MERGE):
            if prev < th <= now:
                game.log(f"⚡ 你举牌 {target.name}，持股达到 {now:.1%}（{_ladder_name(th)}）")
        if target.idx != game.player_idx and now >= STAKE_ALERT and not target.ai_alerted:
            target.ai_alerted = True
            target.reputation = max(0.0, target.reputation - 1.0)
            game.log(f"⚠ {target.name} 管理层已警觉，正在启动反收购准备")
    return cost


def sell_shares(game, holder, target: Company, qty: float) -> float:
    if target.delisted:
        raise MaError("该公司已退市，无法交易")
    hh = holder_holdings(game, holder)
    have = hh.get(target.idx, 0.0)
    qty = min(qty, have)
    if qty <= 0:
        raise MaError("没有可卖出的股份")
    proceeds = target.price * qty * (1.0 - ACQUISITION_PREMIUM * 0.35)
    add_cash(game, holder, proceeds)
    hh[target.idx] = have - qty
    impact = SHARE_IMPACT * qty / max(1.0, target.shares)
    target.price = max(0.3, target.price * (1.0 - impact * 0.5))
    target.note_price()            # 卖出冲击计入下影
    return proceeds


def buyback(game, co: Company, qty: float) -> float:
    """回购注销，股本减少 → EPS 提升（防御/抬价）。"""
    qty = min(qty, co.shares * 0.25)
    if qty <= 0:
        raise MaError("数量无效")
    cost = co.price * qty * 1.01
    if co.cash < cost:
        raise MaError(f"现金不足，回购需要 {money(cost)}")
    co.cash -= cost
    co.shares -= qty
    co.price *= 1.0 + SHARE_IMPACT * qty / max(1.0, co.shares + qty) * 0.8
    co.note_price()
    game.log(f"{co.name} 回购 {qty / max(1.0, co.shares + qty):.2%} 股本，耗资 {money(cost)}")
    return cost


ISSUE_MAX_RATIO = 0.15       # 单次增发上限（占现有股本）
ISSUE_DISCOUNT_BASE = 0.06   # 基础折价
ISSUE_DISCOUNT_SLOPE = 0.80  # 每增发 1% 股本，折价再加 0.8%


def issue_shares(game, co: Company, qty: float, discount: float | None = None) -> float:
    """增发：折价配售。

    拿现金、稀释股本，但市场会按配售价重估——股价直接跌到增发价，
    而不是只吃一个 1% 的冲击系数。否则「股本 +6% / 股价 −1.5%」会让市值凭空变大，
    增发就成了印钞机（市值还是胜利条件之一）。
    """
    if qty <= 0:
        raise MaError("数量无效")
    qty = min(qty, co.shares * ISSUE_MAX_RATIO)
    ratio = qty / max(1.0, co.shares)
    if discount is None:
        # 发得越多、折价越深：6% 起步，最多 25%
        discount = min(0.25, ISSUE_DISCOUNT_BASE + ISSUE_DISCOUNT_SLOPE * ratio)
    issue_price = max(0.3, co.price * (1.0 - discount))
    cash = issue_price * qty
    co.shares += qty
    co.cash += cash
    co.price = issue_price              # 市场按配售价重估（这才是"压低股价"）
    co.note_price()
    game.log(f"{co.name} 增发 {qty:.0f} 万股（占 {ratio:.1%}），募资 {money(cash)}，"
             f"股价砸到 {co.price:.2f}（折价 {discount:.1%}）")
    return cash


# ---------------------------------------------------------------- 控股阶梯
def _ladder_name(th: float) -> str:
    return {
        STAKE_ALERT: "举牌线",
        STAKE_BOARD: "董事席位",
        STAKE_CONTROL: "控股线",
        STAKE_MERGE: "合并线",
    }.get(th, f"{th:.0%}")


def control_stage(game, target: Company) -> str:
    st = player_stake(game, target)
    if st >= STAKE_MERGE:
        return "merge"
    if st >= STAKE_CONTROL:
        return "control"
    if st >= STAKE_BOARD:
        return "board"
    if st >= STAKE_ALERT:
        return "alert"
    return "none"


def take_control(game, target: Company) -> str:
    """51%：控股接管。可以抽血（强制全额分红）、处置冗余资产。"""
    if player_stake(game, target) < STAKE_CONTROL:
        raise MaError("持股不足 51%，无法控股")
    if target.controlled_by == game.player_idx:
        raise MaError("已经控股该公司")
    if target.delisted:
        raise MaError("该公司已退市")
    target.controlled_by = game.player_idx
    target.payout = 1.0
    game.log(f"★ 你控股了 {target.name}！已接管董事会，可强制分红抽血、淘汰其老旧电厂。")
    target.reputation = max(0.0, target.reputation - 2.0)
    return "control"


def force_dividend(game, target: Company) -> None:
    if target.controlled_by != game.player_idx:
        raise MaError("需要先控股")
    target.payout = 1.0
    game.log(f"{target.name} 被要求全额分红，现金将被抽往股东。")


def retire_plant(game, co: Company, plant_id: int, by_player: bool = False) -> None:
    """关停/淘汰机组：省掉固定成本，但损失账面价值并掉声望。"""
    plant = next((p for p in co.plants if p.id == plant_id), None)
    if plant is None:
        raise MaError("机组不存在")
    co.plants.remove(plant)
    w = game.world_for_city(plant.city) if plant.x >= 0 else None
    if w is not None:
        tile = w.tile(plant.x, plant.y)
        if tile.owner == co.idx and tile.ref == plant.id:
            tile.owner, tile.kind, tile.ref = -1, "", -1
    loss = plant.book_value
    co.cash = max(0.0, co.cash - 300.0)   # 拆除费用
    co.reputation = max(0.0, co.reputation - (2.0 if by_player else 1.0))
    game.log(f"{co.name} 淘汰了 {plant.name}（{plant.cap:.0f}MW），账面损失 {money(loss)}")


def absorb_merge(game, target: Company) -> None:
    """75%：吸收合并。资产、负债、矿权、人力全部并入母公司，子公司退市。"""
    if player_stake(game, target) < STAKE_MERGE:
        raise MaError("持股不足 75%，无法吸收合并")
    if target.delisted:
        raise MaError("该公司已退市")
    me = game.companies[game.player_idx]
    net = target.assets() - target.debt
    fee = max(0.0, net) * INTEGRATION_COST_RATIO
    if me.cash < fee:
        raise MaError(f"整合费用不足（需要 {money(fee)}）")
    me.cash -= fee

    # 资产搬家
    for p in target.plants:
        p.owner = me.idx
        me.plants.append(p)
        if p.x >= 0:
            w = game.world_for_city(p.city)
            if w is not None:
                t = w.tile(p.x, p.y)
                t.owner = me.idx
                t.ref = p.id
    for m in target.mines:
        m.owner = me.idx
        me.mines.append(m)
        if m.x >= 0:
            w = game.world_for_city(m.city)
            if w is not None:
                t = w.tile(m.x, m.y)
                t.owner = me.idx
                t.ref = m.id
    for pr in target.projects:
        pr.owner = me.idx
        me.projects.append(pr)
        if pr.x >= 0:
            w = game.world_for_city(pr.city)
            if w is not None:
                t = w.tile(pr.x, pr.y)
                t.owner = me.idx
    if target.home_city >= 0:
        c = game.city_state(target.home_city)
        if c is not None:
            c.owner = me.idx
            c.unlocked = True

    me.cash += target.cash
    me.debt += target.debt
    me.ops_staff += target.ops_staff
    me.eng_teams += target.eng_teams
    me.reputation = max(0.0, me.reputation - 4.0)

    target.plants, target.mines, target.projects = [], [], []
    target.cash, target.debt = 0.0, 0.0
    target.ops_staff = target.eng_teams = 0
    target.alive = False
    target.delisted = True
    target.controlled_by = me.idx
    for co in game.companies:
        co.holdings.pop(target.idx, None)
    game.ceo_holdings.pop(target.idx, None)
    game.log(f"★★ 你吸收合并了 {target.name}！资产/债务/矿权/人力全部并入，对方退市。"
             f"整合费用 {money(fee)}。")
