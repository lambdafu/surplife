#!/usr/bin/env python3
"""
Surplife BLE Capture Parser

Parses Apple PacketLogger BTSnoop captures (.log files) and extracts
ATT Write/Notify operations for the Surplife LED display.

Handles L2CAP fragment reassembly, surplife wrapper stripping,
and command decoding.

Usage:
    python3 parse_capture.py capture.log                    # filtered output
    python3 parse_capture.py --decode capture.log           # decoded commands
    python3 parse_capture.py --extract-images capture.log   # extract image data
    python3 parse_capture.py --json capture.log             # JSON output
"""

import argparse
import base64
import json
import os
import struct
import sys
from dataclasses import dataclass, field
from typing import Optional


# --- Data classes ---

@dataclass
class ATTOperation:
    """A single reassembled ATT operation (Write or Notify)."""
    frame_nr: int
    direction: str          # "WRITE" or "NOTIFY"
    raw_payload: bytes      # Full ATT payload (surplife wrapper + inner)

    # Parsed fields (populated by decode())
    seq: int = 0
    inner: bytes = b""      # After stripping wrapper and 0x0a prefix
    cmd_family: int = 0     # First byte of inner (e0, ea, 10, ...)
    cmd_name: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class ImageData:
    """Extracted image upload data."""
    content_hash: str
    json_metadata: dict
    amt_length: int
    image_header: bytes     # 6-byte header
    compressed_data: bytes  # Raw compressed pixel data


# --- BTSnoop parsing ---

def parse_btsnoop(filepath: str) -> list[tuple[int, int, bytes]]:
    """Parse BTSnoop v1 type 1001 (Apple PacketLogger) file.

    Returns list of (frame_nr, flags, packet_data).
    """
    with open(filepath, "rb") as f:
        data = f.read()

    # Validate header
    if data[:8] != b"btsnoop\x00":
        raise ValueError(f"Not a BTSnoop file: {filepath}")
    version = struct.unpack(">I", data[8:12])[0]
    dtype = struct.unpack(">I", data[12:16])[0]
    if version != 1 or dtype != 1001:
        raise ValueError(f"Unsupported BTSnoop: version={version}, type={dtype}")

    frames = []
    pos = 16
    frame_nr = 0

    while pos + 24 <= len(data):
        orig_len, incl_len, flags, drops, ts = struct.unpack(
            ">IIIIq", data[pos:pos + 24]
        )
        pos += 24
        if pos + incl_len > len(data):
            break
        pkt = data[pos:pos + incl_len]
        pos += incl_len
        frame_nr += 1
        frames.append((frame_nr, flags, pkt))

    return frames


def reassemble_att_operations(frames: list[tuple[int, int, bytes]]) -> list[ATTOperation]:
    """Reassemble L2CAP fragments into complete ATT operations.

    HCI ACL format per frame:
      [handle+flags 2B LE] [hci_data_len 2B LE] [L2CAP data...]
    First fragment (PB=0): L2CAP header [len 2B LE] [CID 2B LE] then ATT data
    Continuation (PB=1): raw L2CAP continuation data (no L2CAP header)

    ATT Write Command: opcode=0x52, handle=0x0003
    ATT Notification:  opcode=0x1b, handle=0x0005
    """
    ops = []
    current_buf: Optional[bytearray] = None
    current_expected = 0
    current_frame = 0
    current_direction = ""

    def flush():
        nonlocal current_buf
        if current_buf is not None and len(current_buf) >= current_expected:
            payload = bytes(current_buf[:current_expected])
            ops.append(ATTOperation(
                frame_nr=current_frame,
                direction=current_direction,
                raw_payload=payload,
            ))
        current_buf = None

    for frame_nr, flags, pkt in frames:
        if len(pkt) < 4:
            continue

        handle_flags = struct.unpack_from("<H", pkt, 0)[0]
        hci_len = struct.unpack_from("<H", pkt, 2)[0]
        hci_data = pkt[4:4 + hci_len]
        pb = (handle_flags >> 12) & 0x03

        if pb in (0, 2) and len(hci_data) >= 7:
            # First L2CAP fragment (PB=0 or PB=2)
            flush()
            l2cap_len = struct.unpack_from("<H", hci_data, 0)[0]
            l2cap_cid = struct.unpack_from("<H", hci_data, 2)[0]
            if l2cap_cid != 0x0004:  # Not ATT channel
                current_buf = None
                continue

            att_opcode = hci_data[4]
            att_handle = struct.unpack_from("<H", hci_data, 5)[0]

            if att_opcode == 0x52 and att_handle == 0x0003:
                # Write Command to handle 3
                current_buf = bytearray(hci_data[7:])
                current_expected = l2cap_len - 3  # minus ATT header
                current_frame = frame_nr
                current_direction = "WRITE"
            elif att_opcode == 0x1b and att_handle == 0x0005:
                # Notification from handle 5
                notify_data = bytes(hci_data[7:])
                # Notifications are typically small, no fragmentation
                ops.append(ATTOperation(
                    frame_nr=frame_nr,
                    direction="NOTIFY",
                    raw_payload=notify_data,
                ))
                current_buf = None
            else:
                current_buf = None

        elif pb in (1, 3) and current_buf is not None:
            # Continuation fragment (PB=1 or PB=3)
            current_buf.extend(hci_data)

        # Check if complete
        if current_buf is not None and len(current_buf) >= current_expected:
            flush()

    flush()
    return ops


# --- Surplife protocol decoding ---

def decode_surplife_wrapper(payload: bytes) -> tuple[int, int, bytes]:
    """Strip surplife 8-byte wrapper, return (seq, inner_len, inner_payload)."""
    if len(payload) < 8:
        return 0, 0, payload
    seq = (payload[0] << 8) | payload[1]
    # flags = payload[2:4]  # always 0x80 0x00
    # len_m1 = (payload[4] << 8) | payload[5]
    inner_len = (payload[6] << 8) | payload[7]
    inner = payload[8:8 + inner_len]
    return seq, inner_len, inner


COMMAND_NAMES = {
    # e0 family — Display control
    (0xe0, 0x01): "e0_01_mode",
    (0xe0, 0x0e): "e0_0e_refresh",
    (0xe0, 0x1e): "e0_1e_screen_prepare",
    (0xe0, 0x30): "e0_30_frame_header",
    (0xe0, 0x32): "e0_32_data_transfer",
    (0xe0, 0x33): "e0_33_end_marker",
    # ea family — Content & parameters
    (0xea, 0x05): "ea_05_cache_check",
    (0xea, 0x06): "ea_06_activate",
    (0xea, 0x07): "ea_07_speed",
    (0xea, 0x09): "ea_09_activate_color",
    (0xea, 0x11): "ea_11_graffiti",
    (0xea, 0x24): "ea_24_confirm",
    (0xea, 0x81): "ea_81_device_hash",
    # 10 family
    (0x10, 0x14): "10_14_config",
}

E0_01_SUBCMDS = {
    0x01: "brightness",
    0x23: "power_on",
    0x24: "power_off",
}

NOTIFY_TYPES = {
    0x15: "ACK",
    0x16: "STATUS",
}


def decode_operation(op: ATTOperation):
    """Populate decoded fields on an ATTOperation."""
    if op.direction == "WRITE":
        seq, inner_len, inner = decode_surplife_wrapper(op.raw_payload)
        op.seq = seq

        if len(inner) == 0:
            op.cmd_name = "empty"
            return

        if inner[0] == 0x0a and len(inner) > 2:
            # Normal command: 0x0a prefix
            cmd_data = inner[1:]
            op.inner = cmd_data
            op.cmd_family = cmd_data[0]

            # Look up command name
            if len(cmd_data) >= 2:
                key = (cmd_data[0], cmd_data[1])
                op.cmd_name = COMMAND_NAMES.get(key, f"{cmd_data[0]:02x}_{cmd_data[1]:02x}")
            else:
                op.cmd_name = f"{cmd_data[0]:02x}"

            # Decode specific commands
            if cmd_data[0] == 0xe0 and cmd_data[1] == 0x01 and len(cmd_data) >= 4:
                subcmd = cmd_data[3]
                subcmd_name = E0_01_SUBCMDS.get(subcmd, f"0x{subcmd:02x}")
                op.details["subcmd"] = subcmd_name
                if subcmd == 0x01 and len(cmd_data) >= 9:
                    op.details["brightness"] = cmd_data[6]
            elif cmd_data[0] == 0xea and cmd_data[1] == 0x07 and len(cmd_data) >= 4:
                op.details["speed"] = cmd_data[3]
            elif cmd_data[0] == 0xea and cmd_data[1] == 0x05 and len(cmd_data) >= 19:
                op.details["cache_type"] = f"0x{cmd_data[2]:02x}"
                op.details["hash"] = cmd_data[3:19].hex()
            elif cmd_data[0] == 0xe0 and cmd_data[1] == 0x30 and len(cmd_data) >= 7:
                total_size = (cmd_data[4] << 8) | cmd_data[5]
                op.details["total_size"] = total_size
            elif cmd_data[0] == 0xe0 and cmd_data[1] == 0x32:
                op.details["data_len"] = len(cmd_data) - 3  # after e0 32

        elif inner[0] == 0x0c:
            # Special init command (no 0x0a prefix)
            op.cmd_name = "init_0c"
            op.inner = inner
        else:
            op.cmd_name = f"raw_0x{inner[0]:02x}"
            op.inner = inner

    elif op.direction == "NOTIFY":
        # Notifications have surplife wrapper too
        seq, inner_len, inner = decode_surplife_wrapper(op.raw_payload)
        op.seq = seq
        op.inner = inner

        if len(inner) > 0:
            notify_type = inner[0]
            op.details["type"] = NOTIFY_TYPES.get(notify_type, f"0x{notify_type:02x}")

            if notify_type == 0x15 and len(inner) >= 3:
                # ACK — next bytes identify the acknowledged command
                op.cmd_name = f"ACK_{inner[1]:02x}_{inner[2]:02x}"
                if len(inner) >= 4:
                    op.details["ack_value"] = inner[3]
            elif notify_type == 0x16 and len(inner) >= 19:
                # Status response
                op.cmd_name = "STATUS"
                if len(inner) >= 15:
                    op.details["brightness"] = inner[14]
                if len(inner) >= 8:
                    op.details["mode"] = f"0x{inner[7]:02x}"


# --- Image extraction ---

def extract_images(ops: list[ATTOperation]) -> list[ImageData]:
    """Extract image upload data from e032 data transfer packets."""
    images = []
    e032_segments: list[bytes] = []
    in_upload = False

    for op in ops:
        if op.direction != "WRITE":
            continue

        cmd_name = op.cmd_name

        if cmd_name == "e0_30_frame_header":
            # Start of upload
            in_upload = True
            e032_segments = []

        elif cmd_name == "e0_32_data_transfer" and in_upload:
            # e032 payload: [3 bytes e0 32 XX] then [7-byte segment header] [content]
            cmd_data = op.inner  # already stripped of 0x0a
            if len(cmd_data) < 10:
                continue
            # Skip "e0 32" (2 bytes), then the remaining data starts
            e032_payload = cmd_data[2:]  # after e0 and 32
            # 7-byte segment header: [size_hi size_lo] [4 zeros] [seg_index]
            if len(e032_payload) < 8:
                continue
            content = e032_payload[7:]
            e032_segments.append(content)

        elif cmd_name == "e0_33_end_marker" and in_upload:
            # End of upload — reassemble content
            in_upload = False
            if not e032_segments:
                continue

            all_content = b"".join(e032_segments)
            img = _parse_upload_content(all_content)
            if img:
                images.append(img)
            e032_segments = []

    return images


def _parse_upload_content(content: bytes) -> Optional[ImageData]:
    """Parse reassembled e032 content into ImageData."""
    if len(content) < 22 or content[:2] != b"\xea\x23":
        return None

    # ea 23 01 03 [16B hash] [2B json_len BE] [json] [image_payload]
    content_hash = content[4:20].hex()
    json_len = (content[20] << 8) | content[21]

    if 22 + json_len > len(content):
        return None

    try:
        json_str = content[22:22 + json_len].decode("ascii")
        json_meta = json.loads(json_str)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    image_payload = content[22 + json_len:]

    # Image payload: [6B header] [compressed_data]
    if len(image_payload) < 6:
        return None

    image_header = image_payload[:6]
    compressed = image_payload[6:]

    amt_length = 0
    if "layers" in json_meta and json_meta["layers"]:
        amt_length = json_meta["layers"][0].get("amt_length", 0)

    return ImageData(
        content_hash=content_hash,
        json_metadata=json_meta,
        amt_length=amt_length,
        image_header=image_header,
        compressed_data=compressed,
    )


# --- Output formatters ---

def output_filter(ops: list[ATTOperation], filename: str):
    """Output in filtered text format (matches legacy -filtered.txt)."""
    basename = os.path.splitext(os.path.basename(filename))[0]
    print(f"# Filtered BLE ATT communication: {basename}")
    print("# Only device communication (handle 0x0003=write, 0x0005=notify)")
    print("# Columns: frame_nr | direction | payload_hex")
    print("#")
    for op in ops:
        direction = ">>WRITE" if op.direction == "WRITE" else "<<NOTIF"
        print(f"{op.frame_nr}\t{direction}\t{op.raw_payload.hex()}")


def output_decode(ops: list[ATTOperation]):
    """Output decoded, human-readable format."""
    for op in ops:
        if op.direction == "WRITE":
            detail_parts = []
            if op.details:
                for k, v in op.details.items():
                    detail_parts.append(f"{k}={v}")
            details_str = " " + ", ".join(detail_parts) if detail_parts else ""
            inner_len = len(op.inner)
            print(
                f"{op.frame_nr:5d}  >> seq=0x{op.seq:04x} "
                f"{op.cmd_name} len={inner_len}{details_str}"
            )
        else:
            detail_parts = []
            if op.details:
                for k, v in op.details.items():
                    detail_parts.append(f"{k}={v}")
            details_str = " " + ", ".join(detail_parts) if detail_parts else ""
            print(f"{op.frame_nr:5d}  << {op.cmd_name}{details_str}")


def output_json(ops: list[ATTOperation]):
    """Output JSON format."""
    result = []
    for op in ops:
        entry = {
            "frame": op.frame_nr,
            "direction": op.direction,
            "seq": op.seq,
            "cmd": op.cmd_name,
            "inner_hex": op.inner.hex(),
            "raw_hex": op.raw_payload.hex(),
        }
        if op.details:
            entry["details"] = op.details
        result.append(entry)
    print(json.dumps(result, indent=2))


def output_extract_images(ops: list[ATTOperation], filename: str):
    """Extract and output image data."""
    images = extract_images(ops)
    basename = os.path.splitext(os.path.basename(filename))[0]

    if not images:
        print(f"No image uploads found in {basename}", file=sys.stderr)
        return

    for i, img in enumerate(images):
        suffix = f"_{i}" if len(images) > 1 else ""
        slug = basename.replace(" ", "-").replace("surplife-", "").strip("-")

        print(f"=== Image{suffix} from {basename} ===")
        print(f"  Hash: {img.content_hash}")
        print(f"  JSON: {json.dumps(img.json_metadata)}")
        print(f"  amt_length: {img.amt_length}")
        print(f"  Image header (6B): {img.image_header.hex()}")
        print(f"  Compressed data: {len(img.compressed_data)} bytes")

        # Write hex dump file
        hex_file = f"{slug}{suffix}.imgdata.hex"
        dirpath = os.path.dirname(os.path.abspath(filename))
        hex_path = os.path.join(dirpath, hex_file)
        with open(hex_path, "w") as f:
            for offset in range(0, len(img.compressed_data), 32):
                chunk = img.compressed_data[offset:offset + 32]
                hex_line = " ".join(f"{b:02x}" for b in chunk)
                f.write(f"{offset:04x}: {hex_line}\n")
        print(f"  Written: {hex_file}")

        # Write binary file
        bin_file = f"{slug}{suffix}.imgdata.bin"
        bin_path = os.path.join(dirpath, bin_file)
        with open(bin_path, "wb") as f:
            f.write(img.compressed_data)
        print(f"  Written: {bin_file}")


# --- Main ---

def parse_and_decode(filepath: str) -> list[ATTOperation]:
    """Full pipeline: parse BTSnoop → reassemble L2CAP → decode surplife."""
    frames = parse_btsnoop(filepath)
    ops = reassemble_att_operations(frames)
    for op in ops:
        decode_operation(op)
    return ops


def main():
    parser = argparse.ArgumentParser(
        description="Parse Surplife BLE captures (Apple PacketLogger BTSnoop format)"
    )
    parser.add_argument("file", help="BTSnoop .log capture file")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--filter", action="store_true", default=True,
        help="Output filtered text (default)"
    )
    mode.add_argument(
        "--decode", action="store_true",
        help="Output decoded, human-readable commands"
    )
    mode.add_argument(
        "--extract-images", action="store_true",
        help="Extract image payloads to files"
    )
    mode.add_argument(
        "--json", action="store_true",
        help="Output JSON format"
    )

    args = parser.parse_args()
    ops = parse_and_decode(args.file)

    if args.decode:
        output_decode(ops)
    elif args.extract_images:
        output_extract_images(ops, args.file)
    elif args.json:
        output_json(ops)
    else:
        output_filter(ops, args.file)


if __name__ == "__main__":
    main()
