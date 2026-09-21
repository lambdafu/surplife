"""Tests for surplife_core.fonts: text rendering."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from surplife_core.fonts import render_text
from surplife_core.protocol import DISPLAY_ROWS


def test_render_text_single_frame():
    fb, num_cols = render_text("AB", fg=(255, 0, 0), font_size=16)
    assert num_cols >= 96          # canvas at least one display width
    assert len(fb) == num_cols * DISPLAY_ROWS * 2


def test_render_text_wide_spans_multiple_frames():
    fb, num_cols = render_text("This is a much longer scrolling text",
                               fg=(255, 0, 0))
    assert num_cols > 96
    assert len(fb) == num_cols * DISPLAY_ROWS * 2


def test_render_text_nonzero_pixels():
    # the text leaves non-black pixels somewhere
    fb, _ = render_text("AB", fg=(255, 0, 0))
    assert any(fb[i:i + 2] != b"\x00\x00" for i in range(0, len(fb), 2))
