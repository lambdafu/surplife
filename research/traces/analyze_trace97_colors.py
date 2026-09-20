#!/usr/bin/env python3
"""Decode e1 05 color structure properly."""
import sys
sys.path.insert(0, "/Users/marcus/Desktop/NDS/Projekt/surplife")
from analyze_traces import parse_btsnoop, reassemble, parse_wrapper

TRACE = "/Users/marcus/Desktop/NDS/Projekt/surplife/traces/97-surplife-audio-wave-custom-cycle-options-configure.log"
frames = parse_btsnoop(TRACE)
ops = reassemble(frames)

print("e1 05 COMMAND STRUCTURE ANALYSIS")
print("=" * 80)

for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) >= 2 and cmd[0] == 0xe1 and cmd[1] == 0x05:
        p = cmd[2:]  # payload after e1 05
        print(f"\nFrame {op.frame_nr:5d} | full: {cmd.hex(' ')}")
        print(f"  Offset layout:")
        for i, b in enumerate(p):
            label = ""
            if i == 0: label = "always 0x00"
            elif i == 1: label = "brightness"
            elif i == 2: label = "style_index"
            elif i == 3: label = "sub_style"
            elif i == 4: label = "num_bars_or_colors"
            elif i == 5: label = "speed"
            elif 6 <= i <= 22: label = "padding"
            elif i == 23: label = "color_block_start"
            else:
                # In color block
                block_offset = i - 23
                label = f"color_block[{block_offset}]"
            print(f"    [{i:2d}] 0x{b:02x} ({b:3d})  {label}")

        # Better color block parsing
        # The color block seems to start at offset 23 with 0xa1
        # Then 00 00 00 [N] followed by N+1 colors, each 4 bytes: a1 H S V
        cb = p[23:]
        print(f"  Color block raw: {cb.hex(' ')}")
        if len(cb) >= 5 and cb[0] == 0xa1:
            # Hmm, wait. Let me look at the actual byte pattern:
            # a1 00 00 00 04 a1 00 64 64 a1 12 64 64 a1 1e 64 64 a1 3c 64 64
            # Could be: [a1] [00 00 00] [04] then 4 x [a1 HH SS VV]
            # But 0x04 = 4 colors and there are 4 groups...
            # Actually: a1 00 [00 00] [04] - header with count=4
            # Then: [a1 00 64 64] [a1 12 64 64] [a1 1e 64 64] [a1 3c 64 64]
            # Each color: [a1] [H_byte] [S_byte] [V_byte]

            # Actually re-examine: offset 23 = 0xa1, offset 24 = 0x00, ...
            # offset 27 = count, then 3-byte colors with a1 prefix?
            # No - let's count: a1 00 00 00 04 = 5 bytes header, then:
            # a1 00 64 64 = 4 bytes each
            # So header = [a1 00 00 00 N], then N colors of [a1 H S V]
            header = cb[:5]
            n_colors = header[4]
            print(f"  Color header: {header.hex(' ')} -> {n_colors} colors")
            for c in range(n_colors):
                base = 5 + c * 4
                if base + 4 <= len(cb):
                    marker = cb[base]
                    h = cb[base+1]
                    s = cb[base+2]
                    v = cb[base+3]
                    print(f"  Color {c}: marker=0x{marker:02x} H={h} S={s} V={v}")

            # Check for the last e1 05 which has different padding
            if p[22] != 0:
                print(f"  NOTE: padding byte[22] = 0x{p[22]:02x} (non-zero!)")
                # This one seems to have a different structure at the end
                # Let me try: header byte[22]=0x01, then color block starts differently
                print(f"  Alternative parse with byte22 as flag:")
                alt_cb = p[22:]
                print(f"    Alt block: {alt_cb.hex(' ')}")
