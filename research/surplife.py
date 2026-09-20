from __future__ import annotations

"""
Surplife LED Display — BLE Control Library

Reverse-engineered protocol for Surplife 96×16 LED matrix displays.
BLE Name: IOTBT996 | Service: 0000ffff | Write: ff01 | Notify: ff02

Display: 96 columns × 16 rows, 2 bytes/pixel (16-bit HSV), column-major.
Pixel data uses a2pl compression (LZ77 variant), see A2PL.md for details.
"""

import asyncio
import colorsys
import datetime
import hashlib
import io
import json
import os
import uuid

from PIL import Image, ImageDraw, ImageFont
from bleak import BleakClient, BleakScanner

WRITE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "spleen-8x16.otf")

# Display geometry
DISPLAY_COLS = 96
DISPLAY_ROWS = 16
BYTES_PER_PIXEL = 2
FRAMEBUFFER_SIZE = DISPLAY_COLS * DISPLAY_ROWS * BYTES_PER_PIXEL  # 3072


# ─── a2pl compression (LZ77 variant) ────────────────────────────────

def _sum_decode(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a sum-encoded integer. Returns (value, new_offset).

    Sum encoding: 0xFF bytes each add 255; the first non-0xFF byte
    adds its value and terminates. Example: FF FF 0D = 255+255+13 = 523.
    """
    value = 0
    while offset < len(data) and data[offset] == 0xFF:
        value += 255
        offset += 1
    if offset < len(data):
        value += data[offset]
        offset += 1
    return value, offset


def decompress_a2pl(data: bytes) -> bytes:
    """Decompress a2pl-compressed pixel data to a raw framebuffer.

    The a2pl format is an LZ77 variant where each command byte encodes:
      - Upper nibble: literal byte count
        - 0x0–0xE: that many literal bytes follow
        - 0xF: 15 + sum_encoded(N) literal bytes follow
      - Lower nibble: back-reference copy length base
        - 0x0–0xE (short): copy = nibble + 4 bytes from LE16 distance
        - 0xF (long): copy = sum_encoded(N) + 19 bytes from LE16 distance

    Both nibbles use the same extension pattern: 0xF means the base value
    (15 for literals, 19 for copies) plus a sum-encoded extension.

    The stream ends when the compressed data runs out.

    Args:
        data: a2pl compressed bytes

    Returns:
        Raw framebuffer bytes (column-major, 2 bytes/pixel HSV).
    """
    fb = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        upper = (b >> 4) & 0x0F
        lower = b & 0x0F
        i += 1

        # Literal count: upper nibble, extended via sum-encoding if 0xF
        if upper == 0x0F:
            ext, i = _sum_decode(data, i)
            n_lit = 15 + ext
        else:
            n_lit = upper

        fb.extend(data[i:i + n_lit])
        i += n_lit

        # Back-reference: LE16 distance + copy
        if i + 1 >= len(data):
            break
        dist = data[i] | (data[i + 1] << 8)
        i += 2

        if lower == 0x0F:
            length, i = _sum_decode(data, i)
            copy_len = length + 19
        else:
            copy_len = lower + 4

        start = len(fb) - dist
        if start < 0:
            for j in range(copy_len):
                fb.append(0 if start + j < 0 else fb[start + j])
        else:
            for j in range(copy_len):
                fb.append(fb[start + (j % dist) if dist > 0 else 0])

    return bytes(fb)


def _sum_encode(value: int) -> bytes:
    """Encode an integer using sum-encoding (inverse of _sum_decode)."""
    result = bytearray()
    while value >= 255:
        result.append(0xFF)
        value -= 255
    result.append(value)
    return bytes(result)


def _find_best_match(fb: bytes, pos: int, max_search: int = 0) -> tuple[int, int]:
    """Find the longest LZ77 match for fb[pos:] in fb[:pos].

    Returns (distance, length). Length 0 means no match found.
    With LZ77 wrapping, a match at distance D tiles the D-byte pattern.

    Args:
        max_search: Maximum search distance (0 = unlimited).
    """
    if pos < 1:
        return 0, 0
    remaining = len(fb) - pos
    if remaining < 4:
        return 0, 0

    best_dist = 0
    best_len = 0
    limit = min(pos, 0xFFFF)
    if max_search > 0:
        limit = min(limit, max_search)

    for dist in range(1, limit + 1):
        start = pos - dist
        # Quick reject on first byte
        if fb[start] != fb[pos]:
            continue
        # Compute match length with LZ77 wrapping
        length = 0
        while length < remaining and fb[pos + length] == fb[start + (length % dist)]:
            length += 1
        if length > best_len:
            best_len = length
            best_dist = dist
            if best_len >= remaining:
                break  # can't do better

    return best_dist, best_len


def _emit_command(out: bytearray, fb: bytes, pos: int, n_lit: int,
                   match_dist: int, match_len: int):
    """Emit one a2pl command: n_lit literal bytes + backref."""
    # Opcode byte
    upper = min(n_lit, 15)
    lower = min(match_len - 4, 15) if match_len >= 4 else 0
    out.append((upper << 4) | lower)

    # Extended literal count (upper = 0xF)
    if n_lit >= 15:
        out.extend(_sum_encode(n_lit - 15))

    # Literal bytes
    out.extend(fb[pos:pos + n_lit])

    # LE16 distance
    out.extend(match_dist.to_bytes(2, 'little'))

    # Extended copy length (lower = 0xF)
    if match_len > 18:
        out.extend(_sum_encode(match_len - 19))


def compress_a2pl(fb: bytes, max_search: int = 0) -> bytes:
    """Compress a raw framebuffer to a2pl format.

    Uses greedy LZ77: at each position, finds the longest back-reference
    match, collecting unmatched bytes as literals. Supports extended literal
    counts (>15) via sum-encoded upper nibble extension.

    Args:
        fb: Raw framebuffer bytes (column-major, must be multiple of 32).
        max_search: Max search distance for match finding (0 = unlimited).
            Use 128–256 for faster compression at slightly worse ratios.

    Returns:
        a2pl compressed bytes.
    """
    col_bytes = DISPLAY_ROWS * BYTES_PER_PIXEL  # 32
    if len(fb) == 0 or len(fb) % col_bytes != 0:
        raise ValueError(f"Framebuffer must be a multiple of {col_bytes} bytes, got {len(fb)}")

    out = bytearray()
    pos = 0

    while pos < len(fb):
        remaining = len(fb) - pos

        # If few bytes remain, emit as final literals (stream ends, no backref)
        if remaining <= 15:
            out.append((remaining << 4) | 0x0)
            out.extend(fb[pos:pos + remaining])
            break

        # Scan forward from current position to find a match
        match_at = None
        match_dist = 0
        match_len = 0

        scan_limit = min(remaining - 3, len(fb) - pos)
        for lit_count in range(scan_limit):
            cur = pos + lit_count
            dist, length = _find_best_match(fb, cur, max_search)
            if length >= 4:
                match_at = lit_count
                match_dist = dist
                match_len = min(length, remaining - lit_count)
                break

        if match_at is None:
            # No match anywhere — emit all remaining as final literals
            if remaining <= 15:
                out.append((remaining << 4) | 0x0)
            else:
                out.append(0xF0)
                out.extend(_sum_encode(remaining - 15))
            out.extend(fb[pos:pos + remaining])
            break

        # Reserve tail bytes so the stream never ends on a backref boundary.
        # The device expects trailing literal bytes after the last backref.
        tail = remaining - match_at - match_len
        if tail == 0 and match_len > 4:
            # Shorten backref, leave last bytes for trailing literal command
            trim = min(5, match_len - 4)
            match_len -= trim

        _emit_command(out, fb, pos, match_at, match_dist, match_len)
        pos += match_at + match_len

    return bytes(out)


# ─── HSV color encoding ─────────────────────────────────────────────

def encode_hsv(h: int, s: int = 100, v: int = 100) -> bytes:
    """Encode an HSV color into the 2-byte display format.

    Args:
        h: Hue in degrees (0–360)
        s: Saturation in percent (0–100)
        v: Value/brightness in percent (0–100)

    Returns:
        2 bytes: packed HSV color.

    Encoding: byte1 = (hue_7bit << 1) | s_bit3
              byte2 = (s_lower3 << 5) | v_5bit
    """
    hue_7 = round(max(0, min(360, h)) / 360 * 127) & 0x7F
    s_4 = round(max(0, min(100, s)) / 100 * 15) & 0xF
    v_5 = round(max(0, min(100, v)) / 100 * 31) & 0x1F
    byte1 = (hue_7 << 1) | ((s_4 >> 3) & 1)
    byte2 = ((s_4 & 0x7) << 5) | v_5
    return bytes([byte1, byte2])


def decode_hsv(b1: int, b2: int) -> tuple[float, float, float]:
    """Decode 2 display bytes to HSV values.

    Args:
        b1, b2: The two packed color bytes.

    Returns:
        (hue, saturation, value) where hue is 0–360°,
        saturation and value are 0.0–1.0.
    """
    hue_7 = (b1 >> 1) & 0x7F
    s_4 = ((b1 & 1) << 3) | ((b2 >> 5) & 0x7)
    v_5 = b2 & 0x1F
    return hue_7 / 127.0 * 360.0, s_4 / 15.0, v_5 / 31.0


# ─── Content hashing ──────────────────────────────────────────────

def content_hash(content_type: str, payload: bytes, attr: dict | None = None) -> bytes:
    """Compute deterministic 16-byte content hash for caching and playlist use.

    Hashes content type + payload bytes + optional attribute dict (for text).
    Uses MD5 for 16-byte output matching the protocol's hash field size.

    Args:
        content_type: One of "a" (image), "b" (GIF), "c" (graffiti),
                      "d" (graffiti anim), "e" (text), "f" (waveform).
        payload: The payload bytes as sent to the device.
        attr: Optional attribute dict (for text — captures gradient, speed, etc.).

    Returns:
        16-byte hash.
    """
    h = hashlib.md5()
    h.update(content_type.encode())
    h.update(payload)
    if attr:
        h.update(json.dumps(attr, sort_keys=True, separators=(',', ':')).encode())
    return h.digest()


# ─── Framebuffer utilities ──────────────────────────────────────────

def framebuffer_to_image(fb: bytes, scale: int = 1) -> Image.Image:
    """Convert a raw framebuffer to a PIL Image.

    Args:
        fb: Raw framebuffer bytes (3072 bytes, column-major, 2 bytes/pixel HSV).
        scale: Integer scale factor (default 1 = 96×16 pixels).

    Returns:
        PIL Image in RGB mode.
    """
    if len(fb) != FRAMEBUFFER_SIZE:
        raise ValueError(f"Expected {FRAMEBUFFER_SIZE} bytes, got {len(fb)}")

    img = Image.new('RGB', (DISPLAY_COLS * scale, DISPLAY_ROWS * scale))
    for col in range(DISPLAY_COLS):
        for row in range(DISPLAY_ROWS):
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            h, s, v = decode_hsv(fb[pos], fb[pos + 1])
            r, g, b = colorsys.hsv_to_rgb(h / 360.0, s, v)
            rgb = (int(r * 255), int(g * 255), int(b * 255))
            if scale == 1:
                img.putpixel((col, row), rgb)
            else:
                for dy in range(scale):
                    for dx in range(scale):
                        img.putpixel((col * scale + dx, row * scale + dy), rgb)
    return img


def decompress_to_image(data: bytes, scale: int = 1) -> Image.Image:
    """Decompress a2pl data and return a PIL Image.

    Convenience function combining decompress_a2pl() and framebuffer_to_image().

    Args:
        data: a2pl compressed bytes.
        scale: Integer scale factor for the output image.

    Returns:
        PIL Image in RGB mode.
    """
    return framebuffer_to_image(decompress_a2pl(data), scale=scale)


def image_to_framebuffer(img: Image.Image) -> bytes:
    """Convert a PIL Image to a raw column-major HSV framebuffer.

    The image is resized to 96×16 if needed. Each pixel is converted from
    RGB to the display's 2-byte HSV encoding, stored in column-major order.

    Args:
        img: PIL Image (any mode, will be converted to RGB).

    Returns:
        3072-byte framebuffer ready for compress_a2pl().
    """
    img = img.convert('RGB').resize((DISPLAY_COLS, DISPLAY_ROWS), Image.LANCZOS)
    fb = bytearray(FRAMEBUFFER_SIZE)
    for col in range(DISPLAY_COLS):
        for row in range(DISPLAY_ROWS):
            r, g, b = img.getpixel((col, row))
            h_f, s_f, v_f = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            hsv_bytes = encode_hsv(h_f * 360, s_f * 100, v_f * 100)
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            fb[pos] = hsv_bytes[0]
            fb[pos + 1] = hsv_bytes[1]
    return bytes(fb)


def render_text_framebuffer(text: str,
                            fg: tuple[int, int, int] = (255, 0, 0),
                            bg: tuple[int, int, int] = (0, 0, 0),
                            font_size: int = 16) -> tuple[bytes, int]:
    """Render text into a wide column-major HSV framebuffer.

    Returns:
        (framebuffer_bytes, num_cols) — framebuffer is num_cols × 16 × 2 bytes.
    """
    if os.path.exists(FONT_PATH):
        font = ImageFont.truetype(FONT_PATH, font_size)
    else:
        font = ImageFont.load_default(size=font_size)

    # Measure text width
    bbox = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]

    # Canvas width: at least one display width
    canvas_w = max(DISPLAY_COLS, text_w + 4)
    canvas = Image.new('RGB', (canvas_w, DISPLAY_ROWS), bg)
    draw = ImageDraw.Draw(canvas)
    y_offset = (DISPLAY_ROWS - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((2, y_offset), text, fill=fg, font=font)

    # Convert to column-major HSV framebuffer
    num_cols = canvas_w
    fb = bytearray(num_cols * DISPLAY_ROWS * BYTES_PER_PIXEL)
    for col in range(num_cols):
        for row in range(DISPLAY_ROWS):
            r, g, b = canvas.getpixel((col, row))
            h_f, s_f, v_f = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            hsv_bytes = encode_hsv(h_f * 360, s_f * 100, v_f * 100)
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            fb[pos] = hsv_bytes[0]
            fb[pos + 1] = hsv_bytes[1]

    return bytes(fb), num_cols


def compress_image(img: Image.Image) -> bytes:
    """Convert a PIL Image to a2pl compressed pixel data.

    Convenience function: resizes to 96×16, converts to HSV framebuffer,
    then compresses with a2pl.

    Args:
        img: PIL Image (any size/mode).

    Returns:
        a2pl compressed bytes ready for upload or ea 11 direct draw.
    """
    return compress_a2pl(image_to_framebuffer(img))


class SurplifeDisplay:

    def __init__(self, address: str):
        self.address = address
        self._client: BleakClient | None = None
        self._seq = 0x0100
        self._responses: list[bytes] = []

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _wrap(self, inner: bytes) -> bytes:
        """Wrap inner payload in the Surplife packet format."""
        seq = self._next_seq()
        plen = len(inner)
        header = bytes([
            (seq >> 8) & 0xFF, seq & 0xFF,
            0x80, 0x00,
            ((plen - 1) >> 8) & 0xFF, (plen - 1) & 0xFF,  # Big Endian!
            (plen >> 8) & 0xFF, plen & 0xFF,                # Big Endian!
        ])
        return header + inner

    def _on_notify(self, _sender, data: bytearray):
        print(f"  << notify: {data.hex()}")
        self._responses.append(bytes(data))

    async def _send_and_collect(self, inner: bytes, wait: float = 1.0) -> list[bytes]:
        """Send a command and collect notification responses.

        Returns inner payloads (8-byte wrapper header stripped).
        """
        self._responses.clear()
        await self.send(inner)
        await asyncio.sleep(wait)
        return [r[8:] for r in self._responses if len(r) > 8]

    async def connect(self):
        """Scan for the device, then connect and subscribe to notifications."""
        print(f"Scanning for {self.address}...")
        device = await BleakScanner.find_device_by_address(self.address, timeout=10.0)
        if not device:
            raise RuntimeError(f"Device {self.address} not found")
        self._client = BleakClient(device, timeout=15.0)
        await self._client.connect()
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)
        print(f"Connected to {device.name} ({self.address})")

    async def disconnect(self):
        """Disconnect from the display."""
        if self._client and self._client.is_connected:
            await self._client.disconnect()
            print("Disconnected")
        self._client = None

    async def send_raw(self, payload: bytes):
        """Send a raw payload (wrapped with seq/header, but no 0x0a prefix added)."""
        packet = self._wrap(payload)
        await self._client.write_gatt_char(WRITE_UUID, packet, response=False)

    async def send(self, inner: bytes):
        """Send a command (inner payload without 0x0a prefix)."""
        await self.send_raw(bytes([0x0a]) + inner)

    @staticmethod
    def _build_config_cmd() -> bytes:
        """Build the 10 14 config command with current local time.

        Format: 10 14 [YY] [MM] [DD] [hh] [mm] [ss] [dow] 00 0f [checksum]
          YY:  year - 2000
          MM:  month (1–12)
          DD:  day (1–31)
          hh:  hour (0–23)
          mm:  minute (0–59)
          ss:  second (0–59)
          dow: day of week (1=Monday .. 7=Sunday, ISO 8601)
          checksum: sum(bytes[0:11]) & 0xFF
        """
        now = datetime.datetime.now()
        cmd = bytes([
            0x10, 0x14,
            now.year - 2000,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.isoweekday(),  # 1=Monday .. 7=Sunday
            0x00,
            0x0f,
        ])
        checksum = sum(cmd) & 0xFF
        return cmd + bytes([checksum])

    async def init(self):
        """Send the init handshake: sync time, exchange device hash.

        1. 0x0c raw (no 0x0a prefix — special init trigger)
        2. 10 14 config command (sets device clock to current local time)
        3. ea 81 device hash — device replies with ACK + Status
        """
        await self.send_raw(b"\x0c")
        config = self._build_config_cmd()
        await self.send(config)
        await self.send(b"\xea\x81\x8a\x8b\x59")
        await asyncio.sleep(2.0)
        now = datetime.datetime.now()
        print(f"Init sequence sent (time synced: {now.strftime('%Y-%m-%d %H:%M:%S')})")

    async def power_on(self):
        """Power on / activate display (e0 01 subcmd 0x23)."""
        await self.send(b"\xe0\x01\x00\x23" + b"\x00" * 10)
        print("Power ON sent")

    async def power_off(self):
        """Power off / deactivate display (e0 01 subcmd 0x24)."""
        await self.send(b"\xe0\x01\x00\x24" + b"\x00" * 10)
        print("Power OFF sent")

    async def set_brightness(self, value: int):
        """Set brightness (0–100). e0 01 subcmd 0x01, value at bytes 6 and 8."""
        v = max(0, min(100, value))
        await self.send(b"\xe0\x01\x00\x01\x00\x00" + bytes([v, 0x00, v, 0x00, 0x00, 0x00, 0x00, 0x00]))
        print(f"Brightness set to {v}")

    async def set_speed(self, value: int):
        """Set animation speed (1–100). ea 07 00 [val]."""
        v = max(1, min(100, value))
        await self.send(b"\xea\x07\x00" + bytes([v]))
        print(f"Speed set to {v}")

    async def _upload_content(self, cache_type: int, meta_bytes: bytes,
                              payload: bytes, activate_cmd: bytes | None,
                              c_hash: bytes | None = None) -> bytes:
        """Upload content to the display (shared upload sequence).

        Args:
            cache_type: ea 05 second byte (0x00=color, 0x01=gif, 0x02=text, 0x04=image)
            meta_bytes: JSON metadata as bytes
            payload: raw content data (6B-header+data for a/c, raw GIF for b)
            activate_cmd: activation command inner bytes, or None to skip
            c_hash: deterministic 16-byte content hash (random if None)

        Returns:
            The 16-byte content hash used for this upload.
        """
        if c_hash is None:
            c_hash = os.urandom(16)

        # 1. Cache check
        self._responses.clear()
        await self.send(bytes([0xea, 0x05, cache_type]) + c_hash)
        await asyncio.sleep(0.5)

        # Check if device already has this content
        cached = False
        for r in self._responses:
            if len(r) > 11 and r[8] == 0x15 and r[9] == 0xea and r[10] == 0x05 and r[11] == 0x01:
                cached = True
                break

        if cached:
            print(f"  Cache hit — skipping upload")
        else:
            # Content = ea_header(4) + hash(16) + json_len(2) + json + payload
            content = (
                b"\xea\x23\x01\x03" +
                c_hash +
                len(meta_bytes).to_bytes(2, 'big') +
                meta_bytes +
                payload
            )
            total_size = len(content)

            # 2. Frame header
            await self.send(b"\xe0\x30" + total_size.to_bytes(4, 'big') + b"\xea\x23")
            await asyncio.sleep(0.5)

            # 3. Data segments (e0 32) — split content into ≤490 byte segments
            MAX_SEG = 490
            for seg_idx in range(0, (total_size + MAX_SEG - 1) // MAX_SEG):
                offset = seg_idx * MAX_SEG
                chunk = content[offset:offset + MAX_SEG]
                seg_header = (
                    b"\xe0\x32" +
                    len(chunk).to_bytes(2, 'big') +
                    b"\x00\x00\x00\x00" +
                    bytes([seg_idx])
                )
                await self.send(seg_header + chunk)
                await asyncio.sleep(0.3)

            # 4. End marker
            await self.send(b"\xe0\x33")
            await asyncio.sleep(0.5)

            # 5. Screen prepare (2×)
            await self.send(b"\xe0\x1e\x00")
            await self.send(b"\xe0\x1e\x00")

        # 6. Activate
        if activate_cmd:
            await self.send(activate_cmd)

        return c_hash

    @staticmethod
    def _encode_hsv(h: int, s: int, v: int) -> tuple[int, int]:
        """Encode HSV into the 2-byte surplife color format.
        Returns (byte1, byte2). See module-level encode_hsv() for details.
        """
        b = encode_hsv(h, s, v)
        return b[0], b[1]

    async def show_solid_color(self, h: int = 0, s: int = 100, v: int = 100,
                               *, hue: int | None = None):
        """Display a solid color.

        Args:
            h: Hue in degrees 0-360 (0=red, 120=green, 240=blue)
            s: Saturation 0-100%
            v: Value/brightness 0-100%
            hue: (deprecated) Legacy 0-255 hue, ignores s/v.
        """
        if hue is not None:
            # Legacy path: convert old 0-255 hue to new HSV
            h = round(hue / 255 * 360)
            s, v = 100, 100

        b1, b2 = self._encode_hsv(h, s, v)

        # Image data (23 bytes) — solid color format (type "c")
        image_data = bytes([
            0x2f, b1, b2, 0x02, 0x00,
        ]) + b'\xff' * 11 + bytes([
            0xf1, 0x50, b2, b1, b2, b1, b2,
        ])

        # JSON metadata
        layer_id = str(uuid.uuid4())
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": layer_id, "type": "c", "amt_pos": 0,
                "frame_num": 1, "amt_length": 6 + len(image_data), "amt_fmt": 0,
            }],
            "all_file_type": "c"
        }, separators=(',', ':'))

        # Payload: 6-byte header + image data
        payload = bytes([0x00, 0x00, 0x00, 0x00, 0x00, len(image_data)]) + image_data

        c_hash = content_hash("c", payload)
        await self._upload_content(0x00, meta.encode('ascii'), payload,
                                   b"\xea\x09\x00\x50\x01", c_hash=c_hash)
        print(f"Solid color H={h}° S={s}% V={v}% (bytes: {b1:02x} {b2:02x}) hash={c_hash.hex()}")
        return c_hash

    async def show_gif(self, gif_data: bytes, speed: int = 50, force: bool = False):
        """Display a GIF animation on the display.

        Args:
            gif_data: raw GIF file bytes (must be 96×16, GIF87a or GIF89a)
            speed: animation speed 1–100 (default 50)
            force: bypass device cache by using a random content hash
        """
        layer_id = str(uuid.uuid4())
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": layer_id, "type": "b", "amt_pos": 0,
                "frame_num": 0, "amt_length": len(gif_data), "amt_fmt": 2,
            }],
            "all_file_type": "b"
        }, separators=(',', ':'))

        # For GIF (type "b"), payload is the raw GIF — no 6-byte size prefix
        c_hash = os.urandom(16) if force else content_hash("b", gif_data)
        await self._upload_content(0x01, meta.encode('ascii'), gif_data, None, c_hash=c_hash)

        # Activation: set speed (triggers playback for animations)
        v = max(1, min(100, speed))
        await self.set_speed(v)
        print(f"GIF animation sent ({len(gif_data)} bytes, speed={v}) hash={c_hash.hex()}")
        return c_hash


    async def show_gif_file(self, path: str, speed: int = 50, force: bool = False):
        """Load a GIF file from disk and display it."""
        with open(path, 'rb') as f:
            gif_data = f.read()
        if gif_data[:3] != b'GIF':
            raise ValueError(f"Not a GIF file: {path}")
        await self.show_gif(gif_data, speed, force=force)

    async def show_image(self, compressed_data: bytes):
        """Display a static image from raw a2pl compressed data.

        Use this to replay images extracted from captures via parse_capture.py.
        """
        layer_id = str(uuid.uuid4())
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": layer_id, "type": "a", "amt_pos": 0,
                "frame_num": 1, "amt_length": 6 + len(compressed_data), "amt_fmt": 0,
            }],
            "all_file_type": "a"
        }, separators=(',', ':'))

        # Payload: 6-byte header + compressed data
        payload = (
            b"\x00\x00\x00\x00" +
            len(compressed_data).to_bytes(2, 'big') +
            compressed_data
        )

        c_hash = content_hash("a", payload)
        await self._upload_content(0x04, meta.encode('ascii'), payload,
                                   b"\xea\x06\x00\x64\x01", c_hash=c_hash)
        print(f"Static image sent ({len(compressed_data)} bytes compressed) hash={c_hash.hex()}")
        return c_hash

    async def show_text(self, text: str,
                        fg_color: tuple[int, int, int] = (255, 0, 0),
                        bg_color: tuple[int, int, int] = (0, 0, 0),
                        font_size: int = 16, speed: int = 50,
                        gradient: list[int] | None = None):
        """Render scrolling text and upload as multi-frame a2pl.

        Args:
            gradient: Optional list of hue values (0-360°) for device-rendered
                      color gradient. Overrides fg_color for rendering.
        """
        fb, num_cols = render_text_framebuffer(text, fg_color, bg_color, font_size)

        frame_num = (num_cols + DISPLAY_COLS - 1) // DISPLAY_COLS
        payload, frames, last_frame_cols = self._build_multiframe_payload(fb, num_cols)

        layer_id = str(uuid.uuid4())
        # Build fg_color list: HSV metadata format "HHSSVV" (H=0-150, S/V=0-100)
        if gradient:
            fg_colors = [f"{round(h / 360 * 150):02x}6464" for h in gradient]
            fg_attr = 0  # gradient mode
        else:
            h_f, s_f, v_f = colorsys.rgb_to_hsv(
                fg_color[0] / 255, fg_color[1] / 255, fg_color[2] / 255)
            fg_colors = [f"{round(h_f * 150):02x}"
                         f"{round(s_f * 100):02x}{round(v_f * 100):02x}"]
            fg_attr = 1  # solid color
        attr = {
            "speed": speed,
            "effect": "b" if frame_num > 1 else "a",
            "pause_t": 1,
            "bg_color": "000000",
            "fg_color": fg_colors,
            "fg_attr": fg_attr, "fg_dir": 1,
        }
        if frame_num > 1:
            attr["last_word_frame"] = last_frame_cols
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": layer_id, "type": "e",
                "attr": [attr],
                "amt_pos": 0, "frame_num": frame_num,
                "amt_length": len(payload), "amt_fmt": 0,
            }],
            "all_file_type": "e"
        }, separators=(',', ':'))

        c_hash = content_hash("e", payload, attr=attr)
        await self._upload_content(0x02, meta.encode('ascii'), payload,
                                   b"\xea\x24", c_hash=c_hash)
        total_compressed = sum(len(f) for f in frames)
        print(f"Text '{text[:30]}' sent ({num_cols} cols, {total_compressed}B, "
              f"{frame_num} pages, speed={speed}) hash={c_hash.hex()}")
        return c_hash

    @staticmethod
    def _build_multiframe_payload(fb: bytes, num_cols: int) -> tuple[bytes, list[bytes], int]:
        """Split a wide framebuffer into 96-col pages and build multi-frame payload.

        Args:
            fb: Wide framebuffer (num_cols × 16 × 2 bytes, column-major HSV).
            num_cols: Total number of columns in the framebuffer.

        Returns:
            (payload, frames, last_frame_cols) where payload is the complete
            multi-frame a2pl blob with offset table.
        """
        frame_num = (num_cols + DISPLAY_COLS - 1) // DISPLAY_COLS
        last_frame_cols = num_cols - (frame_num - 1) * DISPLAY_COLS

        page_size = DISPLAY_COLS * DISPLAY_ROWS * BYTES_PER_PIXEL  # 3072
        frames = []
        for i in range(frame_num):
            start = i * page_size
            end = min(start + page_size, len(fb))
            page_fb = fb[start:end]
            if len(page_fb) < page_size:
                page_fb = page_fb + b'\x00' * (page_size - len(page_fb))
            frames.append(compress_a2pl(page_fb))

        # Header: 00 00 00 00 [frame0_size BE16]
        # Table: (frame_num - 1) entries of [00 00 cumul_end_BE16 next_size_BE16]
        frame_data = b''.join(frames)
        table = bytearray()
        cumul = len(frames[0])
        for i in range(frame_num - 1):
            table.extend(b'\x00\x00')
            table.extend(cumul.to_bytes(2, 'big'))
            table.extend(len(frames[i + 1]).to_bytes(2, 'big'))
            cumul += len(frames[i + 1])

        payload = (
            b"\x00\x00\x00\x00" +
            len(frames[0]).to_bytes(2, 'big') +
            bytes(table) +
            frame_data
        )
        return payload, frames, last_frame_cols

    async def show_wide_image(self, img: Image.Image, speed: int = 50,
                              mode: str = "a", effect: int = 2) -> bytes:
        """Upload a wide image and scroll across it.

        The image is scaled to 16px height, keeping aspect ratio, then split
        into 96-column pages and uploaded as multi-frame a2pl.

        Args:
            img: PIL Image of any width (will be scaled to 16px height).
            speed: Scroll speed 1–100 (default 50).
            mode: Upload type — "e" (text protocol, known to work) or
                  "a" (image protocol, experimental — may not support multi-frame).
            effect: For mode "a", the ea 06 effect ID (default 2 = scroll-r).

        Returns:
            16-byte content hash.
        """
        img = img.convert('RGB')
        # Scale to 16px height, keeping aspect ratio
        scale = DISPLAY_ROWS / img.height
        new_w = max(DISPLAY_COLS, round(img.width * scale))
        img = img.resize((new_w, DISPLAY_ROWS), Image.LANCZOS)
        num_cols = img.width

        # Convert to column-major HSV framebuffer
        fb = bytearray(num_cols * DISPLAY_ROWS * BYTES_PER_PIXEL)
        for col in range(num_cols):
            for row in range(DISPLAY_ROWS):
                r, g, b = img.getpixel((col, row))
                h_f, s_f, v_f = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                hsv_bytes = encode_hsv(h_f * 360, s_f * 100, v_f * 100)
                pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
                fb[pos] = hsv_bytes[0]
                fb[pos + 1] = hsv_bytes[1]

        frame_num = (num_cols + DISPLAY_COLS - 1) // DISPLAY_COLS
        payload, frames, last_frame_cols = self._build_multiframe_payload(bytes(fb), num_cols)
        total_compressed = sum(len(f) for f in frames)

        layer_id = str(uuid.uuid4())

        if mode == "a":
            # Experimental: image type with multi-frame
            meta = json.dumps({
                "v": 1, "mant_type": 0, "enable_a2pl": 1,
                "layers": [{
                    "nm": layer_id, "type": "a", "amt_pos": 0,
                    "frame_num": frame_num, "amt_length": len(payload), "amt_fmt": 0,
                }],
                "all_file_type": "a"
            }, separators=(',', ':'))
            c_hash = content_hash("a", payload)
            activate = bytes([0xea, 0x06, 0x00, speed, effect])
            await self._upload_content(0x04, meta.encode('ascii'), payload,
                                       activate, c_hash=c_hash)
        else:
            # Text protocol (known to work for multi-frame scrolling)
            attr = {
                "speed": speed, "effect": "b", "pause_t": 1,
                "bg_color": "000000",
                "fg_color": ["006464"],  # red placeholder — device renders as-is
                "fg_attr": 1, "fg_dir": 1,
                "last_word_frame": last_frame_cols,
            }
            meta = json.dumps({
                "v": 1, "mant_type": 0, "enable_a2pl": 1,
                "layers": [{
                    "nm": layer_id, "type": "e",
                    "attr": [attr],
                    "amt_pos": 0, "frame_num": frame_num,
                    "amt_length": len(payload), "amt_fmt": 0,
                }],
                "all_file_type": "e"
            }, separators=(',', ':'))
            c_hash = content_hash("e", payload, attr=attr)
            await self._upload_content(0x02, meta.encode('ascii'), payload,
                                       b"\xea\x24", c_hash=c_hash)

        print(f"Wide image sent ({num_cols}x16, {total_compressed}B, "
              f"{frame_num} pages, mode={mode}, speed={speed}) hash={c_hash.hex()}")
        return c_hash

    async def show_pil_image(self, image: Image.Image) -> bytes:
        """Display any PIL Image (resized to 96×16, sent as single-frame GIF)."""
        gif_data = pil_image_to_gif(image)
        return await self.show_gif(gif_data)

    async def show_image_file(self, path: str) -> bytes:
        """Load an image file (PNG/JPG/BMP/GIF) and display it."""
        img = Image.open(path)
        if path.lower().endswith('.gif') and getattr(img, 'n_frames', 1) > 1:
            with open(path, 'rb') as f:
                return await self.show_gif(f.read())
        else:
            return await self.show_pil_image(img)

    # ─── Playlist / carousel management ──────────────────────────────

    async def get_playlist(self) -> list[tuple[int, bytes]]:
        """Get the device playlist via ea 0c.

        Returns list of (flag, 16-byte-hash) tuples.
        flag=0x12 may indicate the currently displayed entry.
        """
        responses = await self._send_and_collect(b"\xea\x0c")
        for r in responses:
            if len(r) >= 4 and r[0] == 0x15 and r[1] == 0xea and r[2] == 0x0c:
                count = r[3]
                entries = []
                for i in range(count):
                    off = 4 + i * 17
                    if off + 17 > len(r):
                        break
                    entries.append((r[off], bytes(r[off + 1:off + 17])))
                return entries
        return []

    async def get_playlist_details(self) -> list[tuple[int, int, int, bytes]]:
        """Get playlist details via ea 0e.

        Returns list of (index, flag1, duration, 16-byte-hash) tuples.
        index: 1-based position in rotation (0 = not active).
        duration: display time in seconds (e.g. 10).
        """
        responses = await self._send_and_collect(b"\xea\x0e")
        for r in responses:
            if len(r) >= 4 and r[0] == 0x15 and r[1] == 0xea and r[2] == 0x0e:
                count = r[3]
                entries = []
                for i in range(count):
                    off = 4 + i * 19
                    if off + 19 > len(r):
                        break
                    entries.append((r[off], r[off + 1], r[off + 2],
                                    bytes(r[off + 3:off + 19])))
                return entries
        return []

    async def set_playlist(self, hashes: list[bytes], byte3: int = 0x00):
        """Set the device playlist via ea 0b.

        Args:
            hashes: List of 16-byte content hashes in desired play order.
            byte3: 0x00 when adding entries, 0x01 when removing/reordering.
        """
        cmd = bytearray([0xea, 0x0b, len(hashes), byte3])
        for h in hashes:
            cmd.append(0x00)
            cmd.extend(h)
        await self.send(bytes(cmd))
        await asyncio.sleep(0.5)

    async def set_playlist_details(self, hashes: list[bytes], duration: int = 10):
        """Set playlist entry details via ea 0d.

        Args:
            hashes: List of 16-byte content hashes (same order as set_playlist).
            duration: Display duration per entry in seconds (default 10).
        """
        cmd = bytearray([0xea, 0x0d, len(hashes)])
        for i, h in enumerate(hashes):
            cmd.append(i + 1)       # 1-based index
            cmd.append(0x00)        # flag1
            cmd.append(duration)    # display duration (seconds)
            cmd.extend(h)
        await self.send(bytes(cmd))
        await asyncio.sleep(0.5)


def make_solid_gif(r: int, g: int, b: int) -> bytes:
    """Generate a single-frame 96×16 GIF filled with one color."""
    img = Image.new('P', (96, 16))
    img.putpalette([0, 0, 0, r, g, b] + [0] * (256 * 3 - 6))
    img.putdata([1] * (96 * 16))
    buf = io.BytesIO()
    img.save(buf, format='GIF')
    return buf.getvalue()


def make_color_cycle_gif(colors: list[tuple[int, int, int]],
                         delay_ms: int = 500) -> bytes:
    """Generate a 96×16 GIF cycling through solid colors."""
    frames = []
    for r, g, b in colors:
        frames.append(Image.new('RGB', (96, 16), (r, g, b)))
    buf = io.BytesIO()
    frames[0].save(buf, format='GIF', save_all=True,
                   append_images=frames[1:],
                   duration=delay_ms, loop=0)
    return buf.getvalue()


def make_scrolling_text_gif(text: str,
                            fg: tuple[int, int, int] = (255, 0, 0),
                            bg: tuple[int, int, int] = (0, 0, 0),
                            font_size: int = 16,
                            step: int = 2) -> bytes:
    """Render scrolling text as a 96×16 GIF animation.

    The text scrolls from right to left across the display.
    step controls pixels per frame (higher = faster scroll, fewer frames).
    """
    if os.path.exists(FONT_PATH):
        font = ImageFont.truetype(FONT_PATH, font_size)
    else:
        font = ImageFont.load_default(size=font_size)
    # Measure text width
    bbox = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]

    # Full canvas: padding on both sides so text scrolls in and out
    canvas_w = text_w + 96 * 2
    canvas = Image.new('RGB', (canvas_w, 16), bg)
    draw = ImageDraw.Draw(canvas)
    # Center text vertically
    y_offset = (16 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((96, y_offset), text, fill=fg, font=font)

    # Generate frames: slide a 96×16 window across the canvas
    frames = []
    for x in range(0, canvas_w - 96 + 1, step):
        frame = canvas.crop((x, 0, x + 96, 16)).convert('P', palette=Image.ADAPTIVE, colors=2)
        frames.append(frame)

    buf = io.BytesIO()
    frames[0].save(buf, format='GIF', save_all=True,
                   append_images=frames[1:],
                   duration=30, loop=0)
    return buf.getvalue()


def pil_image_to_gif(image: Image.Image) -> bytes:
    """Convert any PIL Image to a 96×16 GIF (resized, quantized)."""
    resized = image.resize((96, 16), Image.LANCZOS)
    if resized.mode != 'P':
        resized = resized.convert('P', palette=Image.ADAPTIVE, colors=256)
    buf = io.BytesIO()
    resized.save(buf, format='GIF')
    return buf.getvalue()


async def main():
    address = "92CFA184-8B61-2363-4E4A-A8BAEFB80D17"

    display = SurplifeDisplay(address)
    await display.connect()
    await asyncio.sleep(0.5)

    await display.init()
    await display.power_on()
    await asyncio.sleep(1.0)

    # 1. Solid color (rot)
    print("\n--- Solid color: rot ---")
    await display.show_solid_color(0)
    await asyncio.sleep(5.0)

    # 2. Laufschrift
    print("\n--- Laufschrift ---")
    await display.show_text("Herzlichen Glückwunsch!", fg_color=(255, 0, 0))
    await asyncio.sleep(15.0)

    # 3. Farbwechsel-Animation
    print("\n--- Farbwechsel rot → grün → blau ---")
    gif = make_color_cycle_gif([(255, 0, 0), (0, 255, 0), (0, 0, 255)], delay_ms=500)
    await display.show_gif(gif, speed=50)
    await asyncio.sleep(10.0)

    # 4. Power off
    await display.power_off()
    await asyncio.sleep(0.5)
    await display.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
