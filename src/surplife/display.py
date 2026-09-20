"""Surplife display connection and command interface."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from PIL import Image

from .a2pl import compress
from .color import image_to_framebuffer, rgb_to_meta_color
from .fonts import render_text
from .protocol import (
    BYTES_PER_PIXEL,
    DISPLAY_COLS,
    DISPLAY_ROWS,
    FRAMEBUFFER_SIZE,
    MAX_SEGMENT_SIZE,
    NOTIFY_UUID,
    WRITE_UUID,
)
from .scanner import discover_one, find_by_name

log = logging.getLogger(__name__)

# Default timeout for waiting on device responses.
RESPONSE_TIMEOUT = 5.0

# Inter-segment pacing for e0 32 uploads. The device's firmware write
# queue overflows when segments are written back-to-back in bulk: uploads
# of ~100+ segments complete but crash the device right after. The
# original monolithic script used 300ms; the app paces similarly. 25ms
# (~20 KB/s) has been stable for large GIFs; small uploads (few segments)
# are sent unpaced.
SEGMENT_PACE_S = 0.025
PACED_SEGMENTS_THRESHOLD = 8

# Settle time after the upload end marker before screen prepare/activation.
UPLOAD_SETTLE_S = 0.5

# Device response packets have an 8-byte wrapper header before the inner payload.
RESPONSE_HEADER_SIZE = 8

# Default per-entry display time for playlist entries, in seconds (trace 08).
PLAYLIST_DEFAULT_DURATION = 10


@dataclass
class PlaylistEntry:
    """One entry of the device playlist (carousel).

    Merged from the ea 0c response (flag) and the ea 0e response
    (index, duration). Entries only present in ea 0c have index 0 and
    duration 0.
    """

    hash: bytes                 # 16-byte content hash
    flag: int = 0x00            # ea 0c flag; 0x12 seen on one entry, 0x00 otherwise
    index: int = 0              # 1-based rotation position, 0 = not in rotation
    duration: int = 0           # display time in seconds


class SurplifeDisplay:
    """Connection to a Surplife LED matrix display over BLE.

    Usage::

        display = SurplifeDisplay()          # auto-discover
        display = SurplifeDisplay(address)   # specific device

        async with display:
            await display.show_image_file("photo.png")

    Or without context manager::

        await display.connect()
        await display.init()
        ...
        await display.disconnect()
    """

    def __init__(self, address: str | None = None):
        self.address = address
        self._ble_device: BLEDevice | None = None
        self._client: BleakClient | None = None
        self._seq = 0x0100
        self._notify_event = asyncio.Event()
        self._notify_queue: list[bytes] = []

        # Device info (populated after init from ea 81 status)
        self.name: str | None = None
        self.display_cols: int = DISPLAY_COLS
        self.display_rows: int = DISPLAY_ROWS
        self.brightness: int = 0
        self.speed: int = 0
        self.power: bool = True

    # ── Context manager ──────────────────────────────────────────────

    async def __aenter__(self) -> SurplifeDisplay:
        await self.connect()
        await self.init()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # ── Connection lifecycle ─────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the display over BLE.

        If no address was given, scans for the nearest Surplife device.
        """
        if self.address is None:
            dev = await discover_one()
            self.address = dev.address
            self._ble_device = dev.ble_device
            log.info("Auto-discovered %s", dev)
        elif self._ble_device is None:
            if _looks_like_ble_address(self.address):
                log.debug("Scanning for address %s...", self.address)
                self._ble_device = await BleakScanner.find_device_by_address(
                    self.address, timeout=10.0,
                )
                if self._ble_device is None:
                    raise RuntimeError(f"Device {self.address} not found")
            else:
                dev = await find_by_name(self.address)
                self.address = dev.address
                self._ble_device = dev.ble_device
                log.info("Resolved %s", dev)

        self._client = BleakClient(self._ble_device, timeout=15.0)
        await self._client.connect()
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)
        self.name = self._ble_device.name
        log.info("Connected to %s (%s)", self.name, self.address)

    async def disconnect(self) -> None:
        """Disconnect from the display."""
        if self._client and self._client.is_connected:
            await self._client.disconnect()
            log.info("Disconnected")
        self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Init handshake ───────────────────────────────────────────────

    async def init(self) -> None:
        """Perform the device init handshake.

        Steps (from trace 01):
          1. 0x0c raw (no 0x0a prefix) — init trigger, no ACK
          2. 10 14 time sync — no ACK
          3. ea 81 device hash — ACK: 15 ea 81, then 16 ea 81 (~1.5s later)
        Commands sent before the second ea 81 response don't take effect.

        Occasionally the device misses the second response (observed after
        heavy content playback); one retry of the whole handshake usually
        recovers it.
        """
        for attempt in (1, 2):
            try:
                # Step 1: init trigger (no ACK)
                await self._send_raw(b"\x0c")

                # Step 2: time sync (no ACK)
                await self.set_time()

                # Step 3: device hash exchange (two ACKs)
                await self._send(b"\xea\x81\x8a\x8b\x59")
                await self._wait_for(lambda r: _match(r, 0x15, 0xea, 0x81))
                status = await self._wait_for(
                    lambda r: _match(r, 0x16, 0xea, 0x81))
                self._parse_status(status)
                log.info("Init complete (time synced: %s)",
                         datetime.datetime.now().strftime("%H:%M:%S"))
                return
            except TimeoutError:
                if attempt == 1:
                    log.warning("Init incomplete (second ea 81 missing), retrying...")
                    self._notify_queue.clear()
                    await asyncio.sleep(1.0)
                    continue
                raise

    @property
    def status_str(self) -> str:
        """Human-readable device status summary."""
        power_str = "on" if self.power else "off"
        return (f"power={power_str} brightness={self.brightness}%"
                f" speed={self.speed} {self.display_cols}x{self.display_rows}")

    def _parse_status(self, payload: bytes) -> None:
        """Extract device info from an ea 81 status response."""
        if len(payload) >= 19:
            self.power = payload[7] != 0x24
            self.speed = payload[10]
            self.brightness = payload[14]
            self.display_cols = payload[17]
            self.display_rows = payload[18]

    # ── Low-level send/receive ───────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _wrap(self, inner: bytes) -> bytes:
        """Wrap inner payload in the Surplife packet header."""
        seq = self._next_seq()
        plen = len(inner)
        header = bytes([
            (seq >> 8) & 0xFF, seq & 0xFF,
            0x80, 0x00,
            ((plen - 1) >> 8) & 0xFF, (plen - 1) & 0xFF,
            (plen >> 8) & 0xFF, plen & 0xFF,
        ])
        return header + inner

    async def _send_raw(self, payload: bytes) -> None:
        """Send a raw payload (wrapped with header, no 0x0a prefix). No ACK."""
        packet = self._wrap(payload)
        log.debug(">> %s", packet.hex())
        await self._client.write_gatt_char(WRITE_UUID, packet, response=False)

    async def _send(self, inner: bytes) -> None:
        """Send a command (0x0a prefixed). No ACK expected."""
        await self._send_raw(b"\x0a" + inner)

    async def _send_and_wait(
        self,
        inner: bytes,
        match: Callable[[bytes], bool],
        timeout: float = RESPONSE_TIMEOUT,
    ) -> bytes:
        """Send a command and wait for the expected ACK."""
        self._notify_queue.clear()
        await self._send(inner)
        return await self._wait_for(match, timeout=timeout)

    def _on_notify(self, _sender: int, data: bytearray) -> None:
        """BLE notification callback."""
        log.debug("<< %s", data.hex())
        self._notify_queue.append(bytes(data))
        self._notify_event.set()

    async def _wait_for(
        self,
        match: Callable[[bytes], bool],
        timeout: float = RESPONSE_TIMEOUT,
    ) -> bytes:
        """Wait for a notification matching a predicate.

        Returns the matching inner payload (header stripped).
        Raises TimeoutError if no match within timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for i, raw in enumerate(self._notify_queue):
                inner = raw[RESPONSE_HEADER_SIZE:] if len(raw) > RESPONSE_HEADER_SIZE else raw
                if match(inner):
                    self._notify_queue.pop(i)
                    return inner

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"No matching response within {timeout:.1f}s"
                )
            self._notify_event.clear()
            try:
                await asyncio.wait_for(
                    self._notify_event.wait(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"No matching response within {timeout:.1f}s"
                ) from None

    # ── Commands ─────────────────────────────────────────────────────
    #
    # ACK reference (verified against traces):
    #   e0 01 (brightness, power)  → ACK: 16 ea 81 [status]   (trace 02, 03)
    #   ea 05 (cache check)        → ACK: 15 ea 05 [00|01]    (trace 09)
    #   e0 30 (frame header)       → ACK: 15 e0 30 00 01      (trace 09)
    #   e0 33 (end marker)         → ACK: 15 e0 33 00         (trace 09)
    #   ea 06 (activate image)     → ACK: 15 ea 24 00         (trace 09)
    #   ea 24 (activate text)      → ACK: 15 ea 24 00         (trace 13)
    #   ea 81 (init hash)          → ACK: 15 ea 81, then 16 ea 81  (trace 01)
    #
    #   ea 07 (speed)              → no ACK  (trace 05)
    #   ea 10 (clock)              → no ACK  (trace 93)
    #   e0 32 (data segment)       → no ACK  (trace 09)
    #   e0 1e (screen prepare)     → no ACK  (trace 09)
    #   10 14 (time sync)          → no ACK  (trace 01)

    async def power_on(self) -> None:
        """Power on the display. ACK: 16 ea 81."""
        await self._set_power(0x23)

    async def power_off(self) -> None:
        """Power off the display. ACK: 16 ea 81."""
        await self._set_power(0x24)

    async def _set_power(self, subcommand: int) -> None:
        status = await self._send_and_wait(
            bytes([0xe0, 0x01, 0x00, subcommand]) + b"\x00" * 10,
            match=lambda r: _match(r, 0x16, 0xea, 0x81),
        )
        self._parse_status(status)
        label = "ON" if subcommand == 0x23 else "OFF"
        log.info("Power %s  [%s]", label, self.status_str)

    async def set_brightness(self, value: int) -> None:
        """Set brightness (0-100). ACK: 16 ea 81."""
        v = max(0, min(100, value))
        status = await self._send_and_wait(
            b"\xe0\x01\x00\x01\x00\x00"
            + bytes([v, 0x00, v, 0x00, 0x00, 0x00, 0x00, 0x00]),
            match=lambda r: _match(r, 0x16, 0xea, 0x81),
        )
        self._parse_status(status)
        log.info("Brightness: %d%%  [%s]", v, self.status_str)

    async def set_speed(self, value: int) -> None:
        """Set animation speed (1-100). No ACK.

        Sends ea 07 which is the GIF speed command. Observed side effects
        on non-GIF content: scrolling text switches to blinking, scrolling
        images become static. Likely only appropriate for GIF animations.
        For text and images, speed is set during upload (JSON metadata or
        ea 06 activation command).
        """
        v = max(1, min(100, value))
        await self._send(b"\xea\x07\x00" + bytes([v]))
        log.info("Speed: %d", v)

    async def show_clock(self, style: int = 0, hour_24: bool = True,
                         show_date: bool = False) -> None:
        """Show a firmware-rendered clock. No ACK."""
        s = max(0, min(7, style))
        time_fmt = 0x02 if hour_24 else 0x01
        date_flag = 0x01 if show_date else 0x00
        await self._send(bytes([0xea, 0x10, s, time_fmt, date_flag]))
        fmt_str = "24h" if hour_24 else "12h"
        date_str = " +date" if show_date else ""
        log.info("Clock: style=%d %s%s", s, fmt_str, date_str)

    async def set_time(self, dt: datetime.datetime | None = None) -> None:
        """Sync the device clock. No ACK."""
        if dt is None:
            dt = datetime.datetime.now()
        cmd = bytes([
            0x10, 0x14,
            dt.year - 2000, dt.month, dt.day,
            dt.hour, dt.minute, dt.second,
            dt.isoweekday(),
            0x00, 0x0f,
        ])
        cmd += bytes([sum(cmd) & 0xFF])
        await self._send(cmd)
        log.info("Time synced: %s", dt.strftime("%Y-%m-%d %H:%M:%S"))

    # ── Playlist / carousel ────────────────────────────────────────
    #
    # The device keeps a playlist of cached content and cycles through it.
    # There are no incremental commands: the app reads the playlist,
    # modifies it locally and writes the whole list back (traces 07, 08).
    #
    #   ea 0c  → 15 ea 0c [count] [count × (flag, hash16)]
    #   ea 0e  → 15 ea 0e [count] [count × (index, 00, duration, hash16)]
    #   ea 0b [count] [byte3] [count × (flag, hash16)]     → ACK 15 ea 0b 00
    #   ea 0d [count] [count × (index, 00, duration, hash16)] → ACK 15 ea 0d 00
    #
    # Responses are single notifications; a long playlist could exceed the
    # negotiated MTU, which has not been observed (max 5 entries in traces).

    async def get_playlist(self) -> list[PlaylistEntry]:
        """Read the device playlist (ea 0c + ea 0e), in playlist order."""
        resp = await self._send_and_wait(
            b"\xea\x0c", match=lambda r: _match(r, 0x15, 0xea, 0x0c))
        entries = [
            PlaylistEntry(hash=bytes(h), flag=flag)
            for flag, h in _parse_playlist_response(resp, 17, "ea 0c")
        ]
        resp = await self._send_and_wait(
            b"\xea\x0e", match=lambda r: _match(r, 0x15, 0xea, 0x0e))
        details = {
            bytes(h): (index, duration)
            for index, _flag1, duration, h in _parse_playlist_response(resp, 19, "ea 0e")
        }
        for e in entries:
            if e.hash in details:
                e.index, e.duration = details[e.hash]
        return entries

    async def set_playlist(self, entries: list[PlaylistEntry],
                           editing: bool) -> None:
        """Write the playlist (ea 0b). ACK: 15 ea 0b.

        Args:
            entries: Full playlist in the desired order; only ``hash`` and
                ``flag`` are sent.
            editing: False when appending to the list (byte3 = 0x00,
                trace 07), True when removing or reordering (byte3 = 0x01,
                trace 08).
        """
        cmd = bytearray([0xea, 0x0b, len(entries), 0x01 if editing else 0x00])
        for e in entries:
            cmd.append(e.flag)
            cmd.extend(e.hash)
        await self._send_and_wait(
            bytes(cmd), match=lambda r: _match(r, 0x15, 0xea, 0x0b))
        log.info("Playlist set: %d entries", len(entries))

    async def set_playlist_details(self, entries: list[PlaylistEntry]) -> None:
        """Write rotation positions and durations (ea 0d). ACK: 15 ea 0d.

        Sends ``index`` (1-based, 0 = not in rotation) and ``duration``
        (seconds) for every entry.
        """
        cmd = bytearray([0xea, 0x0d, len(entries)])
        for e in entries:
            cmd.extend([e.index, 0x00, e.duration])
            cmd.extend(e.hash)
        await self._send_and_wait(
            bytes(cmd), match=lambda r: _match(r, 0x15, 0xea, 0x0d))
        log.info("Playlist details set: %d entries", len(entries))

    async def playlist_add(self, c_hash: bytes,
                           duration: int = PLAYLIST_DEFAULT_DURATION) -> list[PlaylistEntry]:
        """Append cached content to the playlist and put it in rotation.

        The content must already be uploaded (``show_image``, ``show_gif``
        and ``show_text`` return the hash). If the hash is already in the
        playlist, nothing is sent.

        Returns the resulting playlist.
        """
        entries = await self.get_playlist()
        if any(e.hash == c_hash for e in entries):
            log.info("Playlist: %s already present", c_hash.hex())
            return entries
        # The app appends the new entry with flag 0x01 and keeps the flags
        # it read for the existing entries (trace 07).
        new = PlaylistEntry(hash=c_hash, flag=0x01, duration=duration,
                            index=max((e.index for e in entries), default=0) + 1)
        entries.append(new)
        await self.set_playlist(entries, editing=False)
        # Trace 07 has no ea 0d after the add; the new entry then reads back
        # with index 0 / duration 0 (trace 08), so put it in rotation
        # explicitly. Existing entries are written back unchanged.
        await self.set_playlist_details(entries)
        return entries

    async def playlist_remove(self, c_hash: bytes) -> list[PlaylistEntry]:
        """Remove content from the playlist. Returns the resulting playlist."""
        entries = await self.get_playlist()
        remaining = [e for e in entries if e.hash != c_hash]
        if len(remaining) == len(entries):
            raise ValueError(f"Hash {c_hash.hex()} not in playlist")
        return await self.playlist_set(remaining)

    async def playlist_set(self, entries: list[PlaylistEntry],
                           duration: int = PLAYLIST_DEFAULT_DURATION) -> list[PlaylistEntry]:
        """Replace the playlist with ``entries`` in the given order.

        Used for removing and reordering. Rotation positions are assigned
        from the list order; entries without a duration get ``duration``.
        Returns the resulting playlist.
        """
        # When editing, the app writes flag 0x00 for every entry (trace 08).
        for e in entries:
            e.flag = 0x00
        await self.set_playlist(entries, editing=True)
        if entries:
            _renumber(entries, duration)
            await self.set_playlist_details(entries)
        return entries

    # ── Waveform streaming ─────────────────────────────────────────

    async def configure_waveform_raw(self, data: bytes) -> None:
        """Send a raw e1 05 waveform config command. No ACK.

        Args:
            data: Full command bytes after e1 05 (i.e. bytes [2] onwards).
        """
        await self._send(b"\xe1\x05" + data)
        log.info("Waveform config (raw): %s", data.hex())

    async def configure_waveform(self, style: int = 1,
                                 colors: list[tuple[int, int, int]] | None = None) -> None:
        """Configure waveform rendering style. No ACK.

        Uses known-good byte patterns from trace 97 for each style.

        Args:
            style: Visual style (1, 2, 3, 4, 7, 8, 12, 13).
            colors: Optional list of RGB colors for the gradient.
        """
        # Known-good patterns: [2]=00, [3]=bright, [4]=style, [5], [6], [7]=speed
        # From trace 97 — only byte [4] varies significantly
        # Known-good byte patterns from trace 97.
        # Untested styles (5,6,9,10,11) use the same pattern as nearby styles.
        style_params = {
            1:  (0x64, 0x00, 0x05, 0x64),  # [3],[5],[6],[7]
            2:  (0x64, 0x00, 0x05, 0x64),
            3:  (0x64, 0x00, 0x00, 0x64),
            4:  (0x64, 0x00, 0x00, 0x64),
            7:  (0x64, 0x00, 0x00, 0x64),
            8:  (0x64, 0x00, 0x00, 0x64),
            12: (0x64, 0x00, 0x00, 0x64),
            13: (0x64, 0x00, 0x00, 0x64),
        }
        if style not in style_params:
            raise ValueError(f"Unknown waveform style {style}. Known: 1-13")

        bright, b5, b6, speed = style_params[style]
        cmd = bytearray([0x00, bright, style, b5, b6, speed])
        cmd.extend(b'\x00' * 17)  # padding bytes [8-24]

        # Color block
        if colors:
            from .color import rgb_to_meta_color
            entries = []
            for r, g, b in colors:
                mc = rgb_to_meta_color(r, g, b)
                entries.append(bytes([0xa1, int(mc[:2], 16), int(mc[2:4], 16), int(mc[4:6], 16)]))
            cmd.extend(b"\xa1\x00\x00\x00")
            cmd.append(len(entries))
            for e in entries:
                cmd.extend(e)
        else:
            # Default rainbow from trace 97
            cmd.extend(b"\xa1\x00\x00\x00\x04\xa1\x00\x64\x64\xa1\x12\x64\x64\xa1\x1e\x64\x64\xa1\x3c\x64\x64")

        await self._send(b"\xe1\x05" + bytes(cmd))
        log.info("Waveform config: style=%d", style)

    async def send_waveform(self, amplitudes: bytes, style_id: int = 0x01) -> None:
        """Send one waveform frame (96 amplitude bytes). No ACK.

        The style_id in the frame header should match the style set via
        configure_waveform(). The device uses it to select rendering mode.

        Args:
            amplitudes: 96 bytes, values 0-100, one per display column.
            style_id: Style ID matching the configured waveform style.
        """
        if len(amplitudes) != 96:
            raise ValueError(f"Expected 96 amplitude bytes, got {len(amplitudes)}")
        await self._send(bytes([0xea, 0x0f, style_id, 0x00]) + amplitudes)

    # ── Content upload ───────────────────────────────────────────────

    async def show_image(self, img: Image.Image, speed: int = 50,
                         effect: int = 1, force: bool = False) -> bytes:
        """Upload and display a static or wide scrolling image.

        Args:
            img: PIL Image (any size/mode).
            speed: Animation speed 1-100 (for scrolling wide images).
            effect: Display effect 1-10 (1=static, 2=scroll-r, ...).
            force: Bypass device cache by using a random content hash.

        Returns:
            16-byte content hash.
        """
        fb, num_cols = image_to_framebuffer(img)
        payload, frame_num, last_frame_cols = _build_a2pl_payload(fb, num_cols)

        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": str(uuid.uuid4()), "type": "a", "amt_pos": 0,
                "frame_num": frame_num, "amt_length": len(payload), "amt_fmt": 0,
            }],
            "all_file_type": "a",
        }, separators=(',', ':'))

        c_hash = os.urandom(16) if force else _content_hash("a", payload)
        activate = bytes([0xea, 0x06, 0x00, speed, effect])
        await self._upload_content(0x04, meta.encode(), payload, activate, c_hash)

        log.info("Image sent (%dx16, %d frame(s), %dB, effect=%d, speed=%d)",
                 num_cols, frame_num, len(payload), effect, speed)
        return c_hash

    async def show_image_file(self, path: str, **kwargs) -> bytes:
        """Load an image file and display it. See show_image() for kwargs."""
        return await self.show_image(Image.open(path), **kwargs)

    async def show_gif(self, gif_data: bytes, speed: int = 50,
                       force: bool = False,
                       check: bool = True) -> bytes:
        """Upload and play a GIF animation.

        Args:
            gif_data: Raw GIF file bytes (should be 96x16).
            speed: Animation speed 1-100.
            force: Bypass device cache.
            check: Validate against the device-safe envelope first
                (see validate_gif(); unsafe GIFs can crash the device).

        Returns:
            16-byte content hash.
        """
        if check:
            report = validate_gif(gif_data)
            log.debug("GIF envelope check: %s", report)
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": str(uuid.uuid4()), "type": "b", "amt_pos": 0,
                "frame_num": 0, "amt_length": len(gif_data), "amt_fmt": 2,
            }],
            "all_file_type": "b",
        }, separators=(',', ':'))

        c_hash = os.urandom(16) if force else _content_hash("b", gif_data)
        activate = bytes([0xea, 0x07, 0x00, speed])
        await self._upload_content(0x01, meta.encode(), gif_data, activate, c_hash)

        log.info("GIF sent (%dB, speed=%d)", len(gif_data), speed)
        return c_hash

    async def show_gif_file(self, path: str, **kwargs) -> bytes:
        """Load a GIF file and play it. See show_gif() for kwargs."""
        with open(path, 'rb') as f:
            data = f.read()
        if data[:3] != b'GIF':
            raise ValueError(f"Not a GIF file: {path}")
        return await self.show_gif(data, **kwargs)

    async def show_text(self, text: str,
                        colors: list[tuple[int, int, int]] | None = None,
                        speed: int = 50, font_size: int = 16,
                        force: bool = False) -> bytes:
        """Render and upload scrolling text.

        Text is rendered as a bitmask; the device applies colors from the
        JSON metadata. Single color = solid, multiple = gradient.

        Args:
            text: Text string to display.
            colors: List of RGB tuples for text color. Single = solid,
                    multiple = gradient. Default: [(255, 0, 0)] (red).
            speed: Scroll speed 1-100.
            font_size: Font size in pixels (default 16 = full height).
            force: Bypass device cache.

        Returns:
            16-byte content hash.
        """
        if colors is None:
            colors = [(255, 0, 0)]

        fb, num_cols = render_text(text, fg=colors[0], font_size=font_size)
        payload, frame_num, last_frame_cols = _build_a2pl_payload(fb, num_cols)

        # Build color metadata
        fg_colors = [rgb_to_meta_color(r, g, b) for r, g, b in colors]
        fg_attr = 0 if len(colors) > 1 else 1  # 0=gradient, 1=solid

        attr = {
            "speed": speed,
            "effect": "b" if frame_num > 1 else "a",
            "pause_t": 1,
            "bg_color": "000000",
            "fg_color": fg_colors,
            "fg_attr": fg_attr,
            "fg_dir": 1,
        }
        if frame_num > 1:
            attr["last_word_frame"] = last_frame_cols

        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": str(uuid.uuid4()), "type": "e",
                "attr": [attr],
                "amt_pos": 0, "frame_num": frame_num,
                "amt_length": len(payload), "amt_fmt": 0,
            }],
            "all_file_type": "e",
        }, separators=(',', ':'))

        c_hash = os.urandom(16) if force else _content_hash("e", payload, attr)
        await self._upload_content(0x02, meta.encode(), payload, b"\xea\x24", c_hash)

        log.info("Text '%s' sent (%d cols, %d frame(s), %dB, speed=%d)",
                 text[:30], num_cols, frame_num, len(payload), speed)
        return c_hash

    # ── Graffiti / direct pixel drawing ─────────────────────────────
    #
    # Two mechanisms (traces 14–62):
    #   - Type "c"/"d" uploads (show_graffiti, show_graffiti_gif): content
    #     goes into the device cache and is activated with ea 09 / ea 0a.
    #   - Direct draw (draw_frame): ea 11 with a raw a2pl stream covering
    #     the full framebuffer. The device renders it immediately; every
    #     draw replaces the whole canvas (trace 60 — the app accumulates
    #     pixels client-side and redraws everything on each stroke).

    async def show_graffiti(self, img: Image.Image,
                            force: bool = False) -> bytes:
        """Upload and display a static graffiti image (content type "c").

        Functionally identical to a static image (show_image with
        effect=1), but cached under the graffiti category.

        Args:
            img: PIL Image (any size/mode).
            force: Bypass device cache by using a random content hash.

        Returns:
            16-byte content hash.
        """
        fb, num_cols = image_to_framebuffer(img)
        payload, frame_num, _ = _build_a2pl_payload(fb, num_cols)

        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": str(uuid.uuid4()), "type": "c", "amt_pos": 0,
                "frame_num": 1, "amt_length": len(payload), "amt_fmt": 0,
            }],
            "all_file_type": "c",
        }, separators=(',', ':'))

        c_hash = os.urandom(16) if force else _content_hash("c", payload)
        await self._upload_content(
            0x00, meta.encode(), payload,
            b"\xea\x09\x00\x50\x01", c_hash,
        )
        log.info("Graffiti sent (%dx16, %dB)", num_cols, len(payload))
        return c_hash

    async def show_graffiti_file(self, path: str, **kwargs) -> bytes:
        """Load an image file and display it as graffiti. See show_graffiti()."""
        return await self.show_graffiti(Image.open(path), **kwargs)

    async def show_graffiti_gif(self, gif_data: bytes,
                                force: bool = False) -> bytes:
        """Upload and play a graffiti animation (content type "d").

        Functionally identical to show_gif() (raw GIF upload), but cached
        under the graffiti-animation category and activated with ea 0a
        instead of ea 07 (trace 62).

        Args:
            gif_data: Raw GIF file bytes (should be 96x16).
            force: Bypass device cache.

        Returns:
            16-byte content hash.
        """
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": str(uuid.uuid4()), "type": "d", "amt_pos": 0,
                "frame_num": 0, "amt_length": len(gif_data), "amt_fmt": 2,
            }],
            "all_file_type": "d",
        }, separators=(',', ':'))

        c_hash = os.urandom(16) if force else _content_hash("d", gif_data)
        await self._upload_content(
            0x02, meta.encode(), gif_data,
            b"\xea\x0a\x00\x50\x01", c_hash,
        )
        log.info("Graffiti animation sent (%dB)", len(gif_data))
        return c_hash

    async def show_graffiti_gif_file(self, path: str, **kwargs) -> bytes:
        """Load a GIF file and play it as a graffiti animation."""
        with open(path, 'rb') as f:
            data = f.read()
        if data[:3] != b'GIF':
            raise ValueError(f"Not a GIF file: {path}")
        return await self.show_graffiti_gif(data, **kwargs)

    async def draw_frame(self, fb: bytes) -> None:
        """Direct draw: replace the entire display contents immediately.

        Sends ea 11 with the canvas as a raw a2pl stream (no header,
        no offset table). No ACK. Trailing literal bytes are guaranteed
        by compress() (firmware off-by-one bug, see A2PL.md).

        Args:
            fb: Full framebuffer (3072 bytes, column-major, 16-bit HSV).
        """
        if len(fb) != FRAMEBUFFER_SIZE:
            raise ValueError(
                f"Framebuffer must be {FRAMEBUFFER_SIZE} bytes, got {len(fb)}")
        stream = compress(fb)
        if len(stream) > 255:
            raise ValueError(
                f"Compressed canvas is {len(stream)} bytes; direct draw "
                "supports at most 255 (a plain black or mostly-uniform "
                "canvas is fine; sparse colorful pixels too).")
        await self._send(bytes([0xea, 0x11, 0x00, 0x00, 0x00, len(stream)])
                         + stream)
        log.debug("Direct draw: %d -> %dB", len(fb), len(stream))

    async def draw_pixels(self, pixels: dict[tuple[int, int],
                                             tuple[int, int, int] | None],
                          clear: bool = False) -> None:
        """Draw pixels at (col, row) positions in one direct-draw frame.

        Args:
            pixels: Mapping of (col, row) to RGB color; a None value
                erases that pixel (black).
            clear: Erase the whole canvas first (black framebuffer).
        """
        from .color import rgb_to_display

        fb = bytearray(FRAMEBUFFER_SIZE)
        for (col, row), rgb in pixels.items():
            if not (0 <= col < DISPLAY_COLS and 0 <= row < DISPLAY_ROWS):
                raise ValueError(
                    f"Pixel ({col}, {row}) outside {DISPLAY_COLS}x{DISPLAY_ROWS}")
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            if rgb is None:
                fb[pos] = fb[pos + 1] = 0x00
            else:
                fb[pos], fb[pos + 1] = rgb_to_display(*rgb)
        await self.draw_frame(bytes(fb))

    async def _upload_content(self, cache_type: int, meta_bytes: bytes,
                              payload: bytes, activate_cmd: bytes,
                              c_hash: bytes) -> None:
        """Upload content to the display.

        Upload sequence from trace 09 (image upload):
          1. ea 05 [cache_type] [hash]  → ACK: 15 ea 05 [00=miss|01=hit]
          2. e0 30 [size BE32] ea 23    → ACK: 15 e0 30 00 01
          3. e0 32 [segments]           → no ACK (sent back-to-back, 0-2ms gap)
          4. e0 33                      → ACK: 15 e0 33 00 (~324ms)
          5. e0 1e 00 (x2)             → no ACK
          6. ea 06 [activate]           → ACK: 15 ea 24 00 (~594ms)
        """
        # 1. Cache check — ACK: 15 ea 05 [00=miss, 01=hit]
        cache_resp = await self._send_and_wait(
            bytes([0xea, 0x05, cache_type]) + c_hash,
            match=lambda r: _match(r, 0x15, 0xea, 0x05),
        )
        cached = len(cache_resp) > 3 and cache_resp[3] == 0x01

        if cached:
            # Cache hit: just send activation, no ACK expected (trace 06)
            log.info("Cache hit")
            await self._send(activate_cmd)
        else:
            # Cache miss: full upload sequence (trace 09)
            content = (
                b"\xea\x23\x01\x03"
                + c_hash
                + len(meta_bytes).to_bytes(2, 'big')
                + meta_bytes
                + payload
            )
            total_size = len(content)

            # 2. Frame header — ACK: 15 e0 30
            await self._send_and_wait(
                b"\xe0\x30" + total_size.to_bytes(4, 'big') + b"\xea\x23",
                match=lambda r: _match(r, 0x15, 0xe0, 0x30),
            )

            # 3. Data segments — no ACK. Paced for bulk uploads (see
            # SEGMENT_PACE_S above): unpaced 100+ segment floods crash the
            # device after the upload completes.
            num_segments = (total_size + MAX_SEGMENT_SIZE - 1) // MAX_SEGMENT_SIZE
            pace = SEGMENT_PACE_S if num_segments > PACED_SEGMENTS_THRESHOLD else 0.0
            for seg_idx in range(num_segments):
                if seg_idx and pace:
                    await asyncio.sleep(pace)
                offset = seg_idx * MAX_SEGMENT_SIZE
                chunk = content[offset:offset + MAX_SEGMENT_SIZE]
                seg_header = (
                    b"\xe0\x32"
                    + len(chunk).to_bytes(2, 'big')
                    + b"\x00\x00\x00\x00"
                    + bytes([seg_idx])
                )
                await self._send(seg_header + chunk)

            # 4. End marker — ACK: 15 e0 33
            await self._send_and_wait(
                b"\xe0\x33",
                match=lambda r: _match(r, 0x15, 0xe0, 0x33),
            )

            # 4b. Settle — the device needs a moment after the end marker
            # before screen prepare/activation.
            await asyncio.sleep(UPLOAD_SETTLE_S)

            # 5. Screen prepare (2x) — no ACK
            await self._send(b"\xe0\x1e\x00")
            await self._send(b"\xe0\x1e\x00")

            # 6. Activate — ACK: 15 ea 24
            await self._send_and_wait(
                activate_cmd,
                match=lambda r: _match(r, 0x15, 0xea, 0x24),
            )


def validate_gif(gif_data: bytes, strict: bool = True) -> dict:
    """Check a GIF against the device's known-safe envelope.

    See PROTOCOL.md "GIF Device Limitations". Unsafe GIFs can crash the
    device firmware *after* the upload completes.

    Args:
        gif_data: Raw GIF bytes.
        strict: Raise on violations. False = return the report only.

    Returns:
        Report dict with per-property ok/violation info.

    Raises:
        ValueError: If strict and any property is outside the envelope.
    """
    if gif_data[:6] not in (b'GIF87a', b'GIF89a'):
        raise ValueError("Not a GIF file")

    packed = gif_data[10]
    report = {
        "global_color_table": bool((packed >> 7) & 1),
        "global_table_entries": 2 << (packed & 7) if (packed >> 7) & 1 else 0,
        "local_color_tables": 0,
        "frames": 0,
        "min_frame_delay_cs": None,
        "size_bytes": len(gif_data),
        "ok": True,
    }

    idx = 13 + (3 * report["global_table_entries"] if (packed >> 7) & 1 else 0)
    delays: list[int] = []
    while idx < len(gif_data):
        b = gif_data[idx]
        if b == 0x21:  # extension
            if gif_data[idx + 1] == 0xF9 and idx + 8 <= len(gif_data):
                delays.append(gif_data[idx + 4] | (gif_data[idx + 5] << 8))
            idx += 2
            while idx < len(gif_data) and gif_data[idx] != 0:
                idx += gif_data[idx] + 1
            idx += 1
        elif b == 0x2C:  # image descriptor
            report["frames"] += 1
            ip = gif_data[idx + 9]
            if (ip >> 7) & 1:
                report["local_color_tables"] += 1
                entries = 2 ** ((ip & 7) + 1)
                idx += 11 + 3 * entries
            else:
                idx += 10
            idx += 1  # LZW min code size
            while idx < len(gif_data) and gif_data[idx] != 0:
                idx += gif_data[idx] + 1
            idx += 1
        elif b == 0x3B:  # trailer
            break
        else:
            idx += 1

    if delays:
        report["min_frame_delay_cs"] = min(delays)

    checks = [
        (report["local_color_tables"] == 0,
         f"{report['local_color_tables']} local color table(s); the device "
         "supports only a single global color table"),
        (report["min_frame_delay_cs"] is None or report["min_frame_delay_cs"] >= 10,
         f"frame delay {report['min_frame_delay_cs']}cs too fast; use >= 10cs "
         "(100ms, <=10 fps)"),
        (report["size_bytes"] <= 64 * 1024,
         f"GIF is {report['size_bytes']} bytes; observed-safe size is <= 64KB"),
    ]
    report["violations"] = [msg for ok, msg in checks if not ok]
    report["ok"] = not report["violations"]

    if strict and not report["ok"]:
        raise ValueError(
            "GIF outside the device-safe envelope (risk of firmware crash):\n  "
            + "\n  ".join(report["violations"])
            + "\nRe-render with a single global color table and frame delay "
              ">= 100ms (see README 'Device-safe GIFs').")
    return report


def _build_a2pl_payload(fb: bytes, num_cols: int) -> tuple[bytes, int, int]:
    """Build a2pl payload with offset table from a wide framebuffer.

    Returns:
        (payload_bytes, frame_count, last_frame_cols)
    """
    page_size = FRAMEBUFFER_SIZE
    frame_num = (num_cols + DISPLAY_COLS - 1) // DISPLAY_COLS
    last_frame_cols = num_cols - (frame_num - 1) * DISPLAY_COLS

    frames: list[bytes] = []
    for i in range(frame_num):
        start = i * page_size
        end = min(start + page_size, len(fb))
        page_fb = fb[start:end]
        if len(page_fb) < page_size:
            page_fb = page_fb + b'\x00' * (page_size - len(page_fb))
        frames.append(compress(page_fb))

    # Header: [00 00 00 00] [frame0_size BE16]
    # Table: (frame_num - 1) entries of [cumul_offset BE32] [frame_size BE16]
    frame_data = b''.join(frames)
    table = bytearray()
    cumul = len(frames[0])
    for i in range(frame_num - 1):
        table.extend(cumul.to_bytes(4, 'big'))
        table.extend(len(frames[i + 1]).to_bytes(2, 'big'))
        cumul += len(frames[i + 1])

    payload = (
        b"\x00\x00\x00\x00"
        + len(frames[0]).to_bytes(2, 'big')
        + bytes(table)
        + frame_data
    )
    return payload, frame_num, last_frame_cols


def _content_hash(content_type: str, payload: bytes,
                   attr: dict | None = None) -> bytes:
    """Compute 16-byte MD5 hash for content caching."""
    h = hashlib.md5()
    h.update(content_type.encode())
    h.update(payload)
    if attr:
        h.update(json.dumps(attr, sort_keys=True, separators=(',', ':')).encode())
    return h.digest()


def _parse_playlist_response(resp: bytes, entry_size: int,
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


def _renumber(entries: list[PlaylistEntry], duration: int) -> None:
    """Assign 1-based rotation indices in list order; fill missing durations."""
    for i, e in enumerate(entries):
        e.index = i + 1
        if e.duration <= 0:
            e.duration = duration


def _match(payload: bytes, *expected: int) -> bool:
    """Check if the start of a response payload matches expected bytes."""
    if len(payload) < len(expected):
        return False
    return all(payload[i] == expected[i] for i in range(len(expected)))


def _looks_like_ble_address(s: str) -> bool:
    """Heuristic: BLE addresses are long hex strings with dashes or colons."""
    return len(s) > 16 or ":" in s
