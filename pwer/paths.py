"""路径工具：区分"源码运行"与"打包成 exe 运行"。

存档目录按优先级回落，任何一个不可写就换下一个：
    exe  : %APPDATA%\\PowerCompany  →  exe 同目录\\saves  →  系统临时目录
    源码 : 项目目录\\saves          →  系统临时目录
这样绿色版、便携版（U 盘）、受限权限环境（如被安全策略锁住的 AppData）都能存档。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "PowerCompany"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """exe 所在目录（源码运行时是项目根目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_dir() -> Path:
    """只读资源目录（图标等）。

    打包后 PyInstaller 会把 `--add-data` 的内容解到临时目录 `sys._MEIPASS`，
    而不是 exe 旁边；源码运行时就直接用项目根目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def _candidates() -> list[Path]:
    if is_frozen():
        out: list[Path] = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / APP_NAME)
        out.append(app_dir() / "saves")
        return out
    return [app_dir() / "saves"]


def _usable(p: Path) -> bool:
    """目录存在且真的能写文件才算数。"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def data_dir() -> Path:
    for cand in _candidates():
        if _usable(cand):
            return cand
    fallback = Path(tempfile.gettempdir()) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def save_file(name: str = "slot1.pkl") -> Path:
    return data_dir() / name


def asset(*parts: str) -> Path:
    """只读资源（图标、背景音乐）——打包后在 _MEIPASS 里，源码运行时在项目根目录。"""
    return resource_dir().joinpath(*parts)
