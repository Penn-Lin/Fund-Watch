# -*- coding: utf-8 -*-
"""PWA 图标生成器 —— 从设计稿源图生成 static/icons/ 下的全套图标。

源图：icon-design/source-kline-3x.png（Ardot 画布「方案 D · K 线」3x 导出，1080×1080）

产物：
  icon-192.png / icon-512.png        圆角透明角，manifest purpose=any
  icon-maskable-192/512.png          主体缩到 78% 铺深色底板，purpose=maskable
                                     （78% 保证内容落在 Android 安全圆内不被裁）
  apple-touch-icon.png               180，满幅不透明（iOS 不支持透明，透明会变黑）
  favicon-32.png / favicon.ico       浏览器标签页（含 16/32/48 三档）

用法：python gen_icons.py
改图标：在 Ardot 画布改完 → 重新导出 3x PNG → 覆盖 source-kline-3x.png → 再跑一次
"""
import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "icon-design", "source-kline-3x.png")
OUT = os.path.join(ROOT, "static", "icons")

RADIUS_RATIO = 0.20      # 设计稿圆角 72/360
PLATE = (6, 8, 14, 255)  # maskable 底板：近黑
SS = 4                   # 超采样倍率，保边缘平滑

os.makedirs(OUT, exist_ok=True)
src = Image.open(SRC).convert("RGBA")


def _resize(img, size):
    return img.resize((size, size), Image.LANCZOS)


def rounded(size):
    """圆角 + 透明角：作为 purpose=any 的主图标。"""
    big = _resize(src, size * SS)
    mask = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size * SS - 1, size * SS - 1],
        radius=int(size * SS * RADIUS_RATIO),
        fill=255,
    )
    out = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    out.paste(big, (0, 0), mask)
    return _resize(out, size)


def full_bleed(size):
    """满幅不透明：用「模糊的自身」做底衬补掉透明角。

    坑：不要用"放大自身"做底衬 —— 那会把右上角的红点复制到画布角落，出现重影。
    模糊底衬只影响圆角处极小区域，等于让渐变自然延伸出去。
    """
    art = _resize(src, size)
    bg = Image.new("RGBA", (size, size), PLATE)
    bg.alpha_composite(art.filter(ImageFilter.GaussianBlur(size * 0.07)))
    bg.alpha_composite(art.filter(ImageFilter.GaussianBlur(size * 0.02)))
    bg.alpha_composite(art)
    return bg


def maskable(size):
    """安全区版：内容缩到 78% 居中铺底板，任何 launcher 遮罩都裁不到主体。"""
    plate = Image.new("RGBA", (size, size), PLATE)
    inner = int(size * 0.78)
    plate.alpha_composite(full_bleed(inner), ((size - inner) // 2, (size - inner) // 2))
    return plate


def main():
    for s in (192, 512):
        rounded(s).save(os.path.join(OUT, f"icon-{s}.png"), "PNG", optimize=True)
    for s in (192, 512):
        maskable(s).save(os.path.join(OUT, f"icon-maskable-{s}.png"), "PNG", optimize=True)
    full_bleed(180).save(os.path.join(OUT, "apple-touch-icon.png"), "PNG", optimize=True)
    rounded(32).save(os.path.join(OUT, "favicon-32.png"), "PNG", optimize=True)
    rounded(64).save(
        os.path.join(OUT, "favicon.ico"), format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    print("icons generated at", OUT)


if __name__ == "__main__":
    main()
