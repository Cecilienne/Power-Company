"""《电力公司》启动入口。

    python main.py                 # 启动图形界面：先进启动页，点【开始】进游戏
    python main.py --seed 2026     # 指定地图/随机种子
    python main.py --selftest      # 无窗口自检（打包后验证 exe 是否完整）
    python main.py --audio-test    # 只验背景音乐：响 3 秒，放不出来会写原因
"""
from __future__ import annotations

import os
import sys
import time


def _tcl_headless_ok(say) -> bool:
    """没有图形环境时的兜底检查。

    CI 的 Windows runner 跑在无桌面的会话里，Tk() 建不了窗，但这不代表
    打包坏了 —— 真正要保证的是 tcl/tk 的运行时数据文件齐全。
    tkinter.Tcl() 会真的去加载 tcl 库并跑 init.tcl，缺文件会直接抛异常，
    所以它能替我们证明「tcl/tk 数据被完整打进产物」。
    """
    try:
        import tkinter

        tcl = tkinter.Tcl()
        patch = tcl.eval("info patchlevel")
        say(f"tcl 运行时可用：Tcl {patch}（无图形环境，未建窗）")
        return True
    except Exception as e:  # noqa: BLE001
        say(f"tcl 运行时不可用：{e}")
        return False


def selftest() -> int:
    """不打开窗口，验证引擎与 tkinter 都在打包产物里。

    注意：PyInstaller --windowed 打包后没有控制台，sys.stdout 可能为 None，
    所以结果同时写到 exe 同目录的 selftest.log。
    """
    lines: list[str] = []
    code = 0
    os.environ["PWER_NO_INTRO"] = "1"

    def say(msg: str) -> None:
        lines.append(msg)
        try:
            print(msg)
        except Exception:  # noqa: BLE001
            pass

    say(f"python {sys.version.split()[0]}  frozen={getattr(sys, 'frozen', False)}")
    try:
        import tkinter
        say(f"tkinter ok, Tcl/Tk {tkinter.TkVersion}")
    except Exception as e:  # noqa: BLE001
        say(f"tkinter 缺失：{e}")
        code = 2
    try:
        from pwer import paths
        from pwer.engine import Game
        g = Game.new_game(7)
        for _ in range(5):
            g.end_turn()
        say(f"engine ok: {g.turn_label()}  股价 {g.player.price:.2f}  "
            f"现金 {g.player.cash:,.0f} 万  份额 {g.player_share():.1%}")
        say(f"存档目录：{paths.data_dir()}")
        if code == 0:
            say("引擎自检通过")
    except Exception as e:  # noqa: BLE001
        import traceback
        say(f"引擎自检失败：{e}")
        lines.append(traceback.format_exc())
        code = 3

    # 真正建一次界面：验证 tcl/tk 数据文件是否被完整打包
    if code == 0:
        try:
            from pwer.ui.app import App
            app = App()
            app.update()
            say(f"界面自检通过：窗口 {app.winfo_width()}x{app.winfo_height()}，"
                f"地图 {app.map.canvas.winfo_width()}x{app.map.canvas.winfo_height()}")
            app.destroy()
            say("自检通过")
        except Exception as e:  # noqa: BLE001
            # 建不了窗可能是「没有桌面」（CI / 服务账号 / 某些远程会话），
            # 也可能真的打坏了。用 Tcl() 区分这两种情况。
            if _tcl_headless_ok(say):
                say(f"界面跳过：{type(e).__name__}: {e}")
                say("自检通过（headless：tcl/tk 数据完整，未验证建窗）")
            else:
                import traceback
                say(f"界面自检失败：{e}")
                lines.append(traceback.format_exc())
                code = 4

    try:
        from pwer import paths
        log = paths.app_dir() / "selftest.log"
        log.write_text("\n".join(lines), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return code


def audio_test(seconds: float = 3.0) -> int:
    """`--audio-test`：只验背景音乐这一条链，最多响 3 秒。

    排查"点了开关但没声音"时用它：能出声就不是播放器的问题，
    出不了声也会把原因写在屏幕上和 audio.log 里。
    """
    import tkinter as tk

    from pwer import audio, paths

    path = str(paths.asset("assets", "bgm", "bgm_01.mp4"))
    print(f"音频文件：{path}")
    print(f"文件存在：{os.path.exists(path)}")
    root = tk.Tk()
    root.withdraw()
    ok = audio.BGM.start(root, path)
    print(f"start() -> {ok}")
    print(f"available -> {audio.BGM.available()}")
    print(f"reason -> {audio.BGM.reason or '（无）'}")
    if ok:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            audio.BGM.poll()
            time.sleep(0.05)
    audio.BGM.stop()
    root.destroy()
    print("结果：", "能放" if ok else "放不出来")
    return 0 if ok else 3


def main() -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    if "--selftest" in sys.argv:
        return selftest()
    if "--audio-test" in sys.argv:
        return audio_test()

    seed = 20240501
    if "--seed" in sys.argv:
        try:
            seed = int(sys.argv[sys.argv.index("--seed") + 1])
        except (IndexError, ValueError):
            pass

    from pwer.ui.app import App

    # 单窗口：启动页是盖在游戏窗口上的覆盖层，点【开始】只是把它摘掉，
    # 不会再销毁窗口重新开一个（那样会闪一下，全屏下尤其明显）。
    app = App(show_start=True, seed=seed)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
