#!/usr/bin/env python3
"""
make_icon.py — generate icon.png / icon.ico / icon.icns for COMTRADE Analyzer.
Run once: python3 make_icon.py
Requires: Pillow  (pip install pillow)
"""

import math
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent

# ── Design constants ──────────────────────────────────────────────────────────
_BG_TOP   = (14,  36,  77)    # dark navy (matches report #1A3A6B)
_BG_BOT   = (26,  58, 107)    # slightly lighter navy
_WAVE_CLR = (255, 255, 255, 220)   # white waveform
_FAULT_CLR = (192,  57,  43, 200)  # trip-red accent (#C0392B)
_TEXT_CLR = (255, 255, 255, 255)


def _draw_icon(size: int) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background: vertical gradient ─────────────────────────────────────
    r0, g0, b0 = _BG_TOP
    r1, g1, b1 = _BG_BOT
    for y in range(size):
        t = y / (size - 1)
        r = int(r0 + (r1 - r0) * t)
        g = int(g0 + (g1 - g0) * t)
        b = int(b0 + (b1 - b0) * t)
        draw.line([(0, y), (size - 1, y)], fill=(r, g, b, 255))

    # Rounded corners
    radius = max(4, size // 8)
    mask   = Image.new("L", (size, size), 0)
    md     = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)
    draw = ImageDraw.Draw(img)

    # ── Waveform: pre-fault load + fault current with DC offset ───────────
    n      = max(400, size * 3)
    cx     = size / 2
    cy     = size * 0.44
    amp    = size * 0.18       # pre-fault amplitude
    x0     = size * 0.07
    x1     = size * 0.93
    width  = x1 - x0

    # Fault inception at ~40% across the icon
    t_fault = 0.40
    dc_amp  = amp * 1.8        # DC offset magnitude at fault inception
    tau     = 0.18             # DC decay time constant (in icon x-units)
    i_fault = amp * 2.8        # fault current amplitude

    def wave_y(t):
        phase = t * 2.2 * 2 * math.pi   # ~2.2 cycles across icon
        if t < t_fault:
            # Pre-fault balanced load
            return cy - math.sin(phase) * amp
        else:
            dt = t - t_fault
            # Fault current + decaying DC offset
            dc  = dc_amp * math.exp(-dt / tau)
            return cy - (math.sin(phase) * i_fault + dc)

    pts = [(x0 + width * i / n, wave_y(i / n)) for i in range(n + 1)]

    lw = max(2, size // 44)
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=_WAVE_CLR, width=lw)

    # ── Fault inception marker — thin red vertical tick ───────────────────
    fx     = x0 + width * t_fault
    tick_h = size * 0.52
    tick_y0 = cy - tick_h / 2
    tick_y1 = cy + tick_h / 2
    tick_w = max(1, size // 64)
    draw.line([(fx, tick_y0), (fx, tick_y1)], fill=_FAULT_CLR, width=tick_w)

    # ── "CT" label ────────────────────────────────────────────────────────
    font_size = max(6, size // 4)
    font      = None
    for candidate in [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]:
        if Path(candidate).exists():
            try:
                from PIL import ImageFont as _IFont
                font = _IFont.truetype(candidate, font_size)
                break
            except Exception:
                pass

    text   = "CT"
    bbox   = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx     = (size - tw) / 2 - bbox[0]
    ty     = size * 0.71 - bbox[1]

    shadow = max(1, size // 128)
    draw.text((tx + shadow, ty + shadow), text, font=font, fill=(0, 0, 0, 90))
    draw.text((tx, ty), text, font=font, fill=_TEXT_CLR)

    # ── Red accent bar at bottom ──────────────────────────────────────────
    bar_h = max(2, size // 48)
    bar_y = size - bar_h - max(2, size // 32)
    draw.rectangle([size * 0.1, bar_y, size * 0.9, bar_y + bar_h],
                   fill=(*_FAULT_CLR[:3], 200))

    return img


def make_png(out: Path, size: int = 512):
    img = _draw_icon(size).convert("RGBA")
    img.save(out)
    print(f"  Created {out}")


def make_ico(out: Path):
    sizes  = [16, 24, 32, 48, 64, 128, 256]
    frames = [_draw_icon(s).convert("RGBA") for s in sizes]
    frames[0].save(
        out, format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print(f"  Created {out}")


def make_icns(out: Path):
    iconset = HERE / "icon.iconset"
    iconset.mkdir(exist_ok=True)

    spec = [
        ("icon_16x16.png",       16),
        ("icon_16x16@2x.png",    32),
        ("icon_32x32.png",       32),
        ("icon_32x32@2x.png",    64),
        ("icon_128x128.png",    128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png",    256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png",    512),
        ("icon_512x512@2x.png",1024),
    ]
    for fname, sz in spec:
        img = _draw_icon(sz).convert("RGBA")
        img.save(iconset / fname)

    import subprocess
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
        capture_output=True, text=True,
    )
    shutil.rmtree(iconset)
    if result.returncode == 0:
        print(f"  Created {out}")
    else:
        print(f"  iconutil failed: {result.stderr.strip()}")
        sys.exit(1)


if __name__ == "__main__":
    print("Generating COMTRADE Analyzer icons…")
    make_png(HERE / "icon.png")
    make_ico(HERE / "icon.ico")
    if sys.platform == "darwin":
        make_icns(HERE / "icon.icns")
    print("Done.")
