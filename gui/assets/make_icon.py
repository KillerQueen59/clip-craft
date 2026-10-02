"""Generate the app icon (clapperboard) as .ico + .png. Run once:
    py -3 gui/assets/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BG = (15, 18, 22)          # --bg
PANEL = (30, 36, 44)       # --panel-2
ACCENT = (255, 204, 51)    # --accent
WHITE = (230, 234, 239)


def render(size: int) -> Image.Image:
    S = size * 4  # supersample for crisp edges
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded background tile
    pad = int(S * 0.06)
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=int(S * 0.22), fill=BG)

    # clapperboard body
    bx0, by0, bx1, by1 = int(S * 0.20), int(S * 0.42), int(S * 0.80), int(S * 0.74)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(S * 0.03), fill=PANEL)

    # clapper top bar (angled)
    top_h = int(S * 0.12)
    d.polygon(
        [(bx0, by0), (bx1, by0 - int(S * 0.06)),
         (bx1, by0 - int(S * 0.06) + top_h), (bx0, by0 + top_h)],
        fill=ACCENT,
    )
    # diagonal stripes on the clapper bar
    stripes = 5
    span = bx1 - bx0
    for i in range(stripes):
        x = bx0 + int(span * (i + 0.15) / stripes)
        d.line([(x, by0 + top_h), (x + int(S * 0.05), by0 - int(S * 0.04))],
               fill=BG, width=int(S * 0.018))

    # play triangle on the body
    cx, cy = (bx0 + bx1) // 2, (by0 + top_h + by1) // 2 + int(S * 0.01)
    r = int(S * 0.075)
    d.polygon([(cx - r, cy - r), (cx - r, cy + r), (cx + int(r * 1.3), cy)], fill=ACCENT)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [render(s) for s in sizes]
    ico = HERE / "icon.ico"
    imgs[-1].save(ico, sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    render(256).save(HERE / "icon.png")
    print(f"wrote {ico} and icon.png")


if __name__ == "__main__":
    main()
