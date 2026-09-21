"""Tests for surplife_core.color: HSV encoding and framebuffer conversion.

Encodings verified against real device traces (red = 01 ff).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from surplife_core.color import (
    decode_hsv,
    encode_hsv,
    framebuffer_to_image,
    image_to_framebuffer,
    parse_rgb_hex,
    rgb_to_display,
    rgb_to_meta_color,
)
from surplife_core.protocol import FRAMEBUFFER_SIZE

# ── 16-bit HSV encoding (bit layout from PROTOCOL.md) ────────────────

def test_encode_hsv_red_matches_traces():
    # trace-confirmed: full red = 01 ff
    assert encode_hsv(0, 100, 100) == b"\x01\xff"
    assert rgb_to_display(255, 0, 0) == b"\x01\xff"


def test_encode_hsv_black():
    assert encode_hsv(0, 0, 0) == b"\x00\x00"
    assert rgb_to_display(0, 0, 0) == b"\x00\x00"


def test_encode_hsv_saturation_carries_into_byte1_lsb():
    # S=100% -> s_4=15 (0b1111); bit3 of s_4 goes to byte1 LSB
    red = encode_hsv(0, 100, 100)
    assert red[0] & 1 == 1           # (15 >> 3) & 1 == 1
    assert (red[1] >> 5) & 0x07 == 7  # remaining 3 bits of s_4


def test_encode_hsv_decode_roundtrip_within_quantization():
    for hue, sat, val in [(0, 100, 100), (90, 100, 50), (180, 50, 100),
                          (270, 25, 25), (360, 100, 100)]:
        b1, b2 = encode_hsv(hue, sat, val)
        h, s, v = decode_hsv(b1, b2)
        # hue resolution ~2.8 degrees, sat ~6.7%, val ~3.2%
        assert abs(h - hue) <= 3.0, (hue, h)
        assert abs(s * 100 - sat) <= 7.0, (sat, s)
        assert abs(v * 100 - val) <= 3.5, (val, v)


def test_encode_hsv_clamps():
    b1, b2 = encode_hsv(400, 150, 150)   # out of range values clamp
    h, s, v = decode_hsv(b1, b2)
    assert h <= 360.1
    assert s <= 1.0
    assert v <= 1.0


# ── Framebuffer conversion ───────────────────────────────────────────

def test_image_to_framebuffer_solid_color():
    from PIL import Image
    img = Image.new("RGB", (96, 16), (255, 0, 0))
    fb, num_cols = image_to_framebuffer(img)
    assert num_cols == 96
    assert len(fb) == FRAMEBUFFER_SIZE
    # every pixel is red (01 ff), column-major layout: pixel (col,row) at col*32+row*2
    for col in (0, 47, 95):
        for row in (0, 8, 15):
            pos = col * 32 + row * 2
            assert fb[pos:pos + 2] == b"\x01\xff"


def test_image_to_framebuffer_wide():
    from PIL import Image
    img = Image.new("RGB", (192, 16), (0, 0, 0))
    fb, num_cols = image_to_framebuffer(img)
    assert num_cols == 192
    assert len(fb) == 192 * 32


def test_image_to_framebuffer_resizes_height():
    from PIL import Image
    img = Image.new("RGB", (96, 32), (0, 0, 0))
    fb, num_cols = image_to_framebuffer(img)
    assert num_cols == 96
    assert len(fb) == FRAMEBUFFER_SIZE


def test_framebuffer_to_image_roundtrip():
    from PIL import Image
    img = Image.new("RGB", (96, 16), (0, 255, 0))
    fb, _ = image_to_framebuffer(img)
    back = framebuffer_to_image(fb)
    # green should survive HSV quantization
    r, g, b = back.getpixel((0, 0))
    assert g > 200
    assert r < 80
    assert b < 80


# ── Metadata colors ──────────────────────────────────────────────────

def test_rgb_to_meta_color_red():
    # trace-confirmed: full red = "006464" (H=0, S=100=0x64, V=100=0x64)
    assert rgb_to_meta_color(255, 0, 0) == "006464"


def test_rgb_to_meta_color_green():
    assert rgb_to_meta_color(0, 255, 0) == "326464"


def test_rgb_to_meta_color_white_and_black():
    assert rgb_to_meta_color(255, 255, 255) == "000064"   # S=0, V=100
    assert rgb_to_meta_color(0, 0, 0) == "000000"


# ── Hex parsing ──────────────────────────────────────────────────────

def test_parse_rgb_hex_plain():
    assert parse_rgb_hex("ff0000") == (255, 0, 0)
    assert parse_rgb_hex("00ff00") == (0, 255, 0)


def test_parse_rgb_hex_hash_prefix():
    assert parse_rgb_hex("#00ff00") == (0, 255, 0)
    assert parse_rgb_hex("#FFFFFF") == (255, 255, 255)


def test_parse_rgb_hex_invalid():
    with pytest.raises(ValueError):
        parse_rgb_hex("ff00")
    with pytest.raises(ValueError):
        parse_rgb_hex("zz0000")
