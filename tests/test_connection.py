"""Tests for surplife_core.connection: command sequencing with a fake transport.

Covers init handshake + retry, control commands, playlist flows, the full
upload sequence (pacing, segments), and error paths — all without BLE.

The FakeTransport delivers scripted responses through a queue that the test
pumps explicitly between writes, keeping the async flow deterministic.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from surplife_core import messages as m
from surplife_core.connection import SurplifeConnection, _PlaylistEntryView
from surplife_core.protocol import MAX_SEGMENT_SIZE


class FakeTransport:
    """Scripted in-memory transport.

    Each scripted entry is delivered as one notification on the write that
    triggered it. Tests call ``await conn._wait_for(...)`` via the normal
    flow; scripted responses are pumped after each awaited write completes
    by ``drain()``.
    """

    def __init__(self):
        self.script: list = []          # bytes | None per triggering write
        self.written: list[bytes] = []
        self.conn: SurplifeConnection | None = None
        self.sleeps: list[float] = []

    async def write(self, packet: bytes) -> None:
        self.written.append(packet)
        # Deliver immediately if the connection is already wired. A script
        # entry may be a single response, None (silence), or a LIST of
        # responses delivered in order for one write.
        if self.conn is not None and self.script:
            entry = self.script.pop(0)
            if entry is None:
                return
            for resp in (entry if isinstance(entry, list) else [entry]):
                self.conn.feed_notification(resp)

    def drain(self) -> None:
        """Deliver any scripted responses that were queued before conn existed."""
        while self.script and self.conn is not None:
            resp = self.script.pop(0)
            if resp is not None:
                self.conn.feed_notification(resp)


# ── Response builders (wire format: 8-byte header + inner) ───────────

def _wrap_raw(body: bytes) -> bytes:
    return (b"\x00\x01\x80\x00"
            + bytes([(len(body) - 1) >> 8, (len(body) - 1) & 0xFF,
                     len(body) >> 8, len(body) & 0xFF]) + body)


def _resp(prefix: int, cmd0: int, cmd1: int, extra: bytes = b"") -> bytes:
    body = bytes([prefix, cmd0, cmd1]) + extra
    return _wrap_raw(body)


def _status(prefix: int = 0x16, power: int = 0x23, speed: int = 0x32,
            brightness: int = 0x64) -> bytes:
    body = bytes([
        prefix, 0xEA, 0x81, 0x00, 0x00, 0xDD, 0x06, power,
        0x75, 0x01, speed, 0x00, 0x00, 0x00, brightness, 0x00, 0x00,
        0x60, 0x10, 0x02, 0x00, 0xDD, 0x02, 0x63, 0x00,
    ])
    return _wrap_raw(body)


def _playlist_raw(cmd: int, entry_blobs: list[bytes]) -> bytes:
    body = bytearray([0x15, 0xEA, cmd, len(entry_blobs)])
    for blob in entry_blobs:
        body.extend(blob)
    return _wrap_raw(bytes(body))


# ── Init handshake ───────────────────────────────────────────────────

def test_init_success_applies_status():
    statuses = []
    t = FakeTransport()

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        task = asyncio.create_task(conn.init(lambda s: statuses.append(s)))
        await asyncio.sleep(0)
        t.conn.feed_notification(_status(0x15))
        await asyncio.sleep(0)
        t.conn.feed_notification(_status(0x16))
        parsed = await asyncio.wait_for(task, timeout=1)
        assert parsed["brightness"] == 0x64
        assert parsed["cols"] == 0x60
        assert statuses == [parsed]
        # sequence: init trigger raw (no 0x0a), then prefixed time + hash
        inners = [p[8:] for p in t.written]
        assert inners[0] == b"\x0c"
        assert inners[1].startswith(b"\x0a\x10\x14")
        assert inners[2] == b"\x0a\xea\x81\x8a\x8b\x59"

    asyncio.run(scenario())


def test_init_retries_when_second_response_missing():
    statuses = []
    t = FakeTransport()
    # attempt 1: 15 arrives on the hash write, then silence → 16-wait times out
    # attempt 2: both responses arrive on the hash write
    t.script = [None, None, _status(0x15),
                None, None, [_status(0x15), _status(0x16)]]

    async def scenario():
        conn = SurplifeConnection(t, response_timeout=0.3)
        t.conn = conn
        parsed = await asyncio.wait_for(
            conn.init(lambda s: statuses.append(s)), timeout=2)
        assert parsed["power"] is True
        assert len(statuses) == 1
        # two full handshakes were sent
        init_triggers = [p for p in t.written if p[8:] == b"\x0c"]
        assert len(init_triggers_written(t)) == 2

    def init_triggers_written(t):
        return [p for p in t.written if p[8:] == b"\x0c"]

    asyncio.run(scenario())


def test_init_fails_after_two_attempts():
    t = FakeTransport()
    # both attempts: 15 arrives, 16 never does
    t.script = [None, None, _status(0x15), None, None, _status(0x15)]

    async def scenario():
        conn = SurplifeConnection(t, response_timeout=0.3)
        t.conn = conn
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(conn.init(lambda s: None), timeout=2)

    asyncio.run(scenario())


# ── Control commands ─────────────────────────────────────────────────

def test_set_speed_sends_ea07():
    t = FakeTransport()

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        await conn.set_speed(80)
        assert t.written[-1][8:] == b"\x0a\xea\x07\x00\x50"

    asyncio.run(scenario())


def test_show_clock_sends_ea10():
    t = FakeTransport()

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        await conn.show_clock(style=3, hour_24=True, show_date=False)
        assert t.written[-1][8:] == b"\x0a\xea\x10\x03\x02\x00"

    asyncio.run(scenario())


def test_power_command_flow():
    statuses = []
    t = FakeTransport()

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        task = asyncio.create_task(
            conn.power(False, lambda s: statuses.append(s)))
        await asyncio.sleep(0)
        t.conn.feed_notification(_status(0x16, power=0x24))
        status = await asyncio.wait_for(task, timeout=1)
        assert status["power"] is False
        pkt = t.written[-1]
        assert pkt[8:13] == b"\x0a\xe0\x01\x00\x24"

    asyncio.run(scenario())


# ── Playlist flows ───────────────────────────────────────────────────

def test_get_playlist_raw_two_stage():
    t = FakeTransport()
    h0, h1 = bytes(range(16)), bytes(range(16, 32))
    t.script = [
        _playlist_raw(0x0C, [bytes([0x12]) + h0, bytes([0x00]) + h1]),
        _playlist_raw(0x0E, [bytes([1, 0, 10]) + h0, bytes([2, 0, 15]) + h1]),
    ]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        flags, details = await asyncio.wait_for(
            conn.get_playlist_raw(), timeout=1)
        assert flags == [(0x12, h0), (0x00, h1)]
        assert details == [(1, 0, 10, h0), (2, 0, 15, h1)]

    asyncio.run(scenario())


def test_set_playlist_raw_writes_full_command():
    t = FakeTransport()
    t.script = [_resp(0x15, 0xEA, 0x0B)]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        entries = [_PlaylistEntryView(flag=0x01, hash=b"\xa1" * 16)]
        await asyncio.wait_for(
            conn.set_playlist_raw(entries, editing=False), timeout=1)
        inner = t.written[-1][8:]
        assert inner[1:3] == b"\xea\x0b"
        assert inner[3] == 1          # count
        assert inner[4] == 0x00       # byte3: appending
        assert inner[5] == 0x01       # entry flag
        assert inner[6:22] == b"\xa1" * 16

    asyncio.run(scenario())


def test_set_playlist_details_raw_layout():
    t = FakeTransport()
    t.script = [_resp(0x15, 0xEA, 0x0D, b"")]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        entries = [_PlaylistEntryView(flag=0, hash=b"\xbb" * 16,
                                      index=3, duration=20)]
        await asyncio.wait_for(
            conn.set_playlist_details_raw(entries), timeout=1)
        inner = t.written[-1][8:]
        assert inner[1:3] == b"\xea\x0d"
        assert inner[3] == 1          # count
        assert inner[4] == 3          # index
        assert inner[5] == 0x00
        assert inner[6] == 20         # duration
        assert inner[7:23] == b"\xbb" * 16

    asyncio.run(scenario())


# ── Content upload ───────────────────────────────────────────────────

def test_upload_cache_hit_skips_upload():
    t = FakeTransport()
    t.script = [_resp(0x15, 0xEA, 0x05, b"\x01")]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        await asyncio.wait_for(
            conn.upload_content(0x01, b"{}", b"payload",
                                b"\xea\x07\x00\x50", b"\x00" * 16),
            timeout=1)
        inners = [p[8:] for p in t.written]
        assert inners[0][1:3] == b"\xea\x05"
        assert inners[-1] == b"\x0a\xea\x07\x00\x50"
        assert all(b"\xe0\x32" not in inner for inner in inners)

    asyncio.run(scenario())


def test_upload_cache_miss_full_sequence():
    t = FakeTransport()
    payload = bytes(96)
    meta = b"{}"
    c_hash = bytes(range(16))
    t.script = [
        _resp(0x15, 0xEA, 0x05, b"\x00"),
        _resp(0x15, 0xE0, 0x30, b"\x00\x01"),
        _resp(0x15, 0xE0, 0x33, b"\x00"),
        _resp(0x15, 0xEA, 0x24, b"\x00"),
    ]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        await asyncio.wait_for(
            conn.upload_content(0x01, meta, payload,
                                b"\xea\x07\x00\x50", c_hash,
                                settle_s=0.0),
            timeout=1)
        inners = [p[8:] for p in t.written]
        assert inners[1][1:3] == b"\xe0\x30"
        assert inners[2][1:3] == b"\xe0\x32"
        assert inners[3][1:3] == b"\xe0\x33"
        assert inners[4][1:3] == b"\xe0\x1e"
        assert inners[6][1:3] == b"\xea\x07"
        blob = m.build_content_blob(c_hash, meta, payload)
        # segment header: 0a + e032 + len2 + pad4 + idx1 = 9 bytes before the blob
        assert inners[2][10:10 + len(blob)] == blob

    asyncio.run(scenario())


def test_upload_multi_segment_chunks():
    t = FakeTransport()
    blob_overhead = 4 + 16 + 2 + 2
    payload = bytes(4 * MAX_SEGMENT_SIZE + 10 - blob_overhead)  # 5 segments
    t.script = [
        _resp(0x15, 0xEA, 0x05, b"\x00"),
        _resp(0x15, 0xE0, 0x30, b"\x00\x01"),
    ] + [_resp(0x15, 0xE0, 0x33, b"\x00"),
         _resp(0x15, 0xEA, 0x24, b"\x00")]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn
        await asyncio.wait_for(
            conn.upload_content(0x01, b"{}", payload,
                                b"\xea\x07\x00\x50", bytes(range(16)),
                                settle_s=0.0),
            timeout=2)
        segs = [p[8:] for p in t.written if p[8:11] == b"\x0a\xe0\x32"]
        assert len(segs) == 5
        assert len(segs[-1][10:]) == 10
        assert [s[9] for s in segs] == [0, 1, 2, 3, 4]

    asyncio.run(scenario())


def test_upload_pacing_counts_sleeps():
    t = FakeTransport()
    blob_overhead = 4 + 16 + 2 + 2
    payload = bytes(10 * MAX_SEGMENT_SIZE - blob_overhead)  # 10 segments
    t.script = [
        _resp(0x15, 0xEA, 0x05, b"\x00"),
        _resp(0x15, 0xE0, 0x30, b"\x00\x01"),
        _resp(0x15, 0xE0, 0x33, b"\x00"),
        _resp(0x15, 0xEA, 0x24, b"\x00"),
    ]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn

        async def counting_sleep(sec):
            t.sleeps.append(sec)

        await asyncio.wait_for(
            conn.upload_content(0x01, b"{}", payload,
                                b"\xea\x07\x00\x50", bytes(range(16)),
                                settle_s=0.0, sleep=counting_sleep),
            timeout=2)
        paced = [s for s in t.sleeps if s > 0]
        assert len(paced) == 9   # segments 1..9 paced, segment 0 not

    asyncio.run(scenario())


def test_upload_unpaced_below_threshold():
    t = FakeTransport()
    blob_overhead = 4 + 16 + 2 + 2
    payload = bytes(8 * MAX_SEGMENT_SIZE - blob_overhead)  # exactly 8 segments
    t.script = [
        _resp(0x15, 0xEA, 0x05, b"\x00"),
        _resp(0x15, 0xE0, 0x30, b"\x00\x01"),
        _resp(0x15, 0xE0, 0x33, b"\x00"),
        _resp(0x15, 0xEA, 0x24, b"\x00"),
    ]

    async def scenario():
        conn = SurplifeConnection(t)
        t.conn = conn

        async def counting_sleep(sec):
            t.sleeps.append(sec)

        await asyncio.wait_for(
            conn.upload_content(0x01, b"{}", payload,
                                b"\xea\x07\x00\x50", bytes(range(16)),
                                settle_s=0.0, sleep=counting_sleep),
            timeout=2)
        assert [s for s in t.sleeps if s > 0] == []

    asyncio.run(scenario())
