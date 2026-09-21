"""Text rendering for Surplife displays."""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from .color import image_to_framebuffer
from .protocol import DISPLAY_COLS, DISPLAY_ROWS

_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_DEFAULT_FONT = os.path.join(_FONT_DIR, "spleen-8x16.otf")


def _load_font(size: int = 16) -> ImageFont.FreeTypeFont:
    """Load the display font, falling back to PIL default."""
    if os.path.exists(_DEFAULT_FONT):
        return ImageFont.truetype(_DEFAULT_FONT, size)
    return ImageFont.load_default(size=size)


def render_text(
    text: str,
    fg: tuple[int, int, int] = (255, 255, 255),
    bg: tuple[int, int, int] = (0, 0, 0),
    font_size: int = 16,
) -> tuple[bytes, int]:
    """Render text into a column-major HSV framebuffer.

    The text is rendered at the specified font size onto a canvas that is
    at least 96 pixels wide. The canvas height is always 16 (display height).
    For text wider than 96 pixels, the framebuffer will span multiple frames.

    Args:
        text: Text string to render.
        fg: Foreground RGB color (used as the bitmask color).
        bg: Background RGB color.
        font_size: Font size in pixels.

    Returns:
        (framebuffer_bytes, num_cols) — framebuffer is column-major HSV,
        num_cols x 16 x 2 bytes.
    """
    font = _load_font(font_size)

    # Measure text width
    bbox = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]

    # Canvas: at least one display width
    canvas_w = max(DISPLAY_COLS, text_w + 4)
    canvas = Image.new('RGB', (canvas_w, DISPLAY_ROWS), bg)
    draw = ImageDraw.Draw(canvas)
    y_offset = (DISPLAY_ROWS - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((2, y_offset), text, fill=fg, font=font)

    return image_to_framebuffer(canvas)
