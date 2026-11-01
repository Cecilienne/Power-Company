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

TS = 60          # 每格像素：18×18 格 = 1080×1080 像素
BASE_TS = 60     # 100% 缩放时的格子边长（18×60 = 1080）


ZOOM_MIN = 0.5
ZOOM_MAX = 2.0
ZOOM_STEP = 1.15


class MapView(ttk.Frame):
    """格子地图。画布是"取景框"，整张棋盘画在里面，可缩放、可拖动。

    - 滚轮：以光标为中心缩放（50%~200%）
    - 中键拖动 / Shift+左键拖动：平移
    - `fit()`：缩放到刚好装下整张棋盘并居中（App 的「适应」按钮）
    「适应」之后若窗口变化，App 会重新 fit；玩家自己缩放/拖动过就不再抢。
    """

    def __init__(self, master, on_click: Callable[[int, int], None],
                 on_hover: Callable[[Optional[tuple[int, int]]], None],
                 on_zoom: Optional[Callable[[float], None]] = None):
        super().__init__(master)
        self.on_click = on_click
        self.on_hover = on_hover
        self.on_zoom = on_zoom
        self.game = None
        self.selected: Optional[tuple[int, int]] = None
        self.build_mode: Optional[str] = None
        self.valid_tiles: set[tuple[int, int]] = set()
        self.zoom = 1.0                 # 相对基准格子边长的倍率
        self.manual = False             # 玩家自己缩放过 → 窗口变化不再自动 fit
        self._pan_from: Optional[tuple[int, int]] = None

        self._busy = False
        self.canvas = tk.Canvas(self, bg=UI_BG, highlightthickness=0, bd=0)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self.on_hover(None))
        # 缩放：滚轮；平移：中键拖 / Shift+左键拖
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._pan_end)
        self.canvas.bind("<Shift-ButtonPress-1>", self._pan_start)
        self.canvas.bind("<Shift-B1-Motion>", self._pan_move)
        self.canvas.bind("<Shift-ButtonRelease-1>", self._pan_end)
        self.bind("<Configure>", self._on_resize)

    # ------------------------------------------------------------ 交互
    def _tile_at(self, ex: int, ey: int) -> Optional[tuple[int, int]]:
        if self.game is None:
            return None
        x, y = int(self.canvas.canvasx(ex) // TS), int(self.canvas.canvasy(ey) // TS)
        if 0 <= x < self.game.world.w and 0 <= y < self.game.world.h:
            return x, y
        return None

    def _click(self, event) -> None:
        if event.state & 0x0001:        # Shift+左键是拖动，不是点击
            return
        pos = self._tile_at(event.x, event.y)
        if pos:
            self.selected = pos
            self.on_click(*pos)

    def _motion(self, event) -> None:
        self.on_hover(self._tile_at(event.x, event.y))

    # ------------------------------------------------------------ 尺寸 / 缩放
    def set_tile(self, ts: int) -> None:
        """直接指定格子边长（App 启动时用），会重画并保持当前视点。"""
        if self.game is not None:
            self._keep_centre(lambda: self._set_ts(max(8, int(ts))))
        else:
            self._set_ts(max(8, int(ts)))

    def _set_ts(self, ts: int) -> None:
        global TS
        TS = max(8, int(ts))
        if self.game is not None:
            self.draw(self.game)
        self._sync_scrollregion()
        self._layout()

    def _layout(self) -> None:
        """棋盘比视口小 → 画布缩成棋盘大小并居中；比视口大 → 画布铺满当取景框。"""
        c = self.canvas
        bw, bh = self._board_px()
        vw, vh = max(1, self.winfo_width()), max(1, self.winfo_height())
        # 哪个方向装不下，那个方向才出现"取景框"；另一方向仍旧居中。
        c.place(relx=0.5, rely=0.5, anchor="center",
                width=min(bw, vw), height=min(bh, vh))
        c.configure(scrollregion=(0, 0, bw, bh))

    def _on_resize(self, _event=None) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            self._layout()
            if not self.manual:
                self.fit()
        finally:
            self._busy = False

    def _sync_scrollregion(self) -> None:
        b = self._board_px()
        self.canvas.configure(scrollregion=(0, 0, b[0], b[1]))

    def _board_px(self) -> tuple[int, int]:
        w = self.game.world.w if self.game is not None else GRID_W
        h = self.game.world.h if self.game is not None else GRID_H
        return w * TS, h * TS

    def base_ts(self) -> int:
        return BASE_TS

    def zoom_by(self, factor: float, ex: Optional[int] = None,
                ey: Optional[int] = None) -> None:
        self.set_zoom(self.zoom * factor, ex, ey)

    def set_zoom(self, zoom: float, ex: Optional[int] = None,
                 ey: Optional[int] = None) -> None:
        """缩放到 zoom（夹在 50%~200%），默认以视口中心为锚点。"""
        if self.game is None:
            return
        zoom = max(ZOOM_MIN, min(ZOOM_MAX, float(zoom)))
        ts = max(8, int(round(BASE_TS * zoom)))
        if ts == TS:
            self.zoom = zoom
            return
        self._keep_anchor(ts, ex, ey)
        self.zoom = ts / BASE_TS
        self.manual = True
        if self.on_zoom is not None:
            self.on_zoom(self.zoom)

    def fit(self) -> None:
        """缩放到刚好装下整张棋盘并居中；装得下就是 100%，不放大。"""
        if self.game is None:
            return
        vw = max(1, self.winfo_width())
        vh = max(1, self.winfo_height())
        board = GRID_W * BASE_TS
        zoom = min(1.0, vw / board, vh / board)
        ts = max(8, int(round(BASE_TS * zoom)))
        self._busy = True
        try:
            self._set_ts(ts)
        finally:
            self._busy = False
        self.zoom = ts / BASE_TS
        self.manual = False
        self._centre()
        if self.on_zoom is not None:
            self.on_zoom(self.zoom)

    def _keep_anchor(self, ts: int, ex: Optional[int], ey: Optional[int]) -> None:
        """缩放时让光标下的那个点留在原地。"""
        c = self.canvas
        if ex is None:
            ex, ey = c.winfo_width() // 2, c.winfo_height() // 2
        cx, cy = c.canvasx(ex), c.canvasy(ey)
        old = TS
        self._set_ts(ts)
        nx, ny = cx / old * TS, cy / old * TS
        self._view_to(nx - ex, ny - ey)

    def _keep_centre(self, fn) -> None:
        c = self.canvas
        b = self._board_px()
        cx = c.canvasx(c.winfo_width() // 2)
        cy = c.canvasy(c.winfo_height() // 2)
        fx, fy = cx / max(1, b[0]), cy / max(1, b[1])
        fn()
        b2 = self._board_px()
        self._view_centre(fx * b2[0], fy * b2[1])

    def _view_to(self, x0: float, y0: float) -> None:
        c = self.canvas
        bw, bh = self._board_px()
        vw, vh = c.winfo_width(), c.winfo_height()
        x0 = max(0.0, min(max(0.0, bw - vw), x0))
        y0 = max(0.0, min(max(0.0, bh - vh), y0))
        c.xview_moveto(x0 / max(1, bw))
        c.yview_moveto(y0 / max(1, bh))

    def _view_centre(self, cx: float, cy: float) -> None:
        c = self.canvas
        self._view_to(cx - c.winfo_width() / 2, cy - c.winfo_height() / 2)

    def _centre(self) -> None:
        bw, bh = self._board_px()
        self._view_centre(bw / 2, bh / 2)

    # -- 滚轮 / 拖动
    def _on_wheel(self, event) -> None:
        step = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        self.zoom_by(step, event.x, event.y)
        return "break"

    def _pan_start(self, event) -> None:
        self._pan_from = (event.x, event.y)
        try:
            self.canvas.configure(cursor="fleur")
        except tk.TclError:
            pass

    def _pan_move(self, event) -> None:
        if self._pan_from is None:
            return
        dx = event.x - self._pan_from[0]
        dy = event.y - self._pan_from[1]
        self._pan_from = (event.x, event.y)
        c = self.canvas
        self._view_to(c.canvasx(0) - dx, c.canvasy(0) - dy)
        self.manual = True

    def _pan_end(self, _event) -> None:
        self._pan_from = None
        try:
            self.canvas.configure(cursor="")
        except tk.TclError:
            pass

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
        self._sync_scrollregion()

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
