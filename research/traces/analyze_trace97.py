#!/usr/bin/env python3
"""Analyze trace 97 - audio waveform custom/cycle/options/configure."""
import sys
import os
from collections import defaultdict

sys.path.insert(0, "/Users/marcus/Desktop/NDS/Projekt/surplife")
from analyze_traces import parse_btsnoop, reassemble, parse_wrapper

TRACE = "/Users/marcus/Desktop/NDS/Projekt/surplife/traces/97-surplife-audio-wave-custom-cycle-options-configure.log"

frames = parse_btsnoop(TRACE)
ops = reassemble(frames)

print(f"Total frames: {len(frames)}")
print(f"Total reassembled ops: {len(ops)} (W={sum(1 for o in ops if o.direction=='W')}, N={sum(1 for o in ops if o.direction=='N')})")

# ─── Collect ALL ea-prefixed commands and upload protocol commands ───
UPLOAD_CMDS = {0xe0: [0x30, 0x32, 0x33, 0x1e, 0x0e], 0xea: [0x05, 0x09, 0x23]}

ea_commands = []
upload_commands = []
all_commands = []

for op in ops:
    seq, inner = parse_wrapper(op.raw)
    direction = "SEND" if op.direction == "W" else "RECV"

    if op.direction == "W" and len(inner) > 1 and inner[0] == 0x0a:
        cmd = inner[1:]
        if len(cmd) >= 2:
            c0, c1 = cmd[0], cmd[1]
            entry = {
                "frame": op.frame_nr,
                "dir": direction,
                "seq": seq,
                "c0": c0,
                "c1": c1,
                "cmd": cmd,
            }
            all_commands.append(entry)
            if c0 == 0xea:
                ea_commands.append(entry)
            if c0 in UPLOAD_CMDS and c1 in UPLOAD_CMDS[c0]:
                upload_commands.append(entry)

    elif op.direction == "N":
        # Notifications (responses)
        if len(inner) >= 3 and inner[0] == 0x15:
            # ACK response
            entry = {
                "frame": op.frame_nr,
                "dir": direction,
                "seq": seq,
                "c0": inner[1],
                "c1": inner[2],
                "cmd": inner,
                "is_ack": True,
            }
            all_commands.append(entry)
            if inner[1] == 0xea:
                ea_commands.append(entry)

print()
print("=" * 80)
print("ALL ea-PREFIXED COMMANDS (sent and ACKs)")
print("=" * 80)

# Group by ea sub-command
by_subcmd = defaultdict(list)
for e in ea_commands:
    key = f"ea {e['c1']:02x}"
    by_subcmd[key].append(e)

for key in sorted(by_subcmd.keys()):
    entries = by_subcmd[key]
    print(f"\n--- {key} ({len(entries)} occurrences) ---")
    for e in entries:
        cmd = e["cmd"]
        is_ack = e.get("is_ack", False)
        if is_ack:
            # ACK: inner bytes
            extra = ""
            if len(cmd) >= 4:
                extra = f" status=0x{cmd[3]:02x}"
            if len(cmd) > 4:
                extra += f" extra={cmd[4:].hex(' ')}"
            print(f"  Frame {e['frame']:5d} [{e['dir']}] ACK ea {e['c1']:02x}{extra}")
        else:
            hex_dump = cmd.hex(' ')
            # Truncate very long dumps
            if len(cmd) > 60:
                hex_dump = cmd[:60].hex(' ') + f" ... ({len(cmd)} bytes total)"
            print(f"  Frame {e['frame']:5d} [{e['dir']}] seq=0x{e['seq']:04x} | {hex_dump}")

print()
print("=" * 80)
print("UPLOAD PROTOCOL COMMANDS (e0 30/32/33/1e/0e, ea 05/09/23)")
print("=" * 80)

by_upload_cmd = defaultdict(list)
for e in upload_commands:
    key = f"{e['c0']:02x} {e['c1']:02x}"
    by_upload_cmd[key].append(e)

if not upload_commands:
    print("  (none found)")
else:
    for key in sorted(by_upload_cmd.keys()):
        entries = by_upload_cmd[key]
        print(f"\n--- {key} ({len(entries)} occurrences) ---")
        for e in entries[:10]:  # limit to first 10
            cmd = e["cmd"]
            hex_dump = cmd.hex(' ')
            if len(cmd) > 60:
                hex_dump = cmd[:60].hex(' ') + f" ... ({len(cmd)} bytes total)"
            print(f"  Frame {e['frame']:5d} [{e['dir']}] seq=0x{e['seq']:04x} | {hex_dump}")
        if len(entries) > 10:
            print(f"  ... and {len(entries) - 10} more")

print()
print("=" * 80)
print("ALL OTHER (non-ea) WRITE COMMANDS")
print("=" * 80)

other_cmds = defaultdict(list)
for e in all_commands:
    if e.get("is_ack"):
        continue
    if e["c0"] != 0xea:
        key = f"{e['c0']:02x} {e['c1']:02x}"
        other_cmds[key].append(e)

for key in sorted(other_cmds.keys()):
    entries = other_cmds[key]
    print(f"\n--- {key} ({len(entries)} occurrences) ---")
    for e in entries[:5]:
        cmd = e["cmd"]
        hex_dump = cmd.hex(' ')
        if len(cmd) > 60:
            hex_dump = cmd[:60].hex(' ') + f" ... ({len(cmd)} bytes total)"
        print(f"  Frame {e['frame']:5d} [{e['dir']}] seq=0x{e['seq']:04x} | {hex_dump}")
    if len(entries) > 5:
        print(f"  ... and {len(entries) - 5} more")

print()
print("=" * 80)
print("DETAILED ea 0f ANALYSIS (waveform commands)")
print("=" * 80)

ea0f = [e for e in ea_commands if e["c1"] == 0x0f and not e.get("is_ack")]
if not ea0f:
    print("  (no ea 0f commands found)")
else:
    # Group by payload structure
    structures = defaultdict(list)
    for e in ea0f:
        cmd = e["cmd"]
        payload = cmd[2:]  # after ea 0f
        if len(payload) >= 2:
            key = f"ea0f_{payload[0]:02x}_{payload[1]:02x}_len{len(cmd)}"
        elif len(payload) == 1:
            key = f"ea0f_{payload[0]:02x}_len{len(cmd)}"
        else:
            key = f"ea0f_empty_len{len(cmd)}"
        structures[key].append(e)

    for key in sorted(structures.keys()):
        entries = structures[key]
        print(f"\n  Structure: {key} ({len(entries)} occurrences)")
        for e in entries[:5]:
            cmd = e["cmd"]
            hex_dump = cmd.hex(' ')
            if len(cmd) > 120:
                hex_dump = cmd[:40].hex(' ') + " ... " + cmd[-10:].hex(' ') + f" ({len(cmd)} bytes total)"
            print(f"    Frame {e['frame']:5d} | {hex_dump}")
        if len(entries) > 5:
            print(f"    ... and {len(entries) - 5} more")

print()
print("=" * 80)
print("DETAILED ea 10 ANALYSIS")
print("=" * 80)

ea10 = [e for e in ea_commands if e["c1"] == 0x10 and not e.get("is_ack")]
if not ea10:
    print("  (no ea 10 commands found)")
else:
    for e in ea10:
        cmd = e["cmd"]
        print(f"  Frame {e['frame']:5d} | {cmd.hex(' ')}")

print()
print("=" * 80)
print("COMMAND SEQUENCE (chronological, all ea + upload commands)")
print("=" * 80)

# Merge ea_commands and upload_commands, sort by frame
combined = []
seen_frames = set()
for e in ea_commands + upload_commands:
    fkey = (e["frame"], e["dir"])
    if fkey not in seen_frames:
        seen_frames.add(fkey)
        combined.append(e)

combined.sort(key=lambda x: x["frame"])

for e in combined:
    cmd = e["cmd"]
    is_ack = e.get("is_ack", False)
    if is_ack:
        extra = ""
        if len(cmd) >= 4:
            extra = f" status=0x{cmd[3]:02x}"
        if len(cmd) > 4:
            extra += f" extra={cmd[4:].hex(' ')}"
        label = f"ACK {e['c0']:02x} {e['c1']:02x}{extra}"
    else:
        label = f"{e['c0']:02x} {e['c1']:02x}"
        payload = cmd[2:]
        if len(payload) <= 30:
            label += f" | {payload.hex(' ')}" if payload else ""
        else:
            label += f" | {payload[:20].hex(' ')} ... ({len(payload)} payload bytes)"
    print(f"  Frame {e['frame']:5d} [{e['dir']:4s}] {label}")

print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)
cmd_freq = defaultdict(int)
for e in all_commands:
    if e.get("is_ack"):
        cmd_freq[f"ACK {e['c0']:02x} {e['c1']:02x}"] += 1
    else:
        cmd_freq[f"{e['c0']:02x} {e['c1']:02x}"] += 1

for cmd, cnt in sorted(cmd_freq.items(), key=lambda x: -x[1]):
    print(f"  {cmd}: {cnt}x")
