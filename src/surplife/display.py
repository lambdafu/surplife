"""Surplife display facade: BLE connection + all public commands.

Thin layer over surplife_core (pure protocol) plus a bleak transport. The
Home Assistant integration uses surplife_core.connection directly with its
own transport; this class preserves the standalone CLI/library API.
"""

from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from PIL import Image

from surplife_core import messages as m
from surplife_core.connection import SurplifeConnection
from surplife_core.content import build_a2pl_payload, content_hash, validate_gif
from surplife_core.media import ensure_canvas_bytes
from surplife_core.protocol import (
    BYTES_PER_PIXEL,
    DISPLAY_COLS,
    DISPLAY_ROWS,
    FRAMEBUFFER_SIZE,
    PACED_SEGMENTS_THRESHOLD,
    SEGMENT_PACE_S,
    UPLOAD_SETTLE_S,
)

from .a2pl import compress
from .color import image_to_framebuffer, rgb_to_display, rgb_to_meta_color
from .fonts import render_text
from .scanner import discover_one, find_by_name
from .transport import BleakTransport

log = logging.getLogger(__name__)

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
        self._transport: BleakTransport | None = None
        self._conn: SurplifeConnection | None = None

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
        self._transport = BleakTransport(self._client)
        self._conn = SurplifeConnection(self._transport)
        self._transport.set_sink(self._conn.feed_notification)
        await self._transport.start()
        self.name = self._ble_device.name
        log.info("Connected to %s (%s)", self.name, self.address)

    async def disconnect(self) -> None:
        """Disconnect from the display."""
        if self._transport:
            await self._transport.stop()
        if self._client and self._client.is_connected:
            await self._client.disconnect()
            log.info("Disconnected")
        self._client = None
        self._transport = None
        self._conn = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def connection(self) -> SurplifeConnection | None:
        """The underlying SurplifeConnection (for advanced/HA-style use)."""
        return self._conn

    # ── Init handshake ───────────────────────────────────────────────

    async def init(self) -> None:
        """Perform the device init handshake (see SurplifeConnection.init)."""
        parsed = await self._conn.init(self._apply_status)
        log.info("Init complete (time synced: %s)",
                 datetime.datetime.now().strftime("%H:%M:%S"))
        _ = parsed

    def _apply_status(self, status: dict) -> None:
        self.power = status.get("power", self.power)
        self.speed = status.get("speed", self.speed)
        self.brightness = status.get("brightness", self.brightness)
        self.display_cols = status.get("cols", self.display_cols)
        self.display_rows = status.get("rows", self.display_rows)

    @property
    def status_str(self) -> str:
        """Human-readable device status summary."""
        power_str = "on" if self.power else "off"
        return (f"power={power_str} brightness={self.brightness}%"
                f" speed={self.speed} {self.display_cols}x{self.display_rows}")

    # ── Commands ─────────────────────────────────────────────────────

    async def power_on(self) -> None:
        """Power on the display. ACK: 16 ea 81."""
        await self._conn.power(True, self._apply_status)
        log.info("Power ON  [%s]", self.status_str)

    async def power_off(self) -> None:
        """Power off the display. ACK: 16 ea 81."""
        await self._conn.power(False, self._apply_status)
        log.info("Power OFF  [%s]", self.status_str)

    async def set_brightness(self, value: int) -> None:
        """Set brightness (0-100). ACK: 16 ea 81."""
        await self._conn.set_brightness(value, self._apply_status)
        log.info("Brightness: %d%%  [%s]", max(0, min(100, value)),
                 self.status_str)

    async def set_speed(self, value: int) -> None:
        """Set animation speed (1-100). No ACK.

        Sends ea 07 which is the GIF speed command. Observed side effects
        on non-GIF content: scrolling text switches to blinking, scrolling
        images become static. Likely only appropriate for GIF animations.
        """
        await self._conn.set_speed(value)
        log.info("Speed: %d", max(1, min(100, value)))

    async def show_clock(self, style: int = 0, hour_24: bool = True,
                         show_date: bool = False) -> None:
        """Show a firmware-rendered clock. No ACK."""
        await self._conn.show_clock(style, hour_24, show_date)
        fmt_str = "24h" if hour_24 else "12h"
        date_str = " +date" if show_date else ""
        log.info("Clock: style=%d %s%s", max(0, min(7, style)),
                 fmt_str, date_str)

    async def set_time(self, dt: datetime.datetime | None = None) -> None:
        """Sync the device clock. No ACK."""
        if dt is None:
            dt = datetime.datetime.now()
        await self._conn.send(m.cmd_time_sync(dt))
        log.info("Time synced: %s", dt.strftime("%Y-%m-%d %H:%M:%S"))

    # ── Playlist / carousel ──────────────────────────────────────────

    async def get_playlist(self) -> list[PlaylistEntry]:
        """Read the device playlist (ea 0c + ea 0e), in playlist order."""
        flags, details = await self._conn.get_playlist_raw()
        entries = [
            PlaylistEntry(hash=bytes(h), flag=flag)
            for flag, h in flags
        ]
        details_map = {
            bytes(h): (index, duration)
            for index, _flag1, duration, h in details
        }
        for e in entries:
            if e.hash in details_map:
                e.index, e.duration = details_map[e.hash]
        return entries

    async def set_playlist(self, entries: list[PlaylistEntry],
                           editing: bool) -> None:
        """Write the playlist (ea 0b). ACK: 15 ea 0b."""
        await self._conn.set_playlist_raw(entries, editing)
        log.info("Playlist set: %d entries", len(entries))

    async def set_playlist_details(self, entries: list[PlaylistEntry]) -> None:
        """Write rotation positions and durations (ea 0d). ACK: 15 ea 0d."""
        await self._conn.set_playlist_details_raw(entries)
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
        await self._conn.send(b"\xe1\x05" + data)
        log.info("Waveform config (raw): %s", data.hex())

    async def send_waveform(self, amplitudes: bytes, style_id: int = 0x01) -> None:
        """Send one waveform frame (96 amplitude bytes). No ACK.

        Args:
            amplitudes: 96 bytes, values 0-100, one per display column.
            style_id: Style ID matching the configured waveform style.
        """
        if len(amplitudes) != 96:
            raise ValueError(f"Expected 96 amplitude bytes, got {len(amplitudes)}")
        await self._conn.send(bytes([0xea, 0x0f, style_id, 0x00]) + amplitudes)

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
        payload, frame_num, last_frame_cols = build_a2pl_payload(fb, num_cols)
        meta = m.image_metadata("a", frame_num, len(payload))

        c_hash = os.urandom(16) if force else content_hash("a", payload)
        activate = bytes([0xea, 0x06, 0x00, speed, effect])
        await self._conn.upload_content(
            0x04, meta, payload, activate, c_hash,
            segment_pace_s=SEGMENT_PACE_S,
            paced_threshold=PACED_SEGMENTS_THRESHOLD,
            settle_s=UPLOAD_SETTLE_S)

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
                (see surplife_core.validate_gif()).

        Returns:
            16-byte content hash.
        """
        if check:
            report = validate_gif(gif_data)
            log.debug("GIF envelope check: %s", report)
        meta = m.gif_metadata("b", len(gif_data))

        c_hash = os.urandom(16) if force else content_hash("b", gif_data)
        activate = bytes([0xea, 0x07, 0x00, speed])
        await self._conn.upload_content(
            0x01, meta, gif_data, activate, c_hash,
            segment_pace_s=SEGMENT_PACE_S,
            paced_threshold=PACED_SEGMENTS_THRESHOLD,
            settle_s=UPLOAD_SETTLE_S)

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

        fb, num_cols = render_text(text, fg=colors[0], font_size=16)
        payload, frame_num, last_frame_cols = build_a2pl_payload(fb, num_cols)

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

        meta = m.text_metadata("e", frame_num, len(payload), attr)

        c_hash = os.urandom(16) if force else content_hash("e", payload, attr)
        await self._conn.upload_content(
            0x02, meta, payload, b"\xea\x24", c_hash,
            segment_pace_s=SEGMENT_PACE_S,
            paced_threshold=PACED_SEGMENTS_THRESHOLD,
            settle_s=UPLOAD_SETTLE_S)

        log.info("Text '%s' sent (%d cols, %d frame(s), %dB, speed=%d)",
                 text[:30], num_cols, frame_num, len(payload), speed)
        return c_hash

    # ── Graffiti / direct pixel drawing ─────────────────────────────

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
        payload, _frame_num, _last = build_a2pl_payload(fb, num_cols)
        meta = m.image_metadata("c", 1, len(payload))

        c_hash = os.urandom(16) if force else content_hash("c", payload)
        await self._conn.upload_content(
            0x00, meta, payload, b"\xea\x09\x00\x50\x01", c_hash,
            segment_pace_s=SEGMENT_PACE_S,
            paced_threshold=PACED_SEGMENTS_THRESHOLD,
            settle_s=UPLOAD_SETTLE_S)
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
        meta = m.gif_metadata("d", len(gif_data))
        c_hash = os.urandom(16) if force else content_hash("d", gif_data)
        await self._conn.upload_content(
            0x02, meta, gif_data, b"\xea\x0a\x00\x50\x01", c_hash,
            segment_pace_s=SEGMENT_PACE_S,
            paced_threshold=PACED_SEGMENTS_THRESHOLD,
            settle_s=UPLOAD_SETTLE_S)
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
        ensure_canvas_bytes(fb)
        stream = compress(fb)
        if len(stream) > 255:
            raise ValueError(
                f"Compressed canvas is {len(stream)} bytes; direct draw "
                "supports at most 255 (a plain black or mostly-uniform "
                "canvas is fine; sparse colorful pixels too).")
        await self._conn.send(m.cmd_direct_draw(stream))
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


def _renumber(entries: list[PlaylistEntry], duration: int) -> None:
    """Assign 1-based rotation indices in list order; fill missing durations."""
    for i, e in enumerate(entries):
        e.index = i + 1
        if e.duration <= 0:
            e.duration = duration


def _looks_like_ble_address(s: str) -> bool:
    """Heuristic: BLE addresses are long hex strings with dashes or colons."""
    return len(s) > 16 or ":" in s
