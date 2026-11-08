"""股价模型 —— 经营通向金融的桥梁。

设计要点（设计文档 §三.7）：股价不是 K 线玄学，是经营结果的函数。
    股价  ← 滚动四季度净利润(EPS) × 估值倍数(PE)
           + 净资产锚定(市净率)
           + 市占率 / 分红率 / 资产负债率 / 声望 / 重大事件
再做一次"向公允价值收敛"的平滑，并保留少量噪音，避免完全可预测。
"""
from __future__ import annotations

import random

from .company import Company
from .constants import (
    PE_BASE,
    PE_MAX,
    PE_MIN,
    PRICE_NOISE,
    PRICE_SMOOTH,
)

MIN_PRICE = 0.30


def ttm_profit(co: Company) -> float:
    return sum(co.profit_history[-4:])


def pe_ratio(co: Company) -> float:
    share = getattr(co, "market_share", 0.0)
    pe = PE_BASE
    pe += 7.0 * min(0.8, share)            # 市占率溢价
    pe += 1.8 * co.payout                  # 分红预期
    pe -= 8.0 * max(0.0, co.debt_ratio() - 0.45)   # 高杠杆折价
    pe -= 3.0 * (1.0 - co.reputation / 100.0)      # 口碑折价
    pe -= 2.5 * len(getattr(co, "risk_flags", []))  # 事故/处罚等风险标记
    # 增长：近两季度利润加速
    ph = co.profit_history[-2:]
    if len(ph) == 2 and ph[1] > ph[0] and ph[1] > 0:
        pe += 2.0
    elif len(ph) == 2 and ph[1] < 0:
        pe -= 2.0
    return max(PE_MIN, min(PE_MAX, pe))


def fair_price(co: Company) -> float:
    bvps = co.book_per_share()
    eps = ttm_profit(co) / max(1.0, co.shares)
    if eps > 0:
        v = eps * pe_ratio(co)
    else:
        v = 0.40 * bvps
    v = 0.78 * v + 0.22 * max(0.0, bvps) * 1.05
    return max(MIN_PRICE, v)


def update_price(co: Company, rng: random.Random) -> None:
    co.price_prev = co.price
    fair = fair_price(co)
    co.fair_value = fair
    drift = PRICE_SMOOTH * (fair - co.price)
    noise = rng.gauss(0.0, PRICE_NOISE) * max(1.0, co.price)
    co.price = max(MIN_PRICE, co.price + drift + noise)
    co.note_price()                     # 记入本回合 K 线的高/低
    co.price_history.append(co.price)
    if len(co.price_history) > 40:
        co.price_history = co.price_history[-40:]


def update_all_prices(game) -> None:
    total = sum(c.demand_mw for c in game.cities) or 1.0
    for co in game.companies:
        if not co.alive or co.delisted:
            continue
        co.market_share = min(1.0, co.served_mw / total)
        update_price(co, game.rng)
        co.close_candle()               # 收出本回合的 K 线
    # 子公司不再是独立上市公司，其价值体现在母公司的分红/股价上
