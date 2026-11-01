"""主菜单 / 开始页 —— 国际主义（瑞士）风格。

版式规格见 `docs/ui_start_screen.md`：所有坐标是 1920×1080 设计稿单位，
运行时按 `min(w/1920, h/1080)` 等比缩放并居中，多出来的部分留黑边（不是拉伸）。

四块功能区对应设计稿的四个色块：
    红  左侧菜单（开始/继续/设置/关于）
    蓝  右上标题块（图标 + 中英文名）
    黄  右侧面板（设置 / 存档摘要 / 关于，随选中项切换）
    绿  左下状态灯（自检结果：绿=通过，红=失败）

刻意不做：进度条、阴影、圆角、渐变、位图。标题是文字画的。
"""
from __future__ import annotations

import sys
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont
from tkinter import messagebox

from .. import paths
from ..constants import (
    BUILD,
    MAX_TURNS,
    MULTIPLAYER_READY,
    UI_START_BG,
    UI_START_BLUE,
    UI_START_DIM1,
    UI_START_DIM2,
    UI_START_GREEN,
    UI_START_INK,
    UI_START_RED,
    UI_START_RULE,
    UI_START_YELLOW,
    VERSION,
    money,
)
from . import prefs, scale

scale.enable_dpi_awareness()

# ---------------------------------------------------------------- 设计稿常量
DW, DH = 1920, 1080

MENU_X, MENU_Y, MENU_W, MENU_H = 96, 260, 404, 150
BLUE_X, BLUE_Y, BLUE_W, BLUE_H = 560, 64, 1264, 288
PANEL_X, PANEL_Y, PANEL_W, PANEL_H = 560, 400, 1264, 460
PANEL_L, PANEL_R = 600, 1784
STATUS_X, STATUS_Y, STATUS_W, STATUS_H = 96, 900, 404, 80
ROWS_Y, ROW_H = 512, 50
# 页脚左侧的退出按钮：坐在页脚线（y=992）下方，与右侧版权行同一条基线
QUIT_X, QUIT_Y, QUIT_W, QUIT_H = 96, 1000, 192, 50

ITEMS = (
    ("new", "01", "开始", "NEW GAME"),
    ("continue", "02", "继续", "CONTINUE"),
    ("settings", "03", "设置", "SETTINGS"),
    ("about", "04", "关于", "ABOUT"),
)

# 字体族在 __init__ 里按可用性决定（Windows 上通常全都有 Noto Sans SC）
F_BLACK = "Noto Sans SC Black"
F_MED = "Noto Sans SC Medium"
F_REG = "Noto Sans SC"
F_LAT = "Arial"
_BLACK_IS_BOLD = False      # 若回退到没有 Black 字重的字体，用 bold 顶替


def _pick(families: set[str], names: tuple[str, ...], fallback: str) -> str:
    for n in names:
        if n in families:
            return n
    return fallback


class StartScreen(tk.Frame):
    """启动页覆盖层。

    它是盖在游戏窗口上的一个 Frame，不是一个独立的 Tk 根窗口——
    点【开始】只是把自己 destroy 掉，窗口不重建，所以不会闪一下再弹新窗口。
    """

    def __init__(self, master, on_new=None, on_continue=None,
                 on_quit=None, on_display=None) -> None:
        super().__init__(master, bg=UI_START_BG)
        self._root = self.winfo_toplevel()
        self._on_new = on_new
        self._on_continue = on_continue
        self._on_quit = on_quit
        self._on_display = on_display
        self._key_bind: str | None = None

        self._resolve_fonts()

        self.cv = tk.Canvas(self, bg=UI_START_BG, highlightthickness=0, bd=0,
                            takefocus=1)
        self.cv.pack(fill="both", expand=True)

        # 运行状态
        self.choice: tuple[str, object] | None = None
        self.sel = "new"
        self.row = 0                     # 设置面板内的行焦点
        self.hover: str | None = None
        self.hover_row: int | None = None  # 鼠标悬停的可点设置行
        self._k = 1.0
        self._ox = self._oy = 0.0
        self._fonts: dict[tuple, tkfont.Font] = {}
        self._pending = False

        # 存档与自检
        self._save_path = paths.save_file()
        self._save_game = self._load_save()
        self.check = self._self_check()

        self._bind_events()
        self.after(16, self._render)
        try:
            self.cv.focus_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 初始化
    def _resolve_fonts(self) -> None:
        global F_BLACK, F_MED, F_REG, _BLACK_IS_BOLD
        fams: set[str] = set()
        try:
            fams = set(tkfont.families(self))
        except Exception:  # noqa: BLE001
            pass
        F_BLACK = _pick(fams, ("Noto Sans SC Black",), "Microsoft YaHei UI")
        F_MED = _pick(fams, ("Noto Sans SC Medium", "Noto Sans SC"), "Microsoft YaHei UI")
        F_REG = _pick(fams, ("Noto Sans SC", "Noto Sans SC Medium"), "Microsoft YaHei UI")
        F_LAT = _pick(fams, ("Arial", "Helvetica"), "Segoe UI")
        _BLACK_IS_BOLD = (F_BLACK != "Noto Sans SC Black")

    def _load_save(self):
        """读最近的存档：手动槽 slot1.pkl 与自动槽 autosave.pkl 取 mtime 更新的那个。"""
        import pickle
        best = None
        for name in ("slot1.pkl", "autosave.pkl"):
            p = paths.save_file(name)
            if not p.exists():
                continue
            try:
                with open(p, "rb") as f:
                    game = pickle.load(f)
                mtime = p.stat().st_mtime
            except Exception:  # noqa: BLE001
                continue
            if best is None or mtime > best[0]:
                best = (mtime, p, game)
        if best is None:
            return None
        self._save_path = best[1]
        return best[2]

    def _self_check(self) -> dict[str, tuple[bool, str]]:
        out: dict[str, tuple[bool, str]] = {}
        fams: set[str] = set()
        try:
            fams = set(tkfont.families(self))
        except Exception:  # noqa: BLE001
            pass
        font_ok = F_MED in fams or "Microsoft YaHei UI" in fams
        out["字体"] = (font_ok, F_MED if font_ok else "缺中文字体")
        data_ok = False
        detail = ""
        try:
            d = paths.data_dir()
            probe = d / ".status_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            data_ok, detail = True, str(d)
        except Exception as e:  # noqa: BLE001
            detail = f"不可写：{e}"
        out["存档目录"] = (data_ok, detail)
        ver_ok = VERSION.count(".") == 2 and BUILD.count(".") == 2
        out["版本"] = (ver_ok, f"v{VERSION} · {BUILD}")
        return out

    @property
    def ok(self) -> bool:
        return all(v[0] for v in self.check.values())

    def _bind_events(self) -> None:
        self.cv.bind("<Configure>", lambda e: self._schedule())
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<Leave>", lambda e: self._set_hover(None))
        self.cv.bind("<Button-1>", lambda e: self.cv.focus_set(), add="+")
        # 键位挂在顶层窗口上（Frame 自己拿不到焦点），关掉时再解绑
        self._key_bind = self._root.bind("<Key>", self._on_key, add="+")

    # ------------------------------------------------------------ 缩放
    def X(self, v: float) -> float:
        return self._ox + v * self._k

    def Y(self, v: float) -> float:
        return self._oy + v * self._k

    def _font(self, family: str, px: float, bold: bool = False) -> tkfont.Font:
        key = (family, round(px, 2), bold)
        f = self._fonts.get(key)
        if f is None:
            size = max(7, int(round(px * self._k)))    # 下限 7px：小屏也别糊成噪点
            f = tkfont.Font(root=self, family=family, size=-size,
                            weight="bold" if bold else "normal")
            self._fonts[key] = f
        return f

    def _text_width(self, s: str, f: tkfont.Font, tracking: float) -> float:
        if not s:
            return 0.0
        w = sum(f.measure(ch) for ch in s)
        w += tracking * self._k * (len(s) - 1)
        return w

    def _t(self, x: float, y: float, s: str, size: float, color: str,
           family: str | None = None, tracking: float = 0.0,
           right: float | None = None, bold: bool = False, dy: float = 0.0) -> None:
        """画一段文字。right 给定时按右边界对齐；tracking>0 时逐字排版（Tk 没有字距）。"""
        if not s:
            return
        fam = family or F_LAT
        f = self._font(fam, size, bold)
        if right is not None:
            x = right - self._text_width(s, f, tracking) / self._k
        if tracking <= 0:
            self.cv.create_text(self.X(x), self.Y(y + dy), text=s, fill=color,
                                font=f, anchor="nw")
            return
        cx = x
        for ch in s:
            self.cv.create_text(self.X(cx), self.Y(y + dy), text=ch, fill=color,
                                font=f, anchor="nw")
            cx += (f.measure(ch) + tracking * self._k) / self._k

    def _rect(self, x: float, y: float, w: float, h: float, color: str) -> None:
        self.cv.create_rectangle(self.X(x), self.Y(y), self.X(x + w), self.Y(y + h),
                                 fill=color, outline="", width=0)

    def _line(self, x1: float, y1: float, x2: float, y2: float, color: str,
              width: float = 1.0) -> None:
        self.cv.create_line(self.X(x1), self.Y(y1), self.X(x2), self.Y(y2),
                            fill=color, width=max(1.0, width * self._k))

    # ------------------------------------------------------------ 渲染
    def _schedule(self) -> None:
        if self._pending:
            return
        self._pending = True
        self.after_idle(self._render)

    def _render(self) -> None:
        self._pending = False
        cv = self.cv
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 40 or h < 40:
            return
        k = min(w / DW, h / DH)
        if abs(k - self._k) > 1e-6:
            self._fonts.clear()
        self._k = k
        self._ox = (w - DW * k) / 2.0
        self._oy = (h - DH * k) / 2.0
        cv.delete("all")
        self._draw_menu_label()
        self._draw_title()
        self._draw_menu()
        self._draw_panel()
        self._draw_status()
        self._draw_footer()
        self._draw_quit()

    def _draw_menu_label(self) -> None:
        self._rect(MENU_X, 92, 10, 10, UI_START_RED)
        self._t(120, 84, "MENU / 001—004", 10, UI_START_DIM2, F_LAT, 2.6)
        self._line(MENU_X, 100, MENU_X + MENU_W, 100, UI_START_RULE)

    def _draw_title(self) -> None:
        self._rect(BLUE_X, BLUE_Y, BLUE_W, BLUE_H, UI_START_BLUE)
        # 图标：白框 + 闪电（与 assets/app.ico 同一套几何）
        self.cv.create_rectangle(self.X(600), self.Y(96), self.X(678), self.Y(174),
                                 outline=UI_START_INK, width=max(1.0, 1.5 * self._k))
        pts = ((0.58, 0.02), (0.14, 0.56), (0.44, 0.56),
               (0.34, 0.98), (0.88, 0.40), (0.55, 0.40))
        ox, oy, s = 614, 108, 50
        coords: list[float] = []
        for px, py in pts:
            coords += [self.X(ox + px * s), self.Y(oy + py * s)]
        self.cv.create_polygon(coords, fill=UI_START_INK, outline="")

        self._t(712, 86, "电力公司", 118, UI_START_INK, F_BLACK, bold=_BLACK_IS_BOLD)
        self._t(720, 246, "POWER COMPANY", 24, UI_START_INK, F_LAT, 9.5)
        self._t(720, 300, "POWER GRID & CAPITAL", 9, "#C9D2EA", F_LAT, 3.4)

        self._line(1560, 96, 1560, 320, "#3A5AAB")
        self._t(0, 104, "回合制经营", 11, "#DCE3F5", F_MED, 0, right=1784)
        self._t(0, 124, "TURN-BASED STRATEGY", 8.5, "#AEBEE0", F_LAT, 2.4, right=1784)
        self._t(0, 292, "2026", 17, UI_START_INK, F_LAT, 1.0, right=1784)

    def _draw_menu(self) -> None:
        for i, (key, num, zh, en) in enumerate(ITEMS):
            y = MENU_Y + i * MENU_H
            active = (key == self.sel)
            disabled = self._disabled(key)
            if active:
                self._rect(MENU_X, y, MENU_W, MENU_H, UI_START_RED)
            else:
                self._line(MENU_X, y + MENU_H, MENU_X + MENU_W, y + MENU_H, UI_START_RULE)
                if self.hover == key and not disabled:
                    self._rect(MENU_X, y, 2, MENU_H, UI_START_RED)

            num_c = "#F4C8C4" if active else UI_START_DIM2
            if active:
                zh_c = UI_START_INK
            elif disabled:
                zh_c = "#3E3E45"
            elif self.hover == key:
                zh_c = "#C9C9CF"
            else:
                zh_c = UI_START_DIM1
            en_c = "#F6D9D6" if active else ("#33333A" if disabled else UI_START_DIM2)

            self._t(MENU_X + 18, y + 18, num, 10, num_c, F_LAT, 2.4)
            self._t(MENU_X + 56, y + 42, zh, 50, zh_c, F_MED if active else F_REG)
            self._t(MENU_X + 60, y + 114, en, 9.5, en_c, F_LAT, 3.6)

            meta = self._meta(key)
            if meta:
                self._t(0, y + 18, meta, 8.5, "#F6D9D6" if active else UI_START_DIM2,
                        F_LAT, 1.4, right=MENU_X + MENU_W - 18)

    def _meta(self, key: str) -> str:
        if key == "continue":
            if self._save_game is None:
                return "无存档 · NO SAVE"
            return f"第 {self._save_game.turn} 回合"
        if key == "settings":
            return "当前配置"
        if key == "about":
            return f"v{VERSION}"
        return ""

    def _disabled(self, key: str) -> bool:
        return key == "continue" and self._save_game is None

    def _draw_panel(self) -> None:
        if self.sel == "continue" and self._save_game is not None:
            self._panel_save()
        elif self.sel == "about":
            self._panel_about()
        else:
            self._panel_settings(editable=(self.sel == "settings"))

    def _panel_frame(self, zh: str, en: str, meta: str, meta_en: str) -> None:
        self._rect(PANEL_X, PANEL_Y, PANEL_W, PANEL_H, UI_START_YELLOW)
        self._t(PANEL_L, 428, zh, 30, UI_START_BG, F_MED)
        self._t(PANEL_L, 474, en, 9, "#4A4310", F_LAT, 4.2)
        self._t(0, 428, meta, 10, "#4A4310", F_MED, 0, right=PANEL_R)
        self._t(0, 448, meta_en, 8, "#6B6318", F_LAT, 2.2, right=PANEL_R)
        self._line(PANEL_L, 500, PANEL_R, 500, "#3A3407")

    def _row(self, i: int, num: str, zh: str, en: str, value: str, *,
             emphasis: bool = False, size: float = 15, focus: bool = False,
             arrow: bool = False, hover: bool = False, chip: bool = False,
             hot: bool = False) -> None:
        y = ROWS_Y + i * ROW_H
        if hover:
            # 可点的行给一块压深的黄底：鼠标扫过就知道这行能按
            self.cv.create_rectangle(self.X(PANEL_L - 22), self.Y(y + 3),
                                     self.X(PANEL_R + 16), self.Y(y + ROW_H - 3),
                                     fill="#DCD000", outline="")
            if not focus:
                self._rect(PANEL_L - 22, y + 3, 3, ROW_H - 6, UI_START_RED)
        if focus:
            self._rect(PANEL_L - 18, y + 8, 4, ROW_H - 16, UI_START_BG)
        self._t(PANEL_L, y + 14, num, 9, "#6B6318", F_LAT, 2.0)
        self._t(PANEL_L + 36, y + 8, zh, 18, UI_START_BG, F_REG)
        self._t(PANEL_L + 38, y + 31, en, 7.5, "#5A5413", F_LAT, 2.2)
        text = f"‹ {value} ›" if arrow else value
        fam = F_MED if emphasis else F_LAT
        track = 1.2 if emphasis else 0.0
        if chip:
            # 把取值画成一个真开关：黑描边方块，悬停反白——一眼看出"这块能按"
            f = self._font(fam, size, False)
            w = self._text_width(text, f, track) / self._k
            self.cv.create_rectangle(
                self.X(PANEL_R - w - 14), self.Y(y + 9),
                self.X(PANEL_R + 14), self.Y(y + ROW_H - 9),
                fill=UI_START_BG if hot else UI_START_YELLOW,
                outline=UI_START_BG, width=max(1.0, 1.5 * self._k))
            self._t(0, y + 14, text, size,
                    UI_START_YELLOW if hot else UI_START_BG,
                    fam, track, right=PANEL_R)
        else:
            self._t(0, y + 14, text, size, UI_START_BG, fam, track, right=PANEL_R)
        self._line(PANEL_L, y + ROW_H, PANEL_R, y + ROW_H, "#B8AC00")

    # -- 设置面板
    def _settings_rows(self) -> list[tuple[str, str, str, str, bool, bool]]:
        disp = "全屏" if prefs.get("display") == "fullscreen" else "窗口"
        auto = "开" if prefs.get("autosave") else "关"
        chk = "通过" if self.ok else "失败"
        return [
            ("01", "语言", "LANGUAGE", "简体中文", False, False),
            ("02", "显示", "DISPLAY", disp, True, True),
            ("03", "难度", "DIFFICULTY", "标准", False, False),
            ("04", "自动存档", "AUTOSAVE", auto, True, True),
            ("05", "数据校验", "DATA CHECK", chk, False, True),
        ]

    def _row_action(self, i: int) -> str | None:
        """设置面板第 i 行按下去做什么；None 表示这行只是信息，点不动。"""
        rows = self._settings_rows()
        if not (0 <= i < len(rows)):
            return None
        return {"显示": "display", "自动存档": "autosave",
                "数据校验": "recheck"}.get(rows[i][1])

    def _panel_settings(self, editable: bool) -> None:
        self._panel_frame(
            "设置", "SETTINGS",
            "正在编辑" if editable else "当前配置 · 按 3 修改",
            "EDITING" if editable else "READ-ONLY PREVIEW",
        )
        for i, (num, zh, en, val, arrow, emphasis) in enumerate(self._settings_rows()):
            can_press = editable and self._row_action(i) is not None
            self._row(i, num, zh, en, val, emphasis=emphasis,
                      focus=(editable and self.row == i),
                      arrow=(editable and arrow),
                      hover=(can_press and self.hover_row == i),
                      chip=can_press,
                      hot=(can_press and self.hover_row == i))
        hint = ("↑↓ 选择 · ←→ 调整 · 点击/ENTER 生效 · ESC 返回" if editable
                else "↑↓ 选择 · ENTER 确认 · ESC 退出")
        self._t(PANEL_L, 838, hint, 8.5, "#4A4310", F_LAT, 2.2)

    # -- 存档摘要
    def _panel_save(self) -> None:
        g = self._save_game
        assert g is not None
        self._panel_frame("存档", "SAVE", f"第 {g.turn} 回合", "CONTINUE")
        try:
            when = datetime.fromtimestamp(self._save_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            when = "—"
        rows = [
            ("01", "进度", "PROGRESS", f"第 {g.turn} 回合 / {MAX_TURNS}"),
            ("02", "存档时间", "SAVED", when),
            ("03", "现金", "CASH", money(g.player.cash)),
            ("04", "市值", "MARKET CAP", money(g.player.market_cap())),
            ("05", "市占", "SHARE", f"{g.player_share():.1%}"),
            ("06", "数据校验", "DATA CHECK", "通过" if self.ok else "失败"),
        ]
        for i, (num, zh, en, val) in enumerate(rows):
            self._row(i, num, zh, en, val, emphasis=(i == 5))
        self._t(PANEL_L, 838, "ENTER 继续 · ESC 退出", 8.5, "#4A4310", F_LAT, 2.2)

    # -- 关于
    def _panel_about(self) -> None:
        self._panel_frame("关于", "ABOUT", f"v{VERSION}", "POWER COMPANY")
        rows = [
            ("01", "版本", "VERSION", f"v{VERSION}"),
            ("02", "构建", "BUILD", BUILD),
            ("03", "引擎", "ENGINE", f"Python {sys.version_info.major}.{sys.version_info.minor} · Tk {tk.TkVersion}"),
            ("04", "存档目录", "SAVE DIR", self._short(str(paths.data_dir()), 46)),
            ("05", "字体", "TYPEFACE", "Noto Sans SC" if F_MED.startswith("Noto") else F_MED),
            ("06", "数据校验", "DATA CHECK", "通过" if self.ok else "失败"),
        ]
        for i, (num, zh, en, val) in enumerate(rows):
            self._row(i, num, zh, en, val, emphasis=(i == 5),
                      size=12 if i == 3 else 15)
        self._t(PANEL_L, 838, "ESC 返回菜单", 8.5, "#4A4310", F_LAT, 2.2)

    @staticmethod
    def _short(s: str, n: int) -> str:
        return s if len(s) <= n else "…" + s[-(n - 1):]

    # -- 状态灯与页脚
    def _draw_status(self) -> None:
        ok = self.ok
        color = UI_START_GREEN if ok else UI_START_RED
        self._rect(STATUS_X, STATUS_Y, 4, STATUS_H, color)
        cx, cy = STATUS_X + 19, STATUS_Y + 27
        self.cv.create_oval(self.X(cx - 5), self.Y(cy - 5), self.X(cx + 5), self.Y(cy + 5),
                            fill=color, outline="")
        if ok:
            tail = "可联机" if MULTIPLAYER_READY else "单机模式"
            main = f"文字校对成功 · {tail}"
        else:
            main = "文字校对失败 · 离线"
        self._t(STATUS_X + 36, STATUS_Y + 16, main, 14, UI_START_INK, F_MED)
        self._t(STATUS_X + 36, STATUS_Y + 44, f"v{VERSION} · BUILD {BUILD}", 9,
                UI_START_DIM1, F_LAT, 2.0)

    def _draw_footer(self) -> None:
        self._line(MENU_X, 992, PANEL_R, 992, "#1C1C21")
        self._t(0, 1014, "© 2026 电力公司 · 单机 · 零第三方依赖", 8.5, "#6A6A72",
                F_LAT, 1.6, right=PANEL_R)

    def _draw_quit(self) -> None:
        """页脚左侧的退出按钮：点它 = 按 ESC，走同一套确认。"""
        hov = (self.hover == "quit")
        self.cv.create_rectangle(
            self.X(QUIT_X), self.Y(QUIT_Y),
            self.X(QUIT_X + QUIT_W), self.Y(QUIT_Y + QUIT_H),
            fill=UI_START_RED if hov else UI_START_BG,
            outline=UI_START_RED if hov else "#3A3A41",
            width=max(1.0, 1.0 * self._k))
        sub = "#F6D9D6" if hov else UI_START_DIM2
        self._t(QUIT_X + 16, QUIT_Y + 8, "退出", 16,
                UI_START_INK if hov else UI_START_DIM1, F_MED)
        self._t(QUIT_X + 17, QUIT_Y + 29, "QUIT", 7.5, sub, F_LAT, 2.4)
        self._t(0, QUIT_Y + 21, "ESC", 8.5, sub, F_LAT, 1.6,
                right=QUIT_X + QUIT_W - 14)

    # ------------------------------------------------------------ 交互
    def _hit(self, px: int, py: int) -> str | None:
        x = (px - self._ox) / self._k
        y = (py - self._oy) / self._k
        if not (MENU_X <= x <= MENU_X + MENU_W):
            return None
        if not (MENU_Y <= y <= MENU_Y + MENU_H * len(ITEMS)):
            return None
        i = int((y - MENU_Y) // MENU_H)
        return ITEMS[i][0]

    def _hit_quit(self, px: int, py: int) -> bool:
        x = (px - self._ox) / self._k
        y = (py - self._oy) / self._k
        return (QUIT_X <= x <= QUIT_X + QUIT_W
                and QUIT_Y <= y <= QUIT_Y + QUIT_H)

    def _hit_row(self, px: int, py: int) -> int | None:
        """设置面板里第几行（0 起）。只有选中「设置」时才认这个区域。"""
        if self.sel != "settings":
            return None
        x = (px - self._ox) / self._k
        y = (py - self._oy) / self._k
        n = len(self._settings_rows())
        if not (PANEL_L - 22 <= x <= PANEL_R + 16):
            return None
        if not (ROWS_Y <= y <= ROWS_Y + n * ROW_H):
            return None
        return max(0, min(n - 1, int((y - ROWS_Y) // ROW_H)))

    def _on_motion(self, e) -> None:
        key = self._hit(e.x, e.y)
        row: int | None = None
        if key is None and self._hit_quit(e.x, e.y):
            key = "quit"
        if key is None:
            row = self._hit_row(e.x, e.y)
        self._set_hover(key, row)

    def _set_hover(self, key: str | None, row: int | None = None) -> None:
        if key is not None and self._disabled(key):
            key = None
        if row is not None and self._row_action(row) is None:
            row = None          # 信息行不给"能按"的假象
        if key == self.hover and row == self.hover_row:
            return
        self.hover = key
        self.hover_row = row
        hand = (key is not None) or (row is not None)
        try:
            self.cv.configure(cursor="hand2" if hand else "")
        except tk.TclError:
            pass
        self._render()

    def _on_click(self, e) -> None:
        if self._hit_quit(e.x, e.y):
            self._quit()
            return
        key = self._hit(e.x, e.y)
        if key is not None:
            if self._disabled(key):
                return
            if key in ("new", "continue"):
                # 开始/继续 是一次性动作：点一下就进游戏，不做"先选中再点一次"
                self.sel = key
                self._activate(key)
                return
            self.sel = key
            self.row = 0
            self.hover_row = None
            self._render()
            return
        row = self._hit_row(e.x, e.y)
        if row is None:
            return
        self.row = row
        if self._row_action(row) is not None:
            self._adjust(1)     # 单击 = 直接切换，不用先选中再按 ←→
        else:
            self._render()

    def _move(self, step: int) -> None:
        if self.sel == "settings":
            # 设置面板内：↑↓ 走行焦点，走到头就跳回菜单（不会把玩家困在面板里）
            n = len(self._settings_rows())
            self.row += step
            if self.row < 0:
                self.row = 0
                self._step_menu(-1)
            elif self.row >= n:
                self.row = n - 1
                self._step_menu(1)
            self._render()
            return
        self._step_menu(step)
        self._render()

    def _step_menu(self, step: int) -> None:
        idx = [i for i, it in enumerate(ITEMS) if not self._disabled(it[0])]
        cur = next((n for n, i in enumerate(idx) if ITEMS[i][0] == self.sel), 0)
        key = ITEMS[idx[(cur + step) % len(idx)]][0]
        if key != self.sel:
            self.sel = key
            self.row = 0

    def _on_key(self, e) -> None:
        k = e.keysym
        if k in ("Up", "Down"):
            self._move(-1 if k == "Up" else 1)
        elif k in ("Return", "KP_Enter"):
            if self.sel == "settings" and self._row_action(self.row) is not None:
                self._adjust(1)
            elif self.sel == "settings":
                self.sel = "new"
                self._render()
            else:
                self._activate(self.sel)
        elif k == "Escape":
            if self.sel in ("settings", "about"):
                self.sel = "new"
                self._render()
            else:
                self._quit()
        elif k in ("Left", "Right") and self.sel == "settings":
            self._adjust(-1 if k == "Left" else 1)
        elif k == "F11":
            self._toggle_display()

    def _apply_display(self) -> None:
        """显示方式由外层窗口负责（它是 App），这里只转发。"""
        if self._on_display is not None:
            self._on_display()

    def _adjust(self, step: int) -> None:
        act = self._row_action(self.row)
        if act is None:
            return              # 只读行：不假装有开关
        if act == "display":
            prefs.put("display", "windowed" if prefs.get("display") == "fullscreen" else "fullscreen")
            self._apply_display()
            self.after(60, self._render)
        elif act == "autosave":
            prefs.put("autosave", not prefs.get("autosave"))
        elif act == "recheck":
            self.check = self._self_check()
        prefs.save()
        self._render()

    def _toggle_display(self) -> None:
        prefs.put("display", "windowed" if prefs.get("display") == "fullscreen" else "fullscreen")
        prefs.save()
        self._apply_display()
        self.after(60, self._render)

    def _close(self) -> None:
        """把自己摘掉：解绑按键 + destroy。窗口留给游戏用，不重建。"""
        if self._key_bind:
            try:
                self._root.unbind("<Key>", self._key_bind)
            except tk.TclError:
                pass
            self._key_bind = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _activate(self, key: str) -> None:
        if key == "new":
            self.choice = ("new", None)
            self._close()
            if self._on_new is not None:
                self._on_new()
        elif key == "continue":
            if self._save_game is None:
                return
            self.choice = ("continue", self._save_game)
            self._close()
            if self._on_continue is not None:
                self._on_continue(self._save_game)

    def _quit(self) -> None:
        try:
            ok = messagebox.askyesno("退出", "确定退出《电力公司》？", parent=self._root)
        except tk.TclError:
            ok = True
        if not ok:
            return
        self.choice = None
        self._close()
        if self._on_quit is not None:
            self._on_quit()
