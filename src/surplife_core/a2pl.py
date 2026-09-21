"""a2pl compression — LZ77 variant used for pixel data.

See A2PL.md for format details. Verified against all 97 traces.
"""

from __future__ import annotations

from .protocol import BYTES_PER_PIXEL, DISPLAY_ROWS

# Each column is DISPLAY_ROWS * BYTES_PER_PIXEL = 32 bytes.
COL_BYTES = DISPLAY_ROWS * BYTES_PER_PIXEL


def _sum_decode(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a sum-encoded integer. Returns (value, new_offset)."""
    value = 0
    while offset < len(data) and data[offset] == 0xFF:
        value += 255
        offset += 1
    if offset < len(data):
        value += data[offset]
        offset += 1
    return value, offset


def _sum_encode(value: int) -> bytes:
    """Encode an integer using sum-encoding."""
    result = bytearray()
    while value >= 255:
        result.append(0xFF)
        value -= 255
    result.append(value)
    return bytes(result)


def decompress(data: bytes) -> bytes:
    """Decompress a2pl data to a raw framebuffer.

    Each command byte encodes:
      - Upper nibble: literal count (0-14 direct, 15 = 15 + sum-encoded)
      - Lower nibble: backref length base (0-14 → +4, 15 → 19 + sum-encoded)
    """
    fb = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        upper = (b >> 4) & 0x0F
        lower = b & 0x0F
        i += 1

        if upper == 0x0F:
            ext, i = _sum_decode(data, i)
            n_lit = 15 + ext
        else:
            n_lit = upper

        fb.extend(data[i:i + n_lit])
        i += n_lit

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


def _find_best_match(fb: bytes, pos: int, max_search: int = 0) -> tuple[int, int]:
    """Find the longest LZ77 match for fb[pos:] in fb[:pos].

    Returns (distance, length). Length 0 means no match found.
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
        if fb[start] != fb[pos]:
            continue
        length = 0
        while length < remaining and fb[pos + length] == fb[start + (length % dist)]:
            length += 1
        if length > best_len:
            best_len = length
            best_dist = dist
            if best_len >= remaining:
                break

    return best_dist, best_len


def _emit_command(out: bytearray, fb: bytes, pos: int, n_lit: int,
                  match_dist: int, match_len: int) -> None:
    """Emit one a2pl command: n_lit literal bytes + backref."""
    upper = min(n_lit, 15)
    lower = min(match_len - 4, 15) if match_len >= 4 else 0
    out.append((upper << 4) | lower)

    if n_lit >= 15:
        out.extend(_sum_encode(n_lit - 15))

    out.extend(fb[pos:pos + n_lit])

    out.extend(match_dist.to_bytes(2, 'little'))

    if match_len > 18:
        out.extend(_sum_encode(match_len - 19))


def compress(fb: bytes, max_search: int = 0) -> bytes:
    """Compress a raw framebuffer to a2pl format.

    Args:
        fb: Raw framebuffer bytes (column-major, must be multiple of 32).
        max_search: Max search distance (0 = unlimited, 128-256 for speed).
    """
    if len(fb) == 0 or len(fb) % COL_BYTES != 0:
        raise ValueError(f"Framebuffer must be a multiple of {COL_BYTES} bytes, got {len(fb)}")

    out = bytearray()
    pos = 0

    while pos < len(fb):
        remaining = len(fb) - pos

        if remaining <= 15:
            out.append((remaining << 4) | 0x0)
            out.extend(fb[pos:pos + remaining])
            break

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
            if remaining <= 15:
                out.append((remaining << 4) | 0x0)
            else:
                out.append(0xF0)
                out.extend(_sum_encode(remaining - 15))
            out.extend(fb[pos:pos + remaining])
            break

        # Ensure stream never ends on a backref boundary (firmware bug).
        tail = remaining - match_at - match_len
        if tail == 0 and match_len > 4:
            trim = min(5, match_len - 4)
            match_len -= trim

        _emit_command(out, fb, pos, match_at, match_dist, match_len)
        pos += match_at + match_len

    return bytes(out)
