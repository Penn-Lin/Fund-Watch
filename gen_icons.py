# -*- coding: utf-8 -*-
"""一次性脚本：生成 PWA 图标 192/512。运行后可删除。"""
import os
from PIL import Image, ImageDraw

def make_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = int(size * 0.06)
    d.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=int(size * 0.18),
        fill=(224, 52, 47, 255),
    )
    # 白色上涨趋势折线
    lw = max(3, size // 28)
    pad = int(size * 0.24)
    w = size - 2 * pad
    pts = [
        (pad, size - pad - int(w * 0.05)),
        (pad + int(w * 0.33), size - pad - int(w * 0.22)),
        (pad + int(w * 0.66), size - pad - int(w * 0.45)),
        (size - pad - int(w * 0.08), pad + int(w * 0.12)),
    ]
    d.line(pts, fill=(255, 255, 255, 255), width=lw, joint='curve')
    # 箭头三角
    ax, ay = pts[-1]
    al = int(size * 0.11)
    d.polygon(
        [(ax, ay - al // 2), (ax + al, ay + al), (ax - int(al * 0.7), ay + al)],
        fill=(255, 255, 255, 255),
    )
    return img

out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'icons')
os.makedirs(out_dir, exist_ok=True)
make_icon(192).save(os.path.join(out_dir, 'icon-192.png'))
make_icon(512).save(os.path.join(out_dir, 'icon-512.png'))
print('icons generated at', out_dir)
