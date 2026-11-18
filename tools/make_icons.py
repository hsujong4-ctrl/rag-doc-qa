"""重新產生 PWA 圖示（選用）。
用法：pip install pillow && python tools/make_icons.py
"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "icons")
os.makedirs(OUT, exist_ok=True)
GREEN, WHITE, LIGHT = (5, 150, 105, 255), (255, 255, 255, 255), (167, 243, 208, 255)


def draw_doc(img, scale=1.0):
    """畫一張文件頁面與三行文字，代表文件問答"""
    d = ImageDraw.Draw(img)
    s = img.size[0]
    def p(x, y):
        return (s * (0.5 + (x - 0.5) * scale), s * (0.5 + (y - 0.5) * scale))
    d.rounded_rectangle([*p(0.28, 0.18), *p(0.72, 0.82)], radius=int(s * 0.04 * scale), fill=WHITE)
    for i, w in enumerate((0.30, 0.30, 0.22)):
        y = 0.36 + i * 0.12
        d.rectangle([*p(0.36, y), *p(0.36 + w * 0.9, y + 0.035)], fill=GREEN)
    d.ellipse([*p(0.60, 0.60), *p(0.80, 0.80)], fill=LIGHT)


def rounded_icon(size, path):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=GREEN)
    draw_doc(img, 0.95)
    img.save(path)


def maskable_icon(size, path):
    img = Image.new("RGBA", (size, size), GREEN)
    draw_doc(img, 0.7)
    img.save(path)


def svg_icon(path):
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#059669"/>
  <rect x="144" y="92" width="224" height="300" rx="20" fill="#fff"/>
  <rect x="180" y="150" width="152" height="18" fill="#059669"/>
  <rect x="180" y="200" width="152" height="18" fill="#059669"/>
  <rect x="180" y="250" width="110" height="18" fill="#059669"/>
  <circle cx="340" cy="340" r="50" fill="#a7f3d0"/>
</svg>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)


if __name__ == "__main__":
    rounded_icon(192, os.path.join(OUT, "icon-192.png"))
    rounded_icon(512, os.path.join(OUT, "icon-512.png"))
    rounded_icon(180, os.path.join(OUT, "apple-touch-icon.png"))
    maskable_icon(512, os.path.join(OUT, "icon-maskable-512.png"))
    svg_icon(os.path.join(OUT, "icon.svg"))
    print("圖示已產生於", os.path.abspath(OUT))
