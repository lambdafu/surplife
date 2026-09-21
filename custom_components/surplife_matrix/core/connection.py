"""SurplifeConnection: command sequencing over an abstract transport.

The transport interface (implemented by callers):

    async def write(self, packet: bytes) -> None
        Send one wrapped packet to the device (GATT write, no response).

    async def read_notifications(self) -> AsyncIterator[bytes]
        Async iterator of raw notification payloads (with the 8-byte
        response header). Must yield promptly after each write.

The connection wraps payloads with sequence numbers, sends the 0x0a prefix,
and matches responses. It contains no transport code, so it works with
bleak (CLI) and bleak-retry-connector / HA's shared scanner (integration).
"""

from __future__ import annotations

import asyncio
import logging

from . import messages as m
from .protocol import RESPONSE_TIMEOUT

log = logging.getLogger(__name__)


class SurplifeConnection:
    """Command layer over a transport.

    Args:
        transport: Object with ``async write(packet)`` and an async
            iterator of raw notification payloads (see module docstring).
        response_timeout: Seconds to wait for each ACK.
    """

    def __init__(self, transport, response_timeout: float = RESPONSE_TIMEOUT,
                 sleep=None):
        self._transport = transport
        self._seq = 0x0100
        self._timeout = response_timeout
        self._sleep = sleep or asyncio.sleep
        self._pending: asyncio.Queue[bytes] = asyncio.Queue()

    def feed_notification(self, raw: bytes) -> None:
        """Feed one raw notification (called by the transport's notify hook)."""
        self._pending.put_nowait(raw)

    # ── Low-level send/receive ───────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _wrap(self, inner: bytes) -> bytes:
        return m.wrap_packet(inner, self._next_seq())

    async def _send_raw(self, payload: bytes) -> None:
        """Send a raw payload (wrapped, no 0x0a prefix). No ACK."""
        await self._transport.write(self._wrap(payload))

    async def send(self, inner: bytes) -> None:
        """Send a command (0x0a prefixed). No ACK expected."""
        await self._send_raw(b"\x0a" + inner)

    async def _wait_for(self, match_fn, timeout: float | None = None) -> bytes:
        """Wait for a notification whose inner payload matches match_fn."""
        timeout = timeout or self._timeout
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"No matching response within {timeout:.1f}s")
            try:
                raw = await asyncio.wait_for(
                    self._pending.get(), timeout=remaining)
            except TimeoutError:
                raise TimeoutError(
                    f"No matching response within {timeout:.1f}s") from None
            inner = m.strip_response_header(raw)
            if match_fn(inner):
                return inner

    async def send_and_wait(self, inner: bytes, match_fn,
                            timeout: float | None = None) -> bytes:
        """Send a command and wait for the expected ACK."""
        await self.send(inner)
        return await self._wait_for(match_fn, timeout)

    # ── Init handshake ───────────────────────────────────────────────

    async def init(self, status_callback) -> dict:
        """Perform the device init handshake.

        Args:
            status_callback: Called with the parsed status dict from the
                first usable ea 81 response.

        Returns:
            Parsed status dict (power, speed, brightness, cols, rows).

        Retries once if the second ea 81 response is missing.
        """
        for attempt in (1, 2):
            try:
                await self._send_raw(m.cmd_init_trigger())
                await self.send(m.cmd_time_sync())
                await self.send(m.cmd_device_hash())
                await self._wait_for(m.match_status_15)
                status = await self._wait_for(m.match_status_16)
                parsed = m.parse_status(status)
                status_callback(parsed)
                return parsed
            except TimeoutError:
                if attempt == 1:
                    log.warning("Init incomplete (second ea 81 missing), retrying...")
                    continue
                raise
        raise TimeoutError("Init handshake failed")  # unreachable

    # ── Control commands ─────────────────────────────────────────────

    async def power(self, on: bool, status_callback) -> dict:
        """Set power; ACK is a 16 ea 81 status."""
        status = await self.send_and_wait(
            m.cmd_power(0x23 if on else 0x24), m.match_status_16)
        parsed = m.parse_status(status)
        status_callback(parsed)
        return parsed

    async def set_brightness(self, value: int, status_callback) -> dict:
        status = await self.send_and_wait(
            m.cmd_brightness(value), m.match_status_16)
        parsed = m.parse_status(status)
        status_callback(parsed)
        return parsed

    async def set_speed(self, value: int) -> None:
        await self.send(m.cmd_speed(value))

    async def show_clock(self, style: int = 0, hour_24: bool = True,
                         show_date: bool = False) -> None:
        await self.send(m.cmd_clock(style, hour_24, show_date))

    # ── Playlist ─────────────────────────────────────────────────────

    async def get_playlist_raw(self) -> tuple[list[tuple], list[tuple]]:
        """Read the device playlist (ea 0c + ea 0e). Returns raw entries."""
        resp = await self.send_and_wait(b"\xea\x0c", m.match_playlist)
        flags = m.parse_playlist_response(resp, 17, "ea 0c")
        resp = await self.send_and_wait(b"\xea\x0e", m.match_playlist_details)
        details = m.parse_playlist_response(resp, 19, "ea 0e")
        return flags, details

    async def set_playlist_raw(self, entries, editing: bool) -> None:
        await self.send_and_wait(
            m.playlist_response_bytes(entries, editing),
            m.match_playlist_set)

    async def set_playlist_details_raw(self, entries) -> None:
        await self.send_and_wait(
            m.playlist_details_bytes(entries),
            m.match_playlist_set_details)

    # ── Content upload ───────────────────────────────────────────────

    async def upload_content(self, cache_type: int, meta_bytes: bytes,
                             payload: bytes, activate_cmd: bytes,
                             c_hash: bytes,
                             segment_pace_s: float = 0.025,
                             paced_threshold: int = 8,
                             settle_s: float = 0.5,
                             sleep=None) -> None:
        """Upload content: cache check → frame header → segments → activate.

        The full sequence (trace 09):
          1. ea 05 cache check  → ACK (01 = cached: activate only)
          2. e0 30 frame header → ACK
          3. e0 32 segments     → paced for bulk uploads
          4. e0 33 end marker   → ACK, then settle
          5. e0 1e 00 x2        → screen prepare
          6. activate           → ACK
        """
        from .protocol import MAX_SEGMENT_SIZE

        doze = sleep or self._sleep

        cache_resp = await self.send_and_wait(
            m.cmd_cache_check(cache_type, c_hash), m.match_cache_check)
        cached = len(cache_resp) > 3 and cache_resp[3] == 0x01

        if cached:
            log.info("Cache hit")
            await self.send(activate_cmd)
            return

        content = m.build_content_blob(c_hash, meta_bytes, payload)
        total_size = len(content)

        await self.send_and_wait(
            m.cmd_frame_header(total_size), m.match_frame_header)

        num_segments = (total_size + MAX_SEGMENT_SIZE - 1) // MAX_SEGMENT_SIZE
        pace = segment_pace_s if num_segments > paced_threshold else 0.0
        for seg_idx in range(num_segments):
            if seg_idx and pace:
                await doze(pace)
            offset = seg_idx * MAX_SEGMENT_SIZE
            chunk = content[offset:offset + MAX_SEGMENT_SIZE]
            try:
                await self.send(m.cmd_segment(chunk, seg_idx))
            except Exception as err:
                # The device holds a partial blob in its upload buffer; the
                # next activation can fail until the next upload completes.
                log.error(
                    "Upload aborted at segment %d/%d (%s). The device may "
                    "refuse to activate content until the next upload "
                    "completes; if it stays frozen, power-cycle the display.",
                    seg_idx, num_segments, type(err).__name__)
                raise

        try:
            await self.send_and_wait(m.cmd_end_marker(), m.match_end_marker)
        except Exception:
            log.error(
                "Upload end marker not acknowledged — content blob is "
                "incomplete on the device; do not activate cached content "
                "from this upload.")
            raise
        await doze(settle_s)

        await self.send(m.cmd_screen_prepare())
        await self.send(m.cmd_screen_prepare())
        await self.send_and_wait(activate_cmd, m.match_activate)

class _PlaylistEntryView:
    """Lightweight playlist entry for raw connection-level playlist ops."""

    __slots__ = ("duration", "flag", "hash", "index")

    def __init__(self, flag: int, hash: bytes,
                 index: int = 0, duration: int = 0):
        self.flag = flag
        self.hash = hash
        self.index = index
        self.duration = duration
