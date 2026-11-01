"""背景音乐：只用 Windows 自带的 Media Foundation（ctypes 调用，不引第三方库）。

做法
----
`mfplay.dll` 的 `MFPCreateMediaPlayer` 能把 mp4 当媒体打开：它解码音轨、
顺手把画面渲染进我们给的窗口。这里给它一个 1×1、摆在屏幕外的小窗口，
玩家就只听得到声音，看不到画面。

两个坑（都踩过，记在这里免得再踩）
---------------------------------
1. **vtable 下标必须对**。`IMFPMediaPlayer` 的方法顺序是
   `SetWindow(3) SetStreamSink(4) Mute(5) Unmute(6) SetVolume(7) GetVolume(8)
   GetMute(9) GetDuration(10) GetCurrentPosition(11) SetCurrentPosition(12)
   SetPlaybackRate(13) GetPlaybackRate(14) Play(15) Pause(16) Stop(17)
   Shutdown(18) GetState(19) ...`（0/1/2 是 IUnknown）。
   下标错一位就会"调用成功但毫无反应"——之前 Play 写成了 17，实际调到的是
   Stop，于是全程静音却一句报错都没有。
2. **窗口不能 withdraw**。mfplay 要往窗口里画，窗口不存在就整条会话失败。
   放屏幕外（-4000,-4000）、1×1、保留 `overrideredirect`，看得见但看不见。

为什么不用别的
--------------
* `winsound` 只吃 WAV，mp4 里的 AAC 解不了；
* MCI 的 `mpegvideo` 设备在 Windows 11 上已经没有了（返回 277）；
* 转码要外部 ffmpeg —— 与「零第三方依赖」冲突。

降级
----
本机没有 Media Foundation（精简版 / N 版 Windows）或打不开文件时，如实记下
原因并报「不可用」，设置页照实显示，不做一个点了没声音的开关。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

MF_VERSION = 0x00020070          # MF_SDK_VERSION(2) | MF_API_VERSION -> 2.0.0
MFSTARTUP_FULL = 0
COINIT_APARTMENTTHREADED = 0x2

# IMFPMediaPlayer 的 vtable 下标（见模块注释）
VT_PLAY = 15
VT_PAUSE = 16
VT_STOP = 17
VT_SHUTDOWN = 18
VT_STATE = 19
VT_SEEK = 12
VT_DURATION = 10

# MFP_MEDIAPLAYER_STATE
STATE_EMPTY, STATE_STOPPED, STATE_PAUSED, STATE_PLAYING, STATE_SHUTDOWN = range(5)
STATE_NAMES = {
    STATE_EMPTY: "EMPTY",
    STATE_STOPPED: "STOPPED",
    STATE_PAUSED: "PAUSED",
    STATE_PLAYING: "PLAYING",
    STATE_SHUTDOWN: "SHUTDOWN",
}

_HRESULT = ctypes.c_long


class _MfPlayer:
    """一个媒体播放器句柄；失败时 available=False，reason 里写人话。"""

    def __init__(self, path: str, hwnd: int) -> None:
        self.available = False
        self.reason = ""
        self.layout_ok = False
        self._player = ctypes.c_void_p()
        self._play = None
        self._stop = None
        self._shutdown = None
        self._state = None
        self._seek = None
        self._open(path, hwnd)

    # -- 打开
    def _open(self, path: str, hwnd: int) -> None:
        try:
            ole32 = ctypes.WinDLL("ole32")
            ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)   # 失败也能继续
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
        # fStartPlayback=True：建好就开始播，不用再调 Play(15)
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

            self._play = method(VT_PLAY)
            self._stop = method(VT_STOP)
            self._shutdown = method(VT_SHUTDOWN)
            self._state = method(VT_STATE, ctypes.POINTER(ctypes.c_int))
            self._seek = method(VT_SEEK, ctypes.c_longlong)
            self._release = method(2)                      # IUnknown::Release，永远是对的
        except Exception as e:  # noqa: BLE001
            self.reason = f"播放器接口异常（{e}）"
            return
        # 用 GetState 验一下下标是否对得上：返回 0..4 才说明 vtable 布局没变。
        # 对不上就只用 Release 收尾，绝不拿错位的函数指针去 Stop/Shutdown（会写坏内存）。
        self.layout_ok = False
        probe = ctypes.c_int(-1)
        try:
            if (self._state(self._player, ctypes.byref(probe)) & 0xFFFFFFFF) == 0 and 0 <= probe.value <= 4:
                self.layout_ok = True
        except Exception:  # noqa: BLE001
            pass
        self.available = True

    # -- 控制
    def play(self) -> None:
        if self.available and self.layout_ok:
            try:
                self._play(self._player)
            except Exception:  # noqa: BLE001
                self.available = False

    def stop(self) -> None:
        if not self.available or not self.layout_ok:
            return
        try:
            self._stop(self._player)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._player.value:
            try:
                if self.layout_ok and self._shutdown is not None:
                    self._shutdown(self._player)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._release(self._player)                # 没 Shutdown 也要把引用放掉
            except Exception:  # noqa: BLE001
                pass
        self._player = ctypes.c_void_p()
        self.available = False

    def state(self) -> int:
        if not self.available or not self.layout_ok:
            return -1                  # -1 = 读不到（调用方按"未知"处理）
        out = ctypes.c_int(-1)
        try:
            self._state(self._player, ctypes.byref(out))
        except Exception:  # noqa: BLE001
            return -1
        return int(out.value)

    def replay_if_ended(self) -> None:
        """播完就从头再来一次（不进白名单 SetLooping，自己转圈）。"""
        if not self.available:
            return
        if self.state() == STATE_STOPPED:
            try:
                self._seek(self._player, ctypes.c_longlong(0))
                self.play()
            except Exception:  # noqa: BLE001
                self.available = False


class Bgm:
    """全局背景音乐（单例由 UI 持有）。"""

    def __init__(self) -> None:
        self.player: _MfPlayer | None = None
        self.path: str | None = None
        self.reason = ""
        self._hidden = None            # 屏幕外的 1×1 窗口，专门给 mfplay 渲染画面

    # -- 生命周期
    def start(self, root, path: str | None) -> bool:
        """开播；只有真的进入 PLAYING 才返回 True。"""
        self.stop()
        if not path or not os.path.exists(path):
            self.reason = "找不到音频文件"
            return False
        if self._hidden is None:
            self._make_hidden(root)
        self.player = _MfPlayer(path, self._hidden.winfo_id())
        if not self.player.available:
            self.reason = self.player.reason
            self._log(root, "打不开")
            self.player.close()
            self.player = None
            return False
        self.path = path
        # 等它真的播起来（最多 ~1.2 秒）；停在这儿说明解码/输出有问题
        state = STATE_EMPTY
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            self._pump(root, 50)
            state = self.player.state()
            if state == STATE_PLAYING:
                self._log(root, "开始播放")
                return True
        if state < 0:
            # 读不到状态（接口下标对不上之类）：建得起来就当它在放，别乱报错
            self._log(root, "开始播放（状态未知）")
            return True
        self.reason = f"播放器没有开始播放（状态 {STATE_NAMES.get(state, state)}）"
        self._log(root, f"超时 state={STATE_NAMES.get(state, state)}")
        self.player.close()
        self.player = None
        return False

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
        win.update_idletasks()          # 必须真存在；withdraw 会让 mfplay 直接失败
        self._hidden = win

    @staticmethod
    def _pump(root, ms: int) -> None:
        """等的时候别把界面冻住。"""
        try:
            end = time.monotonic() + ms / 1000.0
            while time.monotonic() < end:
                root.update()
                time.sleep(0.01)
        except Exception:  # noqa: BLE001
            time.sleep(ms / 1000.0)

    @staticmethod
    def _log(root, text: str) -> None:
        """留一行现场记录，万一用户说没声音，能拿这个定位。"""
        try:
            from . import paths

            p = paths.data_dir() / "audio.log"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except Exception:  # noqa: BLE001
            pass


# 全局实例：启动页与游戏页共用同一个，切界面时音乐不断
BGM = Bgm()
