"""HSV color encoding and framebuffer conversion for Surplife displays.

The display uses a 16-bit packed HSV format with 7-bit hue, 4-bit
saturation, and 5-bit value. See PROTOCOL.md for bit layout details.
"""

from __future__ import annotations

import colorsys

from PIL import Image

from .protocol import BYTES_PER_PIXEL, DISPLAY_COLS, DISPLAY_ROWS


def encode_hsv(h: int, s: int = 100, v: int = 100) -> bytes:
    """Encode an HSV color into the 2-byte display format.

    Args:
        h: Hue in degrees (0-360).
        s: Saturation in percent (0-100).
        v: Value/brightness in percent (0-100).

    Returns:
        2 bytes: packed HSV color.
    """
    hue_7 = round(max(0, min(360, h)) / 360 * 127) & 0x7F
    s_4 = round(max(0, min(100, s)) / 100 * 15) & 0xF
    v_5 = round(max(0, min(100, v)) / 100 * 31) & 0x1F
    byte1 = (hue_7 << 1) | ((s_4 >> 3) & 1)
    byte2 = ((s_4 & 0x7) << 5) | v_5
    return bytes([byte1, byte2])


def rgb_to_display(r: int, g: int, b: int) -> bytes:
    """Convert an RGB color to the 2-byte display framebuffer format."""
    h_f, s_f, v_f = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return encode_hsv(h_f * 360, s_f * 100, v_f * 100)


def decode_hsv(b1: int, b2: int) -> tuple[float, float, float]:
    """Decode 2 display bytes to HSV values.

    Returns:
        (hue_degrees, saturation_0to1, value_0to1).
    """
    hue_7 = (b1 >> 1) & 0x7F
    s_4 = ((b1 & 1) << 3) | ((b2 >> 5) & 0x7)
    v_5 = b2 & 0x1F
    return hue_7 / 127.0 * 360.0, s_4 / 15.0, v_5 / 31.0


def image_to_framebuffer(img: Image.Image) -> bytes:
    """Convert a PIL Image to a raw column-major HSV framebuffer.

    The image is resized to fit the display height (16px), keeping aspect
    ratio. If wider than 96 columns, the result is a wide framebuffer
    suitable for multi-frame upload.

    Returns:
        Framebuffer bytes and the number of columns.
    """
    img = img.convert('RGB')
    # Scale to display height, keep aspect ratio
    if img.height != DISPLAY_ROWS:
        scale = DISPLAY_ROWS / img.height
        new_w = max(DISPLAY_COLS, round(img.width * scale))
        img = img.resize((new_w, DISPLAY_ROWS), Image.LANCZOS)

    num_cols = img.width
    fb = bytearray(num_cols * DISPLAY_ROWS * BYTES_PER_PIXEL)

    for col in range(num_cols):
        for row in range(DISPLAY_ROWS):
            r, g, b = img.getpixel((col, row))
            h_f, s_f, v_f = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            hsv = encode_hsv(h_f * 360, s_f * 100, v_f * 100)
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            fb[pos] = hsv[0]
            fb[pos + 1] = hsv[1]

    return bytes(fb), num_cols


def framebuffer_to_image(fb: bytes, scale: int = 1) -> Image.Image:
    """Convert a raw framebuffer to a PIL Image.

    Args:
        fb: Raw framebuffer bytes (column-major, 2 bytes/pixel HSV).
        scale: Integer scale factor for the output image.
    """
    num_cols = len(fb) // (DISPLAY_ROWS * BYTES_PER_PIXEL)
    img = Image.new('RGB', (num_cols * scale, DISPLAY_ROWS * scale))

    for col in range(num_cols):
        for row in range(DISPLAY_ROWS):
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            h, s, v = decode_hsv(fb[pos], fb[pos + 1])
            r, g, b = colorsys.hsv_to_rgb(h / 360.0, s, v)
            rgb = (int(r * 255), int(g * 255), int(b * 255))
            if scale == 1:
                img.putpixel((col, row), rgb)
            else:
                for dy in range(scale):
                    for dx in range(scale):
                        img.putpixel((col * scale + dx, row * scale + dy), rgb)
    return img


def rgb_to_meta_color(r: int, g: int, b: int) -> str:
    """Convert an RGB color to the device's HHSSVV metadata format.

    The device JSON uses "HHSSVV" strings where:
      HH: 0x00-0x96 (0-150 decimal) maps to 0-360 degrees
      SS: 0x00-0x64 (0-100 decimal)
      VV: 0x00-0x64 (0-100 decimal)

    Args:
        r, g, b: RGB values 0-255.

    Returns:
        6-char hex string like "006464" (red, full saturation/value).
    """
    h_f, s_f, v_f = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    hh = round(h_f * 150)
    ss = round(s_f * 100)
    vv = round(v_f * 100)
    return f"{hh:02x}{ss:02x}{vv:02x}"


def parse_rgb_hex(s: str) -> tuple[int, int, int]:
    """Parse a hex RGB string like 'ff0000' or '#ff0000' to (r, g, b).

    Raises ValueError on invalid input.
    """
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"Expected 6-char hex RGB, got '{s}'")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
