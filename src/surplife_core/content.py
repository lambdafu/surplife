"""Content encoding: a2pl payloads, content hashes, GIF validation.

Pure logic extracted from the display driver so both the CLI transport and
the Home Assistant integration can build device content without BLE.
"""

from __future__ import annotations

import hashlib
import io
import json

from .a2pl import compress
from .protocol import DISPLAY_COLS, FRAMEBUFFER_SIZE


def build_a2pl_payload(fb: bytes, num_cols: int) -> tuple[bytes, int, int]:
    """Build an a2pl payload with offset table from a wide framebuffer.

    Args:
        fb: Column-major HSV framebuffer (num_cols * 16 * 2 bytes).
        num_cols: Total column count of the framebuffer.

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


def content_hash(content_type: str, payload: bytes,
                 attr: dict | None = None) -> bytes:
    """Compute the 16-byte MD5 hash used for device-side content caching."""
    h = hashlib.md5()
    h.update(content_type.encode())
    h.update(payload)
    if attr:
        h.update(json.dumps(attr, sort_keys=True, separators=(',', ':')).encode())
    return h.digest()


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
         (f"{report['local_color_tables']} local color table(s); the device "
         "supports only a single global color table")),
        (report["min_frame_delay_cs"] is None or report["min_frame_delay_cs"] >= 10,
         (f"frame delay {report['min_frame_delay_cs']}cs too fast; use >= 10cs "
         "(100ms, <=10 fps)")),
        (report["size_bytes"] <= 64 * 1024,
         f"GIF is {report['size_bytes']} bytes; observed-safe size is <= 64KB"),
    ]
    report["violations"] = [msg for ok, msg in checks if not ok]
    report["ok"] = not report["violations"]

    if strict and not report["ok"]:
        raise ValueError(
            "GIF outside the device-safe envelope (risk of firmware crash):\n  "
            + "\n  ".join(report["violations"]))
    return report


def fix_gif(gif_data: bytes, frame_delay_cs: int = 15,
            max_colors: int = 128) -> bytes:
    """Re-quantize a GIF into the device-safe envelope.

    Renders every frame as RGB, quantizes against one shared palette
    (so PIL emits a single global color table and no local tables), and
    forces the frame delay. Requires Pillow.

    Args:
        gif_data: Raw GIF bytes (any palette structure / frame timing).
        frame_delay_cs: Per-frame delay in centiseconds (>= 10 required).
        max_colors: Palette size (128 = the app's proven envelope).

    Returns:
        Device-safe GIF bytes.
    """
    from PIL import Image

    frame_delay_cs = max(frame_delay_cs, 10)

    src = Image.open(io.BytesIO(gif_data))
    n_frames = getattr(src, 'n_frames', 1)

    # Composite palette from all frames so every frame quantizes identically
    composite = Image.new('RGB', src.size)
    for i in range(n_frames):
        src.seek(i)
        frame = src.convert('RGB')
        if i == 0:
            composite = frame.copy()
        else:
            composite = Image.blend(composite, frame, 1.0 / (i + 1))
    pal_img = composite.quantize(colors=max_colors)

    frames = []
    for i in range(n_frames):
        src.seek(i)
        frame = src.convert('RGB')
        frames.append(frame.quantize(palette=pal_img, dither=Image.Dither.NONE))

    out = io.BytesIO()
    frames[0].save(out, format='GIF', save_all=True,
                   append_images=frames[1:], duration=frame_delay_cs * 10,
                   loop=0, optimize=False)
    return _strip_local_color_tables(out.getvalue())


def _strip_local_color_tables(gif_data: bytes) -> bytes:
    """Remove local color tables from a GIF, clearing the LCT flag.

    Only safe when every frame uses the same palette (which fix_gif
    guarantees by quantizing against one shared palette). The LCT content
    is then identical to the global table, so clearing the flag suffices.
    """
    data = gif_data
    packed = data[10]
    idx = 13 + (3 * (2 << (packed & 7)) if (packed >> 7) & 1 else 0)
    out = bytearray(data[:idx])
    while idx < len(data):
        b = data[idx]
        if b == 0x21:
            start = idx
            idx += 2
            while idx < len(data) and data[idx] != 0:
                idx += data[idx] + 1
            idx += 1
            out += data[start:idx]
        elif b == 0x2C:
            ip = data[idx + 9]
            if (ip >> 7) & 1:
                entries = 2 ** ((ip & 7) + 1)
                out += data[idx:idx + 9]
                out.append(ip & 0x7F)          # clear the LCT flag
                idx += 10 + 3 * entries        # skip the local table
                out.append(data[idx])
                idx += 1  # LZW min code size
                while idx < len(data) and data[idx] != 0:
                    n = data[idx]
                    out += data[idx:idx + n + 1]
                    idx += n + 1
                out += b"\x00"
                idx += 1
            else:
                start = idx
                idx += 10
                idx += 1
                while idx < len(data) and data[idx] != 0:
                    idx += data[idx] + 1
                idx += 1
                out += data[start:idx]
        elif b == 0x3B:
            out += b"\x3B"
            break
        else:
            idx += 1
    return bytes(out)
