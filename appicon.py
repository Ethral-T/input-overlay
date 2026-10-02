"""Draws the app/tray icon (a keycap with an accent highlight). Run directly to write assets/icon.ico."""
from pathlib import Path

from PIL import Image, ImageDraw


def make_icon(size=256, accent="#38bdf8"):
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3 * s, 3 * s, 61 * s, 61 * s], radius=14 * s, fill="#171b24", outline="#2a3142", width=max(1, int(2 * s)))
    # 2x2 grid of keys, one highlighted
    for i, (x, y) in enumerate([(11, 11), (35, 11), (11, 35), (35, 35)]):
        d.rounded_rectangle([x * s, y * s, (x + 18) * s, (y + 18) * s], radius=5 * s, fill=accent if i == 1 else "#3a4252")
    return img


if __name__ == "__main__":
    out = Path(__file__).parent / "assets"
    out.mkdir(exist_ok=True)
    make_icon().save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", out / "icon.ico")
