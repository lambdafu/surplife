"""Tests for surplife_core.messages: command builders, parsers, matchers.

Fixtures are taken from real device traces (trace 08 for playlist bytes).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from surplife_core import messages as m

# ── Packet framing ───────────────────────────────────────────────────

def test_strip_response_header_long():
    raw = b"\x00" * 8 + b"\x15\xea\x05\x00"
    assert m.strip_response_header(raw) == b"\x15\xea\x05\x00"


def test_strip_response_header_short_passthrough():
    # 8 bytes or fewer: returned as-is
    raw = b"\x15\xea\x05"
    assert m.strip_response_header(raw) == raw
    assert m.strip_response_header(b"") == b""


# ── Command builders ─────────────────────────────────────────────────

def test_cmd_init_trigger():
    assert m.cmd_init_trigger() == b"\x0c"


def test_cmd_device_hash():
    assert m.cmd_device_hash() == b"\xea\x81\x8a\x8b\x59"


def test_cmd_speed_clamps():
    assert m.cmd_speed(1) == b"\xea\x07\x00\x01"
    assert m.cmd_speed(100) == b"\xea\x07\x00\x64"
    assert m.cmd_speed(0) == b"\xea\x07\x00\x01"      # clamped up
    assert m.cmd_speed(200) == b"\xea\x07\x00\x64"    # clamped down


def test_cmd_clock_layout():
    # style 3, 24h, no date
    assert m.cmd_clock(3, hour_24=True, show_date=False) == \
        b"\xea\x10\x03\x02\x00"
    # style 0, 12h, date
    assert m.cmd_clock(0, hour_24=False, show_date=True) == \
        b"\xea\x10\x00\x01\x01"


def test_cmd_clock_style_clamped():
    assert m.cmd_clock(-5)[2] == 0x00
    assert m.cmd_clock(99)[2] == 0x07


def test_cmd_cache_check():
    c_hash = bytes(range(16))
    assert m.cmd_cache_check(0x04, c_hash) == b"\xea\x05\x04" + c_hash


def test_cmd_frame_header_be32():
    cmd = m.cmd_frame_header(98790)
    assert cmd[:2] == b"\xe0\x30"
    assert cmd[2:6] == (98790).to_bytes(4, "big")
    assert cmd[6:8] == b"\xea\x23"


def test_cmd_segment_layout():
    chunk = bytes(range(16))
    cmd = m.cmd_segment(chunk, 7)
    assert cmd[:2] == b"\xe0\x32"
    assert cmd[2:4] == b"\x00\x10"          # chunk length BE16
    assert cmd[4:8] == b"\x00\x00\x00\x00"  # padding
    assert cmd[8] == 7                      # segment index
    assert cmd[9:] == chunk


def test_cmd_end_marker_and_screen_prepare():
    assert m.cmd_end_marker() == b"\xe0\x33"
    assert m.cmd_screen_prepare() == b"\xe0\x1e\x00"


def test_build_content_blob_layout():
    c_hash = bytes(range(16))
    meta = b'{"v":1}'
    payload = b"\x01" * 10
    blob = m.build_content_blob(c_hash, meta, payload)
    assert blob[:4] == b"\xea\x23\x01\x03"
    assert blob[4:20] == c_hash
    assert blob[20:22] == len(meta).to_bytes(2, "big")
    assert blob[22:22 + len(meta)] == meta
    assert blob[22 + len(meta):] == payload


# ── Metadata builders ────────────────────────────────────────────────

def test_image_metadata_shape():
    meta = m.image_metadata("a", frame_num=3, amt_length=798)
    import json
    obj = json.loads(meta)
    assert obj["v"] == 1
    assert obj["mant_type"] == 0
    assert obj["enable_a2pl"] == 1
    layer = obj["layers"][0]
    assert layer["type"] == "a"
    assert layer["frame_num"] == 3
    assert layer["amt_length"] == 798
    assert layer["amt_fmt"] == 0
    assert obj["all_file_type"] == "a"


def test_gif_metadata_amt_fmt_2():
    import json
    obj = json.loads(m.gif_metadata("b", amt_length=378))
    layer = obj["layers"][0]
    assert layer["amt_fmt"] == 2
    assert layer["frame_num"] == 0
    assert layer["amt_length"] == 378


def test_text_metadata_carries_attr():
    import json
    attr = {"speed": 83, "fg_color": ["006464"], "fg_attr": 1}
    obj = json.loads(m.text_metadata("e", frame_num=9, amt_length=6258,
                                     attr=attr))
    assert obj["layers"][0]["attr"] == [attr]
    assert obj["all_file_type"] == "e"


# ── Status parsing ───────────────────────────────────────────────────

def _status_payload(power_byte: int) -> bytes:
    return bytes([
        0x15, 0xEA, 0x81, 0x00, 0x00, 0xDD, 0x06, power_byte,
        0x75, 0x01, 0x32, 0x00, 0x00, 0x00, 0x46, 0x00, 0x00,
        0x60, 0x10, 0x02, 0x00, 0xDD, 0x02, 0x63, 0x00,
    ])


def test_parse_status_on():
    s = m.parse_status(_status_payload(0x23))
    assert s["power"] is True
    assert s["speed"] == 0x32
    assert s["brightness"] == 0x46
    assert s["cols"] == 0x60
    assert s["rows"] == 0x10


def test_parse_status_off():
    s = m.parse_status(_status_payload(0x24))
    assert s["power"] is False


def test_parse_status_short_payload_defaults():
    s = m.parse_status(b"\x15\xea\x81")
    assert s["power"] is True
    assert s["cols"] == 96


# ── Playlist parsing (fixtures from trace 08) ────────────────────────

# Real response: 15 ea 0c 05 + 5 x (flag, hash16)
TRACE08_EA0C = bytes.fromhex(
    "15ea0c0512c6b0e82721e2362ecd5f81f8fbda71be0012815b122a196814b02ad01ead867dc800"
    "fc82d3a61ba7a62b8f9ad245f9b6aa5200ded64dc5887cd30f31be309648e04dc700e00651abc7"
    "4cffb43ffde1a87f73f60d")
# Real response: 15 ea 0e 05 + 5 x (index, 00, duration, hash16)
TRACE08_EA0E = bytes.fromhex(
    "15ea0e0501000ac6b0e82721e2362ecd5f81f8fbda71be02000a12815b122a196814b02ad01ead867dc803000afc82d3a61ba7a62b8f9ad245f9b6aa5204000aded64dc5887cd30f31be309648e04dc705000ae00651abc74cffb43ffde1a87f73f60d")


def test_parse_playlist_response_ea0c_trace08():
    entries = m.parse_playlist_response(TRACE08_EA0C, 17, "ea 0c")
    assert len(entries) == 5
    flags, hashes = zip(*entries)
    assert flags[0] == 0x12
    assert all(f == 0x00 for f in flags[1:])
    assert len(hashes[0]) == 16
    assert hashes[0].hex() == "c6b0e82721e2362ecd5f81f8fbda71be"


def test_parse_playlist_response_ea0e_trace08():
    entries = m.parse_playlist_response(TRACE08_EA0E, 19, "ea 0e")
    assert len(entries) == 5
    index, flag1, duration, h = entries[0]
    assert index == 1
    assert flag1 == 0x00
    assert duration == 0x0A
    assert h.hex() == "c6b0e82721e2362ecd5f81f8fbda71be"


def test_parse_playlist_response_short_raises():
    with pytest.raises(ValueError):
        m.parse_playlist_response(b"\x15\xea\x0c\x03\x00\x00", 17, "ea 0c")


def test_parse_playlist_response_zero_count():
    entries = m.parse_playlist_response(b"\x15\xea\x0c\x00", 17, "ea 0c")
    assert entries == []


# ── Playlist set/detail builders ─────────────────────────────────────

class _Entry:
    def __init__(self, flag=0, hash=b"\x00", index=0, duration=0):
        self.flag = flag
        self.hash = hash
        self.index = index
        self.duration = duration


def test_playlist_response_bytes_editing_flag():
    entries = [
        _Entry(flag=0x12, hash=b"\xa1" * 16),
        _Entry(flag=0x00, hash=b"\xb2" * 16),
    ]
    out = m.playlist_response_bytes(entries, editing=False)
    assert out[0] == 0xEA
    assert out[1] == 0x0B
    assert out[2] == 2
    assert out[3] == 0x00          # byte3: appending
    assert out[4] == 0x12          # first entry echoes its flag
    assert out[4 + 1:4 + 17] == b"\xa1" * 16

    out_edit = m.playlist_response_bytes(entries, editing=True)
    assert out_edit[3] == 0x01     # byte3: removing/reordering


def test_playlist_details_bytes_layout():
    entries = [
        _Entry(hash=bytes(range(16)), index=1, duration=10),
        _Entry(hash=bytes(range(16, 32)), index=2, duration=15),
    ]
    out = m.playlist_details_bytes(entries)
    assert out[:3] == b"\xea\x0d\x02"
    assert out[3] == 1
    assert out[4] == 0x00
    assert out[5] == 10
    assert out[6:22] == bytes(range(16))
    assert out[22:25] == b"\x02\x00\x0f"


def test_playlist_set_builder_ignored_placeholder():
    # parse_playlist_set returns the header stub; full command comes from
    # playlist_response_bytes
    assert m.parse_playlist_set([]) == b"\xea\x0b\x00"


# ── Matchers ─────────────────────────────────────────────────────────

def test_match_short_payload_safe():
    assert m.match(b"\x15", 0x15, 0xEA) is False
    assert m.match(b"", 0x15) is False


def test_match_exact():
    assert m.match(b"\x15\xea\x05\x00", 0x15, 0xEA, 0x05) is True
    assert m.match(b"\x16\xea\x05\x00", 0x15, 0xEA, 0x05) is False


def test_match_helpers():
    assert m.match_status_15(_status_payload(0x23))
    assert not m.match_status_16(_status_payload(0x23))
    assert m.match_status_16(bytes([0x16, 0xEA, 0x81]))
    assert m.match_cache_check(bytes([0x15, 0xEA, 0x05, 0x00]))
    assert m.match_frame_header(bytes([0x15, 0xE0, 0x30, 0x00, 0x01]))
    assert m.match_end_marker(bytes([0x15, 0xE0, 0x33, 0x00]))
    assert m.match_activate(bytes([0x15, 0xEA, 0x24, 0x00]))
    assert m.match_playlist(TRACE08_EA0C)
    assert m.match_playlist_details(TRACE08_EA0E)
    assert m.match_playlist_set(bytes([0x15, 0xEA, 0x0B, 0x00]))
    assert m.match_playlist_set_details(bytes([0x15, 0xEA, 0x0D, 0x00]))
