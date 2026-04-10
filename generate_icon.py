"""Generate YTYoink icon files with a neon/techy aesthetic.

Produces:
  ytyoink.ico   – multi-size Windows icon (9 sizes)
  logo_48.png   – 48 px header logo (glow style)
  logo_32.png   – 32 px header logo (glow style)
  logo_24.png   – 24 px header logo (glow style)

ICO sizes: 16, 20, 24, 32, 40, 48, 64, 128, 256 (all 32-bit RGBA)

Strategy for crisp taskbar icons:
  ≤24px: BOLD filled arrow, NO music note, NO glow, 2x supersample max
  32-40px: Full arrow + music note, NO glow, 2x supersample
  ≥48px: Full design with neon glow effect, 4x supersample
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter


# ── Palette ──────────────────────────────────────────────────────────
BG = (24, 24, 37)              # #181825
NEON = (137, 180, 250)         # #89b4fa
NEON_BRIGHT = (180, 208, 251)  # #b4d0fb


def _round_rect(draw, xy, radius, fill=None, outline=None, width=1):
    try:
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)
    except AttributeError:
        x0, y0, x1, y1 = xy
        d = radius * 2
        draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
        draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
        draw.pieslice([x0, y0, x0 + d, y0 + d], 180, 270, fill=fill)
        draw.pieslice([x1 - d, y0, x1, y0 + d], 270, 360, fill=fill)
        draw.pieslice([x0, y1 - d, x0 + d, y1], 90, 180, fill=fill)
        draw.pieslice([x1 - d, y1 - d, x1, y1], 0, 90, fill=fill)


# ─────────────────────────────────────────────────────────────────────
# TINY icons (16-24px): bold filled arrow only, maximum simplicity
# ─────────────────────────────────────────────────────────────────────

def _make_icon_tiny(size: int) -> Image.Image:
    """16-24px: bold filled down-arrow, no music note, no glow."""
    S = size * 2  # only 2x supersample to keep edges crisp
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    m = max(1, S // 12)
    r = max(2, S // 6)

    # Background
    _round_rect(draw, (m, m, S - m, S - m), r, fill=BG)
    # Border
    _round_rect(draw, (m, m, S - m, S - m), r, outline=NEON, width=max(2, S // 16))

    cx, cy = S // 2, S // 2

    # Bold vertical stem (wide)
    sw = max(3, S // 6)
    st = cy - int(S * 0.26)
    sb = cy - int(S * 0.02)
    draw.rectangle((cx - sw // 2, st, cx + sw // 2, sb), fill=NEON_BRIGHT)

    # Bold filled triangle arrowhead (solid, not a thin chevron)
    aw = int(S * 0.52)
    at = cy - int(S * 0.04)
    tip = cy + int(S * 0.22)
    draw.polygon([
        (cx - aw // 2, at),
        (cx + aw // 2, at),
        (cx, tip),
    ], fill=NEON_BRIGHT)

    # Bold tray line
    ty = tip + max(2, S // 10)
    tw = int(S * 0.54)
    th = max(2, S // 10)
    draw.rectangle((cx - tw // 2, ty, cx + tw // 2, ty + th), fill=NEON_BRIGHT)

    return img.resize((size, size), Image.LANCZOS)


# ─────────────────────────────────────────────────────────────────────
# MEDIUM icons (32-40px): full shapes, no glow
# ─────────────────────────────────────────────────────────────────────

def _make_icon_medium(size: int) -> Image.Image:
    """32-40px: arrow + music note, crisp solid shapes, no glow."""
    S = size * 3  # 3x supersample
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    m = int(S * 0.06)
    r = int(S * 0.16)

    # Background + border
    _round_rect(draw, (m, m, S - m, S - m), r, fill=BG)
    _round_rect(draw, (m, m, S - m, S - m), r, outline=NEON, width=max(2, S // 30))

    cx, cy = S // 2, S // 2

    # Stem
    sw = max(3, int(S * 0.09))
    st = cy - int(S * 0.27)
    sb = cy - int(S * 0.04)
    draw.rectangle((cx - sw // 2, st, cx + sw // 2, sb), fill=NEON_BRIGHT)

    # Filled triangle arrowhead
    aw = int(S * 0.48)
    at = cy - int(S * 0.06)
    tip = cy + int(S * 0.18)
    draw.polygon([
        (cx - aw // 2, at),
        (cx + aw // 2, at),
        (cx, tip),
    ], fill=NEON_BRIGHT)

    # Tray
    ty = tip + int(S * 0.07)
    tw = int(S * 0.52)
    th = max(2, int(S * 0.05))
    draw.rectangle((cx - tw // 2, ty, cx + tw // 2, ty + th), fill=NEON_BRIGHT)

    # Music note (simplified but present)
    ncx = cx + int(S * 0.24)
    ncy = cy - int(S * 0.20)
    nr = max(2, int(S * 0.05))
    nsh = int(S * 0.14)
    nsw = max(2, int(S * 0.03))

    # Note head
    draw.ellipse((ncx - nr, ncy - nr, ncx + nr, ncy + nr), fill=NEON_BRIGHT)
    # Note stem
    draw.rectangle((ncx + nr - nsw, ncy - nsh, ncx + nr, ncy), fill=NEON_BRIGHT)
    # Note flag
    fl = int(S * 0.06)
    draw.polygon([
        (ncx + nr, ncy - nsh),
        (ncx + nr + fl, ncy - nsh + int(fl * 0.8)),
        (ncx + nr, ncy - nsh + fl),
    ], fill=NEON_BRIGHT)

    return img.resize((size, size), Image.LANCZOS)


# ─────────────────────────────────────────────────────────────────────
# LARGE icons (≥48px): full design with neon glow
# ─────────────────────────────────────────────────────────────────────

def _draw_full_shapes(draw, S, color):
    """Draw arrow + note + tray for the large glow version."""
    cx, cy = S // 2, S // 2

    # Stem
    sw = int(S * 0.08)
    st = cy - int(S * 0.28)
    sb = cy - int(S * 0.06)
    draw.rectangle((cx - sw // 2, st, cx + sw // 2, sb), fill=color)

    # Chevron arrowhead
    aw = int(S * 0.44)
    at = cy - int(S * 0.12)
    tip = at + int(S * 0.26)
    bh = int(S * 0.06)
    draw.polygon([
        (cx - aw // 2, at),
        (cx - aw // 2 + bh, at),
        (cx, tip - bh),
        (cx + aw // 2 - bh, at),
        (cx + aw // 2, at),
        (cx, tip),
    ], fill=color)

    # Tray
    ty = tip + int(S * 0.08)
    tw = int(S * 0.50)
    th = int(S * 0.05)
    draw.rectangle((cx - tw // 2, ty, cx + tw // 2, ty + th), fill=color)

    # Music note
    ncx = cx + int(S * 0.26)
    ncy = cy - int(S * 0.22)
    nr = int(S * 0.055)
    nsh = int(S * 0.16)
    nsw = max(2, int(S * 0.025))
    fl = int(S * 0.07)

    draw.ellipse(
        (ncx - nr, ncy - int(nr * 0.7), ncx + nr, ncy + int(nr * 0.7)),
        fill=color,
    )
    draw.rectangle(
        (ncx + nr - nsw, ncy - nsh, ncx + nr, ncy),
        fill=color,
    )
    draw.polygon([
        (ncx + nr, ncy - nsh),
        (ncx + nr + fl, ncy - nsh + int(fl * 0.7)),
        (ncx + nr, ncy - nsh + fl),
    ], fill=color)


def _make_icon_large(size: int) -> Image.Image:
    """≥48px: full glow effect."""
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    m = int(S * 0.06)
    r = int(S * 0.18)

    # Background
    _round_rect(draw, (m, m, S - m, S - m), r, fill=BG)

    # Border glow
    bl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bl)
    _round_rect(bd, (m, m, S - m, S - m), r, outline=(*NEON, 100), width=max(2, S // 80))
    bg = bl.filter(ImageFilter.GaussianBlur(radius=S // 40))
    img = Image.alpha_composite(img, bg)
    img = Image.alpha_composite(img, bl)

    # Glow layer
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    _draw_full_shapes(gd, S, (*NEON, 100))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=S // 16))
    img = Image.alpha_composite(img, glow)

    # Crisp layer
    draw = ImageDraw.Draw(img)
    _draw_full_shapes(draw, S, NEON_BRIGHT)

    return img.resize((size, size), Image.LANCZOS)


# ─────────────────────────────────────────────────────────────────────

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    ico_sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = {}
    for s in ico_sizes:
        if s <= 24:
            frames[s] = _make_icon_tiny(s)
        elif s <= 40:
            frames[s] = _make_icon_medium(s)
        else:
            frames[s] = _make_icon_large(s)

    for s, img in frames.items():
        assert img.mode == "RGBA", f"{s}px mode={img.mode}"

    # Save ICO
    ico_path = os.path.join(script_dir, "ytyoink.ico")
    frames[256].save(
        ico_path, format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=[frames[s] for s in ico_sizes if s != 256],
    )
    print(f"  Created {ico_path}  ({len(ico_sizes)} sizes)")

    # Save debug PNGs for each ICO frame
    for s in ico_sizes:
        p = os.path.join(script_dir, f"_debug_ico_{s}px.png")
        frames[s].save(p, format="PNG")

    # Header logo PNGs (glow style for in-app display at 38px)
    for px in (24, 32, 48):
        p = os.path.join(script_dir, f"logo_{px}.png")
        _make_icon_large(px).save(p, format="PNG")
        print(f"  Created {p}")

    print("Done! Check _debug_ico_*px.png files to inspect each size.")


if __name__ == "__main__":
    main()
