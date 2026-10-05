"""Draws the program's logo, which is also the tray icon and the .exe icon: the W key lit and the mouse's left button pressed, because that is what
the program shows. Run directly to write assets/icon.ico (the build does this); `python appicon.py --png docs/logo.png 256` writes the logo as a picture."""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

BG, BG_EDGE = (17, 21, 29), (42, 49, 66)
KEY, KEY_HI, KEY_LO = (52, 60, 77), (78, 89, 111), (30, 36, 48)
INK = (226, 232, 242)
FONTS = [r"C:\Windows\Fonts\segoeuib.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]


def _hex(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _font(px):
    for name in FONTS:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return None


def make_icon(size=256, accent="#38bdf8"):
    """The logo as a size x size RGBA picture (drawn large and shrunk, so the edges are smooth at every size)."""
    acc = _hex(accent)
    big = 1024 if size >= 128 else 512
    u = big / 100.0                                          # everything below is drawn in a 100-unit square

    def s(v):
        return v * u

    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    def rrect(box, r, fill=None, outline=None, width=0):
        x0, y0, x1, y1 = box
        ImageDraw.Draw(img).rounded_rectangle([s(x0), s(y0), s(x1), s(y1)], radius=s(r), fill=fill, outline=outline, width=int(s(width)))

    def glow(shape, blur, color):
        g = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        shape(ImageDraw.Draw(g), color)
        img.alpha_composite(g.filter(ImageFilter.GaussianBlur(s(blur))))

    # the tile, with a soft glow of the accent colour in the corner
    rrect((2, 2, 98, 98), 22, fill=BG + (255,), outline=BG_EDGE + (255,), width=1.6)
    corner = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(corner).ellipse([s(55), s(-35), s(135), s(45)], fill=acc + (46,))
    corner = corner.filter(ImageFilter.GaussianBlur(s(9)))
    tile = Image.new("L", (big, big), 0)
    ImageDraw.Draw(tile).rounded_rectangle([s(2), s(2), s(98), s(98)], radius=s(22), fill=255)
    corner.putalpha(ImageChops.multiply(corner.getchannel("A"), tile))
    img.alpha_composite(corner)

    def key(x, y, k, lit=False, letter=None):
        r = k * 0.22
        if lit:
            glow(lambda d, c: d.rounded_rectangle([s(x - 1.5), s(y - 1.5), s(x + k + 1.5), s(y + k + 1.5)], radius=s(r + 1.5), fill=c + (120,)), 2.6, acc)
        rrect((x, y + 1.4, x + k, y + k + 1.4), r, fill=KEY_LO + (255,))                   # the key's underside
        rrect((x, y, x + k, y + k), r, fill=(acc if lit else KEY) + (255,))
        hi = tuple(min(255, c + 70) for c in acc) if lit else KEY_HI
        d = ImageDraw.Draw(img)
        d.arc([s(x), s(y), s(x + 2 * r), s(y + 2 * r)], 180, 270, fill=hi + (255,), width=max(1, int(s(0.9))))
        d.line([s(x + r), s(y + 0.45), s(x + k - r), s(y + 0.45)], fill=hi + (255,), width=max(1, int(s(0.9))))
        font = _font(int(s(k * 0.62))) if letter and size >= 96 else None            # letters only where they can be read
        if font:
            d.text((s(x + k / 2), s(y + k / 2 + 0.6)), letter, font=font, fill=((6, 18, 26) if lit else INK) + (255,), anchor="mm")

    k, gap = 22, 3.4
    x0, row2 = 9.5, 55
    row1 = row2 - k - gap
    key(x0 + k + gap, row1, k, lit=True, letter="W")
    key(x0, row2, k, letter="A")
    key(x0 + k + gap, row2, k, letter="S")
    key(x0 + 2 * (k + gap), row2, k, letter="D")

    # the mouse above the D key, left button pressed
    mx0, my0, mx1, my1 = 71.5, 19.5, 90.5, 52.5
    mid = (mx0 + mx1) / 2
    rrect((mx0, my0 + 1.4, mx1, my1 + 1.4), 9.5, fill=KEY_LO + (255,))
    rrect((mx0, my0, mx1, my1), 9.5, fill=KEY + (255,))
    body = Image.new("L", (big, big), 0)
    ImageDraw.Draw(body).rounded_rectangle([s(mx0), s(my0), s(mx1), s(my1)], radius=s(9.5), fill=255)
    part = Image.new("L", (big, big), 0)
    ImageDraw.Draw(part).rectangle([s(mx0 - 1), s(my0 - 1), s(mid - 0.45), s(my0 + 14)], fill=255)
    img.paste(Image.new("RGBA", (big, big), acc + (255,)), (0, 0), ImageChops.multiply(body, part))
    d = ImageDraw.Draw(img)
    d.line([s(mx0 + 1), s(my0 + 14), s(mx1 - 1), s(my0 + 14)], fill=KEY_LO + (255,), width=max(1, int(s(1.0))))
    d.line([s(mid), s(my0 + 0.8), s(mid), s(my0 + 14)], fill=KEY_LO + (255,), width=max(1, int(s(1.0))))
    rrect((mid - 1.5, my0 + 4.5, mid + 1.5, my0 + 10.5), 1.5, fill=KEY_HI + (255,))

    return img.resize((size, size), Image.LANCZOS) if size != big else img


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--png":
        out = Path(sys.argv[2])
        out.parent.mkdir(parents=True, exist_ok=True)
        make_icon(int(sys.argv[3]) if len(sys.argv) > 3 else 256).save(out)
        print("wrote", out)
    else:
        out = Path(__file__).parent / "assets"
        out.mkdir(exist_ok=True)
        make_icon(256).save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        print("wrote", out / "icon.ico")
