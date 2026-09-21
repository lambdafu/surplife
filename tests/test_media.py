"""Tests for surplife_core.media: canvas conversion and GIF loading."""
import colorsys
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from PIL import Image

from surplife_core.content import fix_gif, validate_gif
from surplife_core.media import (
    fix_gif_safe,
    frame_to_canvas,
    image_to_canvas,
    load_gif,
    normalize_image,
)
from surplife_core.protocol import FRAMEBUFFER_SIZE


def _unsafe_gif(duration_ms: int = 30, frames: int = 3) -> bytes:
    """A rainbow GIF outside the safe envelope (fast frames, drifting palette)."""
    imgs = []
    for f in range(frames):
        img = Image.new("RGB", (96, 16))
        for x in range(96):
            r, g, b = colorsys.hsv_to_rgb(x / 96 + f / frames, 1.0, 1.0)
            for y in range(16):
                img.putpixel((x, y), (int(r * 255), int(g * 255), int(b * 255)))
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=duration_ms, loop=0)
    return buf.getvalue()


# ── Canvas helpers ───────────────────────────────────────────────────

def test_image_to_canvas_passthrough():
    img = Image.new("RGB", (96, 16), (0, 0, 0))
    fb, cols = image_to_canvas(img)
    assert cols == 96
    assert len(fb) == FRAMEBUFFER_SIZE


def test_frame_to_canvas_tall_input_fits():
    img = Image.new("RGB", (64, 32), (255, 0, 0))
    fb = frame_to_canvas(img)
    assert len(fb) == FRAMEBUFFER_SIZE
    # center red pixels visible after fit+pad
    pos = 48 * 32  # center column
    assert fb[pos:pos + 2] == b"\x01\xff"


def test_frame_to_canvas_narrow_input_padded_black():
    img = Image.new("RGB", (48, 16), (255, 0, 0))
    fb = frame_to_canvas(img)
    assert len(fb) == FRAMEBUFFER_SIZE
    # center 24..71 has red, edges are black
    assert fb[0:2] == b"\x00\x00"
    assert fb[47 * 32:47 * 32 + 2] == b"\x01\xff"
    assert fb[96 * 2:96 * 2 + 2] == b"\x00\x00"


# ── load_gif ─────────────────────────────────────────────────────────

def test_load_gif_safe_passthrough():
    safe = fix_gif(_unsafe_gif())
    out, report = load_gif(safe)
    assert report["ok"]
    assert out == safe


def test_load_gif_auto_fix():
    unsafe = _unsafe_gif()
    assert not validate_gif(unsafe, strict=False)["ok"]
    out, report = load_gif(unsafe)
    assert report["ok"], report["violations"]
    assert out != unsafe


def test_load_gif_no_autofix_raises():
    unsafe = _unsafe_gif()
    with pytest.raises(ValueError):
        load_gif(unsafe, auto_fix=False)


def test_load_gif_rejects_non_gif():
    with pytest.raises(ValueError):
        load_gif(b"NOTGIF", auto_fix=False)


def test_fix_gif_safe_wrapper():
    fixed = fix_gif_safe(_unsafe_gif())
    assert validate_gif(fixed, strict=False)["ok"]


def test_normalize_image():
    img = Image.new("RGB", (96, 16), (0, 0, 0))
    fb, cols = normalize_image(img)
    assert cols == 96
    assert len(fb) == FRAMEBUFFER_SIZE
