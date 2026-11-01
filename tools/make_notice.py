"""生成发给玩家看的《绿色小游戏告知.txt》（UTF-8 BOM + CRLF，记事本双击不乱码）。

用法：python tools/make_notice.py
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_ONEDIR = ROOT / "dist" / "电力公司"
_ONEFILE = ROOT / "dist" / "电力公司.exe"
APP = _ONEDIR if (_ONEDIR / "电力公司.exe").exists() else ROOT / "dist"
EXE = APP / "电力公司.exe"
OUT = APP / "绿色小游戏告知.txt"


def fmt_size(n: int) -> str:
    return f"{n:,} 字节（{n / 1024 / 1024:.2f} MB）"


def main() -> None:
    size = EXE.stat().st_size
    sha = hashlib.sha256(EXE.read_bytes()).hexdigest().upper()
    mtime = datetime.fromtimestamp(EXE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    total = sum(p.stat().st_size for p in APP.rglob("*")
                if p.is_file() and p != OUT)

    text = f"""《电力公司》 绿色单机小游戏 —— 说明与安全告知
================================================================

一、这是什么
    这是一个用 Python + tkinter 写的 2D 回合制单机小游戏：经营电网、定价抢客户、
    看财报、炒股价、收购对手。
    · 单机运行，不需要联网，不需要注册，没有内购
    · 不需要安装，双击 电力公司.exe 就能玩
    · 存档：%APPDATA%\\PowerCompany\\slot1.pkl（不可写时自动改存到程序目录 saves\\）
    · 背景音乐：把 mp3 / m4a / aac / mp4 / wav / wma 放进存档目录的 bgm\\ 文件夹，
      游戏里左轨【音乐】就能选曲播放（曲库 = 自己放的 + 随包自带的那首，列表循环）


二、为什么 Windows 会提示"已保护你的电脑"
    那是 SmartScreen 的"信誉提示"，不是病毒报警。原因只有两个：
      1. 程序没有购买数字签名证书（个人开发者几乎都没有）
      2. 这个文件在微软那里没有下载/运行记录，没有任何信誉
    任何个人开发者刚编译出来的 exe，第一次运行都会遇到这个提示——
    它说的是"我不认识它"，不是"它是病毒"。


三、怎么放行（三选一）
    方法 1：在提示框里点"更多信息"，再点右下角的"仍要运行"
    方法 2：右键 电力公司.exe → 属性 → 常规页底部勾选"解除锁定" → 确定
    方法 3：PowerShell 里执行
            Unblock-File .\电力公司.exe    （在文件所在目录打开 PowerShell）


四、这个程序到底做了什么（可逐条核对）
    · 不联网：代码里没有任何 socket / http / requests 调用，全程离线
    · 不下载、不上传任何数据，不收集任何信息
    · 不请求管理员权限：没有驱动、没有系统服务、不写注册表、不设开机启动
    · 不常驻后台：关掉窗口进程就退出，任务管理器里不会残留
    · 只会在两个位置写文件：
        1) %APPDATA%\\PowerCompany\\slot1.pkl   —— 你的存档
        2) 程序目录\\selftest.log              —— 只有手动跑 --selftest 才产生
    · 打开后做的事情只有一件：算数 + 在窗口里画格子地图


五、关于"源码"这件事（先说清楚，免得你翻包找不到）
    发行包里**没有**可读的 .py 源文件。程序用 PyInstaller 打包，游戏代码
    已经编译成 Python 字节码，和 Python 运行时一起封在 电力公司.exe 和
    _internal\\ 里。这不是加密，也不是加壳，只是"不是源码"。
    想看源码：https://github.com/Asilentperson/pwer_


六、你可以自己验证
    1) 自检：命令行执行   电力公司.exe --selftest
       会依次检查 tkinter、游戏引擎、界面能否创建，全部通过会打印"自检通过"
    2) 杀毒扫描：右键 dist\\电力公司 文件夹 → 使用 Microsoft Defender 扫描
       本项目是标准 PyInstaller 产物，没有加壳、没有混淆、没有第三方网络 SDK
    3) 看源码 / 自己跑：源码在 https://github.com/Asilentperson/pwer_
       装个 Python 3.10+（带 tkinter）后  python main.py  效果完全一样
    4) 核对文件：程序目录里的 MANIFEST.json 记录了每个文件的 SHA-256，
       PowerShell 执行  Get-FileHash .\\电力公司.exe -Algorithm SHA256
       与 MANIFEST.json 里 电力公司.exe 那一行对比即可


七、文件信息（可核对，防止被掉包）
    文件名    : 电力公司.exe
    大小      : {fmt_size(size)}
    构建时间  : {mtime}
    整个目录  : {fmt_size(total)}
    SHA-256   : {sha}
    核对方法  : PowerShell 执行  Get-FileHash .\\电力公司.exe -Algorithm SHA256


八、作者声明
    本程序是个人业余作品，不含任何恶意代码、广告、捆绑软件或后门。
    因为没买代码签名证书，Windows 会显示"未知发布者"，这属于正常现象。
    如果仍然不放心：删掉它、只跑源码，或者干脆别玩——完全没问题。

    祝玩得开心。
"""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 with BOM + CRLF：Windows 记事本双击打开不会乱码
    data = text.replace("\n", "\r\n").encode("utf-8-sig")
    OUT.write_bytes(data)
    print(f"已生成 {OUT}（{len(data):,} 字节）")
    print(f"SHA-256 {sha}")


if __name__ == "__main__":
    main()
