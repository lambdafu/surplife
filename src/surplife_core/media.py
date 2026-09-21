"""Media resolution: paths, URLs, camera frames → image/GIF bytes.

Pure helpers used by both the CLI and the HA integration. Camera snapshots
and HA-specific input handling live in the integration; this module covers
the byte-level conversions (PIL-based, no BLE).
"""

from __future__ import annotations

from .color import image_to_framebuffer
from .content import validate_gif
from .protocol import DISPLAY_COLS, DISPLAY_ROWS, FRAMEBUFFER_SIZE


def image_to_canvas(img) -> tuple[bytes, int]:
    """PIL Image → column-major HSV framebuffer + column count.

    Thin re-export of image_to_framebuffer kept here so callers only need
    one import surface.
    """
    return image_to_framebuffer(img)


def frame_to_canvas(frame) -> bytes:
    """Convert a PIL image to exactly one full-size display framebuffer.

    Scales to 96x16 (fit, black padding) — used for camera snapshots and
    direct draw.
    """
    from PIL import Image

    frame = frame.convert('RGB')
    # Fit into 96x16, preserving aspect: scale by height, crop/center columns
    if frame.size[1] != DISPLAY_ROWS:
        scale = DISPLAY_ROWS / frame.size[1]
        new_w = max(1, round(frame.size[0] * scale))
        frame = frame.resize((min(new_w, DISPLAY_COLS), DISPLAY_ROWS),
                             Image.LANCZOS)
    if frame.size[0] < DISPLAY_COLS:
        pad = Image.new('RGB', (DISPLAY_COLS, DISPLAY_ROWS), (0, 0, 0))
        pad.paste(frame, ((DISPLAY_COLS - frame.size[0]) // 2, 0))
        frame = pad
    elif frame.size[0] > DISPLAY_COLS:
        frame = frame.crop((0, 0, DISPLAY_COLS, DISPLAY_ROWS))
    fb, _ = image_to_framebuffer(frame)
    return fb


def load_gif(source_bytes: bytes, validate: bool = True,
             auto_fix: bool = True) -> tuple[bytes, dict]:
    """Load, validate and optionally fix a GIF for the device.

    Args:
        source_bytes: Raw GIF bytes.
        validate: Run the envelope check.
        auto_fix: On violation, re-quantize into the safe envelope
            (single global palette, 150ms frame delay).

    Returns:
        (gif_bytes, report) — report from the *final* validation.

    Raises:
        ValueError: Not a GIF, or unsafe and auto_fix disabled.
    """
    report = validate_gif(source_bytes, strict=False)
    if report["ok"]:
        return source_bytes, report
    if not auto_fix:
        raise ValueError(
            "GIF outside the device-safe envelope:\n  "
            + "\n  ".join(report["violations"]))
    fixed = fix_gif_safe(source_bytes)
    report = validate_gif(fixed, strict=False)
    return fixed, report


def fix_gif_safe(gif_data: bytes) -> bytes:
    """Re-quantize a GIF into the device-safe envelope (Pillow required)."""
    from .content import fix_gif
    return fix_gif(gif_data)


def normalize_image(source) -> tuple[bytes, int]:
    """Normalize any PIL-openable image into (wide_framebuffer, num_cols)."""
    fb, num_cols = image_to_framebuffer(source)
    return fb, num_cols


def ensure_canvas_bytes(fb: bytes) -> bytes:
    """Validate a direct-draw framebuffer size."""
    if len(fb) != FRAMEBUFFER_SIZE:
        raise ValueError(
            f"Framebuffer must be {FRAMEBUFFER_SIZE} bytes, got {len(fb)}")
    return fb
