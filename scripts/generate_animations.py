#!/usr/bin/env python3
"""Generate the device-safe animations shipped with the repo and the
Home Assistant integration.

Every animation is rendered as RGB PIL frames (96x16), pushed through
`surplife_core.content.fix_gif()` (which guarantees the device-safe
envelope: single global color table, no local tables, >= 100 ms frame
delay), then asserted with `validate_gif()` before it is written. Unsafe
source GIFs (the flattened originals in assets/) are re-fixed through the
same pipeline.

Outputs:
- assets/animations/*.gif                (repo copy)
- custom_components/surplife_matrix/animations/*.gif  (HA integration copy)

Usage:
    python3 scripts/generate_animations.py            # all
    python3 scripts/generate_animations.py --list
    python3 scripts/generate_animations.py --only plasma,fire
"""
from __future__ import annotations

import argparse
import io
import math
import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PIL import Image, ImageFile

# Several "flattened" source GIFs in assets/ are truncated at the wire level
# (incomplete captures); recover what is renderable rather than skip them.
ImageFile.LOAD_TRUNCATED_IMAGES = True

from surplife_core.content import fix_gif, validate_gif

W, H = 96, 16
REPO_ANIM_DIR = os.path.join(os.path.dirname(__file__), "..", "assets",
                             "animations")
INTEGRATION_ANIM_DIR = os.path.join(os.path.dirname(__file__), "..",
                                    "custom_components", "surplife_matrix",
                                    "animations")
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
FRAME_DELAY_MS = 150  # 10 fps, the verified-safe device rate


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    """HSV (all 0..1) to RGB tuple."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


# ── Animation renderers (each returns a list of RGB frames) ──────────

def anim_rainbow_wave() -> list[Image.Image]:
    """Hue-drift rainbow with a soft vertical brightness wave."""
    frames = []
    for f in range(64):
        img = Image.new("RGB", (W, H))
        for x in range(W):
            hue = ((x / W) + (f / 64)) % 1.0
            for y in range(H):
                v = 0.55 + 0.45 * math.sin(
                    2 * math.pi * (y / H * 2 + f / 64 * 2 + x / 24))
                img.putpixel((x, y), _hsv(hue, 1.0, max(0.25, min(1.0, v))))
        frames.append(img)
    return frames


def anim_plasma() -> list[Image.Image]:
    """Additive sine plasma, warm-cool palette cycling."""
    frames = []
    for f in range(48):
        img = Image.new("RGB", (W, H))
        t = f / 48
        for x in range(W):
            for y in range(H):
                v = (math.sin(x * 0.19 + t * 2 * math.pi)
                     + math.sin(y * 0.38 - t * 2 * math.pi)
                     + math.sin((x + y) * 0.14 + t * math.pi)
                     + math.sin(math.hypot(x - 48, y - 8) * 0.23 - t * 3))
                hue = 0.55 + 0.25 * math.sin(t * 2 * math.pi + x / 48)
                col = _hsv(hue, 0.85, 0.5 + 0.5 * (v / 4 + 0.5))
                img.putpixel((x, y), col)
        frames.append(img)
    return frames


def anim_fire() -> list[Image.Image]:
    """Classic bottom-up fire with a flickering flame front."""
    rng = random.Random(42)
    frames = []
    heat = [[0.0] * W for _ in range(H)]
    for _f in range(48):
        # seed the bottom row with random hot cells
        for x in range(W):
            heat[H - 1][x] = rng.random() * 1.4 if rng.random() < 0.6 else 0.0
        # propagate upward with cooling
        new = [[0.0] * W for _ in range(H)]
        for y in range(H):
            for x in range(W):
                below = heat[min(H - 1, y + 1)][x]
                side = heat[y][(x - 1) % W] + heat[y][(x + 1) % W]
                new[y][x] = max(0.0, (below * 0.7 + side * 0.15) - 0.08)
        heat = new
        img = Image.new("RGB", (W, H))
        for y in range(H):
            for x in range(W):
                h = min(1.0, heat[y][x])
                # black -> deep red -> orange -> yellow -> white
                if h <= 0:
                    col = (0, 0, 0)
                else:
                    col = (min(255, int(h * 300)),
                           min(255, int(max(0, h - 0.45) * 340)),
                           min(255, int(max(0, h - 0.8) * 500)))
                img.putpixel((x, y), col)
        frames.append(img)
    return frames


def anim_ocean() -> list[Image.Image]:
    """Layered sine waves in a blue-teal palette."""
    frames = []
    for f in range(48):
        t = f / 48 * 2 * math.pi
        img = Image.new("RGB", (W, H))
        for x in range(W):
            base = (math.sin(x / 15 + t) + math.sin(x / 7 - t * 1.3)) / 2
            for y in range(H):
                wave = (math.sin(x / 12 + t + y / 5) * 0.5
                        + math.sin(x / 5 - t * 0.7 + y / 9) * 0.5)
                depth = (y / H) * 0.5 + base * 0.25
                hue = 0.52 + 0.06 * wave
                img.putpixel((x, y), _hsv(hue % 1.0, 0.9,
                                          max(0.15, 0.45 + 0.5 * depth
                                              + 0.2 * wave)))
        frames.append(img)
    return frames


def anim_starfield() -> list[Image.Image]:
    """Sparse twinkling stars drifting right on black."""
    rng = random.Random(7)
    stars = [[rng.random(), rng.random(), rng.random()]  # x, y, phase
             for _ in range(24)]
    frames = []
    for f in range(64):
        img = Image.new("RGB", (W, H))
        t = f / 64 * 2 * math.pi
        for x, y, phase in stars:
            px = int((x + f / 64) % 1 * W)
            py = int(y * H)
            tw = 0.5 + 0.5 * math.sin(t * 2 + phase * 2 * math.pi)
            col = _hsv(0.58, 0.25, 0.3 + 0.7 * tw)
            img.putpixel((px, py), col)
            # faint neighbor for a star trail
            img.putpixel(((px - 1) % W, py), _hsv(0.58, 0.4, 0.15 * tw))
        frames.append(img)
    return frames


def anim_police_light() -> list[Image.Image]:
    """Alternating red/blue bars sweeping, with a soft glow."""
    frames = []
    for f in range(16):
        phase = (f / 16) * 2  # two swaps per loop
        img = Image.new("RGB", (W, H))
        for x in range(W):
            left = x < W // 2
            blue_side = (f % 4) < 2  # alternate which half is blue
            is_blue = left == blue_side
            # glow pulse toward the swap point
            edge = abs(x - W // 2) / (W // 2)
            v = 1.0 if (x % 2 == f % 2) else 0.35
            v *= (1.1 - edge * 0.3)
            if is_blue:
                img.putpixel((x, 0), (0, 0, int(255 * max(0.3, v))))
            else:
                img.putpixel((x, 0), (min(255, int(255 * max(0.3, v))), 0, 0))
            for y in range(1, H):
                img.putpixel((x, y), img.getpixel((x, 0)))
        frames.append(img)
    return frames


GENERATORS = {
    "rainbow_wave": anim_rainbow_wave,
    "plasma": anim_plasma,
    "fire": anim_fire,
    "ocean": anim_ocean,
    "starfield": anim_starfield,
    "police_light": anim_police_light,
}

# Existing repo GIFs to re-fix into device-safe copies (unsafe originals stay
# in assets/ untouched). police_light_flattened.gif is truncated garbage and
# is NOT re-fixed — the generated police_light replaces it.
EXISTING_TO_FIX = [
    "color_cycle.gif",
    "d20_flames_flattened.gif",
    "filmstrip_flattened.gif",
    "popcorn2_flattened.gif",
    "scroll_text.gif",
    "spaceship_flattened.gif",
    "wizard_fireball_flattened.gif",
]


def _render_and_fix(frames: list[Image.Image]) -> bytes:
    """Render frames -> GIF -> fix_gif -> assert device-safe."""
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True,
                   append_images=frames[1:], duration=FRAME_DELAY_MS, loop=0)
    fixed = fix_gif(buf.getvalue())
    report = validate_gif(fixed)  # strict: raises on any violation
    return fixed, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="List available generators")
    parser.add_argument("--only", help="Comma-separated generator names")
    args = parser.parse_args()

    if args.list:
        for name in GENERATORS:
            print(name)
        return 0

    os.makedirs(REPO_ANIM_DIR, exist_ok=True)
    os.makedirs(INTEGRATION_ANIM_DIR, exist_ok=True)

    selected = (set(args.only.split(","))
                if args.only else set(GENERATORS) | set(EXISTING_TO_FIX))
    failures = 0

    # ── Procedural generators ────────────────────────────────────────
    for name in (n for n in GENERATORS if n in selected):
        print(f"generating {name} ...", end=" ", flush=True)
        try:
            fixed, report = _render_and_fix(GENERATORS[name]())
            for out_dir in (REPO_ANIM_DIR, INTEGRATION_ANIM_DIR):
                with open(os.path.join(out_dir, f"{name}.gif"), "wb") as f:
                    f.write(fixed)
            print(f"OK ({report['frames']} frames, {len(fixed)}B, "
                  f"delay {report['min_frame_delay_cs']}cs)")
        except Exception as err:
            print(f"FAIL: {type(err).__name__}: {err}")
            failures += 1

    # ── Device-safe copies of existing assets ────────────────────────
    for filename in (f for f in EXISTING_TO_FIX if f in selected):
        src = os.path.join(ASSETS_DIR, filename)
        if not os.path.exists(src):
            print(f"skip missing {filename}")
            continue
        print(f"re-fixing {filename} ...", end=" ", flush=True)
        try:
            with open(src, "rb") as f:
                raw = f.read()
            fixed = fix_gif(raw)
            report = validate_gif(fixed)
            out = filename.removesuffix(".gif") + ".gif"
            for out_dir in (REPO_ANIM_DIR, INTEGRATION_ANIM_DIR):
                with open(os.path.join(out_dir, out), "wb") as f:
                    f.write(fixed)
            print(f"OK ({report['frames']} frames, {len(fixed)}B)")
        except (OSError, ValueError, struct.error) as err:
            # e.g. truncated captures PIL cannot fully recover
            print(f"SKIP: {filename} is unusable ({type(err).__name__})")
        except Exception as err:
            print(f"FAIL: {type(err).__name__}: {err}")
            failures += 1

    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print("\nAll animations generated and device-safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
