"""2D 格子地图视图（tkinter Canvas）。

约定：
- 俯视 2D，格子边长 TS（按 DPI 缩放），不做斜角/伪 3D。
- 地块底色 = 地形；边框颜色 = 归属公司；符号 = 建筑类型；连线 = 输电网。
- 所有内部尺寸都从 TS 推导，保证任意 DPI 下比例一致。
"""
from __future__ import annotations

import math
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
from ..world import (CITY_RADIUS, CITY_TERRAIN_IDX, GRID_H, GRID_W,
                     TERRAIN_ORDER, Terrain)

TS = 6           # 当前每格像素（随缩放变化，见 _set_ts）
BASE_TS = 60     # 100% 缩放时的格子边长
_LINE_JOIN_KINDS = {"line", "substation", "coal", "gas", "hydro", "wind",
                    "solar", "nuclear"}


ZOOM_MIN = 0.02     # 960×960 时整图要缩到 ~2%
ZOOM_MAX = 2.0
ZOOM_STEP = 1.15


class MapView(ttk.Frame):
    """格子地图。画布是"取景框"，整张棋盘画在里面，可缩放、可拖动。

    - 滚轮：以光标为中心缩放（50%~200%）
    - 右键拖动（也支持中键 / Shift+左键拖动）：平移
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
        self._redraw_job: Optional[str] = None
        self._drawn: Optional[tuple[int, int, int, int]] = None
        self._shades: Optional[list[tuple[str, str, str]]] = None
        self._shade_key: tuple = ()

        self._busy = False
        self.canvas = tk.Canvas(self, bg=UI_BG, highlightthickness=0, bd=0)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self.on_hover(None))
        # 缩放：滚轮；平移：右键拖（主）/ 中键拖 / Shift+左键拖
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-3>", self._pan_start)
        self.canvas.bind("<B3-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-3>", self._pan_end)
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
            self._keep_centre(lambda: self._set_ts(max(1, int(ts))))
        else:
            self._set_ts(max(1, int(ts)))

    def _notify_zoom(self) -> None:
        if self.on_zoom is None:
            return
        try:
            self.on_zoom(self.zoom)
        except Exception:  # noqa: BLE001
            pass

    def _set_ts(self, ts: int) -> None:
        global TS
        TS = max(1, int(ts))
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

    def focus_on(self, tx: int, ty: int, zoom: float = 0.5) -> None:
        """把镜头对准某一格（开局用：直接看自己的电厂，而不是 2% 的全图）。"""
        if self.game is None:
            return
        zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        self._busy = True
        try:
            self._set_ts(max(1, int(round(BASE_TS * zoom))))
        finally:
            self._busy = False
        self.zoom = TS / BASE_TS
        self._notify_zoom()
        self._layout()
        c = self.canvas
        vw, vh = max(1, c.winfo_width()), max(1, c.winfo_height())
        self._view_to(tx * TS - vw / 2, ty * TS - vh / 2)
        self.manual = True

    def fit(self) -> None:
        """缩放到刚好装下整张棋盘并居中；装得下就是 100%，不放大。"""
        if self.game is None:
            return
        vw = max(1, self.winfo_width())
        vh = max(1, self.winfo_height())
        w = self.game.world
        board = max(w.w, w.h) * BASE_TS
        zoom = min(1.0, vw / board, vh / board)
        ts = max(1, int(round(BASE_TS * zoom)))
        self._busy = True
        try:
            self._set_ts(ts)
        finally:
            self._busy = False
        self.zoom = ts / BASE_TS
        self._notify_zoom()
        self.manual = False
        self._centre()
        self._redraw_now()
        if self.on_zoom is not None:
            self.on_zoom(self.zoom)

    def _redraw_soon(self) -> None:
        """视点变了就补画一次（合并到下一帧，别每次鼠标移动都重画）。"""
        if self._redraw_job is not None or self.game is None:
            return
        try:
            self._redraw_job = self.after(30, self._redraw_now)
        except Exception:  # noqa: BLE001
            self._redraw_job = None

    def _redraw_now(self) -> None:
        self._redraw_job = None
        if self.game is None or self._busy:
            return
        # 拖动/滚轮只是移动视口：画过的那块还盖得住就交给 canvas 自己滚，
        # 不必把几千个矩形删了重画（Tk 上重画一次要 20~50ms，拖起来就是卡）
        if self._drawn is not None:
            xa, ya, xb, yb = self._visible_tiles(pad=0.0)
            dxa, dya, dxb, dyb = self._drawn
            if dxa <= xa and dya <= ya and xb <= dxb and yb <= dyb:
                return
        self.draw(self.game)

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
        self._redraw_soon()

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
    def _visible_tiles(self, pad: float = 0.35) -> tuple[int, int, int, int]:
        """取景框里可见的格子范围（含边界，已裁到地图内）。

        `pad` 是四周多画一圈的比例：拖动时先把下一屏也画好，滚出去不至于空白。
        """
        c = self.canvas
        ts = max(1, TS)
        x0, y0 = c.canvasx(0), c.canvasy(0)
        vw = max(1, c.winfo_width())
        vh = max(1, c.winfo_height())
        mx, my = vw * pad, vh * pad
        return (max(0, int((x0 - mx) // ts)), max(0, int((y0 - my) // ts)),
                int((x0 + vw + mx) // ts), int((y0 + vh + my) // ts))

    def _shade_table(self) -> list[tuple[str, str, str]]:
        """每种地形按高程分三档，给底图上一点起伏感（同色块太板）。"""
        key = tuple(TERRAIN_ORDER)
        if getattr(self, "_shades", None) is not None and self._shade_key == key:
            return self._shades
        table = []
        for t in TERRAIN_ORDER:
            base = TERRAIN_INFO[t].color
            table.append(tuple(_shade(base, f) for f in (0.87, 0.95, 1.03, 1.11)))
        self._shades = table
        self._shade_key = key
        return table

    def _lod_block(self) -> int:
        """格子太小时把 block×block 归成一个色块。

        960×960 缩到整图时一格不到 2 像素，逐格画要 92 万个矩形；
        按"一格至少 ~14 像素"聚合，可见矩形数稳定在几千个以内。
        """
        ts = max(1, TS)
        # 每块至少 ~26px：Tk 的 canvas 一帧画得下几千个矩形，但滚动时整块重绘，
        # 6000 个 item 滚一下就要 40~60ms，拖起来发滞；控制在两千出头的量级
        block = 1 if ts >= 12 else max(2, int(math.ceil(12.0 / ts)))
        # 再按"最多 ~6000 个矩形"兜一次：整图缩放时 TS 只有 3~4，
        # 5,000~8,000 个矩形在 Tk 上就要 30~60ms，拖起来会明显卡
        xa, ya, xb, yb = self._visible_tiles(pad=0.0)
        while (xb - xa + 1) > 0 and ((xb - xa + 1) // block + 1) * ((yb - ya + 1) // block + 1) > 4000:
            block += 1
        return block

    def draw(self, game) -> None:
        self.game = game
        c = self.canvas
        c.delete("all")
        w = game.world
        ts = max(1, TS)
        W, H = w.w, w.h
        xa, ya, xb, yb = self._visible_tiles()
        xb, yb = min(xb, W - 1), min(yb, H - 1)
        if xb < xa or yb < ya:
            self._drawn = None
            self._sync_scrollregion()
            return
        self._drawn = (xa, ya, xb, yb)
        block = self._lod_block()

        # 1) 地形底图（LOD 聚合）
        shades = self._shade_table()
        terr = w.terr
        elev = w.elev
        # 网格线平时不画（整片格子像坐标纸）；只有建造模式才显示，方便对准格点
        outline = "#2b2f36" if (self.build_mode and block == 1 and ts >= 6) else ""
        span = ts * block
        half = block // 2
        # 同一行里颜色相同的相邻块合成一个矩形：整片平原/海面只占几个 item。
        # 有网格线时不合并（否则格子线就没了）。
        merge = not outline
        for by in range(ya, yb + 1, block):
            sy = min(H - 1, by + half)
            run_x = -1
            run_fill = ""
            for bx in range(xa, xb + 1, block):
                sx = min(W - 1, bx + half)
                ti = terr[sy * W + sx]
                if ti == CITY_TERRAIN_IDX:
                    fill = "#8f7f6d"
                else:
                    name = TERRAIN_ORDER[ti].value
                    fill = shades[ti][_elev_bucket(elev[sy * W + sx], name)]
                if merge:
                    if fill == run_fill:
                        continue
                    if run_x >= 0:
                        c.create_rectangle(run_x * ts, by * ts,
                                           bx * ts, by * ts + span,
                                           fill=run_fill, outline="")
                    run_x, run_fill = bx, fill
                else:
                    x0 = bx * ts
                    y0 = by * ts
                    c.create_rectangle(x0, y0, x0 + span, y0 + span,
                                       fill=fill, outline=outline, width=1)
            if merge and run_x >= 0:
                end = (xb + block) * ts
                c.create_rectangle(run_x * ts, by * ts, end, by * ts + span,
                                   fill=run_fill, outline="")

        # 2) 线路（在所有地块之上、建筑之下）
        for owner, x, y in w.all_owned():
            if w.kind[y * W + x] != "line":
                continue
            cx, cy = x * ts + ts / 2, y * ts + ts / 2
            col = COMPANY_COLORS[owner % len(COMPANY_COLORS)]
            for nx, ny in w.neighbors4(x, y):
                n = ny * W + nx
                if w.own[n] != owner:
                    continue
                if w.kind[n] not in _LINE_JOIN_KINDS:
                    continue
                c.create_line(cx, cy, nx * ts + ts / 2, ny * ts + ts / 2,
                              fill=col, width=max(2, int(ts * 0.07)))
            r = max(1, ts * 0.06)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline="")

        # 3) 城市：缩到全图时也要能看见，所以先画一个标记点
        for city in w.cities:
            cx, cy = city.x * ts + ts / 2, city.y * ts + ts / 2
            r = max(4.0, ts * 0.55)
            c.create_rectangle(cx - r, cy - r, cx + r, cy + r,
                               fill="#f4f2ec", outline="#8f7f6d", width=1)
        # 3b) 城市服务圈（变电站必须落在这个圈里）
        for city in w.cities:
            if not (xa - CITY_RADIUS - 1 <= city.x <= xb + CITY_RADIUS + 1
                    and ya - CITY_RADIUS - 1 <= city.y <= yb + CITY_RADIUS + 1):
                continue
            c.create_rectangle((city.x - CITY_RADIUS) * ts + 2, (city.y - CITY_RADIUS) * ts + 2,
                               (city.x + CITY_RADIUS + 1) * ts - 2,
                               (city.y + CITY_RADIUS + 1) * ts - 2,
                               outline="#ffd479", width=1, dash=(4, 3))

        # 4) 建筑 / 资源点：稀疏遍历（有主的格子只有几十个、矿点只有几个），
        #    别按可视矩形逐格扫——整图缩放时那是十几万次循环
        coal, uranium = w.coal, w.uranium
        for _owner, x, y in w.all_owned():
            if xa <= x <= xb and ya <= y <= yb:
                self._draw_occupant(game, w.tile(x, y))
        for sx, sy in w.coal_spots:
            if xa <= sx <= xb and ya <= sy <= yb and coal[sy * W + sx] > 0:
                x0, y0 = sx * ts, sy * ts
                r = max(1.5, ts * 0.09)
                c.create_oval(x0 + ts * 0.18 - r, y0 + ts * 0.82 - r,
                              x0 + ts * 0.18 + r, y0 + ts * 0.82 + r,
                              fill="#1e1e1e", outline="#000")
        for sx, sy in w.uranium_spots:
            if xa <= sx <= xb and ya <= sy <= yb and uranium[sy * W + sx] > 0:
                x0, y0 = sx * ts, sy * ts
                r = max(1.5, ts * 0.09)
                c.create_oval(x0 + ts * 0.82 - r, y0 + ts * 0.82 - r,
                              x0 + ts * 0.82 + r, y0 + ts * 0.82 + r,
                              fill="#3fae5a", outline="#000")

        # 5) 建造模式：可见范围内把不可建的格子压暗
        if self.build_mode and self.valid_tiles:
            bsz = max(1, block)
            # 先把"有可建格"的块标出来，再按块压暗：O(可建格 + 块数)
            lit = set()
            for (vx, vy) in self.valid_tiles:
                lit.add((vx // bsz, vy // bsz))
            span_d = ts * bsz
            for by in range(ya, yb + 1, bsz):
                for bx in range(xa, xb + 1, bsz):
                    if (bx // bsz, by // bsz) in lit:
                        continue
                    c.create_rectangle(bx * ts, by * ts,
                                       bx * ts + span_d, by * ts + span_d,
                                       fill="#0c0e12", outline="", stipple="gray12")

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
        if ts < 9:
            # 缩到全图时格子只有几像素，图标文字只会糊成一片：只留一个色点
            if t.kind == "city":
                col = "#f4f2ec"
            elif t.kind == "line":
                col = COMPANY_COLORS[(t.owner or 0) % len(COMPANY_COLORS)]
            elif t.owner is not None and t.owner >= 0:
                col = COMPANY_COLORS[t.owner % len(COMPANY_COLORS)]
            else:
                col = "#d8d2c4"
            r = max(1.2, ts * 0.35)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline="")
            return
        if t.kind == "city":
            city = game.world.city_at(t.x, t.y)
            bw = ts * 0.26
            c.create_rectangle(cx - bw * 1.4, cy - bw * 0.9, cx - bw * 0.1, cy + bw * 1.1,
                               fill="#d8d2c4", outline="#5a5246")
            c.create_rectangle(cx - bw * 0.3, cy - bw * 1.5, cx + bw * 0.7, cy + bw * 1.1,
                               fill="#c9c2b2", outline="#5a5246")
            c.create_rectangle(cx + bw * 0.9, cy - bw * 0.6, cx + bw * 1.9, cy + bw * 1.1,
                               fill="#d8d2c4", outline="#5a5246")
            if city and ts >= 10:
                c.create_text(cx, y0 + ts * 0.80, text=f"{city.name} {city.demand_mw:.0f}MW",
                              fill="#ffffff",
                              font=("Microsoft YaHei UI", -max(7, int(ts * 0.19)), "bold"))
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


# 每种地形自己的高程带（和 terrain.py 的阈值对齐），带内再分四档：
# 平原整片 0~480m 如果只按全图 0~1200 分档，会全部落在同一档 → 看起来一块死绿。
_BAND = {
    "PLAIN": (0.0, 480.0),
    "DESERT": (0.0, 660.0),
    "HILL": (420.0, 1200.0),
    "RIVER": (0.0, 1.0),
    "COAST": (0.0, 1.0),
    "CITY": (0.0, 1.0),
}


def _elev_bucket(e: float, terrain_name: str = "PLAIN") -> int:
    """按该地形的高程带取 0..3 档，给底图配四档色。"""
    lo, hi = _BAND.get(terrain_name, (0.0, 1200.0))
    if hi <= lo:
        return 1
    n = (e - lo) / (hi - lo)
    return 0 if n < 0.25 else (1 if n < 0.5 else (2 if n < 0.75 else 3))


def _shade(color: str, factor: float) -> str:
    """把 #rrggbb 乘个系数：>1 提亮、<1 压深（夹在 0..255）。"""
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)

    def f(v: int) -> int:
        return max(0, min(255, int(round(v * factor))))

    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


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
