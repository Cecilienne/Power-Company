"""《电力公司》主界面。

布局：
    顶栏   —— 回合、现金、负债、股价、市值、份额、政策
    左栏   —— 2D 格子地图 + 公司图例 + 选中地块信息 + 回合动态
    右栏   —— 经营 / 财报 / 股市 / 政策日志 四个页签
    底栏   —— 结束回合 + 建造模式提示
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from .. import audio, ma, paths, world
from ..company import Company
from ..constants import (
    ANTITRUST_SHARE,
    COMPANY_COLORS,
    DIVIDEND_MAX,
    INFRA_SPECS,
    LOAN_LIMIT_RATIO,
    PLANT_SPECS,
    POLICY_SCHEDULE,
    TERRAIN_INFO,
    UI_ACCENT,
    UI_BAD,
    UI_BG,
    UI_BLUE,
    UI_BLUE_HI,
    UI_DIM,
    UI_DIM2,
    UI_GOOD,
    UI_HOVER,
    UI_PANEL,
    UI_PANEL2,
    UI_RED,
    UI_RULE,
    UI_SEL,
    UI_TEXT,
    UI_WARN,
    money,
)
from ..engine import Game
from ..ma import MaError, ownership, player_stake
from . import mapview, prefs, scale, set_window_icon
from .mapview import MapView, tile_tooltip

scale.enable_dpi_awareness()

FONT = "Microsoft YaHei UI"
BG2 = UI_PANEL2
TS_LOGICAL = 36          # 每格逻辑像素（乘 DPI 缩放）
WIN_W, WIN_H = 1240, 736
# 股市配色：沿用 A 股习惯「红涨绿跌」。财报里的盈亏仍用 UI_GOOD/UI_BAD，两者语义不同：
#   涨跌 = 相对上一根 K 线的方向；盈亏 = 经营结果的好坏。
K_UP = UI_RED
K_DOWN = UI_GOOD


def chg_pct(co: Company) -> float:
    """涨跌幅基准：本回合动过价（买入/卖出/增发）就对齐本回合开盘价，否则用上回合收盘价。

    这样"点一下增发"能立刻看到 −10.8%，而"结束回合"时仍然显示刚结算那回合的涨跌。
    """
    ref = co.price_prev or co.price
    if getattr(co, "turn_open", 0.0) and abs(co.price - co.turn_open) > 1e-9:
        ref = co.turn_open
    return (co.price / ref - 1.0) * 100 if ref else 0.0


class App(tk.Tk):
    def __init__(self, game: Game | None = None, intro: bool = True,
                 show_start: bool = False, seed: int | None = None) -> None:
        super().__init__()
        S = scale.init(self)
        mapview.TS = scale.px(TS_LOGICAL)
        self.title("电力公司 · Power Company")
        self.geometry(f"{scale.px(WIN_W)}x{scale.px(WIN_H)}+0+0")
        self.minsize(scale.px(1120), scale.px(700))
        self.configure(bg=UI_BG)
        set_window_icon(self)
        self.game = game if game is not None else Game.new_game()
        self.build_mode: str | None = None
        self.selected: tuple[int, int] | None = None
        self._syncing = False
        self._toast_job = None
        self._log_rendered = 0
        self.panel: str | None = None      # 当前打开的抽屉：policy / stock / company
        self._map_fit_job = None
        self._seed = seed
        self.start = None                  # 启动页覆盖层（不是独立窗口）
        self._style()
        self._menu()
        self._apply_display()              # 放在 _menu 之后：全屏要把菜单栏一起收起来
        self._apply_audio()                # 背景音乐按偏好起（默认关）
        self.after(1500, self._bgm_tick)   # 循环播放靠定时器：播完就从头再来
        self._build_buildbar()             # 红：底部建造条（先占位，保证地图吃剩下的空间）
        self._build_top()                  # 青：数据条 + 蓝：时间块
        body = tk.Frame(self, bg=UI_BG)
        body.pack(fill="both", expand=True)
        self._build_rail(body)             # 黄/绿：左侧页签轨
        self._build_stage(body)            # 白：地图 + 抽屉
        self.refresh()
        self.after(80, self._fit_map)
        if show_start:
            # 启动页盖在同一个窗口上：点【开始】只把覆盖层摘掉，不销毁窗口再开一个
            self.open_start()
        else:
            self._bind_keys()
            if intro and not os.environ.get("PWER_NO_INTRO"):
                self.after(200, self._intro)

    # ------------------------------------------------------------ 启动页覆盖层
    def open_start(self) -> None:
        from .start import StartScreen

        try:
            self.config(menu="")           # 启动页期间收起菜单栏
        except tk.TclError:
            pass
        self.start = StartScreen(
            self,
            on_new=self._start_new,
            on_continue=self._start_continue,
            on_quit=self.destroy,
            on_display=self._apply_display,
        )
        self.start.place(x=0, y=0, relwidth=1, relheight=1)
        self.start.lift()

    def _start_new(self, company_name: str | None = None,
                   account_name: str | None = None) -> None:
        self.start = None
        self._apply_display()              # 菜单栏/全屏状态交还给游戏
        self._apply_audio()                # 启动页可能刚改过音频开关
        self._bind_keys()
        self.game = Game.new_game(self._seed, company_name=company_name,
                                 account_name=account_name)
        self.refresh()
        self._fit_map()
        if not os.environ.get("PWER_NO_INTRO"):
            self.after(160, self._intro)

    def _start_continue(self, game) -> None:
        self.start = None
        self._apply_display()
        self._apply_audio()
        self._bind_keys()
        self.game = game
        self.build_mode = None
        self.map.build_mode = None
        self.map.valid_tiles = set()
        self.selected = None
        self.refresh()
        self._fit_map()
        self.toast("存档已载入")

    def _bind_keys(self) -> None:
        self.bind("<Control-Return>", lambda e: self.end_turn())
        self.bind("<Escape>", self._on_escape)
        self.bind("<F11>", self._toggle_display)
        self.bind("<Control-Key-1>", lambda e: self.toggle_panel("policy"))
        self.bind("<Control-Key-2>", lambda e: self.toggle_panel("stock"))
        self.bind("<Control-Key-3>", lambda e: self.toggle_panel("company"))

    def _unbind_keys(self) -> None:
        """回主菜单时把游戏内的快捷键摘掉，免得和启动页抢键。"""
        for seq in ("<Control-Return>", "<Escape>", "<F11>",
                    "<Control-Key-1>", "<Control-Key-2>", "<Control-Key-3>"):
            try:
                self.unbind(seq)
            except tk.TclError:
                pass

    def back_to_menu(self) -> None:
        """游戏内 → 主菜单：进度先落自动存档，再铺回启动页（窗口不重建）。"""
        if getattr(self, "start", None) is not None:
            return
        try:
            ok = messagebox.askyesno("返回菜单",
                                     "返回主菜单？当前进度会写入自动存档。",
                                     parent=self)
        except tk.TclError:
            ok = True
        if not ok:
            return
        self.save_game(silent=True)
        self.build_mode = None
        self.map.build_mode = None
        self.map.valid_tiles = set()
        self.selected = None
        self.toggle_panel(None)
        self._unbind_keys()
        self.open_start()

    def _apply_display(self) -> None:
        """跟启动页共用同一个偏好：全屏就真全屏（连标题栏、任务栏、菜单栏一起让位）。"""
        full = prefs.get("display") == "fullscreen"
        try:
            # Windows 下 tkinter 的菜单栏不吃 -fullscreen，必须手动摘掉，否则顶部留一条 39px 的白条
            self.config(menu="" if full else getattr(self, "_menubar", ""))
        except tk.TclError:
            pass
        try:
            self.attributes("-fullscreen", full)
        except tk.TclError:
            return
        if not full:
            self.geometry(f"{scale.px(WIN_W)}x{scale.px(WIN_H)}+0+0")

    def _toggle_display(self, _e=None) -> None:
        prefs.put("display", "windowed" if prefs.get("display") == "fullscreen"
                  else "fullscreen")
        prefs.save()
        self._apply_display()
        self.after(120, self._fit_map)

    # ------------------------------------------------------------ 背景音乐
    def _apply_audio(self) -> None:
        """按偏好开/关 BGM；启动页改过开关，进游戏时以偏好为准。"""
        if prefs.get("audio") != "on":
            audio.BGM.stop()
            return
        audio.BGM.reload()
        if not audio.BGM.tracks:
            prefs.put("audio", "off")
            prefs.save()
            return
        if audio.BGM.available():
            return                    # 已经在放，切界面不该重启
        idx = audio.BGM.find(str(prefs.get("track") or ""))
        if idx < 0:
            idx = 0
        if not audio.BGM.play_index(idx):
            # 放不出来就别留着"开"的假状态
            prefs.put("audio", "off")
            prefs.save()

    def _bgm_tick(self) -> None:
        try:
            audio.BGM.poll()
            self._music_sync()
        except Exception:  # noqa: BLE001
            pass
        self.after(1500, self._bgm_tick)

    def _music_sync(self) -> None:
        """自动切歌后，若正开着音乐页就把「正在播放」刷过来（没变化就不重画）。"""
        if getattr(self, "panel", None) != "music":
            return
        cur = audio.BGM.index if audio.BGM.available() else -1
        if getattr(self, "_music_key", None) != (cur, len(audio.BGM.tracks)):
            self._refresh_music()

    def destroy(self) -> None:
        try:
            audio.BGM.stop()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    def _on_escape(self, _e=None) -> None:
        if self.build_mode:
            self.cancel_build()
        elif self.panel:
            self.toggle_panel(None)

    # ------------------------------------------------------------ 样式
    def _style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=UI_PANEL, foreground=UI_TEXT, font=(FONT, 9))
        st.configure("TFrame", background=UI_PANEL)
        st.configure("TNotebook", background=UI_BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=UI_PANEL2, foreground=UI_DIM,
                     padding=(scale.px(12), scale.px(6)), font=(FONT, 10))
        st.map("TNotebook.Tab", background=[("selected", UI_PANEL)],
               foreground=[("selected", UI_ACCENT)])
        st.configure("Treeview", background=BG2, fieldbackground=BG2,
                     foreground=UI_TEXT, rowheight=scale.px(20), borderwidth=0)
        st.configure("Treeview.Heading", background=UI_PANEL2, foreground=UI_DIM,
                     font=(FONT, 9, "bold"))
        st.map("Treeview", background=[("selected", UI_SEL)],
               foreground=[("selected", UI_TEXT)])
        st.configure("TButton", background=UI_PANEL2, foreground=UI_TEXT,
                     padding=(scale.px(8), scale.px(4)))
        st.map("TButton", background=[("active", UI_HOVER)])
        st.configure("Accent.TButton", background=UI_BLUE, foreground=UI_TEXT,
                     padding=(scale.px(12), scale.px(7)), font=(FONT, 11, "bold"))
        st.map("Accent.TButton", background=[("active", UI_BLUE_HI)])
        st.configure("Small.TButton", padding=(scale.px(4), scale.px(2)), font=(FONT, 8))
        st.configure("TLabel", background=UI_PANEL, foreground=UI_TEXT)
        st.configure("TSpinbox", fieldbackground=BG2, foreground=UI_TEXT,
                     background=UI_PANEL2, arrowcolor=UI_TEXT)
        st.configure("TScale", background=UI_PANEL, troughcolor=UI_PANEL2)
        st.configure("Dark.Vertical.TScrollbar", gripcount=0,
                     background=UI_HOVER, darkcolor=UI_PANEL, lightcolor=UI_PANEL,
                     troughcolor=UI_BG, bordercolor=UI_BG, arrowcolor=UI_DIM,
                     arrowsize=scale.px(9), relief="flat", borderwidth=0)
        st.map("Dark.Vertical.TScrollbar",
               background=[("active", UI_BLUE), ("pressed", UI_BLUE_HI)])
        st.configure("Dark.TSpinbox", fieldbackground=UI_PANEL2,
                     background=UI_PANEL2, foreground=UI_TEXT, insertcolor=UI_TEXT,
                     arrowcolor=UI_DIM, bordercolor=UI_RULE,
                     lightcolor=UI_PANEL2, darkcolor=UI_PANEL2,
                     selectbackground=UI_SEL, selectforeground=UI_TEXT)
        st.map("Dark.TSpinbox", foreground=[("disabled", UI_DIM)],
               fieldbackground=[("disabled", UI_PANEL2)])

    def _menu(self) -> None:
        bar = tk.Menu(self, bg=UI_PANEL, fg=UI_TEXT, tearoff=0)
        m = tk.Menu(bar, tearoff=0, bg=UI_PANEL, fg=UI_TEXT)
        m.add_command(label="新游戏", command=self.new_game)
        m.add_separator()
        m.add_command(label="保存进度", command=self.save_game)
        m.add_command(label="读取进度", command=self.load_game)
        m.add_separator()
        m.add_command(label="退出", command=self.destroy)
        bar.add_cascade(label="游戏", menu=m)
        h = tk.Menu(bar, tearoff=0, bg=UI_PANEL, fg=UI_TEXT)
        h.add_command(label="玩法与规则", command=self.show_rules)
        bar.add_cascade(label="帮助", menu=h)
        self._menubar = bar
        self.config(menu=bar)

    # ------------------------------------------------------------ 顶部：数据条 + 时间块
    def _build_top(self) -> None:
        P = scale.px
        top = tk.Frame(self, bg=UI_BG, height=P(84))
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        # 蓝：时间块（右上）——回合 / 年季 / 结束回合
        time_box = tk.Frame(top, bg=UI_BLUE, width=P(340))
        time_box.pack(side="right", fill="y")
        time_box.pack_propagate(False)
        self.lbl_turn = tk.Label(time_box, text="", bg=UI_BLUE, fg=UI_TEXT,
                                 font=(FONT, 12, "bold"), anchor="w")
        self.lbl_turn.pack(fill="x", padx=P(12), pady=(P(8), 0))
        row = tk.Frame(time_box, bg=UI_BLUE)
        row.pack(fill="x", padx=P(12), pady=(P(2), P(8)))
        self.lbl_season = tk.Label(row, text="", bg=UI_BLUE, fg=UI_DIM,
                                   font=(FONT, 8), anchor="w")
        self.lbl_season.pack(side="left")
        self.btn_end = ttk.Button(row, text="结束回合 ▶", command=self.end_turn,
                                  style="Accent.TButton")
        self.btn_end.pack(side="right")

        # 青：数据条（左上），底部 1px 细线与启动页的页脚线同一套规矩
        stats = tk.Frame(top, bg=UI_BG)
        stats.pack(side="left", fill="both", expand=True)
        tk.Frame(stats, bg=UI_RULE, height=P(1)).pack(side="bottom", fill="x")
        inner = tk.Frame(stats, bg=UI_BG)
        inner.pack(fill="x", padx=P(10), pady=(P(5), 0))
        self.top_stats = {}
        for key, label, color in (
            ("cash", "现金", UI_TEXT), ("debt", "负债", UI_DIM),
            ("price", "股价", UI_ACCENT), ("cap", "市值", UI_GOOD),
            ("share", "市占", UI_WARN), ("policy", "监管阶段", UI_DIM),
        ):
            f = tk.Frame(inner, bg=UI_BG)
            f.pack(side="left", padx=P(8))
            tk.Label(f, text=label, bg=UI_BG, fg=UI_DIM,
                     font=(FONT, 8)).pack(anchor="w")
            lab = tk.Label(f, text="-", bg=UI_BG, fg=color, font=(FONT, 11, "bold"))
            lab.pack(anchor="w")
            self.top_stats[key] = lab

    # ------------------------------------------------------------ 左轨：页签 + 其它按钮
    def _build_rail(self, parent) -> None:
        P = scale.px
        rail = tk.Frame(parent, bg=UI_BG, width=P(116))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Frame(rail, bg=UI_BLUE, width=P(3)).pack(side="left", fill="y")
        inner = tk.Frame(rail, bg=UI_BG)
        inner.pack(side="left", fill="both", expand=True, padx=(P(8), P(6)), pady=P(10))

        self.rail_items: dict[str, dict] = {}
        for key, zh, en in (("policy", "政策", "POLICY"),
                            ("stock", "股市", "MARKET"),
                            ("company", "公司", "COMPANY")):
            self.rail_items[key] = self._rail_button(inner, key, zh, en)

        tk.Frame(inner, bg=UI_RULE, height=1).pack(fill="x", pady=P(10))
        tk.Label(inner, text="其它 / MORE", bg=UI_BG, fg=UI_DIM2, font=(FONT, 7),
                 anchor="w").pack(fill="x", pady=(0, P(2)))
        for label, cmd in (("菜单", self.back_to_menu), ("存档", self.save_game),
                           ("读档", self.load_game), ("规则", self.show_rules),
                           ("音乐", lambda: self.toggle_panel("music"))):
            tk.Button(inner, text=label, command=cmd, bg=UI_PANEL2, fg=UI_DIM,
                      activebackground=UI_HOVER, activeforeground=UI_TEXT,
                      bd=0, relief="flat", font=(FONT, 9), anchor="w",
                      padx=P(8), pady=P(5), cursor="hand2").pack(fill="x", pady=P(2))
        tk.Button(inner, text="退出", command=self.destroy, bg=UI_RED, fg=UI_TEXT,
                  activebackground=UI_RED, activeforeground=UI_TEXT,
                  bd=0, relief="flat", font=(FONT, 9, "bold"), anchor="w",
                  padx=P(8), pady=P(5), cursor="hand2").pack(fill="x", pady=(P(10), P(2)))

    def _rail_button(self, parent, key: str, zh: str, en: str) -> dict:
        P = scale.px
        box = tk.Frame(parent, bg=UI_PANEL2, cursor="hand2")
        box.pack(fill="x", pady=P(3))
        bar = tk.Frame(box, bg=UI_BG, width=P(3))
        bar.pack(side="left", fill="y")
        txt = tk.Frame(box, bg=UI_PANEL2)
        txt.pack(side="left", fill="both", expand=True, padx=P(8), pady=P(6))
        zh_lbl = tk.Label(txt, text=zh, bg=UI_PANEL2, fg=UI_TEXT,
                          font=(FONT, 11, "bold"), anchor="w")
        zh_lbl.pack(fill="x")
        en_lbl = tk.Label(txt, text=en, bg=UI_PANEL2, fg=UI_DIM,
                          font=(FONT, 7), anchor="w")
        en_lbl.pack(fill="x")
        widgets = (box, bar, txt, zh_lbl, en_lbl)
        for w in widgets:
            w.bind("<Button-1>", lambda e, k=key: self.toggle_panel(k))
        return {"box": box, "bar": bar, "txt": txt, "zh": zh_lbl, "en": en_lbl}

    def _sync_rail(self) -> None:
        for key, it in self.rail_items.items():
            on = (self.panel == key)
            bg = UI_BLUE if on else UI_PANEL2
            it["box"].config(bg=bg)
            it["txt"].config(bg=bg)
            it["zh"].config(bg=bg, fg=UI_TEXT)
            it["en"].config(bg=bg, fg=UI_ACCENT if on else UI_DIM)
            it["bar"].config(bg=UI_ACCENT if on else UI_BG)

    # ------------------------------------------------------------ 中间：地图 + 抽屉
    def _build_stage(self, parent) -> None:
        P = scale.px
        stage = tk.Frame(parent, bg=UI_BG)
        stage.pack(side="left", fill="both", expand=True)
        self.stage = stage

        strip = tk.Frame(stage, bg=UI_PANEL)
        strip.pack(side="bottom", fill="x")
        self.legend_row = tk.Frame(strip, bg=UI_PANEL)
        self.legend_row.pack(side="left", padx=P(8), pady=P(3))

        # 缩放控件（右下）：− 比例 + 适应
        self.zoom_lbl = tk.Label(strip, text="100%", bg=UI_PANEL, fg=UI_DIM,
                                 font=(FONT, 8), width=P(4), anchor="e")
        self.zoom_lbl.pack(side="right", padx=(P(2), P(10)))
        self._zoom_btn(strip, "适应", self._map_fit_view).pack(side="right", padx=P(2))
        self._zoom_btn(strip, "+", self._map_zoom_in).pack(side="right", padx=P(2))
        self._zoom_btn(strip, "−", self._map_zoom_out).pack(side="right", padx=P(2))

        self.lbl_tile = tk.Label(strip, text="点击地图查看地块", bg=UI_PANEL, fg=UI_DIM,
                                 anchor="w", justify="left", font=(FONT, 8))
        self.lbl_tile.pack(side="left", fill="x", expand=True, padx=P(8))

        self.map_host = tk.Frame(stage, bg=UI_BG)
        self.map_host.pack(fill="both", expand=True)
        self.map = MapView(self.map_host, self.on_tile_click, self.on_hover,
                           on_zoom=self._on_map_zoom)
        self.map.pack(fill="both", expand=True)

        # 抽屉：选中左侧页签时盖在地图右侧，不挤压地图
        self.drawer = tk.Frame(stage, bg=UI_PANEL, highlightthickness=1,
                               highlightbackground=UI_RULE)
        head = tk.Frame(self.drawer, bg=UI_PANEL2)
        head.pack(fill="x")
        self.drawer_title = tk.Label(head, text="", bg=UI_PANEL2, fg=UI_TEXT,
                                     font=(FONT, 10, "bold"), anchor="w")
        self.drawer_title.pack(side="left", padx=P(10), pady=P(6))
        tk.Button(head, text="✕", command=lambda: self.toggle_panel(None),
                  bg=UI_PANEL2, fg=UI_DIM, activebackground=UI_HOVER,
                  bd=0, relief="flat", font=(FONT, 9),
                  cursor="hand2").pack(side="right", padx=P(6))
        # 抽屉内容可滚动：运营页在 1240×736 下装不满，硬挤压会把「分红率」切掉
        body_outer = tk.Frame(self.drawer, bg=UI_PANEL)
        body_outer.pack(fill="both", expand=True)
        self._scroll_canvas = tk.Canvas(body_outer, bg=UI_PANEL, highlightthickness=0,
                                        bd=0, takefocus=0)
        sbar = ttk.Scrollbar(body_outer, orient="vertical",
                             command=self._scroll_canvas.yview,
                             style="Dark.Vertical.TScrollbar")
        self._scroll_canvas.configure(yscrollcommand=sbar.set)
        sbar.pack(side="right", fill="y")
        self._scroll_canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(self._scroll_canvas, bg=UI_PANEL)
        self._scroll_win = self._scroll_canvas.create_window((0, 0), window=body,
                                                             anchor="nw")
        body.bind("<Configure>", self._on_drawer_body)
        self._scroll_canvas.bind("<Configure>", self._on_drawer_canvas)
        self._scroll_canvas.bind("<Enter>", self._wheel_on)
        self._scroll_canvas.bind("<Leave>", self._wheel_off)
        self.drawer_body = body

        self.tab_log = tk.Frame(body, bg=UI_PANEL)
        self.tab_mkt = tk.Frame(body, bg=UI_PANEL)
        self._co_panel = tk.Frame(body, bg=UI_PANEL)
        self.tab_music = tk.Frame(body, bg=UI_PANEL)
        self._build_log(self.tab_log)
        self._build_mkt(self.tab_mkt)
        self._build_company(self._co_panel)
        self._build_music(self.tab_music)

        stage.bind("<Configure>", self._on_stage_resize)

    # ------------------------------------------------------------ 抽屉滚动
    def _on_drawer_body(self, _e=None) -> None:
        self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))

    def _on_drawer_canvas(self, e) -> None:
        self._scroll_canvas.itemconfigure(self._scroll_win, width=e.width)

    def _wheel_on(self, _e=None) -> None:
        self._scroll_canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _wheel_off(self, _e=None) -> None:
        self._scroll_canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, e) -> None:
        self._scroll_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # ------------------------------------------------------------ 公司面板（运营 / 财报）
    def _build_company(self, root) -> None:
        P = scale.px
        seg = tk.Frame(root, bg=UI_PANEL)
        seg.pack(fill="x", padx=P(8), pady=(P(6), P(2)))
        self.co_tab = "ops"
        self.btn_co_ops = ttk.Button(seg, text="运营", style="Small.TButton",
                                     command=lambda: self.show_company_tab("ops"))
        self.btn_co_ops.pack(side="left")
        self.btn_co_fin = ttk.Button(seg, text="财报", style="Small.TButton",
                                     command=lambda: self.show_company_tab("fin"))
        self.btn_co_fin.pack(side="left", padx=P(4))
        self.co_body = tk.Frame(root, bg=UI_PANEL)
        self.co_body.pack(fill="both", expand=True)
        self.tab_ops = tk.Frame(self.co_body, bg=UI_PANEL)
        self.tab_fin = tk.Frame(self.co_body, bg=UI_PANEL)
        self._build_ops(self.tab_ops)
        self._build_fin(self.tab_fin)
        self.show_company_tab("ops")

    def show_company_tab(self, which: str) -> None:
        self.co_tab = which
        for f in (self.tab_ops, self.tab_fin):
            f.pack_forget()
        (self.tab_ops if which == "ops" else self.tab_fin).pack(fill="both", expand=True)
        self.btn_co_ops.config(style="Accent.TButton" if which == "ops" else "Small.TButton")
        self.btn_co_fin.config(style="Accent.TButton" if which == "fin" else "Small.TButton")

    # ------------------------------------------------------------ 音乐面板（曲库）
    def _build_music(self, root) -> None:
        P = scale.px
        head = tk.Frame(root, bg=UI_PANEL)
        head.pack(fill="x", padx=P(8), pady=(P(6), P(2)))
        self.lbl_music_now = tk.Label(head, text="", bg=UI_PANEL, fg=UI_ACCENT,
                                      font=(FONT, 10, "bold"), anchor="w")
        self.lbl_music_now.pack(fill="x")
        self.lbl_music_hint = tk.Label(head, text="", bg=UI_PANEL, fg=UI_DIM,
                                       justify="left", anchor="w", font=(FONT, 8))
        self.lbl_music_hint.pack(fill="x", pady=(P(2), 0))

        bar = tk.Frame(root, bg=UI_PANEL)
        bar.pack(fill="x", padx=P(8), pady=(P(2), P(4)))
        ttk.Button(bar, text="下一首", style="Small.TButton",
                   command=self._music_next).pack(side="left")
        ttk.Button(bar, text="停止", style="Small.TButton",
                   command=self._music_stop).pack(side="left", padx=P(4))
        ttk.Button(bar, text="重新扫描", style="Small.TButton",
                   command=self._refresh_music).pack(side="left")

        self.music_list = tk.Frame(root, bg=UI_PANEL)
        self.music_list.pack(fill="both", expand=True, padx=P(8), pady=(P(2), P(8)))

    def _refresh_music(self) -> None:
        """重扫曲库并重画列表（玩家往 bgm/ 里丢了新歌后按「重新扫描」即可）。"""
        P = scale.px
        audio.BGM.reload()
        for w in self.music_list.winfo_children():
            w.destroy()
        tracks = audio.BGM.tracks
        cur = audio.BGM.index if audio.BGM.available() else -1
        self._music_key = (cur, len(tracks))
        if 0 <= cur < len(tracks):
            self.lbl_music_now.config(text=f"正在播放  {cur + 1:02d} · {tracks[cur].name}",
                                      fg=UI_ACCENT)
        else:
            self.lbl_music_now.config(text="未播放", fg=UI_DIM2)
        self.lbl_music_hint.config(
            text=(f"共 {len(tracks)} 首 · 列表循环（放完自动下一首）\n"
                  f"投递目录：{paths.music_dir()}\n"
                  f"支持 {audio.AUDIO_EXTS_LABEL}"))

        if not tracks:
            tk.Label(self.music_list, text="还没有音乐文件", bg=UI_PANEL, fg=UI_DIM,
                     font=(FONT, 9), anchor="w").pack(fill="x", pady=P(4))
            return

        for i, t in enumerate(tracks):
            on = (i == cur)
            bg = UI_BLUE if on else UI_PANEL2
            fg = UI_TEXT
            row = tk.Frame(self.music_list, bg=bg, cursor="hand2")
            row.pack(fill="x", pady=P(3))
            kids = []
            idx = tk.Label(row, text=f"{i + 1:02d}", bg=bg, fg=(UI_ACCENT if on else UI_DIM2),
                           font=(FONT, 8), width=3, anchor="w")
            idx.pack(side="left", padx=(P(4), 0))
            name = tk.Label(row, text=t.name, bg=bg, fg=fg, font=(FONT, 9),
                            anchor="w", justify="left")
            name.pack(side="left", fill="x", expand=True, padx=P(4))
            src = tk.Label(row, text=t.source, bg=bg, fg=(UI_ACCENT if on else UI_DIM),
                           font=(FONT, 7), width=4)
            src.pack(side="left")
            mark = tk.Label(row, text="▶" if on else "  ", bg=bg,
                            fg=(UI_TEXT if on else UI_DIM2), font=(FONT, 9), width=2)
            mark.pack(side="left", padx=(0, P(4)))
            kids = (row, idx, name, src, mark)

            for w in kids:
                w.bind("<Button-1>", lambda e, k=i: self._music_play(k))
                if not on:
                    w.bind("<Enter>", lambda e, ws=kids: [x.config(bg=UI_HOVER) for x in ws])
                    w.bind("<Leave>", lambda e, ws=kids: [x.config(bg=UI_PANEL2) for x in ws])

    def _music_play(self, index: int) -> None:
        """点列表里的一首就播它（失败就给原因，不留假状态）。"""
        if audio.BGM.play_index(index):
            prefs.put("audio", "on")
            cur = audio.BGM.current()
            if cur:
                prefs.put("track", cur.name)
            prefs.save()
            self._refresh_music()
            return
        prefs.put("audio", "off")
        prefs.save()
        self._refresh_music()
        try:
            messagebox.showinfo("音频不可用",
                                f"{audio.BGM.reason or '本机无法解码这个音频'}\n\n"
                                "游戏其余功能不受影响。", parent=self)
        except tk.TclError:
            pass

    def _music_stop(self) -> None:
        audio.BGM.stop()
        prefs.put("audio", "off")
        prefs.save()
        self._refresh_music()

    def _music_next(self) -> None:
        if not audio.BGM.tracks:
            self._refresh_music()
            return
        if audio.BGM.skip():
            prefs.put("audio", "on")
            cur = audio.BGM.current()
            if cur:
                prefs.put("track", cur.name)
            prefs.save()
        self._refresh_music()

    # ------------------------------------------------------------ 抽屉开关
    def toggle_panel(self, key: str | None) -> None:
        if key == self.panel:
            key = None
        self.panel = key
        if key is None:
            self.drawer.place_forget()
        else:
            for f in (self.tab_log, self.tab_mkt, self._co_panel, self.tab_music):
                f.pack_forget()
            frame, title = {
                "policy": (self.tab_log, "政策 · 日志"),
                "stock": (self.tab_mkt, "股市 · 行情"),
                "company": (self._co_panel, "公司 · 经营财报"),
                "music": (self.tab_music, "音乐 · 曲库"),
            }[key]
            if key == "music":
                self._refresh_music()
            self.drawer_title.config(text=title)
            frame.pack(fill="both", expand=True)
            w = min(scale.px(500), max(scale.px(360),
                                       int(self.stage.winfo_width() * 0.44)))
            self.drawer.place(relx=1.0, rely=0.0, anchor="ne", width=w,
                              relheight=1.0, x=-scale.px(6), y=scale.px(4))
        self._sync_rail()
        if key == "stock":
            self.draw_kline()

    def _on_stage_resize(self, _e=None) -> None:
        if self._map_fit_job is not None:
            return
        self._map_fit_job = self.after(80, self._fit_map)

    def _map_focus_spot(self) -> tuple[int, int] | None:
        """开局镜头落点：玩家自己的第一座电厂 → 自己的变电站 → 第一座城。"""
        try:
            me = self.game.player
            for p in me.plants:
                return (p.x, p.y)
            for t in self.game.world.owned_tiles(me.idx):
                if t.kind in ("substation", "line"):
                    return (t.x, t.y)
        except Exception:  # noqa: BLE001
            pass
        if self.game.world.cities:
            c = self.game.world.cities[0]
            return (c.x, c.y)
        return None

    def _fit_map(self) -> None:
        self._map_fit_job = None
        if not hasattr(self, "map"):
            return
        if self.map.canvas.winfo_width() < 60:
            return
        # 玩家自己缩放过就不抢镜头，只有「适应」或刚开局才重新贴合窗口。
        if self.map.manual:
            return
        # 960×960 全图缩到 2% 什么都看不见，开局直接看玩家自己的电厂
        spot = self._map_focus_spot()
        if spot is not None:
            self.map.focus_on(spot[0], spot[1], 0.5)
            return
        self.map.fit()

    # -- 地图缩放控件
    def _zoom_btn(self, parent, text: str, command):
        P = scale.px
        btn = tk.Button(parent, text=text, command=command, bg=UI_PANEL2, fg=UI_TEXT,
                        activebackground=UI_HOVER, activeforeground=UI_TEXT,
                        bd=0, relief="flat", padx=P(9), pady=P(1),
                        font=(FONT, 8), cursor="hand2")
        btn.bind("<Enter>", lambda e: btn.config(bg=UI_HOVER))
        btn.bind("<Leave>", lambda e: btn.config(bg=UI_PANEL2))
        return btn

    def _map_zoom_in(self) -> None:
        self.map.zoom_by(mapview.ZOOM_STEP)

    def _map_zoom_out(self) -> None:
        self.map.zoom_by(1 / mapview.ZOOM_STEP)

    def _map_fit_view(self) -> None:
        self.map.fit()

    def _on_map_zoom(self, zoom: float) -> None:
        if hasattr(self, "zoom_lbl"):
            self.zoom_lbl.config(text=f"{int(round(zoom * 100))}%")

    # ------------------------------------------------------------ 公司 · 运营
    def _build_ops(self, root) -> None:
        P = scale.px
        box = tk.Frame(root, bg=UI_PANEL)
        box.pack(fill="x", padx=P(8), pady=(P(5), 0))
        tk.Label(box, text="资金与债务", bg=UI_PANEL, fg=UI_ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        row = tk.Frame(box, bg=UI_PANEL)
        row.pack(fill="x", pady=P(1))
        self.lbl_funds = tk.Label(row, text="", bg=UI_PANEL, fg=UI_TEXT, font=(FONT, 9),
                                  anchor="w", justify="left")
        self.lbl_funds.pack(side="left")
        btns = tk.Frame(box, bg=UI_PANEL)
        btns.pack(fill="x")
        for label, cmd in (("借款 +2亿", lambda: self.do_borrow(20_000)),
                           ("还款 -2亿", lambda: self.do_repay(20_000)),
                           ("借满额度", self.do_borrow_max)):
            ttk.Button(btns, text=label, command=cmd,
                       style="Small.TButton").pack(side="left", padx=P(2))

        box = tk.Frame(root, bg=UI_PANEL)
        box.pack(fill="x", padx=P(8), pady=(P(4), 0))
        tk.Label(box, text="定价策略（元/MWh）", bg=UI_PANEL, fg=UI_ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        self.var_res = tk.DoubleVar(value=500.0)
        self.var_ind = tk.DoubleVar(value=460.0)
        for label, var, hint in (("居民", self.var_res, "价格敏感"),
                                 ("工业", self.var_ind, "承受力强")):
            row = tk.Frame(box, bg=UI_PANEL)
            row.pack(fill="x", pady=P(1))
            tk.Label(row, text=label, bg=UI_PANEL, fg=UI_TEXT, width=4,
                     font=(FONT, 9, "bold")).pack(side="left")
            tk.Scale(row, variable=var, from_=150, to=1200, resolution=5,
                     orient="horizontal", bg=UI_PANEL, fg=UI_TEXT, showvalue=0,
                     troughcolor=UI_HOVER, activebackground=UI_ACCENT,
                     sliderrelief="flat", highlightthickness=0, bd=0,
                     length=P(232), sliderlength=P(16),
                     command=lambda v: self.on_price_change()).pack(side="left", padx=P(3))
            tk.Label(row, textvariable=var, bg=UI_PANEL, fg=UI_ACCENT, width=4,
                     font=(FONT, 10, "bold")).pack(side="left", padx=P(4))
            tk.Label(row, text=hint, bg=UI_PANEL, fg=UI_DIM,
                     font=(FONT, 8)).pack(side="left")
        self.lbl_cost = tk.Label(box, text="", bg=UI_PANEL, fg=UI_DIM, font=(FONT, 8),
                                 justify="left", anchor="w")
        self.lbl_cost.pack(fill="x")
        self.lbl_antitrust = tk.Label(box, text="", bg=UI_PANEL, fg=UI_WARN,
                                      font=(FONT, 8), justify="left", anchor="w")
        self.lbl_antitrust.pack(fill="x")

        box = tk.Frame(root, bg=UI_PANEL)
        box.pack(fill="x", padx=P(8), pady=(P(4), 0))
        tk.Label(box, text="电厂与在建工程", bg=UI_PANEL, fg=UI_ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        cols = ("kind", "cap", "cf", "fixed", "gen", "status")
        self.tv_plants = ttk.Treeview(box, columns=cols, show="headings", height=4)
        for c, t, w in (("kind", "电厂", 84), ("cap", "容量", 50), ("cf", "出力", 44),
                        ("fixed", "固定/季", 72), ("gen", "本季发电", 70),
                        ("status", "状态", 88)):
            self.tv_plants.heading(c, text=t)
            self.tv_plants.column(c, width=P(w), anchor="center")
        self.tv_plants.pack(fill="x")
        self.tv_projects = ttk.Treeview(box, columns=("name", "left", "cost"),
                                        show="headings", height=2)
        for c, t, w in (("name", "在建工程", 140), ("left", "剩余工期", 70),
                        ("cost", "已投", 90)):
            self.tv_projects.heading(c, text=t)
            self.tv_projects.column(c, width=P(w), anchor="center")
        self.tv_projects.tag_configure("empty", foreground=UI_DIM)
        self.tv_projects.pack(fill="x", pady=(P(3), 0))

        box = tk.Frame(root, bg=UI_PANEL)
        box.pack(fill="x", padx=P(8), pady=(P(4), P(8)))
        self.lbl_staff = tk.Label(box, text="", bg=UI_PANEL, fg=UI_TEXT,
                                  font=(FONT, 9), justify="left", anchor="w")
        self.lbl_staff.pack(fill="x")
        row = tk.Frame(box, bg=UI_PANEL)
        row.pack(fill="x", pady=P(1))
        for label, cmd in (("招运营 +2", lambda: self.do_hire(2, 0)),
                           ("招工程 +1", lambda: self.do_hire(0, 1)),
                           ("裁运营 -2", lambda: self.do_fire(2, 0))):
            ttk.Button(row, text=label, command=cmd,
                       style="Small.TButton").pack(side="left", padx=P(2))
        row2 = tk.Frame(box, bg=UI_PANEL)
        row2.pack(fill="x", pady=P(1))
        tk.Label(row2, text="分红率", bg=UI_PANEL, fg=UI_TEXT,
                 font=(FONT, 9)).pack(side="left")
        self.var_payout = tk.DoubleVar(value=0.35)
        tk.Scale(row2, variable=self.var_payout, from_=0.0, to=DIVIDEND_MAX,
                 resolution=0.05, orient="horizontal", bg=UI_PANEL, fg=UI_TEXT,
                 showvalue=0, troughcolor=UI_HOVER, activebackground=UI_ACCENT,
                 sliderrelief="flat", highlightthickness=0, bd=0, length=P(200),
                 sliderlength=P(16),
                 command=lambda v: self.on_payout()).pack(side="left", padx=P(4))
        self.lbl_subsidy = tk.Label(box, text="", bg=UI_PANEL, fg=UI_DIM,
                                    font=(FONT, 8), anchor="w")
        self.lbl_subsidy.pack(fill="x")

    # ------------------------------------------------------------ 财报页
    def _build_fin(self, root) -> None:
        P = scale.px
        top = tk.Frame(root, bg=UI_PANEL)
        top.pack(fill="x", padx=P(8), pady=(P(6), 0))
        self.lbl_fin_head = tk.Label(top, text="", bg=UI_PANEL, fg=UI_ACCENT,
                                     font=(FONT, 11, "bold"), justify="left", anchor="w")
        self.lbl_fin_head.pack(fill="x")
        self.lbl_fin_kpi = tk.Label(top, text="", bg=UI_PANEL, fg=UI_TEXT,
                                    font=(FONT, 9), justify="left", anchor="w")
        self.lbl_fin_kpi.pack(fill="x", pady=(P(2), P(4)))
        self.tv_fin = ttk.Treeview(root, columns=("item", "val"), show="headings", height=13)
        self.tv_fin.heading("item", text="利润表科目（本季度）")
        self.tv_fin.heading("val", text="金额")
        self.tv_fin.column("item", width=P(240), anchor="w")
        self.tv_fin.column("val", width=P(150), anchor="e")
        self.tv_fin.pack(fill="both", expand=True, padx=P(8))
        self.tv_fin.tag_configure("cost", foreground=UI_RED)
        self.tv_fin.tag_configure("sum", foreground=UI_TEXT, background=UI_PANEL2)
        self.tv_fin.tag_configure("profit", foreground=UI_GOOD)

        box = tk.Frame(root, bg=UI_PANEL)
        box.pack(fill="x", padx=P(8), pady=P(6))
        tk.Label(box, text="季度净利润走势（近 12 季，单位：万元）", bg=UI_PANEL,
                 fg=UI_DIM, font=(FONT, 8)).pack(anchor="w")
        self.spark = tk.Canvas(box, height=P(56), bg=BG2, highlightthickness=0)
        self.spark.pack(fill="x", pady=(P(2), 0))

    # ------------------------------------------------------------ 股市页
    def _build_mkt(self, root) -> None:
        P = scale.px
        # 行情：选中公司的 K 线（每回合一根，开/高/低/收）
        box = tk.Frame(root, bg=UI_PANEL)
        box.pack(fill="x", padx=P(8), pady=(P(6), P(2)))
        head = tk.Frame(box, bg=UI_PANEL)
        head.pack(fill="x")
        self.lbl_kline = tk.Label(head, text="", bg=UI_PANEL, fg=UI_TEXT,
                                  font=(FONT, 10, "bold"), anchor="w")
        self.lbl_kline.pack(side="left")
        self.lbl_kline2 = tk.Label(head, text="", bg=UI_PANEL, fg=UI_DIM,
                                   font=(FONT, 7), anchor="e")
        self.lbl_kline2.pack(side="right")
        self.cv_kline = tk.Canvas(box, height=P(96), bg=BG2, highlightthickness=0)
        self.cv_kline.pack(fill="x", pady=(P(2), 0))
        self.cv_kline.bind("<Configure>", lambda e: self.draw_kline())

        head = tk.Frame(root, bg=UI_PANEL)
        head.pack(fill="x", padx=P(8), pady=(P(6), P(2)))
        self.lbl_ceo = tk.Label(head, text="", bg=UI_PANEL, fg=UI_TEXT,
                                font=(FONT, 10, "bold"), justify="left", anchor="w")
        self.lbl_ceo.pack(fill="x")
        cols = ("name", "price", "chg", "cap", "my", "state")
        self.tv_mkt = ttk.Treeview(root, columns=cols, show="headings", height=3)
        for c, t, w in (("name", "公司", 78), ("price", "股价", 52), ("chg", "涨跌", 54),
                        ("cap", "市值", 66), ("my", "我的持股", 64), ("state", "状态", 58)):
            self.tv_mkt.heading(c, text=t)
            self.tv_mkt.column(c, width=P(w), anchor="center")
        self.tv_mkt.pack(fill="x", padx=P(8))
        self.tv_mkt.bind("<<TreeviewSelect>>", lambda e: self._on_pick_company())

        ctrl = tk.Frame(root, bg=UI_PANEL)
        ctrl.pack(fill="x", padx=P(8), pady=(P(6), 0))
        tk.Label(ctrl, text="数量（占总股本 %）", bg=UI_PANEL, fg=UI_TEXT,
                 font=(FONT, 9)).pack(side="left")
        self.var_qty = tk.StringVar(value="2")
        ttk.Spinbox(ctrl, from_=0.5, to=20, increment=0.5, width=5,
                    textvariable=self.var_qty, style="Dark.TSpinbox").pack(side="left", padx=P(3))
        self.lbl_deal = tk.Label(ctrl, text="", bg=UI_PANEL, fg=UI_DIM, font=(FONT, 8))
        self.lbl_deal.pack(side="left", padx=P(4))

        row = tk.Frame(root, bg=UI_PANEL)
        row.pack(fill="x", padx=P(8), pady=P(3))
        for label, cmd in (
            ("公司账户买入", lambda: self.do_buy(company_fund=True)),
            ("CEO账户买入", lambda: self.do_buy(company_fund=False)),
            ("卖出", self.do_sell),
            ("回购自家", self.do_buyback),
            ("增发", self.do_issue),
        ):
            ttk.Button(row, text=label, command=cmd,
                       style="Small.TButton").pack(side="left", padx=P(2))

        row2 = tk.Frame(root, bg=UI_PANEL)
        row2.pack(fill="x", padx=P(8), pady=P(2))
        for label, cmd in (("控股接管 51%", self.do_take_control),
                           ("吸收合并 75%", self.do_merge),
                           ("强制全额分红", self.do_sweep)):
            ttk.Button(row2, text=label, command=cmd,
                       style="Small.TButton").pack(side="left", padx=P(2))

        self.lbl_stake = tk.Label(root, text="", bg=UI_PANEL, fg=UI_DIM,
                                  font=(FONT, 9), justify="left", anchor="w",
                                  wraplength=P(560))
        self.lbl_stake.pack(fill="x", padx=P(8), pady=(P(4), 0))

        tk.Label(root, text="控股子公司资产处置", bg=UI_PANEL, fg=UI_ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=P(8), pady=(P(6), P(2)))
        self.lb_subs = tk.Listbox(root, bg=BG2, fg=UI_TEXT, bd=0, height=2,
                                  font=(FONT, 9), highlightthickness=0)
        self.lb_subs.pack(fill="x", padx=P(8))
        btns = tk.Frame(root, bg=UI_PANEL)
        btns.pack(fill="x", padx=P(8), pady=P(4))
        ttk.Button(btns, text="淘汰选中机组（省固定成本）", command=self.do_retire,
                   style="Small.TButton").pack(side="left")
        tk.Label(root, text="对手情报（机组 / 电价 / 财务风险）", bg=UI_PANEL, fg=UI_ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=P(8), pady=(P(4), P(2)))
        self.txt_intel = tk.Text(root, bg=BG2, fg=UI_TEXT, bd=0, height=2, wrap="word",
                                 font=(FONT, 9), highlightthickness=0, padx=P(6), pady=P(4))
        self.txt_intel.pack(fill="both", expand=True, padx=P(8), pady=(0, P(8)))
        self.txt_intel.configure(state="disabled")

    def _on_pick_company(self) -> None:
        """在行情表里换一家公司：同步刷新股权信息与 K 线。"""
        self.refresh_mkt_controls()
        self.draw_kline()

    # ------------------------------------------------------------ K 线
    def draw_kline(self) -> None:
        """画选中公司的 K 线：每回合一根，红色阳线 / 绿色阴线。"""
        if not hasattr(self, "cv_kline"):
            return
        cv = self.cv_kline
        cv.delete("all")
        P = scale.px
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < P(120) or h < P(50):
            return
        tgt = self._sel_company() or self.game.player
        candles = list(getattr(tgt, "candles", []))
        # 本回合还没收盘：只要价格动过（比如刚点了增发/买入），就把这根"活的"K 线补上
        live = None
        if getattr(tgt, "turn_open", 0.0) > 0:
            hi_t = max(getattr(tgt, "turn_high", tgt.price), tgt.price)
            lo_t = min(getattr(tgt, "turn_low", tgt.price), tgt.price)
            if abs(tgt.price - tgt.turn_open) > 1e-9 or hi_t > lo_t + 1e-9:
                live = (tgt.turn_open, hi_t, lo_t, tgt.price)
                candles.append(live)
        candles = candles[-26:]
        if not candles:
            cv.create_text(w // 2, h // 2, text="还没有行情 · 结束一个回合后生成第一根 K 线",
                           fill=UI_DIM, font=(FONT, 9))
            self.lbl_kline.config(text=f"{tgt.name}  尚未开盘", fg=UI_DIM)
            self.lbl_kline2.config(text="")
            return

        pad_l, pad_r, pad_t, pad_b = P(6), P(52), P(12), P(18)
        pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
        hi = max(c[1] for c in candles)
        lo = min(c[2] for c in candles)
        if hi - lo < 1e-6:
            hi, lo = hi * 1.02 + 0.01, lo * 0.98
        margin = (hi - lo) * 0.08
        hi, lo = hi + margin, lo - margin

        def yy(v: float) -> float:
            return pad_t + (hi - v) / (hi - lo) * ph

        for i in range(5):                      # 网格 + 右侧价格刻度
            v = hi - (hi - lo) * i / 4
            y = yy(v)
            cv.create_line(pad_l, y, pad_l + pw, y, fill=UI_RULE)
            cv.create_text(pad_l + pw + P(6), y, text=f"{v:.1f}", anchor="w",
                           fill=UI_DIM, font=(FONT, 7))

        n = len(candles)
        slot = pw / max(1, n)
        bw = max(P(2), slot * 0.62)
        for i, (o, hh, ll, c) in enumerate(candles):
            cx = pad_l + slot * (i + 0.5)
            color = K_UP if c >= o else K_DOWN
            cv.create_line(cx, yy(hh), cx, yy(ll), fill=color, width=max(1, P(1)))
            top, bot = yy(max(o, c)), yy(min(o, c))
            if bot - top < P(1):
                bot = top + P(1)
            cv.create_rectangle(cx - bw / 2, top, cx + bw / 2, bot,
                                fill=color, outline=color)

        last = candles[-1]
        y = yy(last[3])
        cv.create_line(pad_l, y, pad_l + pw, y, fill=UI_DIM2, dash=(2, 3))
        # 最新价用色块贴标，避免和右侧刻度数字叠在一起看不清
        tag_x = pad_l + pw + P(3)
        cv.create_rectangle(tag_x, y - P(8), tag_x + P(46), y + P(8),
                            fill=K_UP if last[3] >= last[0] else K_DOWN, outline="")
        cv.create_text(tag_x + P(23), y, text=f"{last[3]:.2f}", fill=UI_TEXT,
                       font=(FONT, 8, "bold"))
        last_turn = self.game.turn if live else self.game.turn - 1
        cv.create_text(pad_l + P(2), h - P(3), text=f"T{last_turn - n + 1}",
                       anchor="sw", fill=UI_DIM, font=(FONT, 7))
        cv.create_text(pad_l + pw, h - P(3), text=f"T{last_turn}",
                       anchor="se", fill=UI_DIM, font=(FONT, 7))

        o, hh, ll, c = last
        chg = (c / candles[-2][3] - 1.0) * 100 if n > 1 else 0.0
        color = K_UP if c >= o else K_DOWN
        self.lbl_kline.config(text=f"{tgt.name}  {c:.2f}  {chg:+.1f}%",
                              fg=color)
        self.lbl_kline2.config(
            text=("进行中 · " if live else "")
                 + f"红涨绿跌 · 开 {o:.2f}  高 {hh:.2f}  低 {ll:.2f}  收 {c:.2f}")

    # ------------------------------------------------------------ 日志页
    def _build_log(self, root) -> None:
        P = scale.px
        top = tk.Frame(root, bg=UI_PANEL)
        top.pack(fill="x", padx=P(8), pady=(P(6), 0))
        self.lbl_policy = tk.Label(top, text="", bg=UI_PANEL, fg=UI_TEXT,
                                   font=(FONT, 9), justify="left", anchor="w")
        self.lbl_policy.pack(fill="x")
        self.lbl_market = tk.Label(top, text="", bg=UI_PANEL, fg=UI_TEXT,
                                   font=(FONT, 9), justify="left", anchor="w")
        self.lbl_market.pack(fill="x", pady=(P(2), P(2)))
        self.lbl_weather = tk.Label(top, text="", bg=UI_PANEL, fg=UI_DIM,
                                    font=(FONT, 9), justify="left", anchor="w")
        self.lbl_weather.pack(fill="x", pady=(0, P(4)))
        self.txt_log = tk.Text(root, bg=BG2, fg=UI_TEXT, bd=0, wrap="word",
                               font=(FONT, 9), highlightthickness=0,
                               padx=P(6), pady=P(4))
        self.txt_log.pack(fill="both", expand=True, padx=P(8), pady=(0, P(8)))
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------ 底部：建造条
    def _build_buildbar(self) -> None:
        P = scale.px
        bar = tk.Frame(self, bg=UI_BG)
        bar.pack(fill="x", side="bottom")
        tk.Frame(bar, bg=UI_BLUE, height=P(3)).pack(side="top", fill="x")
        inner = tk.Frame(bar, bg=UI_BG)
        inner.pack(fill="x", padx=P(10), pady=P(5))

        left = tk.Frame(inner, bg=UI_BG)
        left.pack(side="left")
        tk.Label(left, text="建造", bg=UI_BG, fg=UI_ACCENT,
                 font=(FONT, 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, P(4)))
        self.build_buttons: dict[str, ttk.Button] = {}
        items = [(k, PLANT_SPECS[k]) for k in PLANT_SPECS] + \
                [(k, INFRA_SPECS[k]) for k in INFRA_SPECS]
        for i, (kind, spec) in enumerate(items):
            r, c = divmod(i, 6)
            cost = spec.capex if kind in INFRA_SPECS else spec.capex_per_mw * spec.cap
            b = ttk.Button(left, text=f"{spec.name} {money(cost)}", width=12,
                           command=lambda k=kind: self.set_build(k))
            b.grid(row=r, column=c + 1, padx=P(1), pady=P(1), sticky="ew")
            self.build_buttons[kind] = b
        ttk.Button(left, text="取消", command=self.cancel_build,
                   style="Small.TButton").grid(row=0, column=7, padx=(P(6), 0), sticky="w")

        right = tk.Frame(inner, bg=UI_BG)
        right.pack(side="right", fill="x", expand=True, padx=(P(12), 0))
        self.lbl_hint = tk.Label(right, text="选择建造类型 → 点地图放置；Esc 取消",
                                 bg=UI_BG, fg=UI_DIM, font=(FONT, 9), anchor="e")
        self.lbl_hint.pack(fill="x")
        # 内联校验：钱不够/没地块的类型直接置灰，并说明原因
        self.lbl_build_info = tk.Label(right, text="", bg=UI_BG, fg=UI_DIM,
                                       font=(FONT, 8), anchor="e", justify="right")
        self.lbl_build_info.pack(fill="x")

    # ------------------------------------------------------------ 交互：地图
    def toast(self, msg: str, bad: bool = False, ms: int = 2600) -> None:
        """就近反馈：底部状态条显示 2.6 秒后自动回到当前提示。"""
        self.lbl_hint.config(text=("✕ " if bad else "✓ ") + msg,
                             fg=UI_BAD if bad else UI_GOOD)
        if self._toast_job is not None:
            try:
                self.after_cancel(self._toast_job)
            except Exception:  # noqa: BLE001
                pass
        self._toast_job = self.after(ms, self._restore_hint)

    def _restore_hint(self) -> None:
        self._toast_job = None
        self.lbl_hint.config(text=self._idle_hint(), fg=UI_DIM)

    def _idle_hint(self) -> str:
        if self.build_mode:
            name = (PLANT_SPECS[self.build_mode].name if self.build_mode in PLANT_SPECS
                    else INFRA_SPECS[self.build_mode].name)
            n = len(self.map.valid_tiles)
            return f"建造模式：{name} · 可建 {n} 处（其余地块已变暗）· 点击地图确认，Esc 退出"
        return "选择建筑类型后点击地图建造；调好电价再结束回合"

    def on_tile_click(self, x: int, y: int) -> None:
        self.selected = (x, y)
        me = self.game.player
        if self.build_mode:
            kind = self.build_mode
            ok, why = self.game.can_build(me, kind, x, y)
            if ok and self.game.build(me, kind, x, y):
                self.map.draw(self.game)
                self.refresh()
                return
            self.toast(f"无法建造：{why}", bad=True)
            self.map.draw(self.game)
            return
        self.update_tile_label()
        self.map.draw(self.game)

    def on_hover(self, pos) -> None:
        if pos is None or self.game is None:
            return
        t = self.game.world.tile(*pos)
        if self.build_mode:
            kind = self.build_mode
            ok, why = self.game.world.check_build(kind, *pos)
            name = PLANT_SPECS[kind].name if kind in PLANT_SPECS else INFRA_SPECS[kind].name
            price = self.game.total_build_cost(self.game.player, kind, *pos)
            self.lbl_tile.config(
                text=f"({pos[0]},{pos[1]}) {TERRAIN_INFO[t.terrain].name} · {name} "
                     f"造价 {money(price)} · " + ("可建造，点击确认" if ok else f"不可建：{why}"),
                fg=UI_GOOD if ok else UI_BAD)
        elif t.kind == "city":
            self.lbl_tile.config(text=tile_tooltip(self.game, *pos), fg=UI_TEXT)

    def update_tile_label(self) -> None:
        if not self.selected:
            return
        self.lbl_tile.config(text=tile_tooltip(self.game, *self.selected), fg=UI_TEXT)

    def set_build(self, kind: str) -> None:
        self.build_mode = kind
        me = self.game.player
        # 960×960 全图逐格检查要 1~2 秒，只算视野附近（外扩 40 格）——
        # 玩家看到的范围里"能建/不能建"都对，拖到别处再进一次建造模式即可
        w = self.game.world
        # 刚 focus_on / 刚滚过，画布的滚动位置要等一次 idle 才生效，先让它落定
        self.map.update_idletasks()
        xa, ya, xb, yb = self.map._visible_tiles()
        pad = 40
        valid = {(x, y)
                 for y in range(max(0, ya - pad), min(w.h - 1, yb + pad) + 1)
                 for x in range(max(0, xa - pad), min(w.w - 1, xb + pad) + 1)
                 if self.game.can_build(me, kind, x, y)[0]}
        self.map.build_mode = kind
        self.map.valid_tiles = valid
        self.map.draw(self.game)
        self._restore_hint()

    def cancel_build(self) -> None:
        self.build_mode = None
        self.map.build_mode = None
        self.map.valid_tiles = set()
        self.map.draw(self.game)
        self.lbl_hint.config(text=self._idle_hint(), fg=UI_DIM)

    # ------------------------------------------------------------ 交互：按钮
    def do_borrow(self, amount: float) -> None:
        try:
            self.game.borrow(self.game.player, amount)
        except ValueError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_borrow_max(self) -> None:
        me = self.game.player
        self.do_borrow(max(0.0, me.assets() * LOAN_LIMIT_RATIO - me.debt))

    def do_repay(self, amount: float) -> None:
        try:
            self.game.repay(self.game.player, amount)
        except ValueError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_hire(self, ops: int, eng: int) -> None:
        try:
            self.game.hire(self.game.player, ops, eng)
        except ValueError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_fire(self, ops: int, eng: int) -> None:
        try:
            self.game.fire(self.game.player, ops, eng)
        except ValueError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def on_price_change(self) -> None:
        if self._syncing:
            return
        me = self.game.player
        cap = self.game.policy.price_cap
        me.res_price = min(cap, float(self.var_res.get()))
        me.ind_price = min(cap, float(self.var_ind.get()))
        self.update_cost_label()

    def on_payout(self) -> None:
        if self._syncing:
            return
        self.game.player.payout = float(self.var_payout.get())
        self.refresh()

    def update_cost_label(self) -> None:
        me = self.game.player
        st = me.last_income
        mwh = max(1.0, st.served_mwh)
        var = st.fuel * 10_000.0 / mwh if st.fuel > 0 else 0.0
        fixed = (st.fixed + st.depreciation + st.labor + st.grid) * 10_000.0 / mwh
        unit = var + fixed
        self.lbl_cost.config(
            text=f"上季单位成本：可变 {var:.0f} + 固定 {fixed:.0f} = {unit:.0f} 元/MWh   "
                 f"营收 {money(st.revenue)}   净利 {money(st.net_profit)}")
        warn = ""
        if me.market_share > ANTITRUST_SHARE and (
                me.res_price < unit * 0.98 or me.ind_price < unit * 0.98):
            warn = "⚠ 市占率超过 55% 且低于成本价销售，反垄断风险上升（连续 3 回合触发处罚）"
        self.lbl_antitrust.config(text=warn)

    # ------------------------------------------------------------ 交互：股票
    def _sel_company(self) -> Company | None:
        sel = self.tv_mkt.selection()
        if not sel:
            return None
        return self.game.companies[int(sel[0])]

    def _qty(self, tgt: Company) -> float:
        try:
            pct = float(self.var_qty.get()) / 100.0
        except ValueError:
            pct = 0.02
        return tgt.shares * max(0.001, pct)

    def do_buy(self, company_fund: bool) -> None:
        tgt = self._sel_company()
        if not tgt:
            self.toast("先在表格里选中一家公司", bad=True)
            return
        me = self.game.player
        if tgt.idx == me.idx:
            self.toast("买自家股票请用【回购自家】", bad=True)
            return
        holder = me.idx if company_fund else "CEO"
        try:
            qty = self._qty(tgt)
            cost = ma.buy_cost(tgt, qty)
            ma.buy_shares(self.game, holder, tgt, qty)
            self.toast(f"买入 {tgt.name} {qty:,.0f}万股，花费 {money(cost)}")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_sell(self) -> None:
        tgt = self._sel_company()
        if not tgt:
            self.toast("先选中一家公司", bad=True)
            return
        me = self.game.player
        holder = me.idx if me.holdings.get(tgt.idx, 0) >= self.game.ceo_holdings.get(tgt.idx, 0) else "CEO"
        try:
            got = ma.sell_shares(self.game, holder, tgt, tgt.shares * 0.5)
            self.toast(f"卖出套现 {money(got)}")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_buyback(self) -> None:
        me = self.game.player
        try:
            ma.buyback(self.game, me, me.shares * 0.02)
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_issue(self) -> None:
        me = self.game.player
        price0, cap0 = me.price, me.market_cap()
        try:
            cash = ma.issue_shares(self.game, me, me.shares * 0.06)
            self.toast(f"增发募资 {money(cash)}；股价 {price0:.2f}→{me.price:.2f}，"
                       f"市值 {money(cap0)}→{money(me.market_cap())}")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_take_control(self) -> None:
        tgt = self._sel_company()
        if not tgt:
            return
        try:
            ma.take_control(self.game, tgt)
            self.toast(f"已控股 {tgt.name}")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_merge(self) -> None:
        tgt = self._sel_company()
        if not tgt:
            return
        if not messagebox.askyesno("吸收合并",
                                   f"确认吸收合并 {tgt.name}？\n将接收其全部资产、电厂、矿权与负债，"
                                   f"并支付整合费。"):
            return
        try:
            ma.absorb_merge(self.game, tgt)
            self.toast(f"{tgt.name} 已并入你的集团")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_sweep(self) -> None:
        tgt = self._sel_company()
        if not tgt:
            return
        try:
            ma.force_dividend(self.game, tgt)
            self.toast(f"{tgt.name} 已改为全额分红")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    def do_retire(self) -> None:
        sel = self.lb_subs.curselection()
        if not sel:
            self.toast("先选中要淘汰的机组", bad=True)
            return
        try:
            key = self._sub_keys[sel[0]]
            idx, plant_id = (int(v) for v in key.split("|"))
        except (ValueError, IndexError):
            self.toast("无法解析机组", bad=True)
            return
        try:
            ma.retire_plant(self.game, self.game.companies[idx], plant_id, by_player=True)
            self.toast("机组已淘汰，固定成本下降")
        except MaError as e:
            self.toast(str(e), bad=True)
        self.refresh()

    # ------------------------------------------------------------ 回合
    def end_turn(self) -> None:
        if self.game.game_over:
            return
        self.build_mode = None
        self.map.build_mode = None
        self.game.end_turn()
        self.refresh()
        if prefs.get("autosave"):
            self.save_game(silent=True)
        self.after(30, self._after_turn)

    def _after_turn(self) -> None:
        g = self.game
        if g.game_over:
            title = {"victory_dominance": "🏆 霸权结局",
                     "victory_green": "🌿 绿色转型结局",
                     "defeat": "💀 出局"}.get(g.result, "任期满")
            messagebox.showinfo(title, g.result_detail + "\n\n点击确定开始新游戏。")
            self.new_game()
            return
        self.lbl_hint.config(text=f"第 {g.turn} 回合开始 · {g.policy.summary()}", fg=UI_DIM)

    def new_game(self) -> None:
        # 竞局结束自动重开时沿用这局的名字，别把玩家刚起的公司名丢掉。
        old = self.game.player
        self.game = Game.new_game(
            company_name=getattr(old, "name", None),
            account_name=getattr(old, "account_name", None))
        self.build_mode = None
        self.map.build_mode = None
        self.map.valid_tiles = set()
        self.selected = None
        self.refresh()


    def save_game(self, silent: bool = False) -> None:
        import pickle
        # 手动存档与自动存档分开：自动存档不能把玩家的手动存档冲掉
        path = paths.save_file("autosave.pkl" if silent else "slot1.pkl")
        try:
            with open(path, "wb") as f:
                pickle.dump(self.game, f)
            if not silent:
                self.toast(f"已保存到 {path}")
        except Exception as e:  # noqa: BLE001
            if not silent:
                self.toast(f"保存失败：{e}", bad=True)

    def load_game(self) -> None:
        import pickle
        path = paths.save_file()
        if not path.exists():
            path = paths.save_file("autosave.pkl")
        if not path.exists():
            self.toast("没有找到存档", bad=True)
            return
        try:
            with open(path, "rb") as f:
                self.game = pickle.load(f)
            self.build_mode = None
            self.map.build_mode = None
            self.map.valid_tiles = set()
            self.selected = None
            self.refresh()
            self.toast(f"读取成功：{path}")
        except Exception as e:  # noqa: BLE001
            self.toast(f"读取失败：{e}", bad=True)

    def show_rules(self) -> None:
        win = tk.Toplevel(self)
        win.title("玩法与规则")
        win.geometry(f"{scale.px(760)}x{scale.px(640)}")
        win.configure(bg=UI_BG)
        txt = tk.Text(win, bg=BG2, fg=UI_TEXT, wrap="word", font=(FONT, 10),
                      padx=scale.px(12), pady=scale.px(10), bd=0)
        txt.pack(fill="both", expand=True, padx=scale.px(8), pady=scale.px(8))
        seed = getattr(self.game, "seed", 0)
        txt.insert("1.0", RULES + f"\n\n本局地图种子：{seed}\n（地形、矿脉、城市位置都由它铺开；同种子逐格一致，想复现同一张图就记下这个数。）")
        txt.configure(state="disabled")

    # ------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        g = self.game
        me = g.player
        self._syncing = True
        P = scale.px

        self.lbl_turn.config(text=g.turn_label())
        self.lbl_season.config(text=f"{g.policy.stage()} · 需求 {g.total_demand():,.0f}MW")
        self.top_stats["cash"].config(text=money(me.cash))
        self.top_stats["debt"].config(text=money(me.debt))
        # 涨跌基准见 chg_pct()：动过价看本回合，没动看上一回合
        pct = chg_pct(me)
        arrow = "▲ " if pct > 0.05 else ("▼ " if pct < -0.05 else "")
        self.top_stats["price"].config(
            text=f"{arrow}{me.price:.2f}", fg=K_UP if pct >= 0 else K_DOWN)
        self.top_stats["cap"].config(text=money(me.market_cap()))
        self.top_stats["share"].config(text=f"{g.player_share():.1%}")
        self.top_stats["policy"].config(text=g.policy.stage())

        self.var_res.set(round(me.res_price))
        self.var_ind.set(round(me.ind_price))
        self.var_payout.set(round(me.payout, 2))

        self.lbl_funds.config(
            text=f"现金 {money(me.cash)} · 负债 {money(me.debt)} · "
                 f"负债率 {me.debt_ratio():.0%}（上限 {LOAN_LIMIT_RATIO:.0%}）\n"
                 f"剩余额度 {money(max(0.0, me.assets() * LOAN_LIMIT_RATIO - me.debt))}")
        self.lbl_staff.config(
            text=f"运营班组 {me.ops_staff}/{me.staff_need()}（满足率 {me.staff_ratio():.0%}，缺人压出力） · "
                 f"工程队 {me.eng_teams}（在建 {len(me.projects)} 项）\n"
                 f"声望 {me.reputation:.0f}")
        self.lbl_subsidy.config(text=f"新能源 {me.renewable_share():.0%}"
                                     f"/配额 {g.policy.renewable_quota:.0%}")
        self.update_cost_label()

        st = me.last_income
        tv = self.tv_plants
        tv.delete(*tv.get_children())
        short = me.staff_ratio() < 0.99
        for p in me.plants:
            if not p.online:
                status = "停机检修"
            elif short:
                status = f"缺人 · 出力×{max(0.55, me.staff_ratio()):.0%}"
            else:
                status = "运行"
            tv.insert("", "end", values=(
                p.name, f"{p.cap:.0f}MW", f"{p.cf_actual:.0%}",
                f"{p.fixed_q:,.0f}万", f"{p.dispatched_mwh:,.0f}", status))
        tp = self.tv_projects
        tp.delete(*tp.get_children())
        if not me.projects:
            tp.insert("", "end", values=("暂无在建工程", "—", "—"), tags=("empty",))
        for pr in me.projects:
            tp.insert("", "end", values=(pr.name, f"{pr.turns_left} 回合", f"{pr.spent:,.0f}万"))

        st = me.last_income
        self.lbl_fin_head.config(text=f"{me.name} · {g.year_of()}年 第{(g.turn - 1) % 4 + 1}季度 利润表")
        self.lbl_fin_kpi.config(
            text=f"净资产 {money(me.equity())}    总资产 {money(me.assets())}    "
                 f"季度净利 {money(st.net_profit)}    ROE {st.net_profit / max(1.0, me.equity()):.1%}/季    "
                 f"碳强度 {me.carbon_intensity() * 1000:.0f}g/kWh    新能源 {me.renewable_share():.0%}")
        tvf = self.tv_fin
        tvf.delete(*tvf.get_children())
        for name, val, is_cost in st.rows():
            if abs(val) < 0.5:
                continue
            tvf.insert("", "end", values=(name, money(val)), tags=("cost",) if is_cost else ())
        tvf.insert("", "end", values=("营业收入合计", money(st.revenue)), tags=("sum",))
        tvf.insert("", "end", values=("成本合计", money(-st.total_cost)), tags=("sum",))
        tvf.insert("", "end", values=("净利润", money(st.net_profit)), tags=("profit",))
        self._draw_spark()

        self.refresh_market()
        self.lbl_policy.config(
            text=f"政策阶段：{g.policy.stage()}    {g.policy.summary()}\n"
                 f"下一档：" + self._next_policy_text())
        self.lbl_market.config(
            text=f"大宗：煤 {g.market['coal']:,.0f} 元/吨（{g.commodity_mod['coal']:.2f}x）    "
                 f"铀 {g.market['uranium']:,.0f} 元/吨    外购电价 880 元/MWh")
        self.lbl_weather.config(
            text=f"本季天气：来水 {g.weather['rain']:.2f}   风 {g.weather['wind']:.2f}   "
                 f"光 {g.weather['solar']:.2f}   气温 {g.weather['heat']:.2f}   "
                 f"需求系数 {g.demand_shock:.2f}")
        self._refresh_log()
        self._refresh_legend()
        self.map.selected = self.selected
        if self.build_mode:
            self.set_build(self.build_mode)
        else:
            self.map.draw(g)
        if self.selected:
            self.update_tile_label()

        self._syncing = False
        self._sync_build_buttons()
        self.btn_end.config(state="disabled" if g.game_over else "normal")

    def _kind_cost(self, kind: str) -> float:
        """按钮上标注的基础造价（未含地形乘数，用作可负担性判断的下界）。"""
        if kind in PLANT_SPECS:
            spec = PLANT_SPECS[kind]
            return spec.capex_per_mw * spec.cap
        return INFRA_SPECS[kind].capex

    def _sync_build_buttons(self) -> None:
        """内联校验：不可行的建造类型直接置灰，并在下方说明原因。"""
        me = self.game.player
        ready = 0
        missing: dict[str, list[str]] = {}
        for kind, btn in self.build_buttons.items():
            cost = self._kind_cost(kind)
            name = (PLANT_SPECS[kind].name if kind in PLANT_SPECS
                    else INFRA_SPECS[kind].name)
            tiles = self.game.world.count_buildable(kind)[0]
            approved = (kind != "nuclear") or self.game.policy.nuclear_approved
            can_pay = me.cash >= cost * 1.05
            ok = tiles > 0 and can_pay and approved
            btn.state(["!disabled"] if ok else ["disabled"])
            if ok:
                ready += 1
            elif not approved:
                missing.setdefault("核电待核准", []).append(name)
            elif tiles == 0:
                missing.setdefault("无地可建", []).append(name)
            else:
                missing.setdefault(f"缺 {money(cost - me.cash)}", []).append(name)
        if ready == len(self.build_buttons):
            text, color = f"当前 {ready}/{len(self.build_buttons)} 类可开工", UI_DIM
        else:
            parts = [f"{k}：{'/'.join(v)}" for k, v in missing.items()]
            text = (f"当前可开工 {ready}/{len(self.build_buttons)} · "
                    + "；".join(parts[:2]) + (f" 等 {len(parts)} 项" if len(parts) > 2 else ""))
            color = UI_WARN
        self.lbl_build_info.config(text=text, fg=color)

    def _next_policy_text(self) -> str:
        g = self.game
        for step in POLICY_SCHEDULE:
            if step.turn > g.turn:
                return (f"第 {step.turn} 回合起：碳税 {step.carbon_tax:.0f}元/t、"
                        f"新能源配额 {step.renewable_quota:.0%}、电价上限 {step.price_cap:.0f}")
        return "已到最终监管档位"

    def _refresh_legend(self) -> None:
        P = scale.px
        for w in self.legend_row.winfo_children():
            w.destroy()
        for i, co in enumerate(self.game.companies):
            if not co.alive:
                continue
            tk.Frame(self.legend_row, bg=COMPANY_COLORS[i % len(COMPANY_COLORS)],
                     width=P(11), height=P(11)).pack(side="left", padx=(P(4), P(2)))
            tag = "（你）" if co.is_player else ("（子公司）" if co.delisted else "")
            tk.Label(self.legend_row, text=f"{co.name}{tag}", bg=UI_PANEL, fg=UI_DIM,
                     font=(FONT, 8)).pack(side="left")

    def refresh_market(self) -> None:
        g = self.game
        tv = self.tv_mkt
        tv.delete(*tv.get_children())
        for co in g.companies:
            if not co.alive:
                continue
            chg = chg_pct(co)
            my = player_stake(g, co)
            state = "你的公司" if co.is_player else (
                "已退市" if co.delisted else
                ("已控股" if co.controlled_by == g.player_idx else "对手"))
            tv.insert("", "end", iid=str(co.idx), values=(
                co.name, f"{co.price:.2f}", f"{chg:+.1f}%", money(co.market_cap()),
                f"{my:.1%}", state))
        acct = getattr(g.player, "account_name", "") or "CEO"
        self.lbl_ceo.config(
            text=f"经营者 {acct}  ·  CEO 资金 {money(g.ceo_cash)}  ·  "
                 f"公司现金 {money(g.player.cash)}")
        self.lb_subs.delete(0, "end")
        self._sub_keys = []
        for co in g.companies:
            if co.controlled_by != g.player_idx or co.delisted or co.is_player:
                continue
            for p in co.plants:
                self.lb_subs.insert("end", f"{co.name} · {p.name} {p.cap:.0f}MW "
                                           f"固定 {p.fixed_q:,.0f}万/季 [{p.id}]")
                self._sub_keys.append(f"{co.idx}|{p.id}")
        if not self._sub_keys:
            self.lb_subs.insert("end", "尚未控股任何公司（持股 51% 后可在此处置其机组）")
            self.lb_subs.itemconfig(0, foreground=UI_DIM)
        self._refresh_intel()
        self.refresh_mkt_controls()
        self.draw_kline()

    def refresh_mkt_controls(self) -> None:
        tgt = self._sel_company()
        if not tgt:
            self.lbl_stake.config(text="选中一家公司查看股权结构")
            return
        st = player_stake(self.game, tgt)
        own = ownership(self.game, tgt)
        parts = [f"{k} {v:.1%}" for k, v in own.items() if v > 0.001]
        qty = self._qty(tgt)
        self.lbl_stake.config(
            text=f"{tgt.name}：你的合计持股 {st:.1%}（51% 控股 / 75% 合并）    "
                 f"{qty:,.0f} 万股约需 {money(ma.buy_cost(tgt, qty))}\n"
                 f"股权结构：" + "   ".join(parts))

    def _refresh_intel(self) -> None:
        g = self.game
        lines = []
        for co in g.companies:
            if not co.alive or co.delisted or co.is_player:
                continue
            mix: dict[str, int] = {}
            for p in co.plants:
                mix[p.name] = mix.get(p.name, 0) + 1
            mix_s = "、".join(f"{k}×{v}" for k, v in mix.items()) or "无机组"
            np_ = co.profit_history[-1] if co.profit_history else 0.0
            pbr = co.price / max(0.01, co.book_per_share())
            risk = []
            if pbr < 0.6:
                risk.append("股价远低于净资产（收购窗口）")
            if co.debt_ratio() > 0.55:
                risk.append("高负债")
            if np_ < 0:
                risk.append("亏损")
            if co.controlled_by == g.player_idx:
                risk.append("已被你控股")
            lines.append(
                f"{co.name} {co.gen_capacity():.0f}MW 电价{co.res_price:.0f}/{co.ind_price:.0f} "
                f"负债{co.debt_ratio():.0%} 净利{money(np_)} 市占{co.market_share:.0%} "
                + ("⚠" + "/".join(risk) if risk else "稳健"))
            if pbr < 0.85 or co.debt_ratio() > 0.55:
                lines.append(f"　　可交付 {co.grid_capacity:.0f}MW · P/B {pbr:.2f}x · 现金 {money(co.cash)}")
        self.txt_intel.configure(state="normal")
        self.txt_intel.delete("1.0", "end")
        self.txt_intel.insert("1.0", "\n".join(lines) if lines else "暂无对手")
        self.txt_intel.configure(state="disabled")

    def _draw_spark(self) -> None:
        c = self.spark
        c.delete("all")
        hist = self.game.player.profit_history[-12:]
        if not hist:
            return
        w = c.winfo_width() or scale.px(520)
        h = scale.px(56)
        lo = min(min(hist), 0.0)
        hi = max(max(hist), 1.0)
        rng = hi - lo or 1.0
        pts = []
        for i, v in enumerate(hist):
            x = scale.px(6) + (w - scale.px(12)) * i / max(1, len(hist) - 1)
            y = h - scale.px(8) - (v - lo) / rng * (h - scale.px(16))
            pts.append((x, y))
        zero_y = h - scale.px(8) - (0 - lo) / rng * (h - scale.px(16))
        c.create_line(0, zero_y, w, zero_y, fill=UI_RULE, dash=(2, 2))
        flat = [coord for p in pts for coord in p]
        if len(flat) >= 4:
            c.create_line(*flat, fill=UI_ACCENT, width=2, smooth=True)
        for (x, y), v in zip(pts, hist):
            r = scale.px(2)
            c.create_oval(x - r, y - r, x + r, y + r,
                          fill=UI_GOOD if v >= 0 else UI_BAD, outline="")

    def _refresh_log(self) -> None:
        """增量渲染日志：只追加新行，避免每次刷新重贴几百行（这是最容易被忽略的卡顿源）。"""
        lines = self.game.log_lines
        if len(lines) < self._log_rendered or self._log_rendered == 0:
            self.txt_log.configure(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("1.0", "\n".join(lines))
            self._log_rendered = len(lines)
        elif len(lines) > self._log_rendered:
            self.txt_log.configure(state="normal")
            for line in lines[self._log_rendered:]:
                self.txt_log.insert("end", "\n" + line)
            self._log_rendered = len(lines)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")


RULES = """《电力公司》玩法与规则
================================================

一、核心闭环
  地形/资源 → 建电厂与电网 → 定电价抢客户 → 季度结算出财报
          → 财报决定股价 → 股价决定收购成本与风险 → 收购对手扩张

二、每回合你要做的四件事
  1. 看环境：需求、煤价/铀价、政策、对手财报与股价（左侧【政策】）
  2. 调资产：建电厂/变电站/线路/矿，招人或裁员，借款或还款（左侧【公司】→ 运营）
  3. 定电价：居民与工业分开定价（左侧【公司】→ 运营）
  4. 股市：买对手、回购自家、控股后抽血或合并（左侧【股市】）
  最后点右上【结束回合】。

三、必须理解的机制
  1. 固定成本照扣
     每座电厂每季度都要付折旧 + 运维，哪怕一度电都不发。产能过剩会直接吞掉利润。
  2. 电网不是摆设
     电厂必须和【城市半径 2 格内的变电站】通过【相邻的线路/电厂】连成一片，
     城市接口容量 = 半径内变电站(180MW) + 线路(120MW) 之和，线损随距离上升。
     送不出去的电 = 没有收入，但你依然付燃料和固定成本。
  3. 客户看两件事：电价 + 可靠性
     份额按「(市场均价/你的价)^弹性 × 口碑」分配，但每家拿到的份额不超过它的可交付容量。
     想抢份额，必须先建产能；只降价不建厂没有意义。
  4. 掠夺定价与反垄断
     把价格压到单位成本以下、同时份额超过 55%，连续 3 个回合会被罚 6000 万并禁止收购 2 回合。
  5. 财报 → 股价
     股价 = 滚动四季度 EPS × PE + 净资产锚定。市占率、负债率、口碑、事故会调整 PE。
     亏损会把股价打到净资产以下——那正是你被收购的窗口。
  6. 股权阶梯
     5% 举牌（对手警觉） / 20% 董事席位 / 51% 控股（强制分红抽血、淘汰其机组）
     / 75% 吸收合并（资产、债务、矿权、人力全部并入，对方退市）。
     对手 AI 也会用同样手段对付你，负债过高、连续亏损时最危险。

四、胜负
  胜利（任一）：
   · 霸权：全市场供电份额 ≥ 50% 且总市值第一
   · 绿色：新能源占比 ≥ 55%、碳强度 ≤ 180g/kWh、且碳税进入最高档
  失败：资不抵债破产 / 被 AI 持股超过 51%。

五、上手建议
  1. 先抢钢城：560MW 完全没人供电，建变电站 + 一座电厂就能吃下。
  2. 头 4 个季度没有碳税，抓紧用火电攒现金流，但别把装机堆到需求的 1.5 倍以上。
  3. 第 5 回合开始有碳税与新能源配额，提前铺光伏（荒漠/平原）。
  4. 看到对手股价跌到净资产 60% 以下就买；先到 51% 抽血，再到 75% 合并。
  5. 永远留一笔现金：风暴、罢工、煤价暴涨随时可能砸穿你的现金流。

六、快捷键
  Ctrl+Enter：结束回合
"""


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
