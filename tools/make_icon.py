"""Draws assets\\privatepartynet.ico. Run once after a design change: py tools\\make_icon.py

A blue rounded tile with a white monitor and a stand, a party hat on the monitor's top edge, and confetti on the screen.
Pillow only, so it draws without any internet or fonts. Each size is drawn on its own at eight times the size and
shrunk, so the small icons stay crisp.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "privatepartynet.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPER = 8

BLUE = (59, 130, 246, 255)
WHITE = (255, 255, 255, 255)
SCREEN = (30, 64, 175, 255)
PINK = (244, 114, 182, 255)  # the hat
PINK_DEEP = (219, 39, 119, 255)  # the hat's brim
YELLOW = (253, 224, 71, 255)  # the hat's stripes and pom-pom
MINT = (110, 231, 183, 255)


def hat_edge(y, apex_y, base_y, apex_x, half_base):
    """The left and right x of the hat's triangle at height y."""
    spread = half_base * (y - apex_y) / float(base_y - apex_y)
    return apex_x - spread, apex_x + spread


def paint(size):
    """The icon at size x size pixels, drawn at SUPER times that and shrunk."""
    big = size * SUPER
    k = big / 256.0

    def pts(*points):
        return [(x * k, y * k) for x, y in points]

    def box(x1, y1, x2, y2):
        return (x1 * k, y1 * k, x2 * k, y2 * k)

    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # the tile, the monitor, its screen and stand
    draw.rounded_rectangle(box(8, 8, 248, 248), radius=48 * k, fill=BLUE)
    draw.rounded_rectangle(box(44, 62, 212, 182), radius=14 * k, fill=WHITE)
    draw.rounded_rectangle(box(52, 70, 204, 174), radius=6 * k, fill=SCREEN)
    draw.rectangle(box(112, 182, 144, 204), fill=WHITE)
    draw.rounded_rectangle(box(80, 200, 176, 216), radius=8 * k, fill=WHITE)

    # confetti on the screen
    for cx, cy, r, color in ((78, 100, 7, PINK), (108, 146, 6, YELLOW), (136, 104, 6, MINT), (176, 138, 7, PINK),
                             (92, 156, 5, MINT), (186, 96, 5, YELLOW), (150, 154, 5, WHITE)):
        draw.ellipse(box(cx - r, cy - r, cx + r, cy + r), fill=color)

    # the party hat, standing on the monitor's top edge toward the right
    apex_x, apex_y, base_y, half = 158, 14, 66, 36
    draw.polygon(pts((apex_x, apex_y), (apex_x + half, base_y), (apex_x - half, base_y)), fill=PINK)
    for top, bottom in ((30, 38), (48, 56)):  # two yellow stripes across the cone
        left_top, right_top = hat_edge(top, apex_y, base_y, apex_x, half)
        left_bottom, right_bottom = hat_edge(bottom, apex_y, base_y, apex_x, half)
        draw.polygon(pts((left_top, top), (right_top, top), (right_bottom, bottom), (left_bottom, bottom)), fill=YELLOW)
    draw.rounded_rectangle(box(apex_x - half - 4, base_y - 6, apex_x + half + 4, base_y + 4), radius=5 * k, fill=PINK_DEEP)
    draw.ellipse(box(apex_x - 10, apex_y - 10, apex_x + 10, apex_y + 10), fill=YELLOW)  # the pom-pom

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    images = [paint(size) for size in SIZES]
    images[-1].save(OUT, format="ICO", sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
