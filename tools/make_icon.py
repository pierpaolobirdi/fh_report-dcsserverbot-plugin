"""Draws the icon of the configuration editor (an F and an H joined by a gear) and writes
tools/fh_report_config.ico with its sizes. Needs Pillow; run it only to change the icon:

    python tools/make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fh_report_config.ico")
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
N = 1024                                   # drawn big, then reduced
BG, BORDER = (18, 22, 29), (48, 58, 73)    # the window's colours (fh_report_config_theme.py)
AMBER, WHITE = (240, 164, 58), (232, 235, 240)


def draw(n: int = N) -> Image.Image:
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * 0.2), fill=BG)
    d.rounded_rectangle((18, 18, n - 19, n - 19), radius=int(n * 0.18), outline=BORDER, width=6)
    t, top, h = 96, 236, 552
    # F
    fx, fw = 168, 216
    d.rounded_rectangle((fx, top, fx + t, top + h), radius=22, fill=AMBER)
    d.rounded_rectangle((fx, top, fx + fw, top + t), radius=22, fill=AMBER)
    d.rounded_rectangle((fx, top + h * 0.44, fx + fw * 0.78, top + h * 0.44 + t), radius=22, fill=AMBER)
    # H: two stems, and a gear as its crossbar
    hx, hw = 440, 416
    d.rounded_rectangle((hx, top, hx + t, top + h), radius=22, fill=AMBER)
    d.rounded_rectangle((hx + hw - t, top, hx + hw, top + h), radius=22, fill=AMBER)
    cx, cy = hx + hw / 2, top + h / 2
    radius = (hw - 2 * t) / 2 + 34
    inner, teeth = radius - 52, 10
    pitch = 2 * math.pi / teeth
    points = []
    for i in range(teeth):
        for da, rad in ((-0.30 * pitch, inner), (-0.17 * pitch, radius), (0.17 * pitch, radius), (0.30 * pitch, inner)):
            a = i * pitch + da
            points.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(points, fill=WHITE)
    d.ellipse((cx - inner * 0.52, cy - inner * 0.52, cx + inner * 0.52, cy + inner * 0.52), fill=BG)
    d.ellipse((cx - inner * 0.24, cy - inner * 0.24, cx + inner * 0.24, cy + inner * 0.24), fill=AMBER)
    return im


def main() -> None:
    draw().resize((256, 256), Image.LANCZOS).save(OUT, format="ICO", sizes=SIZES)
    print(f"{OUT}: {os.path.getsize(OUT)} bytes")


if __name__ == "__main__":
    main()
