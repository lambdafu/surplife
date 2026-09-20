#!/usr/bin/env python3
"""Deep analysis of trace 97 - decode e1 05 commands, upload JSON, ea 0f style transitions."""
import sys
import os
import json

sys.path.insert(0, "/Users/marcus/Desktop/NDS/Projekt/surplife")
from analyze_traces import parse_btsnoop, reassemble, parse_wrapper

TRACE = "/Users/marcus/Desktop/NDS/Projekt/surplife/traces/97-surplife-audio-wave-custom-cycle-options-configure.log"

frames = parse_btsnoop(TRACE)
ops = reassemble(frames)

# ─── 1. Extract and decode upload JSON ───
print("=" * 80)
print("UPLOAD CONTENT (e0 30/32/33 sequence)")
print("=" * 80)

segments = []
for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) < 2:
        continue
    c0, c1 = cmd[0], cmd[1]
    if c0 == 0xe0 and c1 == 0x32:
        segments.append(cmd[2:])

if segments:
    # Reassemble segment content (skip 7-byte header per segment)
    all_content = b""
    for seg in segments:
        if len(seg) >= 7:
            all_content += seg[7:]

    print(f"  Total reassembled content: {len(all_content)} bytes")

    # Parse ea23 header
    if len(all_content) >= 22 and all_content[:2] == b"\xea\x23":
        print(f"  Content marker: ea 23")
        print(f"  Bytes 2-3: {all_content[2:4].hex()}")
        content_hash = all_content[4:20].hex()
        print(f"  Hash: {content_hash}")
        json_len = (all_content[20] << 8) | all_content[21]
        print(f"  JSON length: {json_len}")

        json_bytes = all_content[22:22+json_len]
        try:
            json_str = json_bytes.decode("ascii")
            meta = json.loads(json_str)
            print(f"\n  JSON metadata:")
            print(json.dumps(meta, indent=4))
        except Exception as e:
            print(f"  JSON decode error: {e}")
            print(f"  Raw: {json_bytes.hex()}")

        payload = all_content[22+json_len:]
        print(f"\n  Payload: {len(payload)} bytes")
        if len(payload) >= 6:
            print(f"  Payload header: {payload[:6].hex(' ')}")
            print(f"  Pixel data: {len(payload)-6} bytes")
            print(f"  First 64 bytes: {payload[6:70].hex(' ')}")

# ─── 2. Decode ALL e1 05 commands ───
print()
print("=" * 80)
print("ALL e1 05 COMMANDS (waveform style configuration)")
print("=" * 80)

e1_05_cmds = []
for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) >= 2 and cmd[0] == 0xe1 and cmd[1] == 0x05:
        e1_05_cmds.append((op.frame_nr, seq, cmd))

for frame, seq, cmd in e1_05_cmds:
    payload = cmd[2:]
    print(f"\n  Frame {frame:5d} seq=0x{seq:04x}")
    print(f"    Full hex: {cmd.hex(' ')}")
    if len(payload) >= 23:
        print(f"    byte2:     0x{payload[0]:02x} ({payload[0]})")     # 00
        print(f"    byte3:     0x{payload[1]:02x} ({payload[1]})")     # brightness?
        print(f"    byte4:     0x{payload[2]:02x} ({payload[2]})")     # style index?
        print(f"    byte5:     0x{payload[3]:02x} ({payload[3]})")     # sub-style?
        print(f"    byte6:     0x{payload[4]:02x} ({payload[4]})")     # num colors?
        print(f"    byte7:     0x{payload[5]:02x} ({payload[5]})")     # speed?
        # bytes 6-22 seem to be zeros/padding
        print(f"    padding:   {payload[6:23].hex(' ')}")
        # Then color data
        remaining = payload[23:]
        print(f"    marker:    0x{remaining[0]:02x}" if remaining else "")
        if len(remaining) > 1:
            print(f"    zeros:     {remaining[1:4].hex(' ')}" if len(remaining) >= 4 else "")
        if len(remaining) >= 4:
            num_colors_field = remaining[3]
            print(f"    num_colors: {num_colors_field}")
            colors = remaining[4:]
            i = 0
            while i + 3 <= len(colors):
                marker = colors[i]
                h = colors[i+1]
                s = colors[i+2]
                v = colors[i+3] if i+3 < len(colors) else "?"
                print(f"    color: marker=0x{marker:02x} H={h} S={s}" + (f" V={v}" if isinstance(v, int) else ""))
                i += 4
                if i > 28:
                    break

# ─── 3. Track ea 0f style transitions ───
print()
print("=" * 80)
print("ea 0f STYLE TRANSITIONS (byte[2] = style ID)")
print("=" * 80)

prev_style = None
style_runs = []
ea0f_count = 0

for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) >= 4 and cmd[0] == 0xea and cmd[1] == 0x0f:
        style = cmd[2]
        ea0f_count += 1
        if style != prev_style:
            style_runs.append({"style": style, "start_frame": op.frame_nr, "count": 1})
            prev_style = style
        else:
            style_runs[-1]["count"] += 1
            style_runs[-1]["end_frame"] = op.frame_nr

print(f"  Total ea 0f commands: {ea0f_count}")
print(f"  Style transitions:")
for run in style_runs:
    style = run["style"]
    end = run.get("end_frame", run["start_frame"])
    print(f"    Style 0x{style:02x} ({style:3d}): frames {run['start_frame']:5d}-{end:5d}, {run['count']:3d} waveform frames")

# ─── 4. Interleaving: show e1 05 commands relative to ea 0f style changes ───
print()
print("=" * 80)
print("CHRONOLOGICAL: e1 05 config vs ea 0f style changes")
print("=" * 80)

events = []
prev_style = None
for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) < 2:
        continue

    if cmd[0] == 0xe1 and cmd[1] == 0x05:
        payload = cmd[2:]
        events.append(("e1_05", op.frame_nr, f"brightness={payload[1]} style={payload[2]} sub={payload[3]} ncolors={payload[4]} speed={payload[5]}"))
    elif cmd[0] == 0xea and cmd[1] == 0x0f and len(cmd) >= 3:
        style = cmd[2]
        if style != prev_style:
            events.append(("ea0f_change", op.frame_nr, f"style 0x{style:02x} ({style})"))
            prev_style = style
    elif cmd[0] == 0xe0 and cmd[1] == 0x1e:
        events.append(("e0_1e", op.frame_nr, f"activate: {cmd[2:].hex(' ')}"))
    elif cmd[0] == 0xea and cmd[1] == 0x05:
        events.append(("ea_05", op.frame_nr, "cache check"))
    elif cmd[0] == 0xe0 and cmd[1] == 0x30:
        events.append(("e0_30", op.frame_nr, "upload start"))
    elif cmd[0] == 0xe0 and cmd[1] == 0x33:
        events.append(("e0_33", op.frame_nr, "upload end"))

for etype, frame, desc in events:
    print(f"  Frame {frame:5d} [{etype:12s}] {desc}")

# ─── 5. ea 0f byte[3] analysis ───
print()
print("=" * 80)
print("ea 0f byte[3] values (always 0x00?)")
print("=" * 80)

byte3_vals = set()
for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) >= 4 and cmd[0] == 0xea and cmd[1] == 0x0f:
        byte3_vals.add(cmd[3])

print(f"  Unique byte[3] values: {sorted(byte3_vals)}")
print(f"  (byte[3] is always the second byte after 'ea 0f [style]')")

# ─── 6. Amplitude value ranges per style ───
print()
print("=" * 80)
print("AMPLITUDE RANGES PER STYLE")
print("=" * 80)

style_ranges = {}
for op in ops:
    if op.direction != "W":
        continue
    seq, inner = parse_wrapper(op.raw)
    if len(inner) < 2 or inner[0] != 0x0a:
        continue
    cmd = inner[1:]
    if len(cmd) >= 4 and cmd[0] == 0xea and cmd[1] == 0x0f:
        style = cmd[2]
        amplitudes = cmd[4:]  # skip ea 0f [style] [00]
        if amplitudes:
            if style not in style_ranges:
                style_ranges[style] = {"min": 255, "max": 0, "count": 0}
            style_ranges[style]["min"] = min(style_ranges[style]["min"], min(amplitudes))
            style_ranges[style]["max"] = max(style_ranges[style]["max"], max(amplitudes))
            style_ranges[style]["count"] += 1

for style in sorted(style_ranges.keys()):
    r = style_ranges[style]
    print(f"  Style 0x{style:02x} ({style:3d}): min={r['min']:3d} max={r['max']:3d}, {r['count']} frames")
