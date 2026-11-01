"""2D 格子地图视图（tkinter Canvas）。

约定：
- 俯视 2D，格子边长 TS（按 DPI 缩放），不做斜角/伪 3D。
- 地块底色 = 地形；边框颜色 = 归属公司；符号 = 建筑类型；连线 = 输电网。
- 所有内部尺寸都从 TS 推导，保证任意 DPI 下比例一致。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from ..constants import (
    COMPANY_COLORS,
    INFRA_SPECS,
    PLANT_SPECS,
    TERRAIN_INFO,
    UI_BG,
    UI_DIM,
)
from ..world import CITY_RADIUS, GRID_H, GRID_W, Terrain

TS = 36          # 每格像素（由 App 在初始化时按 DPI 改写）


class MapView(ttk.Frame):
    def __init__(self, master, on_click: Callable[[int, int], None],
                 on_hover: Callable[[Optional[tuple[int, int]]], None]):
        super().__init__(master)
        self.on_click = on_click
        self.on_hover = on_hover
        self.game = None
        self.selected: Optional[tuple[int, int]] = None
        self.build_mode: Optional[str] = None
        self.valid_tiles: set[tuple[int, int]] = set()
        self.canvas = tk.Canvas(self, width=GRID_W * TS, height=GRID_H * TS,
                                bg=UI_BG, highlightthickness=0)
        self.canvas.pack(padx=4, pady=4)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self.on_hover(None))

    # ------------------------------------------------------------ 交互
    def _tile_at(self, ex: int, ey: int) -> Optional[tuple[int, int]]:
        if self.game is None:
            return None
        x, y = ex // TS, ey // TS
        if 0 <= x < self.game.world.w and 0 <= y < self.game.world.h:
            return x, y
        return None

    def _click(self, event) -> None:
        pos = self._tile_at(event.x, event.y)
        if pos:
            self.selected = pos
            self.on_click(*pos)

    def _motion(self, event) -> None:
        self.on_hover(self._tile_at(event.x, event.y))

    # ------------------------------------------------------------ 尺寸
    def set_tile(self, ts: int) -> None:
        """按可用面积重算格子边长（App 在地图区尺寸变化时调用）。"""
        global TS
        TS = max(8, int(ts))
        w = self.game.world.w if self.game is not None else 18
        h = self.game.world.h if self.game is not None else 12
        self.canvas.config(width=w * TS, height=h * TS)
        if self.game is not None:
            self.draw(self.game)

    # ------------------------------------------------------------ 绘制
    def draw(self, game) -> None:
        self.game = game
        c = self.canvas
        c.delete("all")
        w = game.world
        ts = TS
        for row in w.tiles:
            for t in row:
                x0, y0 = t.x * ts, t.y * ts
                info = TERRAIN_INFO[t.terrain]
                fill = info.color if t.terrain is not Terrain.CITY else "#8f7f6d"
                c.create_rectangle(x0, y0, x0 + ts, y0 + ts, fill=fill,
                                   outline="#2b2f36", width=1)
                if t.coal > 0 and not t.occupied:
                    r = ts * 0.09
                    c.create_oval(x0 + ts * 0.18 - r, y0 + ts * 0.82 - r,
                                  x0 + ts * 0.18 + r, y0 + ts * 0.82 + r,
                                  fill="#1e1e1e", outline="#000")
                if t.uranium > 0 and not t.occupied:
                    r = ts * 0.09
                    c.create_oval(x0 + ts * 0.82 - r, y0 + ts * 0.82 - r,
                                  x0 + ts * 0.82 + r, y0 + ts * 0.82 + r,
                                  fill="#3fae5a", outline="#000")
        # 建造模式：只压暗地面（不可建的地块），建筑与城市仍保持清晰
        if self.build_mode:
            for row in w.tiles:
                for t in row:
                    if (t.x, t.y) in self.valid_tiles:
                        continue
                    c.create_rectangle(t.x * ts + 1, t.y * ts + 1,
                                       t.x * ts + ts - 1, t.y * ts + ts - 1,
                                       fill="#0c0e12", outline="", stipple="gray50")
        # 输电线路：先画在地块之上、建筑之下
        for row in w.tiles:
            for t in row:
                if t.kind != "line" or t.owner < 0:
                    continue
                x0, y0 = t.x * ts, t.y * ts
                cx, cy = x0 + ts / 2, y0 + ts / 2
                col = COMPANY_COLORS[t.owner % len(COMPANY_COLORS)]
                for nx, ny in w.neighbors4(t.x, t.y):
                    n = w.tile(nx, ny)
                    if n.owner == t.owner and n.kind in (
                            "line", "substation", "coal", "gas", "hydro", "wind",
                            "solar", "nuclear"):
                        c.create_line(cx, cy, nx * ts + ts / 2, ny * ts + ts / 2,
                                      fill=col, width=max(2, int(ts * 0.07)))
                r = max(2, ts * 0.06)
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline="")
        # 城市供电服务圈（变电站必须落在这个圈里）
        for city in w.cities:
            c.create_rectangle((city.x - CITY_RADIUS) * ts + 2, (city.y - CITY_RADIUS) * ts + 2,
                               (city.x + CITY_RADIUS + 1) * ts - 2, (city.y + CITY_RADIUS + 1) * ts - 2,
                               outline="#ffd479", width=1, dash=(4, 3))
        for row in w.tiles:
            for t in row:
                self._draw_occupant(game, t)
        if self.selected:
            x, y = self.selected
            c.create_rectangle(x * ts, y * ts, x * ts + ts, y * ts + ts,
                               outline="#ffffff", width=2)

    def _cc(self, idx: int) -> str:
        return COMPANY_COLORS[idx % len(COMPANY_COLORS)]

    def _draw_occupant(self, game, t) -> None:
        c = self.canvas
        ts = TS
        x0, y0 = t.x * ts, t.y * ts
        cx, cy = x0 + ts / 2, y0 + ts / 2
        if t.kind == "city":
            city = game.world.city_at(t.x, t.y)
            bw = ts * 0.26
            c.create_rectangle(cx - bw * 1.4, cy - bw * 0.9, cx - bw * 0.1, cy + bw * 1.1,
                               fill="#d8d2c4", outline="#5a5246")
            c.create_rectangle(cx - bw * 0.3, cy - bw * 1.5, cx + bw * 0.7, cy + bw * 1.1,
                               fill="#c9c2b2", outline="#5a5246")
            c.create_rectangle(cx + bw * 0.9, cy - bw * 0.6, cx + bw * 1.9, cy + bw * 1.1,
                               fill="#d8d2c4", outline="#5a5246")
            if city:
                c.create_text(cx, y0 + ts * 0.80, text=f"{city.name} {city.demand_mw:.0f}MW",
                              fill="#ffffff", font=("Microsoft YaHei UI", -int(ts * 0.19), "bold"))
                # 市场份额条：谁在给这座城市供电
                bx = x0 + ts * 0.08
                bw = ts * 0.84
                by = y0 + ts * 0.88
                bh = max(2, ts * 0.07)
                c.create_rectangle(bx, by, bx + bw, by + bh,
                                   fill="#2a2a2a", outline="")
                acc = 0.0
                for cid in sorted(city.share):
                    frac = city.share[cid]
                    if frac <= 0.01:
                        continue
                    c.create_rectangle(bx + bw * acc, by,
                                       bx + bw * (acc + frac), by + bh,
                                       fill=COMPANY_COLORS[cid % len(COMPANY_COLORS)],
                                       outline="")
                    acc += frac
            return
        if not t.kind:
            return
        color = self._cc(t.owner) if t.owner >= 0 else UI_DIM
        if t.kind in PLANT_SPECS:
            spec = PLANT_SPECS[t.kind]
            c.create_rectangle(x0 + ts * 0.14, y0 + ts * 0.18,
                               x0 + ts * 0.86, y0 + ts * 0.68,
                               fill=spec.color, outline=color, width=2)
            c.create_text(cx, y0 + ts * 0.43, text=spec.short, fill="#ffffff",
                          font=("Microsoft YaHei UI", -int(ts * 0.30), "bold"))
            plant = None
            if t.owner >= 0:
                plant = next((p for p in game.companies[t.owner].plants
                              if p.x == t.x and p.y == t.y), None)
            if plant is not None:
                c.create_rectangle(x0 + ts * 0.14, y0 + ts * 0.76,
                                   x0 + ts * 0.86, y0 + ts * 0.90,
                                   fill="#333333", outline="")
                cfw = (ts * 0.72) * max(0.0, min(1.0, plant.cf_actual))
                c.create_rectangle(x0 + ts * 0.14, y0 + ts * 0.76,
                                   x0 + ts * 0.14 + cfw, y0 + ts * 0.90,
                                   fill="#57c785" if plant.online else "#e0605e", outline="")
        elif t.kind in ("coal_mine", "uranium_mine"):
            r = ts * 0.30
            c.create_oval(cx - r, cy - r, cx + r, cy + r,
                          fill="#2a2a2a", outline=color, width=2)
            c.create_text(cx, cy, text=INFRA_SPECS[t.kind].short, fill="#f0e6c8",
                          font=("Microsoft YaHei UI", -int(ts * 0.26), "bold"))
            mine = None
            if t.owner >= 0:
                mine = next((m for m in game.companies[t.owner].mines
                             if m.x == t.x and m.y == t.y), None)
            if mine:
                pct = min(1.0, mine.reserve / max(1.0, mine.output * 60))
                c.create_rectangle(x0 + ts * 0.20, y0 + ts * 0.86,
                                   x0 + ts * 0.80, y0 + ts * 0.94, fill="#333333", outline="")
                c.create_rectangle(x0 + ts * 0.20, y0 + ts * 0.86,
                                   x0 + ts * 0.20 + ts * 0.60 * pct, y0 + ts * 0.94,
                                   fill="#c9a227", outline="")
        elif t.kind == "substation":
            c.create_polygon(cx, cy - ts * 0.34, cx + ts * 0.32, cy,
                             cx, cy + ts * 0.34, cx - ts * 0.32, cy,
                             fill="#f2ecd8", outline=color, width=2)
            c.create_text(cx, cy, text="变", fill="#3b3b3b",
                          font=("Microsoft YaHei UI", -int(ts * 0.22), "bold"))
        if t.ref < 0 and t.owner >= 0:
            c.create_rectangle(x0 + ts * 0.10, y0 + ts * 0.10,
                               x0 + ts * 0.90, y0 + ts * 0.90,
                               outline="#e5b454", width=2, dash=(3, 2))
            pr = next((p for p in game.companies[t.owner].projects
                       if p.x == t.x and p.y == t.y), None)
            if pr:
                c.create_text(cx, y0 + ts * 0.30, text=f"建{pr.turns_left}", fill="#ffd479",
                              font=("Microsoft YaHei UI", -int(ts * 0.20), "bold"))


def tile_tooltip(game, x: int, y: int) -> str:
    """状态栏用的地块信息。"""
    t = game.world.tile(x, y)
    parts = [f"({x},{y}) {TERRAIN_INFO[t.terrain].name}"]
    if t.terrain is not Terrain.CITY:
        parts.append(f"风 {t.wind:.2f} / 光 {t.solar:.2f}")
    city = game.world.city_at(x, y)
    if city:
        shares = "  ".join(f"{game.companies[cid].name}{v:.0%}"
                           for cid, v in city.share.items() if v > 0.01)
        parts.append(f"城市 需求 {city.demand_mw:.0f}MW")
        if shares:
            parts.append("份额: " + shares)
        if city.outage_mw > 1:
            parts.append(f"缺电 {city.outage_mw:.0f}MW")
    if t.coal > 0:
        parts.append(f"煤矿 储量 {t.coal:,.0f}t")
    if t.uranium > 0:
        parts.append(f"铀矿 储量 {t.uranium:,.0f}t")
    if t.kind and t.kind not in ("line", "city"):
        owner = game.companies[t.owner].name if t.owner >= 0 else "无主"
        parts.append(f"{owner}·{_kind_name(t.kind)}")
    return "  |  ".join(parts)


def _kind_name(kind: str) -> str:
    if kind in PLANT_SPECS:
        return PLANT_SPECS[kind].name
    if kind in INFRA_SPECS:
        return INFRA_SPECS[kind].name
    return kind
