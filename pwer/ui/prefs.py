"""启动页偏好：只存四件事（显示方式 / 自动存档 / 语言 / 背景音乐）。

存档位置跟着 `paths.data_dir()` 走，与游戏存档同目录，避免又多一个路径分支。
读失败一律回落到默认值——配置文件坏掉不该让游戏起不来。
"""
from __future__ import annotations

import json
from typing import Any

from .. import paths

DEFAULTS: dict[str, Any] = {
    "display": "fullscreen",   # fullscreen / windowed
    "autosave": True,
    "language": "zh",
    "audio": "off",            # off / on —— 默认不出声，玩家自己开
}

_cache: dict[str, Any] | None = None


def _file():
    return paths.data_dir() / "settings.json"


def load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    data = dict(DEFAULTS)
    try:
        raw = json.loads(_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS:
                    data[k] = v
    except Exception:  # noqa: BLE001
        pass
    _cache = data
    return data


def get(key: str, default: Any = None) -> Any:
    return load().get(key, DEFAULTS.get(key, default))


def put(key: str, value: Any) -> None:
    data = load()
    if data.get(key) == value:
        return
    data[key] = value
    try:
        _file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def save() -> None:
    try:
        _file().write_text(json.dumps(load(), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
