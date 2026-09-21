"""Command builders and response parsers for the Surplife BLE protocol.

Pure byte-level logic. The connection layer (connection.py) sequences these
over an injected transport.
"""

from __future__ import annotations

import datetime
import json
import uuid


def match(payload: bytes, *expected: int) -> bool:
    """Check whether the start of a response payload matches expected bytes."""
    if len(payload) < len(expected):
        return False
    return all(payload[i] == expected[i] for i in range(len(expected)))


# ── Packet framing ────────────────────────────────────────────────────

def wrap_packet(inner: bytes, seq: int) -> bytes:
    """Wrap an inner command payload in the Surplife packet header.

    [SEQ_HI][SEQ_LO][0x80][0x00][LEN-1 BE16][LEN BE16][inner]
    """
    plen = len(inner)
    header = bytes([
        (seq >> 8) & 0xFF, seq & 0xFF,
        0x80, 0x00,
        ((plen - 1) >> 8) & 0xFF, (plen - 1) & 0xFF,
        (plen >> 8) & 0xFF, plen & 0xFF,
    ])
    return header + inner


def strip_response_header(raw: bytes, header_size: int = 8) -> bytes:
    """Strip the 8-byte response wrapper, returning the inner payload."""
    return raw[header_size:] if len(raw) > header_size else raw


# ── Command builders (inner payloads, before 0x0a prefixing) ─────────

def cmd_init_trigger() -> bytes:
    """0x0c raw init trigger (sent without the 0x0a prefix)."""
    return b"\x0c"


def cmd_time_sync(dt: datetime.datetime | None = None) -> bytes:
    """10 14 device time sync with checksum."""
    if dt is None:
        dt = datetime.datetime.now()
    cmd = bytes([
        0x10, 0x14,
        dt.year - 2000, dt.month, dt.day,
        dt.hour, dt.minute, dt.second,
        dt.isoweekday(),
        0x00, 0x0f,
    ])
    return cmd + bytes([sum(cmd) & 0xFF])


def cmd_device_hash() -> bytes:
    """ea 81 device hash exchange."""
    return b"\xea\x81\x8a\x8b\x59"


def cmd_power(subcommand: int) -> bytes:
    """e0 01 power on (0x23) / off (0x24)."""
    return bytes([0xe0, 0x01, 0x00, subcommand]) + b"\x00" * 10


def cmd_brightness(value: int) -> bytes:
    """e0 01 brightness 0-100."""
    v = max(0, min(100, value))
    return (b"\xe0\x01\x00\x01\x00\x00"
            + bytes([v, 0x00, v, 0x00, 0x00, 0x00, 0x00, 0x00]))


def cmd_speed(value: int) -> bytes:
    """ea 07 GIF speed 1-100."""
    return b"\xea\x07\x00" + bytes([max(1, min(100, value))])


def cmd_clock(style: int = 0, hour_24: bool = True,
              show_date: bool = False) -> bytes:
    """ea 10 firmware clock (style 0-7)."""
    time_fmt = 0x02 if hour_24 else 0x01
    return bytes([0xea, 0x10, max(0, min(7, style)), time_fmt,
                  0x01 if show_date else 0x00])


def cmd_cache_check(cache_type: int, c_hash: bytes) -> bytes:
    """ea 05 cache check."""
    return bytes([0xea, 0x05, cache_type]) + c_hash


def cmd_frame_header(total_size: int) -> bytes:
    """e0 30 content-size declaration."""
    return b"\xe0\x30" + total_size.to_bytes(4, 'big') + b"\xea\x23"


def cmd_segment(chunk: bytes, seg_idx: int) -> bytes:
    """e0 32 data segment (chunk <= 490 bytes)."""
    return (b"\xe0\x32"
            + len(chunk).to_bytes(2, 'big')
            + b"\x00\x00\x00\x00"
            + bytes([seg_idx])
            + chunk)


def cmd_end_marker() -> bytes:
    """e0 33 upload end marker."""
    return b"\xe0\x33"


def cmd_screen_prepare() -> bytes:
    """e0 1e screen prepare (sent 2x before activation)."""
    return b"\xe0\x1e\x00"


def cmd_direct_draw(stream: bytes) -> bytes:
    """ea 11 direct draw: raw a2pl stream covering the full framebuffer."""
    return bytes([0xea, 0x11, 0x00, 0x00, 0x00, len(stream)]) + stream


def build_content_blob(c_hash: bytes, meta_bytes: bytes, payload: bytes) -> bytes:
    """Assemble the upload blob: header + hash + JSON length + JSON + payload."""
    return (b"\xea\x23\x01\x03"
            + c_hash
            + len(meta_bytes).to_bytes(2, 'big')
            + meta_bytes
            + payload)


# ── Metadata builders ────────────────────────────────────────────────

def image_metadata(content_type: str, frame_num: int, amt_length: int) -> bytes:
    """JSON metadata for a2pl content (types a, c, e handled by callers)."""
    meta = {
        "v": 1, "mant_type": 0, "enable_a2pl": 1,
        "layers": [{
            "nm": str(uuid.uuid4()), "type": content_type,
            "amt_pos": 0, "frame_num": frame_num,
            "amt_length": amt_length, "amt_fmt": 0,
        }],
        "all_file_type": content_type,
    }
    return json.dumps(meta, separators=(',', ':')).encode()


def gif_metadata(content_type: str, amt_length: int) -> bytes:
    """JSON metadata for raw GIF content (types b, d)."""
    meta = {
        "v": 1, "mant_type": 0, "enable_a2pl": 1,
        "layers": [{
            "nm": str(uuid.uuid4()), "type": content_type,
            "amt_pos": 0, "frame_num": 0,
            "amt_length": amt_length, "amt_fmt": 2,
        }],
        "all_file_type": content_type,
    }
    return json.dumps(meta, separators=(',', ':')).encode()


def text_metadata(text_type: str, frame_num: int, amt_length: int,
                  attr: dict) -> bytes:
    """JSON metadata for scrolling text (type e) with attr block."""
    meta = {
        "v": 1, "mant_type": 0, "enable_a2pl": 1,
        "layers": [{
            "nm": str(uuid.uuid4()), "type": text_type,
            "attr": [attr], "amt_pos": 0, "frame_num": frame_num,
            "amt_length": amt_length, "amt_fmt": 0,
        }],
        "all_file_type": text_type,
    }
    return json.dumps(meta, separators=(',', ':')).encode()


# ── Response parsers ─────────────────────────────────────────────────

def parse_status(payload: bytes) -> dict:
    """Extract device info from an ea 81 status response.

    Returns dict with power, speed, brightness, cols, rows.
    """
    status = {"power": True, "speed": 0, "brightness": 0,
              "cols": 96, "rows": 16}
    if len(payload) >= 19:
        status["power"] = payload[7] != 0x24
        status["speed"] = payload[10]
        status["brightness"] = payload[14]
        status["cols"] = payload[17]
        status["rows"] = payload[18]
    return status


def parse_playlist_response(resp: bytes, entry_size: int,
                            name: str) -> list[tuple]:
    """Split a 15 ea 0c/0e response into fixed-size entries.

    Each entry is returned as a tuple of its leading bytes followed by the
    16-byte hash: (flag, hash) for 17-byte entries, (index, flag1, duration,
    hash) for 19-byte entries.
    """
    count = resp[3] if len(resp) > 3 else 0
    body = resp[4:]
    if len(body) < count * entry_size:
        raise ValueError(
            f"Short {name} response: {count} entries announced, "
            f"{len(body)} payload bytes")
    entries = []
    for i in range(count):
        chunk = body[i * entry_size:(i + 1) * entry_size]
        head = chunk[:entry_size - 16]
        entries.append((*head, chunk[entry_size - 16:]))
    return entries


def parse_playlist_set(entries: list) -> bytes:
    """ea 0b set-playlist command bytes.

    Args:
        entries: Objects with .flag and .hash (16 bytes).
    """
    cmd = bytearray([0xea, 0x0b, len(entries)])
    return bytes(cmd)  # connection layer appends byte3 + entry payload


def playlist_response_bytes(entries: list, editing: bool) -> bytes:
    """Full ea 0b payload: [count][byte3][count x (flag, hash16)]."""
    cmd = bytearray([0xea, 0x0b, len(entries), 0x01 if editing else 0x00])
    for e in entries:
        cmd.append(e.flag)
        cmd.extend(e.hash)
    return bytes(cmd)


def playlist_details_bytes(entries: list) -> bytes:
    """Full ea 0d payload: [count][count x (index, 00, duration, hash16)]."""
    cmd = bytearray([0xea, 0x0d, len(entries)])
    for e in entries:
        cmd.extend([e.index, 0x00, e.duration])
        cmd.extend(e.hash)
    return bytes(cmd)


# ── Response matchers (device → app) ────────────────────────────────

def match_status_15(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x81)


def match_status_16(payload: bytes) -> bool:
    return match(payload, 0x16, 0xEA, 0x81)


def match_cache_check(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x05)


def match_frame_header(payload: bytes) -> bool:
    return match(payload, 0x15, 0xE0, 0x30)


def match_end_marker(payload: bytes) -> bool:
    return match(payload, 0x15, 0xE0, 0x33)


def match_activate(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x24)


def match_playlist(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x0C)


def match_playlist_details(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x0E)


def match_playlist_set(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x0B)


def match_playlist_set_details(payload: bytes) -> bool:
    return match(payload, 0x15, 0xEA, 0x0D)
