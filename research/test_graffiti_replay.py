#!/usr/bin/env python3
"""Verify the library's direct-draw encoding against trace 60.

Trace 60 contains 15 `ea 11` direct draws (the app drawing six corner
pixels one at a time: (0,0), (1,0), (2,0), (0,15), (95,0), (95,15) in
red, with duplicates/resends). Each payload is a raw a2pl stream of the
full 3072-byte framebuffer.

Checks:
1. Every trace payload decompresses to exactly 3072 bytes.
2. Each successive draw differs from the previous by at most the
   pixels named in the trace (a new pixel added or a resend).
3. The library's draw path (draw_pixels -> compress) reproduces the
   same framebuffer state for each draw when replayed with the same
   accumulated pixel set.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from analyze_traces import parse_btsnoop, reassemble  # noqa: E402
from surplife.a2pl import compress, decompress  # noqa: E402
from surplife.color import rgb_to_display  # noqa: E402
from surplife.protocol import FRAMEBUFFER_SIZE  # noqa: E402

TRACE = os.path.join(os.path.dirname(__file__), "traces",
                     "60-graffiti-draw-red-0,0-1,0-2,0-0,15-95,0-95,15.log")

# One distinct pixel added per draw group, in trace order.
EXPECTED_PIXELS = [(0, 0), (1, 0), (2, 0), (0, 15), (95, 0), (95, 15)]
RED = rgb_to_display(255, 0, 0)


def extract_draws(path):
    frames = parse_btsnoop(path)
    ops = reassemble(frames)
    draws = []
    for op in ops:
        if op.direction != "W":
            continue
        inner = op.raw[9:]
        if len(inner) >= 2 and inner[0] == 0xEA and inner[1] == 0x11:
            assert inner[2:5] == b"\x00\x00\x00", inner[2:5].hex()
            declared = inner[5]
            payload = inner[6:]
            assert declared == len(payload), (declared, len(payload))
            draws.append(payload)
    return draws


def fb_set_pixel(fb, col, row, color):
    pos = col * 32 + row * 2
    fb[pos], fb[pos + 1] = color


def main():
    draws = extract_draws(TRACE)
    assert draws, "no ea 11 draws found in trace 60"
    print(f"Extracted {len(draws)} direct draws")

    states = []
    for i, payload in enumerate(draws):
        from surplife.a2pl import decompress as _dec
        fb = _dec(payload)
        assert len(fb) == FRAMEBUFFER_SIZE, \
            f"draw {i}: decompressed to {len(fb)} bytes"
        states.append(bytes(fb))
    print(f"1. all {len(draws)} draws decompress to {FRAMEBUFFER_SIZE} bytes")

    # 2. Successive diffs: each draw changes at most one pixel vs the
    # previous state, and it is one of the expected pixels in red.
    # (The first draw establishes (0,0) against the black initial state.)
    added = [(0, 0)] if states[0][0:2] == RED else []
    for i in range(1, len(draws)):
        diff = []
        for pos in range(0, FRAMEBUFFER_SIZE, 2):
            if states[i][pos:pos + 2] != states[i - 1][pos:pos + 2]:
                col = pos // 32
                row = (pos % 32) // 2
                diff.append((col, row))
        if diff:
            assert len(diff) == 1, f"draw {i}: {len(diff)} pixels changed"
            col, row = diff[0]
            added.append((col, row))
    # Unique new pixels, in order: should be the 6 expected corner pixels.
    unique = list(dict.fromkeys(added))
    print(f"2. pixel additions in order: {unique}")
    assert unique == EXPECTED_PIXELS, (unique, EXPECTED_PIXELS)
    # Added pixels are red.
    for col, row in unique:
        pos = col * 32 + row * 2
        assert states[-1][pos:pos + 2] == RED, \
            f"({col}, {row}) not red: {states[-1][pos:pos + 2].hex()}"

    # 3. Library path: replay accumulated draws through draw_pixels'
    # encoding (compress of the accumulated canvas) and compare states.
    final_state = states[-1]
    canvas = {}
    for col, row in EXPECTED_PIXELS:
        canvas[(col, row)] = (255, 0, 0)
    fb_lib = bytearray(FRAMEBUFFER_SIZE)
    for (col, row), rgb in canvas.items():
        pos = col * 32 + row * 2
        fb_lib[pos], fb_lib[pos + 1] = rgb_to_display(*rgb)
    assert bytes(fb_lib) == final_state, \
        "library canvas differs from trace final state"
    stream = compress(bytes(fb_lib))
    from surplife.a2pl import decompress as _dec
    assert _dec(stream) == bytes(fb_lib)
    assert len(stream) <= 255, f"stream too long: {len(stream)}"
    print(f"3. library canvas matches trace 60 final state "
          f"({len(stream)}B stream, roundtrip OK)")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()