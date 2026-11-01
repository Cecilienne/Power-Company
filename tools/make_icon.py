"""生成应用图标 assets/app.ico（纯矢量绘制，无美术素材、无第三方依赖除 Pillow）。

几何取自启动页标题块：蓝底 + 白色闪电多边形（与 `pwer/ui/start.py` 同一组点）。
只输出到 assets/ 与 docs/screenshots/，不在 tools/ 里落任何游戏资源。

用法：
    python tools/make_icon.py            # 生成 ico + 512/1080 png + 多尺寸预览图
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT_ICO = ROOT / "assets" / "app.ico"
OUT_PNG = ROOT / "assets" / "app_512.png"
OUT_PNG_MASTER = ROOT / "assets" / "app_1080.png"   # 封面/商店图的母版（1080×1080）
OUT_PREVIEW = ROOT / "docs" / "screenshots" / "app_icon_preview.png"

# 启动页色板（constants.UI_START_*）
BLUE = (33, 68, 156, 255)      # #21449C
INK = (244, 242, 236, 255)     # #F4F2EC

# 闪电：与 start.py 里标题块的 6 点多边形完全一致（单位框 0..1）
BOLT = (
    (0.58, 0.02), (0.14, 0.56), (0.44, 0.56),
    (0.34, 0.98), (0.88, 0.40), (0.55, 0.40),
)
SS = 4          # 超采样倍数：斜边在 16px 也不出锯齿


def _bolt(cx: float, cy: float, h: float) -> list[tuple[float, float]]:
    """把单位框里的闪电缩放到高度 h 并居中。宽高比取自多边形自身包围盒。"""
    xs = [p[0] for p in BOLT]
    ys = [p[1] for p in BOLT]
    w0, h0 = max(xs) - min(xs), max(ys) - min(ys)
    w = h * w0 / h0
    mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    out = []
    for px, py in BOLT:
        out.append((cx + (px - mx) / w0 * w, cy + (py - my) / h0 * h))
    return out


def draw(size: int, framed: bool = False) -> Image.Image:
    s = max(8, size) * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, s - 1, s - 1], fill=BLUE)

    if framed:
        m = s * 0.105
        t = max(1, round(s * 0.040))
        d.rectangle([m, m, s - 1 - m, s - 1 - m], outline=INK, width=t)
        bh = s * 0.50
    else:
        bh = s * 0.72

    d.polygon(_bolt(s / 2, s / 2, bh), fill=INK)
    return img.resize((max(8, size), max(8, size)), Image.LANCZOS)


def build_preview() -> None:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    pad = 28
    cell = 256
    col_w = 300
    w = pad * 2 + col_w * len(sizes)
    row1 = pad + 26
    row2 = row1 + cell + 56
    h = row2 + cell + 30
    canvas = Image.new("RGBA", (w, h), (11, 11, 12, 255))
    d = ImageDraw.Draw(canvas)

    d.text((pad, pad - 4), "A  frameless  (chosen)", fill=(190, 190, 196, 255))
    d.text((pad, row2 - 26), "B  framed  (reference only: mud at 16-32)", fill=(140, 140, 146, 255))

    for i, s in enumerate(sizes):
        cx = pad + i * col_w + col_w // 2
        for row, framed in ((row1, False), (row2, True)):
            icon = draw(s, framed=framed)
            canvas.alpha_composite(icon, (cx - s // 2, row + (cell - s) // 2))
        d.text((cx - 6, row1 + cell + 6), str(s), fill=(120, 120, 126, 255))

    OUT_PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT_PREVIEW)
    print(f"已生成预览 {OUT_PREVIEW}")


def main() -> None:
    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    # ICO 用同一套几何（无框版）：小尺寸下框线会糊成双色边，所以图标只保留闪电
    base = draw(256, framed=False)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(OUT_ICO, format="ICO", sizes=sizes)
    draw(512, framed=False).save(OUT_PNG, format="PNG")
    draw(1080, framed=False).save(OUT_PNG_MASTER, format="PNG")
    build_preview()
    print(f"已生成 {OUT_ICO} ({OUT_ICO.stat().st_size / 1024:.1f} KB)")
    print(f"已生成 {OUT_PNG} ({OUT_PNG.stat().st_size / 1024:.1f} KB)")
    print(f"已生成 {OUT_PNG_MASTER} ({OUT_PNG_MASTER.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
