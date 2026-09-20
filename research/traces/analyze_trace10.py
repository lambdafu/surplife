#!/usr/bin/env python3
"""
Analyze trace 10 (cycle-through-image-animations):
  - What commands set animation type/effect on already-uploaded images
  - JSON metadata for each upload (if any)
  - All ea commands that control animation style

Also pulls context from neighboring traces (09 and 11) for comparison.
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyze_traces import parse_btsnoop, reassemble, parse_wrapper, extract_uploads


TRACE_DIR = os.path.dirname(os.path.abspath(__file__))


def analyze_trace(path, label):
    """Parse one trace and return (ops, uploads, ea_cmds)."""
    frames = parse_btsnoop(path)
    ops = reassemble(frames)
    uploads = extract_uploads(ops, path)

    ea_cmds = []
    for op in ops:
        if op.direction != "W":
            continue
        seq, inner = parse_wrapper(op.raw)
        if len(inner) < 3 or inner[0] != 0x0a:
            continue
        cmd = inner[1:]
        if cmd[0] == 0xea:
            ea_cmds.append({
                "frame": op.frame_nr,
                "subcmd": cmd[1],
                "payload": cmd[2:],
                "raw": cmd,
            })
    return ops, uploads, ea_cmds


def print_section(title):
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")


def main():
    # ── Trace 10 (main target) ──
    t10_path = os.path.join(TRACE_DIR, "10-cycle-through-image-animations.log")
    ops10, uploads10, ea10 = analyze_trace(t10_path, "Trace 10")

    print_section("TRACE 10: cycle-through-image-animations")
    print(f"  Total ops: {len(ops10)}  "
          f"(W={sum(1 for o in ops10 if o.direction == 'W')}, "
          f"N={sum(1 for o in ops10 if o.direction == 'N')})")

    # ── Uploads ──
    print_section("UPLOADS IN TRACE 10")
    full_uploads = [u for u in uploads10 if u.get("type") != "ea_11_draw"]
    if not full_uploads:
        print("  ** No uploads in this trace — it only sends control commands **")
    for u in full_uploads:
        if "json" in u:
            print(f"  JSON: {json.dumps(u['json'], indent=4)}")

    # ── ea commands ──
    print_section("ALL ea COMMANDS IN TRACE 10")
    print(f"  {'Frame':>7}  {'Cmd':6}  {'Payload (hex)':40}  Interpretation")
    print(f"  {'-'*7}  {'-'*6}  {'-'*40}  {'-'*30}")
    for c in ea10:
        p = c["payload"]
        interp = ""
        if c["subcmd"] == 0x06:
            # ea 06: activate/set animation effect
            # byte0=slot?, byte1=speed?, byte2=effect_id
            slot = p[0] if len(p) > 0 else "?"
            speed = p[1] if len(p) > 1 else "?"
            effect = p[2] if len(p) > 2 else "?"
            interp = f"slot={slot} speed=0x{speed:02x}({speed}) effect={effect}"
        print(f"  {c['frame']:7d}  ea {c['subcmd']:02x}  {p.hex(' '):40s}  {interp}")

    # ── Context: trace 09 (preceding upload) ──
    print_section("CONTEXT: TRACE 09 (upload-and-show-image, preceding trace)")
    t09_path = os.path.join(TRACE_DIR, "09-upload-and-show-image.log")
    ops09, uploads09, ea09 = analyze_trace(t09_path, "Trace 09")

    for u in uploads09:
        if "json" in u:
            print(f"  Upload JSON: {json.dumps(u['json'], indent=4)}")
            print(f"  Payload type: {u.get('payload_type')}, len: {u.get('payload_len')}")
    print(f"\n  ea commands in trace 09:")
    for c in ea09:
        p = c["payload"]
        print(f"    Frame {c['frame']:5d}  ea {c['subcmd']:02x}  {p.hex(' ')}")
        if c["subcmd"] == 0x06:
            print(f"      -> ea 06: slot={p[0]}, speed=0x{p[1]:02x}({p[1]}), effect={p[2]}")

    # ── Context: trace 11 (back to static) ──
    print_section("CONTEXT: TRACE 11 (back-to-static, following trace)")
    t11_path = os.path.join(TRACE_DIR, "11-cycle-through-image-animations-back-to-static.log")
    ops11, uploads11, ea11 = analyze_trace(t11_path, "Trace 11")
    print(f"  ea commands in trace 11:")
    for c in ea11:
        p = c["payload"]
        print(f"    Frame {c['frame']:5d}  ea {c['subcmd']:02x}  {p.hex(' ')}")
        if c["subcmd"] == 0x06:
            print(f"      -> ea 06: slot={p[0]}, speed=0x{p[1]:02x}({p[1]}), effect={p[2]}")

    # ── Cross-reference: ea 06 byte2 (effect) values ──
    print_section("ANALYSIS: ea 06 EFFECT BYTE (byte[4] / 3rd payload byte)")
    print("""
  ea 06 payload format: [slot] [speed] [effect_id]

  Trace 09 (show image):     ea 06 00 64 01  -> effect=1 (static/none)
  Trace 10 (cycle effects):  ea 06 00 64 02  -> effect=2
                              ea 06 00 64 03  -> effect=3
                              ea 06 00 64 04  -> effect=4
                              ea 06 00 64 05  -> effect=5
                              ea 06 00 64 06  -> effect=6
  Trace 11 (back to static): ea 06 00 64 05  -> effect=5
                              ea 06 00 64 04  -> effect=4
                              ea 06 00 64 03  -> effect=3
                              ea 06 00 64 02  -> effect=2
                              ea 06 00 64 01  -> effect=1 (static)

  Conclusions:
    - ea 06 is the "activate/display" command
    - Byte 0 (0x00): slot index (which carousel slot)
    - Byte 1 (0x64 = 100): speed (100 = default, see trace 05 for speed changes)
    - Byte 2: ANIMATION EFFECT TYPE:
        1 = static (no animation)
        2 = effect 2 (first animation)
        3 = effect 3
        4 = effect 4
        5 = effect 5
        6 = effect 6
    - The image is already uploaded; ea 06 just changes the display effect
    - No re-upload or JSON metadata change needed to switch effects
    - Speed is controlled separately via ea 07 (see trace 05)
""")

    # ── Cross-reference: ea 07 speed from trace 05 ──
    print_section("CONTEXT: ea 07 SPEED VALUES (from trace 05)")
    t05_path = os.path.join(TRACE_DIR, "05-animation-speed.log")
    if os.path.exists(t05_path):
        _, _, ea05 = analyze_trace(t05_path, "Trace 05")
        for c in ea05:
            p = c["payload"]
            if c["subcmd"] == 0x07:
                speed = p[1] if len(p) > 1 else p[0] if len(p) > 0 else "?"
                print(f"    Frame {c['frame']:5d}  ea 07  {p.hex(' ')}  -> speed={speed} (0x{speed:02x})")
        print(f"\n  ea 07 format: [slot] [speed]")
        print(f"  Speed range: 0x02 (2) to 0x64 (100)")


if __name__ == "__main__":
    main()
