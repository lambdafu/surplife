#!/usr/bin/env python3
"""
Surplife BLE Trace Analyzer

Phase 1: Extract all raw facts from traces without interpretation.
Phase 2: Cross-trace analysis and pattern detection.

Usage:
    python3 analyze_traces.py traces/*.log              # Analyze all traces
    python3 analyze_traces.py --raw traces/01-*.log     # Raw fact extraction only
    python3 analyze_traces.py --summary traces/*.log    # Cross-trace summary
"""

import json
import os
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional


# ─── BTSnoop / L2CAP / ATT parsing (from parse_capture.py, verified) ───


def parse_btsnoop(filepath: str) -> list[tuple[int, int, bytes]]:
    with open(filepath, "rb") as f:
        data = f.read()
    if data[:8] != b"btsnoop\x00":
        raise ValueError(f"Not a BTSnoop file: {filepath}")
    version, dtype = struct.unpack(">II", data[8:16])
    if version != 1 or dtype != 1001:
        raise ValueError(f"Unsupported BTSnoop: version={version}, type={dtype}")
    frames, pos, frame_nr = [], 16, 0
    while pos + 24 <= len(data):
        orig_len, incl_len, flags, drops, ts = struct.unpack(">IIIIq", data[pos:pos + 24])
        pos += 24
        if pos + incl_len > len(data):
            break
        frames.append((frame_nr := frame_nr + 1, flags, data[pos:pos + incl_len]))
        pos += incl_len
    return frames


@dataclass
class Op:
    frame_nr: int
    direction: str  # "W" (write to device) or "N" (notify from device)
    raw: bytes      # full payload after ATT header
    # parsed:
    seq: int = 0
    inner: bytes = b""  # after 8-byte wrapper


def reassemble(frames):
    ops = []
    buf: Optional[bytearray] = None
    expected = frame0 = 0
    direction = ""

    def flush():
        nonlocal buf
        if buf is not None and len(buf) >= expected:
            ops.append(Op(frame_nr=frame0, direction=direction, raw=bytes(buf[:expected])))
        buf = None

    for frame_nr, flags, pkt in frames:
        if len(pkt) < 4:
            continue
        handle_flags = struct.unpack_from("<H", pkt, 0)[0]
        hci_len = struct.unpack_from("<H", pkt, 2)[0]
        hci_data = pkt[4:4 + hci_len]
        pb = (handle_flags >> 12) & 0x03

        if pb in (0, 2) and len(hci_data) >= 7:
            flush()
            l2cap_len = struct.unpack_from("<H", hci_data, 0)[0]
            l2cap_cid = struct.unpack_from("<H", hci_data, 2)[0]
            if l2cap_cid != 0x0004:
                buf = None
                continue
            att_op = hci_data[4]
            att_handle = struct.unpack_from("<H", hci_data, 5)[0]
            if att_op == 0x52 and att_handle == 0x0003:
                buf = bytearray(hci_data[7:])
                expected = l2cap_len - 3
                frame0 = frame_nr
                direction = "W"
            elif att_op == 0x1b and att_handle == 0x0005:
                ops.append(Op(frame_nr=frame_nr, direction="N", raw=bytes(hci_data[7:])))
                buf = None
            else:
                buf = None
        elif pb in (1, 3) and buf is not None:
            buf.extend(hci_data)
        if buf is not None and len(buf) >= expected:
            flush()
    flush()
    return ops


def parse_wrapper(raw: bytes):
    """Parse 8-byte surplife wrapper. Returns (seq, inner)."""
    if len(raw) < 8:
        return 0, raw
    seq = (raw[0] << 8) | raw[1]
    inner_len = (raw[6] << 8) | raw[7]  # Big Endian (verified)
    return seq, raw[8:8 + inner_len]


# ─── Phase 1: Raw fact extraction ───


def extract_facts(ops: list[Op], filename: str) -> list[dict]:
    """Extract every observable fact from a trace, no interpretation."""
    facts = []
    basename = os.path.splitext(os.path.basename(filename))[0]

    for op in ops:
        seq, inner = parse_wrapper(op.raw)
        op.seq = seq
        op.inner = inner
        fact = {
            "trace": basename,
            "frame": op.frame_nr,
            "dir": op.direction,
            "seq": f"0x{seq:04x}",
            "wrapper_bytes": op.raw[:8].hex(),
            "inner_len": len(inner),
        }

        if op.direction == "W":
            # Write command
            if len(inner) == 0:
                fact["type"] = "empty"
            elif inner[0] == 0x0c:
                fact["type"] = "raw_0c"
                fact["inner_hex"] = inner.hex()
            elif inner[0] == 0x0a and len(inner) > 1:
                cmd = inner[1:]
                fact["prefix"] = "0a"
                if len(cmd) >= 2:
                    fact["cmd"] = f"{cmd[0]:02x}_{cmd[1]:02x}"
                    fact["cmd_bytes"] = cmd[:min(len(cmd), 20)].hex()
                    if len(cmd) > 20:
                        fact["cmd_bytes_truncated"] = True
                        fact["full_cmd_len"] = len(cmd)

                    # Extract cmd-specific raw values (no interpretation)
                    c0, c1 = cmd[0], cmd[1]
                    if c0 == 0xe0 and c1 == 0x01 and len(cmd) >= 14:
                        fact["type"] = "e0_01"
                        fact["byte3"] = f"0x{cmd[3]:02x}"
                        fact["payload"] = cmd[2:14].hex()
                    elif c0 == 0xe0 and c1 == 0x30:
                        fact["type"] = "e0_30"
                        fact["payload"] = cmd[2:].hex()
                    elif c0 == 0xe0 and c1 == 0x32:
                        fact["type"] = "e0_32"
                        seg_data = cmd[2:]
                        if len(seg_data) >= 7:
                            fact["seg_size_be"] = (seg_data[0] << 8) | seg_data[1]
                            fact["seg_bytes_2_5"] = seg_data[2:6].hex()
                            fact["seg_index"] = seg_data[6]
                            fact["seg_content_len"] = len(seg_data) - 7
                    elif c0 == 0xe0 and c1 == 0x33:
                        fact["type"] = "e0_33"
                    elif c0 == 0xe0 and c1 == 0x1e:
                        fact["type"] = "e0_1e"
                        fact["payload"] = cmd[2:].hex()
                    elif c0 == 0xe0 and c1 == 0x0e:
                        fact["type"] = "e0_0e"
                        fact["payload"] = cmd[2:].hex()
                    elif c0 == 0xea and c1 == 0x05:
                        fact["type"] = "ea_05"
                        fact["byte2"] = f"0x{cmd[2]:02x}" if len(cmd) > 2 else "?"
                        fact["hash"] = cmd[3:19].hex() if len(cmd) >= 19 else "?"
                    elif c0 == 0xea and c1 == 0x06:
                        fact["type"] = "ea_06"
                        fact["payload"] = cmd[2:].hex()
                        if len(cmd) >= 5:
                            fact["bytes"] = [f"0x{b:02x}" for b in cmd[2:5]]
                    elif c0 == 0xea and c1 == 0x07:
                        fact["type"] = "ea_07"
                        if len(cmd) >= 4:
                            fact["byte2"] = f"0x{cmd[2]:02x}"
                            fact["byte3"] = f"0x{cmd[3]:02x}"
                            fact["byte3_dec"] = cmd[3]
                    elif c0 == 0xea and c1 == 0x09:
                        fact["type"] = "ea_09"
                        fact["payload"] = cmd[2:].hex()
                    elif c0 == 0xea and c1 == 0x24:
                        fact["type"] = "ea_24"
                    elif c0 == 0xea and c1 == 0x11:
                        fact["type"] = "ea_11"
                        fact["payload_len"] = len(cmd) - 2
                    elif c0 == 0xea and c1 == 0x81:
                        fact["type"] = "ea_81"
                        fact["payload"] = cmd[2:].hex()
                    elif c0 == 0xea and c1 == 0x14:
                        fact["type"] = "ea_14"
                        fact["payload"] = cmd[2:].hex()
                    elif c0 == 0xea and c1 == 0x0b:
                        fact["type"] = "ea_0b"
                        fact["payload_len"] = len(cmd) - 2
                        if len(cmd) >= 3:
                            fact["byte2"] = cmd[2]
                    elif c0 == 0xea and c1 == 0x0c:
                        fact["type"] = "ea_0c"
                    elif c0 == 0xea and c1 == 0x0d:
                        fact["type"] = "ea_0d"
                        fact["payload_len"] = len(cmd) - 2
                    elif c0 == 0xea and c1 == 0x0e:
                        fact["type"] = "ea_0e"
                    elif c0 == 0x10 and c1 == 0x14:
                        fact["type"] = "10_14"
                        fact["payload"] = cmd[2:].hex()
                    else:
                        fact["type"] = f"unknown_{c0:02x}_{c1:02x}"
                        fact["payload"] = cmd[2:].hex()
                else:
                    fact["type"] = "short_cmd"
                    fact["cmd_bytes"] = cmd.hex()
            else:
                fact["type"] = f"unknown_prefix_0x{inner[0]:02x}"
                fact["inner_hex"] = inner[:20].hex()

        else:
            # Notification from device
            if len(inner) > 0:
                fact["resp_type_byte"] = f"0x{inner[0]:02x}"
                if inner[0] == 0x15:
                    fact["type"] = "ack"
                    if len(inner) >= 3:
                        fact["ack_cmd"] = f"{inner[1]:02x}_{inner[2]:02x}"
                    if len(inner) >= 4:
                        fact["ack_byte3"] = f"0x{inner[3]:02x}"
                        fact["ack_byte3_dec"] = inner[3]
                    if len(inner) > 4:
                        fact["ack_extra"] = inner[4:].hex()
                elif inner[0] == 0x16:
                    fact["type"] = "status"
                    fact["status_hex"] = inner.hex()
                    if len(inner) >= 25:
                        fact["status_bytes"] = {
                            "byte1_2": inner[1:3].hex(),
                            "byte3": f"0x{inner[3]:02x}",
                            "byte4_5": inner[4:6].hex(),
                            "byte6": f"0x{inner[6]:02x}",
                            "byte7": f"0x{inner[7]:02x}",
                            "byte8": f"0x{inner[8]:02x}",
                            "byte9": f"0x{inner[9]:02x}",
                            "byte10": f"0x{inner[10]:02x}",
                            "byte10_dec": inner[10],
                            "byte11_13": inner[11:14].hex(),
                            "byte14": f"0x{inner[14]:02x}",
                            "byte14_dec": inner[14],
                            "byte15": f"0x{inner[15]:02x}",
                            "byte15_dec": inner[15],
                            "byte16": f"0x{inner[16]:02x}",
                            "byte16_dec": inner[16],
                            "byte17": f"0x{inner[17]:02x}",
                            "byte18_19": inner[18:20].hex(),
                            "byte20_21": inner[20:22].hex(),
                            "byte22": f"0x{inner[22]:02x}",
                            "byte23": f"0x{inner[23]:02x}",
                            "byte24": f"0x{inner[24]:02x}" if len(inner) > 24 else "?",
                        }
                else:
                    fact["type"] = f"unknown_resp_0x{inner[0]:02x}"
                    fact["resp_hex"] = inner.hex()

        facts.append(fact)
    return facts


# ─── Phase 1b: Upload content extraction ───


def extract_uploads(ops: list[Op], filename: str) -> list[dict]:
    """Extract and reassemble upload sequences."""
    uploads = []
    segments = []
    in_upload = False
    cache_check = None
    frame_header_cmd = None

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

        if c0 == 0xea and c1 == 0x05:
            cache_check = {
                "byte2": f"0x{cmd[2]:02x}" if len(cmd) > 2 else "?",
                "hash": cmd[3:19].hex() if len(cmd) >= 19 else "?",
            }
        elif c0 == 0xe0 and c1 == 0x30:
            in_upload = True
            segments = []
            frame_header_cmd = cmd[2:]
        elif c0 == 0xe0 and c1 == 0x32 and in_upload:
            seg_data = cmd[2:]
            if len(seg_data) >= 7:
                segments.append({
                    "seg_size": (seg_data[0] << 8) | seg_data[1],
                    "seg_pad": seg_data[2:6].hex(),
                    "seg_idx": seg_data[6],
                    "content": seg_data[7:],
                })
        elif c0 == 0xe0 and c1 == 0x33 and in_upload:
            in_upload = False
            if not segments:
                continue
            all_content = b"".join(s["content"] for s in segments)
            upload = {
                "trace": os.path.splitext(os.path.basename(filename))[0],
                "cache_check": cache_check,
                "frame_header_raw": frame_header_cmd.hex() if frame_header_cmd else "?",
                "num_segments": len(segments),
                "total_content_bytes": len(all_content),
                "segment_sizes": [s["seg_size"] for s in segments],
                "segment_pads": list(set(s["seg_pad"] for s in segments)),
            }
            # Parse content: ea23 0103 [16B hash] [2B json_len] [json] [payload]
            if len(all_content) >= 22 and all_content[:2] == b"\xea\x23":
                upload["content_marker"] = all_content[:2].hex()
                upload["content_bytes_2_3"] = all_content[2:4].hex()
                upload["content_hash"] = all_content[4:20].hex()
                json_len = (all_content[20] << 8) | all_content[21]
                upload["json_len"] = json_len
                if 22 + json_len <= len(all_content):
                    try:
                        json_str = all_content[22:22 + json_len].decode("ascii")
                        upload["json"] = json.loads(json_str)
                    except:
                        upload["json_raw"] = all_content[22:22 + json_len].hex()
                    payload = all_content[22 + json_len:]
                    upload["payload_len"] = len(payload)
                    # Check for GIF
                    if payload[:3] == b"GIF":
                        upload["payload_type"] = "GIF"
                        upload["gif_version"] = payload[:6].decode("ascii", errors="replace")
                        if len(payload) >= 10:
                            w = payload[6] | (payload[7] << 8)
                            h = payload[8] | (payload[9] << 8)
                            upload["gif_size"] = f"{w}x{h}"
                    else:
                        # 6-byte header + compressed data
                        upload["payload_type"] = "a2pl_compressed"
                        if len(payload) >= 6:
                            header = payload[:6]
                            upload["payload_header"] = header.hex()
                            upload["payload_header_size_be"] = (header[4] << 8) | header[5]
                            pix_data = payload[6:]
                            upload["pixel_data_len"] = len(pix_data)
                            upload["pixel_data_first_32"] = pix_data[:32].hex()
                            upload["pixel_data_last_16"] = pix_data[-16:].hex() if len(pix_data) >= 16 else pix_data.hex()
                            upload["pixel_data_hex"] = pix_data.hex(' ')
            uploads.append(upload)
            cache_check = None
            frame_header_cmd = None
        elif c0 == 0xea and c1 == 0x11:
            # ea 11: direct draw command (live graffiti mode)
            # Format: ea 11 [3 bytes: 00 00 00] [1 byte: length] [pixel_data]
            # The 4th header byte is the pixel data length.
            if len(cmd) >= 6:
                ea11_len = cmd[5]
                pix_data = cmd[6:]
                upload = {
                    "trace": os.path.splitext(os.path.basename(filename))[0],
                    "frame": op.frame_nr,
                    "type": "ea_11_draw",
                    "ea11_header": cmd[2:6].hex(),
                    "declared_size": ea11_len,
                    "pixel_data_len": len(pix_data),
                    "pixel_data_hex": pix_data.hex(' '),
                }
                uploads.append(upload)
    return uploads


# ─── Phase 2: Cross-trace analysis ───


def cross_trace_analysis(all_facts: list[dict], all_uploads: list[dict]):
    """Analyze patterns across all traces."""
    print("=" * 70)
    print("CROSS-TRACE ANALYSIS")
    print("=" * 70)

    # 1. Wrapper format verification
    print("\n--- Wrapper Format ---")
    for f in all_facts[:5]:
        wb = bytes.fromhex(f["wrapper_bytes"])
        f1_be = (wb[4] << 8) | wb[5]
        f2_be = (wb[6] << 8) | wb[7]
        print(f"  {f['dir']} frame={f['frame']} wrapper={f['wrapper_bytes']}"
              f"  field1_BE={f1_be} field2_BE={f2_be} inner_len={f['inner_len']}"
              f"  field2==inner_len: {f2_be == f['inner_len']}"
              f"  field1==field2-1: {f1_be == f2_be - 1}")

    # 2. Sequence numbers
    print("\n--- Sequence Numbers ---")
    by_trace = defaultdict(list)
    for f in all_facts:
        by_trace[f["trace"]].append(f)
    for trace, facts in sorted(by_trace.items()):
        w_seqs = [int(f["seq"], 16) for f in facts if f["dir"] == "W"]
        n_seqs = [int(f["seq"], 16) for f in facts if f["dir"] == "N"]
        if w_seqs:
            print(f"  {trace}: W seq range 0x{min(w_seqs):04x}-0x{max(w_seqs):04x} ({len(w_seqs)} cmds)")
        if n_seqs:
            print(f"  {trace}: N seq range 0x{min(n_seqs):04x}-0x{max(n_seqs):04x} ({len(n_seqs)} notifs)")

    # 3. Command frequency
    print("\n--- Command Frequencies ---")
    cmd_counts = Counter()
    for f in all_facts:
        t = f.get("type", f.get("cmd", "unknown"))
        cmd_counts[t] += 1
    for cmd, cnt in cmd_counts.most_common():
        print(f"  {cmd}: {cnt}")

    # 4. e0_01 subcmd values
    print("\n--- e0_01 Byte3 Values (subcmd) ---")
    for f in all_facts:
        if f.get("type") == "e0_01":
            print(f"  {f['trace']} frame={f['frame']}: byte3={f['byte3']} payload={f['payload']}")

    # 5. ea_05 cache check types
    print("\n--- ea_05 Cache Check Byte2 Values ---")
    for f in all_facts:
        if f.get("type") == "ea_05":
            print(f"  {f['trace']}: byte2={f['byte2']} hash={f['hash']}")

    # 6. ea_06 activate values
    print("\n--- ea_06 Activate Payloads ---")
    for f in all_facts:
        if f.get("type") == "ea_06":
            print(f"  {f['trace']} frame={f['frame']}: bytes={f.get('bytes',[])} payload={f['payload']}")

    # 7. ea_07 speed values
    print("\n--- ea_07 Speed Values ---")
    for f in all_facts:
        if f.get("type") == "ea_07":
            print(f"  {f['trace']}: byte2={f['byte2']} byte3={f['byte3']} (dec={f['byte3_dec']})")

    # 8. Status response analysis
    print("\n--- Status Responses ---")
    seen_statuses = set()
    for f in all_facts:
        if f.get("type") == "status":
            h = f["status_hex"]
            if h not in seen_statuses:
                seen_statuses.add(h)
                sb = f["status_bytes"]
                print(f"  {f['trace']}: byte7={sb['byte7']} byte10={sb['byte10']}(dec={sb['byte10_dec']})"
                      f" byte14={sb['byte14']}(dec={sb['byte14_dec']})"
                      f" byte15={sb['byte15']}(dec={sb['byte15_dec']})"
                      f" byte16={sb['byte16']}(dec={sb['byte16_dec']})"
                      f" byte17={sb['byte17']}")

    # 9. ACK analysis
    print("\n--- ACK Responses ---")
    ack_types = Counter()
    for f in all_facts:
        if f.get("type") == "ack":
            key = f"ACK {f.get('ack_cmd','?')} byte3={f.get('ack_byte3','?')}"
            ack_types[key] += 1
            if f.get("ack_extra"):
                print(f"  {f['trace']} frame={f['frame']}: {f['ack_cmd']}"
                      f" byte3={f['ack_byte3']} extra={f['ack_extra']}")
    for key, cnt in ack_types.most_common():
        print(f"  {key}: {cnt}×")

    # 10. Upload analysis
    print("\n--- Uploads ---")
    for u in all_uploads:
        print(f"  {u['trace']}:")
        print(f"    cache_check: {u.get('cache_check')}")
        print(f"    frame_header_raw: {u.get('frame_header_raw')}")
        print(f"    segments: {u['num_segments']}, total: {u['total_content_bytes']}B")
        print(f"    segment_pads: {u['segment_pads']}")
        if "json" in u:
            meta = u["json"]
            layers = meta.get("layers", [{}])
            layer = layers[0] if layers else {}
            print(f"    json.all_file_type: {meta.get('all_file_type')}")
            print(f"    json.enable_a2pl: {meta.get('enable_a2pl')}")
            print(f"    layer.type: {layer.get('type')}")
            print(f"    layer.amt_length: {layer.get('amt_length')}")
            print(f"    layer.amt_fmt: {layer.get('amt_fmt')}")
            print(f"    layer.frame_num: {layer.get('frame_num')}")
            if "attr" in layer:
                for attr in layer["attr"]:
                    print(f"    layer.attr: {json.dumps(attr)}")
        print(f"    payload_type: {u.get('payload_type')}")
        if u.get("payload_type") == "GIF":
            print(f"    gif: {u.get('gif_version')} {u.get('gif_size')}")
        elif u.get("payload_type") == "a2pl_compressed":
            print(f"    payload_header: {u.get('payload_header')}")
            print(f"    header_size_field: {u.get('payload_header_size_be')}")
            print(f"    pixel_data_len: {u.get('pixel_data_len')}")
            print(f"    pixel_first32: {u.get('pixel_data_first_32')}")
            print(f"    pixel_last16: {u.get('pixel_data_last_16')}")
        print(f"    payload_len: {u.get('payload_len')}")

    # 11. Content hash tracking
    print("\n--- Content Hashes ---")
    hashes = {}
    for u in all_uploads:
        h = u.get("content_hash", u.get("cache_check", {}).get("hash"))
        if h:
            file_type = u.get("json", {}).get("all_file_type", "?")
            hashes[h] = f"{u['trace']} type={file_type}"
    for f in all_facts:
        if f.get("type") == "ea_05":
            h = f.get("hash")
            if h and h not in hashes:
                hashes[h] = f"{f['trace']} (cache check only)"
    for h, src in sorted(hashes.items(), key=lambda x: x[1]):
        print(f"  {h} <- {src}")


# ─── Output ───


def print_facts(facts, raw_mode=False):
    """Print facts in a readable format."""
    for f in facts:
        trace = f["trace"]
        frame = f["frame"]
        d = ">>W" if f["dir"] == "W" else "<<N"
        t = f.get("type", f.get("cmd", "?"))

        if raw_mode:
            # Full fact dump
            compact = {k: v for k, v in f.items()
                       if k not in ("trace", "frame", "dir", "wrapper_bytes")}
            print(f"{trace} #{frame:5d} {d} {json.dumps(compact, separators=(',',':'))}")
        else:
            # Concise summary
            extras = []
            for key in ("byte3", "payload", "hash", "byte2", "byte3_dec",
                         "bytes", "ack_cmd", "ack_byte3", "ack_extra",
                         "payload_len", "seg_index", "seg_content_len"):
                if key in f:
                    extras.append(f"{key}={f[key]}")
            extra_str = " " + " ".join(extras) if extras else ""
            print(f"{trace} #{frame:5d} {d} {t}{extra_str}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Surplife BLE trace analyzer")
    parser.add_argument("files", nargs="+", help="BTSnoop .log trace files")
    parser.add_argument("--raw", action="store_true", help="Full raw fact output")
    parser.add_argument("--summary", action="store_true", help="Cross-trace summary only")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--uploads", action="store_true", help="Upload extraction only")
    args = parser.parse_args()

    all_facts = []
    all_uploads = []

    for filepath in args.files:
        try:
            frames = parse_btsnoop(filepath)
        except (ValueError, IOError) as e:
            print(f"Skipping {filepath}: {e}", file=sys.stderr)
            continue

        ops = reassemble(frames)
        facts = extract_facts(ops, filepath)
        uploads = extract_uploads(ops, filepath)
        all_facts.extend(facts)
        all_uploads.extend(uploads)

        if args.json:
            continue
        if args.summary:
            continue

        basename = os.path.splitext(os.path.basename(filepath))[0]
        print(f"\n{'=' * 70}")
        print(f"TRACE: {basename}")
        print(f"  Total ops: {len(ops)} (W={sum(1 for o in ops if o.direction=='W')},"
              f" N={sum(1 for o in ops if o.direction=='N')})")
        print(f"{'=' * 70}")

        if args.uploads:
            for u in uploads:
                print(json.dumps(u, indent=2, default=str))
        else:
            # Skip e0_32 data transfer details in non-raw mode
            filtered = [f for f in facts if not args.raw or True]
            if not args.raw:
                filtered = [f for f in facts if f.get("type") != "e0_32"]
            print_facts(filtered, raw_mode=args.raw)

    if args.json:
        output = {"facts": all_facts, "uploads": all_uploads}
        print(json.dumps(output, indent=2, default=str))
    elif args.summary:
        cross_trace_analysis(all_facts, all_uploads)


if __name__ == "__main__":
    main()
