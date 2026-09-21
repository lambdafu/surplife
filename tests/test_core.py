"""Tests for surplife_core: pure protocol logic, no BLE required."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from surplife_core import messages as m
from surplife_core.a2pl import compress, decompress
from surplife_core.connection import SurplifeConnection
from surplife_core.content import build_a2pl_payload, content_hash, fix_gif, validate_gif
from surplife_core.media import ensure_canvas_bytes, frame_to_canvas
from surplife_core.protocol import (
    DISPLAY_COLS,
    DISPLAY_ROWS,
    FRAMEBUFFER_SIZE,
)

# ── Packet framing ───────────────────────────────────────────────────

def test_wrap_packet_layout():
    inner = b"\x0a\xe0\x01"
    pkt = m.wrap_packet(inner, 0x1234)
    assert pkt[:2] == b"\x12\x34"
    assert pkt[2:4] == b"\x80\x00"
    # LEN-1 BE16 = 2 (3 - 1)
    assert pkt[4:6] == b"\x00\x02"
    # LEN BE16 = 3
    assert pkt[6:8] == b"\x00\x03"
    assert pkt[8:] == inner


def test_wrap_packet_long():
    inner = bytes(300)
    pkt = m.wrap_packet(inner, 1)
    assert pkt[4:6] == b"\x01\x2b"   # 299
    assert pkt[6:8] == b"\x01\x2c"   # 300


# ── Command builders ─────────────────────────────────────────────────

def test_cmd_time_sync_checksum():
    import datetime
    dt = datetime.datetime(2026, 9, 20, 14, 30, 0)
    cmd = m.cmd_time_sync(dt)
    assert cmd[:2] == b"\x10\x14"
    assert cmd[2] == 26              # 2026 - 2000
    assert cmd[11] == sum(cmd[:11]) & 0xFF
    assert len(cmd) == 12


def test_cmd_brightness_clamps():
    assert m.cmd_brightness(0)[6] == 0
    assert m.cmd_brightness(100)[6] == 100
    assert m.cmd_brightness(150)[6] == 100


def test_cmd_power_subcommands():
    assert m.cmd_power(0x23)[3] == 0x23
    assert m.cmd_power(0x24)[3] == 0x24
    # convenience mapping used by the connection layer
    assert m.cmd_power(0x23 if True else 0x24)[3] == 0x23


def test_cmd_direct_draw_header():
    stream = b"\x00" * 10
    cmd = m.cmd_direct_draw(stream)
    assert cmd[:5] == b"\xea\x11\x00\x00\x00"
    assert cmd[5] == 10


# ── a2pl roundtrip ───────────────────────────────────────────────────

def test_a2pl_black_canvas_roundtrip():
    fb = bytes(FRAMEBUFFER_SIZE)
    assert decompress(compress(fb)) == fb


def test_a2pl_sparse_pixels_roundtrip():
    fb = bytearray(FRAMEBUFFER_SIZE)
    fb[0:2] = b"\x01\xff"
    fb[95 * 32:95 * 32 + 2] = b"\x1f\xff"
    assert decompress(compress(bytes(fb))) == bytes(fb)


def test_a2pl_full_random_roundtrip():
    import random
    random.seed(42)
    fb = bytes(random.randrange(256) for _ in range(FRAMEBUFFER_SIZE))
    assert decompress(compress(fb)) == bytes(fb)


def test_build_a2pl_payload_single_frame():
    fb = bytes(FRAMEBUFFER_SIZE)
    payload, frame_num, last = build_a2pl_payload(fb, DISPLAY_COLS)
    assert frame_num == 1
    assert last == DISPLAY_COLS
    assert payload[:4] == b"\x00\x00\x00\x00"


def test_build_a2pl_payload_multi_frame():
    cols = DISPLAY_COLS * 3 + 10
    fb = bytes(cols * DISPLAY_ROWS * 2)
    payload, frame_num, last = build_a2pl_payload(fb, cols)
    assert frame_num == 4
    assert last == 10


# ── Content hash ─────────────────────────────────────────────────────

def test_content_hash_deterministic():
    a = content_hash("a", b"payload")
    b = content_hash("a", b"payload")
    assert a == b
    assert a != content_hash("a", b"other")
    assert len(a) == 16


def test_content_hash_attr_order_insensitive():
    a = content_hash("e", b"x", {"fg_color": ["006464"], "speed": 50})
    b = content_hash("e", b"x", {"speed": 50, "fg_color": ["006464"]})
    assert a == b


# ── GIF validation ───────────────────────────────────────────────────

def test_validate_gif_rejects_non_gif():
    with pytest.raises(ValueError):
        validate_gif(b"NOTGIF")


def test_validate_gif_accepts_safe():
    data = open(os.path.join(os.path.dirname(__file__), "..",
                             "assets", "cat_flattened.gif"), "rb").read()
    report = validate_gif(data, strict=False)
    assert report["global_color_table"]
    assert report["frames"] >= 1


def test_validate_gif_strict_raises_on_violation(tmp_path):
    # A GIF with no frame delay (0) violates the envelope
    data = b"GIF89a" + bytes(90)
    # corrupt-ish; use real GIF instead: build via fix_gif from an image
    from PIL import Image
    img = Image.new("RGB", (96, 16), (255, 0, 0))
    buf = __import__("io").BytesIO()
    img.save(buf, format="GIF", save_all=True, duration=30, loop=0)
    fast_gif = buf.getvalue()
    with pytest.raises(ValueError):
        validate_gif(fast_gif, strict=True)


def test_fix_gif_produces_safe_envelope(tmp_path):
    import colorsys

    from PIL import Image
    frames = []
    for f in range(4):
        img = Image.new("RGB", (96, 16))
        for x in range(96):
            r, g, b = colorsys.hsv_to_rgb(x / 96 + f / 4, 1.0, 1.0)
            for y in range(16):
                img.putpixel((x, y), (int(r * 255), int(g * 255), int(b * 255)))
        frames.append(img)
    buf = __import__("io").BytesIO()
    frames[0].save(buf, format="GIF", save_all=True,
                   append_images=frames[1:], duration=30, loop=0)
    unsafe = buf.getvalue()
    assert not validate_gif(unsafe, strict=False)["ok"]
    fixed = fix_gif(unsafe)
    report = validate_gif(fixed, strict=False)
    assert report["ok"], report["violations"]
    assert report["min_frame_delay_cs"] >= 10


# ── Media helpers ────────────────────────────────────────────────────

def test_frame_to_canvas_exact_size():
    from PIL import Image
    img = Image.new("RGB", (96, 16), (255, 0, 0))
    fb = frame_to_canvas(img)
    assert len(fb) == FRAMEBUFFER_SIZE


def test_frame_to_canvas_wide_input_cropped():
    from PIL import Image
    img = Image.new("RGB", (200, 16), (0, 255, 0))
    fb = frame_to_canvas(img)
    assert len(fb) == FRAMEBUFFER_SIZE


def test_ensure_canvas_bytes_rejects_short():
    with pytest.raises(ValueError):
        ensure_canvas_bytes(b"\x00" * 100)


# ── Connection logic (fake transport) ────────────────────────────────

class FakeTransport:
    """In-memory transport for testing the connection layer."""

    def __init__(self, script: list[tuple[bytes | None, bytes]]):
        # script: list of (notify_payload | None, ...) — None = no response
        self.script = list(script)
        self.written: list[bytes] = []
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    async def write(self, packet: bytes) -> None:
        self.written.append(packet)

    def feed(self, raw: bytes) -> None:
        self._conn.feed_notification(raw)


def _status_response(prefix: int) -> bytes:
    body = bytes([prefix, 0xEA, 0x81, 0x00, 0x00, 0xDD, 0x06, 0x23,
                  0x75, 0x01, 0x64, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00,
                  0x60, 0x10, 0x02, 0x00, 0xDD, 0x02, 0x63, 0x00])
    return b"\x05\x01\x80\x00" + bytes([(len(body) - 1) >> 8, (len(body) - 1) & 0xFF,
                                        len(body) >> 8, len(body) & 0xFF]) + body


def _resp(prefix: int, cmd0: int, cmd1: int, extra: bytes = b"") -> bytes:
    body = bytes([prefix, cmd0, cmd1]) + extra
    header = b"\x00\x00\x80\x00" + bytes([(len(body) - 1) >> 8, (len(body) - 1) & 0xFF,
                                          len(body) >> 8, len(body) & 0xFF])
    return header + body


def test_connection_power_command_flow():
    statuses = []
    received = []

    async def scenario():
        transport = FakeTransport([])
        conn = SurplifeConnection(transport)
        transport._conn = conn

        task = asyncio.create_task(conn.power(True, lambda s: None))
        await asyncio.sleep(0)
        # device responds with a 16 ea 81
        conn.feed_notification(_status_response(0x16))
        status = await asyncio.wait_for(task, timeout=1)
        assert status["power"] is True
        assert status["brightness"] == 100
        # verify the written packet was wrapped + prefixed
        pkt = transport.written[-1]
        assert pkt[8] == 0x0A
        assert pkt[9:13] == b"\xe0\x01\x00\x23"

    asyncio.run(scenario())


def test_connection_upload_cache_hit():
    async def scenario():
        transport = FakeTransport([])
        conn = SurplifeConnection(transport)
        task = asyncio.create_task(conn.upload_content(
            0x01, b"{}", b"payload", b"\xea\x07\x00\x50", b"\x00" * 16))
        await asyncio.sleep(0)
        conn.feed_notification(_resp(0x15, 0xEA, 0x05, b"\x01"))
        await asyncio.sleep(0)
        await asyncio.wait_for(task, timeout=1)
        # cache hit: only the activate command after the cache check
        inners = [p[8:] for p in transport.written]
        assert b"\x0a\xea\x07\x00\x50" in inners[-1]

    asyncio.run(scenario())
