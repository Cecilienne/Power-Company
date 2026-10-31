"""背景音乐：只用 Windows 自带的 Media Foundation（ctypes 调用，不引第三方库）。

做法
----
`mfplay.dll` 的 `MFPCreateMediaPlayer` 能把 mp4 直接当媒体打开：它解码音轨、
顺手把画面塞进我们给的窗口里。这里传一个 1×1、拉到屏幕外的隐藏窗口，
玩家就只听得到声音，看不到画面。

为什么不用别的
--------------
* `winsound` 只吃 WAV，mp4 里的 AAC 解不了；
* MCI 的 `mpegvideo` 设备在 Windows 11 上已经没有了（返回 277）；
* 转码要外部 ffmpeg —— 与「零第三方依赖」冲突。

降级
----
本机没有 Media Foundation（精简版 / N 版 Windows）时 `MFPCreateMediaPlayer`
返回失败码，这里老实记下原因并报「不可用」，设置页也照实显示，
不做一个点了没声音的开关。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

MF_VERSION = 0x00020070          # MF_SDK_VERSION(2) | MF_API_VERSION -> 2.0.0
MFSTARTUP_FULL = 0
COINIT_APARTMENTTHREADED = 0x2

# MFP_MEDIAPLAYER_STATE
STATE_EMPTY, STATE_STOPPED, STATE_PAUSED, STATE_PLAYING, STATE_SHUTDOWN = range(5)

_HRESULT = ctypes.c_long


class _MfPlayer:
    """一个媒体播放器句柄；失败时 available=False，reason 里写人话。"""

    def __init__(self, path: str, hwnd: int, volume: float = 0.35) -> None:
        self.available = False
        self.reason = ""
        self._player = ctypes.c_void_p()
        self._played = False
        self._volume = max(0.0, min(1.0, float(volume)))
        self._play = None
        self._stop = None
        self._shutdown = None
        self._state = None
        self._seek = None
        self._volume_fn = None
        self._open(path, hwnd)

    # -- 打开
    def _open(self, path: str, hwnd: int) -> None:
        try:
            ole32 = ctypes.WinDLL("ole32")
            ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)   # 失败了也能继续
            mfplat = ctypes.WinDLL("mfplat")
            mfplat.MFStartup(MF_VERSION, MFSTARTUP_FULL)
            dll = ctypes.WinDLL("mfplay")
        except OSError as e:                                     # 系统太旧 / 精简版
            self.reason = f"缺少 Media Foundation（{e}）"
            return

        create = dll.MFPCreateMediaPlayer
        create.restype = _HRESULT
        create.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, ctypes.c_int,
                           ctypes.c_void_p, wintypes.HWND,
                           ctypes.POINTER(ctypes.c_void_p)]
        rc = create(path, True, 0, None, wintypes.HWND(hwnd),
                    ctypes.byref(self._player))
        if (rc & 0xFFFFFFFF) != 0 or not self._player.value:
            self.reason = f"播放器打不开这个文件（HRESULT 0x{rc & 0xFFFFFFFF:08X}）"
            self._player = ctypes.c_void_p()
            return

        try:
            vt = ctypes.cast(self._player,
                             ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

            def method(index: int, *args):
                proto = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, *args)
                return proto(vt[index])

            self._state = method(16, ctypes.POINTER(ctypes.c_int))      # GetState
            self._play = method(17)                                    # Play
            self._stop = method(19)                                    # Stop
            self._shutdown = method(20)                                # Shutdown
            self._volume_fn = method(22, ctypes.c_float)               # SetVolume
            self._seek = method(12, ctypes.c_longlong)                 # SetCurrentPosition
        except Exception as e:  # noqa: BLE001
            self.reason = f"播放器接口异常（{e}）"
            return
        self.available = True

    # -- 控制
    def play(self) -> None:
        if not self.available:
            return
        try:
            self._volume_fn(self._player, ctypes.c_float(self._volume))
            self._play(self._player)
            self._played = True
        except Exception:  # noqa: BLE001
            self.available = False

    def stop(self) -> None:
        if not self.available:
            return
        try:
            self._stop(self._player)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._player.value and self._shutdown is not None:
            try:
                self._shutdown(self._player)
            except Exception:  # noqa: BLE001
                pass
        self._player = ctypes.c_void_p()
        self.available = False

    def state(self) -> int:
        if not self.available:
            return STATE_EMPTY
        out = ctypes.c_int(-1)
        try:
            self._state(self._player, ctypes.byref(out))
        except Exception:  # noqa: BLE001
            return STATE_EMPTY
        return out.value

    def replay_if_ended(self) -> None:
        """播完就从头再来一次（mfplay 的 SetLooping 没在接口白名单里，自己转圈）。"""
        if not self.available or not self._played:
            return
        if self.state() == STATE_STOPPED:
            try:
                self._seek(self._player, ctypes.c_longlong(0))
                self._play(self._player)
            except Exception:  # noqa: BLE001
                self.available = False


class Bgm:
    """全局背景音乐（单例由 UI 持有）。"""

    def __init__(self) -> None:
        self.player: _MfPlayer | None = None
        self.path: str | None = None
        self.reason = ""
        self._hidden = None            # 1×1 隐藏窗口，专门给 mfplay 渲染画面用

    # -- 生命周期
    def start(self, root, path: str | None) -> bool:
        self.stop()
        if not path or not os.path.exists(path):
            self.reason = "找不到音频文件"
            return False
        if self._hidden is None:
            self._make_hidden(root)
        self.player = _MfPlayer(path, self._hidden.winfo_id())
        self.reason = self.player.reason
        if not self.player.available:
            self.player.close()
            self.player = None
            return False
        self.path = path
        self.player.play()
        return True

    def stop(self) -> None:
        if self.player is not None:
            self.player.close()
            self.player = None
        self.path = None

    def available(self) -> bool:
        return self.player is not None and self.player.available

    def poll(self) -> None:
        if self.player is not None:
            self.player.replay_if_ended()

    # -- 内部
    def _make_hidden(self, root) -> None:
        import tkinter as tk

        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.geometry("1x1+-4000+-4000")
        win.withdraw()                  # 画到屏幕外，玩家看不见
        self._hidden = win


# 全局实例：启动页与游戏页共用同一个，切界面时音乐不断
BGM = Bgm()
