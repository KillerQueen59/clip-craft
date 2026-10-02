"""Render the headline as a single rounded 'yellow card' PNG.

libass can only draw a box that hugs each line separately; to match the
reference — one rounded card behind all lines, with a soft drop shadow and small
corner accents — we render the whole card with Pillow and overlay it as an image.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config

_DEFAULT_FONT = "C:/Windows/Fonts/ariblk.ttf"


def _rgb(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _darken(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, int(c * f)) for c in rgb)  # type: ignore[return-value]


def render_card(text: str, w: int, cfg: Config, out_path: Path) -> Path | None:
    """Render `text` to a transparent PNG card sized to fit within the frame width
    `w`. Returns the path, or None if Pillow is unavailable or text is empty."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except Exception:
        print("[titlecard] Pillow not installed — run `pip install pillow`. "
              "Falling back to the box title.")
        return None

    tcfg = cfg.raw.get("title", {})
    prefix = tcfg.get("prefix", "").strip()
    if prefix:
        text = f"{prefix} {text}".strip()
    if tcfg.get("uppercase", True):
        text = text.upper()

    scale = w / 1080
    size = max(12, int(tcfg.get("font_size", 54) * scale))
    try:
        font = ImageFont.truetype(tcfg.get("font_file") or _DEFAULT_FONT, size)
    except Exception:
        font = ImageFont.load_default()

    box_rgb = _rgb(tcfg.get("box_color", "#FBE38C"))
    text_rgb = _rgb(tcfg.get("text_color", "#2B1E0A"))
    pad = int(tcfg.get("pad", 24) * scale)
    radius = int(tcfg.get("radius", 28) * scale)
    side = float(tcfg.get("side_margin", 0.06))
    max_card_w = int(w * (1 - 2 * side))
    max_text_w = max_card_w - 2 * pad

    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))

    def line_w(s: str) -> int:
        b = probe.textbbox((0, 0), s, font=font)
        return b[2] - b[0]

    words = text.split()
    lines: list[str] = []
    cur = ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if not cur or line_w(trial) <= max_text_w:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)

    line_h = int(size * 1.20)
    text_w = min(max_text_w, max((line_w(l) for l in lines), default=0))
    card_w = min(max_card_w, text_w + 2 * pad)
    card_h = line_h * len(lines) + 2 * pad

    sh = max(0, int(tcfg.get("shadow", 8) * scale))
    sh_op = float(tcfg.get("shadow_opacity", 0.30))
    sh_blur = max(1, int(sh * 1.3)) if sh else 0
    pad_out = sh + sh_blur + int(8 * scale)
    W = card_w + 2 * pad_out
    H = card_h + 2 * pad_out
    x0, y0 = pad_out, pad_out

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if sh and sh_op > 0:
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [x0, y0 + sh, x0 + card_w, y0 + sh + card_h], radius=radius,
            fill=(*_rgb(tcfg.get("shadow_color", "#000000")), int(255 * sh_op)))
        img = Image.alpha_composite(img, shadow.filter(
            ImageFilter.GaussianBlur(sh_blur)))

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], radius=radius,
                           fill=(*box_rgb, 255))

    # Small corner accents (three short dashes), like the reference card.
    if tcfg.get("accents", True):
        acc = _darken(box_rgb, 0.62)
        dw = max(2, int(4 * scale))
        dl = int(26 * scale)
        gap = int(9 * scale)
        ax = x0 + int(pad * 0.7)
        ay = y0 + int(pad * 0.55)
        for i in range(3):
            yy = ay + i * gap
            draw.line([(ax, yy), (ax + dl - i * int(6 * scale), yy)],
                      fill=(*acc, 255), width=dw)
        bx = x0 + card_w - int(pad * 0.7)
        by = y0 + card_h - int(pad * 0.55)
        for i in range(3):
            yy = by - i * gap
            draw.line([(bx - dl + i * int(6 * scale), yy), (bx, yy)],
                      fill=(*acc, 255), width=dw)

    ty = y0 + pad
    for l in lines:
        draw.text((W // 2, ty + line_h // 2), l, font=font,
                  fill=(*text_rgb, 255), anchor="mm")
        ty += line_h

    img.save(out_path)
    return out_path
