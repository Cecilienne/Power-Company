"""把 dist 里的产物打成可发布的发行版：MANIFEST + zip + 校验文件。

做三件事：
  1. 在程序目录里写 MANIFEST.json —— 版本、git 提交、每个文件的 SHA-256，
     玩家/你以后都能核对"发出去的到底是哪份代码"。
  2. 把整个程序目录压成 release/<名字>-win64.zip。
  3. 写 release/<名字>-win64.zip.sha256，供下载页校验。

用法：python tools/pack_release.py
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONST = ROOT / "pwer" / "constants.py"
DIST = ROOT / "dist"
ONEDIR = DIST / "电力公司"
RELEASE = ROOT / "release"
APP_NAME = "电力公司"
MANIFEST = "MANIFEST.json"
# 不参与哈希的东西（MANIFEST 自己不哈希自己，日志是本地运行产物）
SKIP_NAMES = {MANIFEST, "selftest.log"}
# 不放进发行包的东西（日志是构建/运行痕迹，玩家不需要）
EXCLUDE_FROM_ZIP = {"selftest.log"}
SKIP_DIRS = {"__pycache__", "saves"}


def read_const(name: str, default: str = "") -> str:
    try:
        text = CONST.read_text(encoding="utf-8")
    except OSError:
        return default
    m = re.search(rf'^{name}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else default


def git(*args: str) -> str:
    """跑一条 git 命令拿 stdout。

    故意用临时文件而不是管道收输出：某些受限环境（沙箱、部分企业策略）会拒绝
    `CreatePipe`，`capture_output=True` 直接抛 WinError 5，那样 MANIFEST 里的
    git 提交号就会莫名其妙变成空字符串。
    """
    try:
        with tempfile.TemporaryFile() as out:
            rc = subprocess.run(
                ["git", *args], cwd=ROOT, stdout=out,
                stderr=subprocess.DEVNULL, timeout=20,
            ).returncode
            if rc != 0:
                return ""
            out.seek(0)
            return out.read().decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def locate() -> tuple[Path, str]:
    """返回 (程序目录, 'onedir' | 'onefile')。"""
    if (ONEDIR / f"{APP_NAME}.exe").exists():
        return ONEDIR, "onedir"
    exe = DIST / f"{APP_NAME}.exe"
    if exe.exists():
        return DIST, "onefile"
    raise SystemExit(f"[错误] 没找到产物：{ONEDIR}\\{APP_NAME}.exe 或 {exe}")


def collect(app: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(app.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(app)
        if p.name in SKIP_NAMES or rel.parts[0] in SKIP_DIRS:
            continue
        files.append(p)
    return files


def main() -> int:
    version = read_const("VERSION", "0.0.0")
    build = read_const("BUILD", "")
    app, kind = locate()
    files = collect(app)

    rev = git("rev-parse", "--short", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    now = datetime.now().astimezone()

    entries = []
    total = 0
    for p in files:
        size = p.stat().st_size
        total += size
        entries.append({
            "path": p.relative_to(app).as_posix(),
            "size": size,
            "sha256": sha256(p),
        })

    manifest = {
        "app": APP_NAME,
        "version": version,
        "build": build,
        "kind": kind,
        "gitRev": rev,
        "gitDirty": dirty,
        "builtAt": now.isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "fileCount": len(entries),
        "totalBytes": total,
        "files": entries,
    }
    (app / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    RELEASE.mkdir(parents=True, exist_ok=True)
    stem = f"{APP_NAME}-{version}-win64"
    zip_path = RELEASE / f"{stem}.zip"
    if zip_path.exists():
        zip_path.unlink()

    # 整个程序目录（含 MANIFEST.json，含空目录结构以外的一切文件）打成一个包
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        top = app.name
        for p in sorted(app.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(app)
            if any(part in SKIP_DIRS or part == "__pycache__" for part in rel.parts):
                continue
            if p.name in EXCLUDE_FROM_ZIP or p.suffix.lower() == ".log":
                continue
            zf.write(p, f"{top}/{rel.as_posix()}")

    zsha = sha256(zip_path)
    (RELEASE / f"{stem}.zip.sha256").write_text(
        f"{zsha}  {zip_path.name}\n", encoding="utf-8"
    )

    exe = app / f"{APP_NAME}.exe"
    mb = lambda n: f"{n / 1024 / 1024:.2f} MB"  # noqa: E731
    print(f"MANIFEST   : {app / MANIFEST}")
    print(f"  版本     : v{version} · BUILD {build} · {rev}{'+dirty' if dirty else ''}")
    print(f"  文件数   : {len(entries)} 个，共 {mb(total)}")
    print(f"  主程序   : {exe.name} {mb(exe.stat().st_size)}  sha256 {sha256(exe)[:16]}…")
    print(f"发行包     : {zip_path}  {mb(zip_path.stat().st_size)}")
    print(f"  校验     : {zsha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
