"""UI 层（tkinter）。"""
from __future__ import annotations

from .. import paths


def _icon_candidates(name: str):
    """先找打包解包目录（sys._MEIPASS），再找 exe 旁边，最后找项目根目录。"""
    seen: set = set()
    for base in (paths.resource_dir(), paths.app_dir()):
        p = base / "assets" / name
        if p in seen:
            continue
        seen.add(p)
        yield p


def set_window_icon(win) -> None:
    """给窗口装应用图标。

    `assets/app.ico` 是可选资源，不存在时全部静默跳过——图标缺失不该让游戏起不来。
    """
    for ico in _icon_candidates("app.ico"):
        if not ico.exists():
            continue
        try:
            win.iconbitmap(str(ico))
            return
        except Exception:  # noqa: BLE001
            pass
    for png in _icon_candidates("app_512.png"):
        if not png.exists():
            continue
        try:
            import tkinter as tk
            img = tk.PhotoImage(master=win, file=str(png))
            win.iconphoto(True, img)
            win._icon_img = img      # 必须留引用，否则会被回收成空图标
            return
        except Exception:  # noqa: BLE001
            pass
