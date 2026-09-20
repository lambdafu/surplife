"""
Test a2pl pixel data by sending raw payloads to the display.

Usage:
    python3 test_a2pl.py replay       — replay exact (5,3) red pixel from trace 34
    python3 test_a2pl.py pixel X Y    — red pixel at column X, row Y (0-based)
    python3 test_a2pl.py raw HEXDATA  — send arbitrary a2pl pixel data (hex string)
    python3 test_a2pl.py draw HEXDATA — send arbitrary a2pl pixel data via ea 11 (direct draw)
    python3 test_a2pl.py clear        — clear display (all black) via ea 11
    python3 test_a2pl.py rainbow [EFFECT] [SPEED] — rainbow with animation effect + speed
        effects: static, scroll-r, scroll-l, flicker, breathe, snowflake,
                 blend, sweep, bands, wipe (or 1-10)
    python3 test_a2pl.py life [SEED]  — Conway's Game of Life (cell age → orange to blue)
    python3 test_a2pl.py clock [STYLE] [date] [12h] — firmware clock (style 0-7, +date, +12h)
    python3 test_a2pl.py text "MESSAGE" [SPEED] [GRADIENT] — scrolling text
        gradients: rainbow, warm, cool, viridis, fire, ocean, sunset, neon, ice, forest
        custom: hues:0,120,240 (comma-separated hue degrees 0-360)
    python3 test_a2pl.py gif FILE [SPEED] — upload and play GIF animation
    python3 test_a2pl.py img FILE [EFFECT] [SPEED] — upload and show static image (PNG/JPG/BMP)
    python3 test_a2pl.py wave [STYLE]  — audio waveform demo (sine wave, style 1-13/ff)
    python3 test_a2pl.py wave-style S [brightness] [speed] [H,H,...] — configure waveform style
    python3 test_a2pl.py wide PATTERN|IMAGE [SPEED] — scroll wide image
        patterns: rainbow, wolfram [RULE] [PAGES] [snail|hue|bw],
                  moire [PAGES], mandelbrot [PAGES], julia [PAGES], plasma [PAGES]
    python3 test_a2pl.py playlist          — show current device playlist
    python3 test_a2pl.py playlist-add HASH [DURATION] — add content hash to playlist
    python3 test_a2pl.py playlist-rm HASH  — remove content hash from playlist
"""

import asyncio
import json
import math
import os
import random
import sys
import uuid

from surplife import SurplifeDisplay, compress_a2pl, encode_hsv, content_hash
from surplife import DISPLAY_COLS, DISPLAY_ROWS, BYTES_PER_PIXEL, FRAMEBUFFER_SIZE

ADDRESS = "92CFA184-8B61-2363-4E4A-A8BAEFB80D17"



def make_a2pl_payload(pixel_data: bytes) -> bytes:
    """Wrap pixel data in the 6-byte a2pl payload header."""
    return b"\x00\x00\x00\x00" + len(pixel_data).to_bytes(2, 'big') + pixel_data


def make_meta(data_len: int) -> bytes:
    """Generate JSON metadata for a graffiti upload."""
    layer_id = str(uuid.uuid4())
    meta = json.dumps({
        "v": 1, "mant_type": 0, "enable_a2pl": 1,
        "layers": [{
            "nm": layer_id, "type": "c", "amt_pos": 0,
            "frame_num": 1, "amt_length": 6 + data_len, "amt_fmt": 0,
        }],
        "all_file_type": "c"
    }, separators=(',', ':'))
    return meta.encode('ascii')


async def send_a2pl(display: SurplifeDisplay, pixel_data: bytes):
    """Upload a2pl pixel data and activate it."""
    payload = make_a2pl_payload(pixel_data)
    meta = make_meta(len(pixel_data))
    c_hash = content_hash("c", payload)
    await display._upload_content(0x00, meta, payload, b"\xea\x09\x00\x50\x01", c_hash=c_hash)
    print(f"Sent {len(pixel_data)} bytes of pixel data")
    print(f"  hash: {c_hash.hex()}")
    print(f"  hex: {pixel_data.hex(' ')}")


async def send_ea11(display: SurplifeDisplay, pixel_data: bytes):
    """Send a2pl pixel data via ea 11 direct draw. Max 255 bytes."""
    assert len(pixel_data) <= 255, f"ea 11 payload too large: {len(pixel_data)} bytes (max 255)"
    ea11_cmd = b"\xea\x11\x00\x00\x00" + bytes([len(pixel_data)]) + pixel_data
    await display.send(ea11_cmd)


def build_single_pixel(x: int, y: int, color: bytes) -> bytes:
    """Build compressed a2pl data for a single pixel at (x,y)."""
    fb = bytearray(FRAMEBUFFER_SIZE)
    pos = x * DISPLAY_ROWS * BYTES_PER_PIXEL + y * BYTES_PER_PIXEL
    fb[pos] = color[0]
    fb[pos + 1] = color[1]
    return compress_a2pl(bytes(fb))


def build_rainbow_fb(num_cols: int = DISPLAY_COLS) -> bytes:
    """Build a rainbow gradient framebuffer (column-major HSV)."""
    fb = bytearray(num_cols * DISPLAY_ROWS * BYTES_PER_PIXEL)
    for col in range(num_cols):
        hue = round(col / num_cols * 360) % 360
        color = encode_hsv(hue, 100, 100)
        for row in range(DISPLAY_ROWS):
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            fb[pos] = color[0]
            fb[pos + 1] = color[1]
    return bytes(fb)


# Age color palette: orange → blue in discrete steps.
# Fewer unique colors = much better LZ77 compression.
_AGE_PALETTE = [encode_hsv(h, 100, 100) for h in [30, 60, 120, 180, 240]]


def age_to_hsv(age: int) -> bytes:
    """Map cell age to HSV color: orange (30) → blue (240) in 5 steps."""
    idx = min(age - 1, len(_AGE_PALETTE) - 1)
    return _AGE_PALETTE[idx]


def life_step(grid: list[list[int]]) -> list[list[int]]:
    """Advance one Game of Life generation. Cell values = age (0 = dead)."""
    rows, cols = len(grid), len(grid[0])
    new = [[0] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            alive_neighbors = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = (r + dr) % rows, (c + dc) % cols
                    if grid[nr][nc] > 0:
                        alive_neighbors += 1
            if grid[r][c] > 0:
                if alive_neighbors in (2, 3):
                    new[r][c] = grid[r][c] + 1
            else:
                if alive_neighbors == 3:
                    new[r][c] = 1
    return new


def life_to_framebuffer(grid: list[list[int]]) -> bytes:
    """Convert Game of Life grid to column-major HSV framebuffer."""
    fb = bytearray(FRAMEBUFFER_SIZE)
    for col in range(DISPLAY_COLS):
        for row in range(DISPLAY_ROWS):
            pos = col * DISPLAY_ROWS * BYTES_PER_PIXEL + row * BYTES_PER_PIXEL
            if grid[row][col] > 0:
                hsv = age_to_hsv(grid[row][col])
                fb[pos] = hsv[0]
                fb[pos + 1] = hsv[1]
            # else: stays 0x00 0x00 (black)
    return bytes(fb)


def life_init(seed: int | None = None, density: float = 0.20) -> list[list[int]]:
    """Create a random initial grid."""
    rng = random.Random(seed)
    return [[1 if rng.random() < density else 0
             for _ in range(DISPLAY_COLS)]
            for _ in range(DISPLAY_ROWS)]


async def run_life(display: SurplifeDisplay, seed: int | None = None):
    """Run Conway's Game of Life on the display."""
    grid = life_init(seed)
    gen = 0
    alive = sum(cell > 0 for row in grid for cell in row)
    print(f"Game of Life: 96x16, seed={seed}, {alive} initial cells")
    print("Press Ctrl+C to stop")

    try:
        while True:
            fb = life_to_framebuffer(grid)
            compressed = compress_a2pl(fb)

            if len(compressed) <= 255:
                await send_ea11(display, compressed)
            else:
                await send_a2pl(display, compressed)

            alive = sum(cell > 0 for row in grid for cell in row)
            mode = "ea11" if len(compressed) <= 255 else "upld"
            print(f"\rGen {gen}: {alive} cells, {len(compressed)}B [{mode}]",
                  end="", flush=True)

            if alive == 0:
                print("\nAll cells dead. Restarting...")
                grid = life_init()
                gen = 0
                await asyncio.sleep(1.0)
                continue

            grid = life_step(grid)
            gen += 1
    except KeyboardInterrupt:
        print(f"\nStopped at generation {gen}")


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    display = SurplifeDisplay(ADDRESS)
    await display.connect()
    await asyncio.sleep(0.5)
    await display.init()
    await asyncio.sleep(1.0)

    if cmd == "life":
        seed = int(sys.argv[2]) if len(sys.argv) > 2 else None
        await run_life(display, seed)
        await display.disconnect()
        return

    if cmd == "replay":
        pixel_data = bytes.fromhex(
            "1f000100922f01ffa700920f0200ffffffffffffffffffffa5500000000000"
        )
        print("Replaying trace 34: red pixel at (5,3)")
        await send_a2pl(display, pixel_data)

    elif cmd == "pixel" and len(sys.argv) >= 4:
        x, y = int(sys.argv[2]), int(sys.argv[3])
        pixel_data = build_single_pixel(x, y, encode_hsv(0, 100, 100))
        print(f"Sending red pixel at ({x},{y})")
        await send_a2pl(display, pixel_data)

    elif cmd == "raw":
        hex_str = sys.argv[2].replace(' ', '')
        pixel_data = bytes.fromhex(hex_str)
        print(f"Sending raw pixel data via upload protocol")
        await send_a2pl(display, pixel_data)

    elif cmd == "rainbow":
        EFFECTS = {
            "static": 1, "scroll-r": 2, "scroll-l": 3, "flicker": 4,
            "breathe": 5, "snowflake": 6, "blend": 7, "sweep": 8,
            "bands": 9, "wipe": 10,
        }
        EFFECT_NAMES = {v: k for k, v in EFFECTS.items()}
        arg = sys.argv[2] if len(sys.argv) > 2 else "static"
        effect = EFFECTS.get(arg) or int(arg)
        speed = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        fb = build_rainbow_fb()
        compressed = compress_a2pl(fb)
        # Upload as static image, then activate with effect
        layer_id = str(uuid.uuid4())
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": layer_id, "type": "a", "amt_pos": 0,
                "frame_num": 1, "amt_length": 6 + len(compressed), "amt_fmt": 0,
            }],
            "all_file_type": "a"
        }, separators=(',', ':'))
        payload = make_a2pl_payload(compressed)
        c_hash = content_hash("a", payload)
        # ea 06: slot 0, speed, effect_id
        await display._upload_content(
            0x04, meta.encode('ascii'), payload,
            bytes([0xea, 0x06, 0x00, speed, effect]), c_hash=c_hash)
        name = EFFECT_NAMES.get(effect, f"#{effect}")
        print(f"Rainbow ({len(compressed)}B, {name}, speed {speed}) hash={c_hash.hex()}")

    elif cmd == "clear":
        pixel_data = bytes.fromhex(
            "2f 00 00 02 00 ff ff ff ff ff ff ff ff ff ff ff f1 50 00 00 00 00 00"
            .replace(' ', '')
        )
        await send_ea11(display, pixel_data)
        print("Cleared display (all black)")

    elif cmd == "draw":
        hex_str = sys.argv[2].replace(' ', '')
        pixel_data = bytes.fromhex(hex_str)
        await send_ea11(display, pixel_data)
        print(f"Sent {len(pixel_data)} bytes via ea 11 direct draw")
        print(f"  hex: {pixel_data.hex(' ')}")

    elif cmd == "text":
        GRADIENTS = {
            "rainbow":  [0, 60, 120, 180, 240, 300],
            "warm":     [0, 30, 60],
            "cool":     [180, 210, 240],
            "viridis":  [288, 223, 177, 122, 54],
            "fire":     [0, 15, 30, 45, 60],
            "ocean":    [180, 200, 220, 240],
            "sunset":   [0, 20, 40, 270, 300],
            "neon":     [300, 270, 180, 120],
            "ice":      [180, 195, 210, 225, 240],
            "forest":   [90, 110, 130, 150],
        }
        text = sys.argv[2] if len(sys.argv) > 2 else "Hello World!"
        speed = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        opts = sys.argv[4:]
        gradient = None
        for opt in opts:
            if opt in GRADIENTS:
                gradient = GRADIENTS[opt]
            elif opt.startswith("hues:"):
                gradient = [int(h) for h in opt[5:].split(',')]
        await display.show_text(text, speed=speed, gradient=gradient)

    elif cmd == "clock":
        # ea 10 [style] 02 [date_flag] — activate firmware clock
        # style: 0-7 (8 different clock faces)
        # date_flag: 00=no date, 01=show date
        style = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        opts = sys.argv[3:]
        show_date = 1 if "date" in opts else 0
        time_fmt = 0x01 if "12h" in opts else 0x02  # 01=12h, 02=24h
        assert 0 <= style <= 7, f"Clock style must be 0-7, got {style}"
        await display.send(bytes([0xea, 0x10, style, time_fmt, show_date]))
        fmt_str = "12h" if time_fmt == 0x01 else "24h"
        date_str = " +date" if show_date else ""
        print(f"Activated clock style {style} ({fmt_str}{date_str})")

    elif cmd == "wave-style":
        # e1 05 — configure waveform style
        style = int(sys.argv[2], 0) if len(sys.argv) > 2 else 1
        brightness = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        speed = int(sys.argv[4]) if len(sys.argv) > 4 else 100

        # Build e1 05 command
        cmd_bytes = bytearray([0xe1, 0x05, 0x00, brightness, style])
        if len(sys.argv) > 5:
            # Custom colors: comma-separated hue values
            hues = [int(h) for h in sys.argv[5].split(',')]
            cmd_bytes.append(0x02)  # sub_style = custom colors
            cmd_bytes.append(len(hues))  # num_bars
            cmd_bytes.append(speed)
            cmd_bytes.extend(bytes(17))  # padding
            # Color block: a1 00 00 00 [N] [a1 H S V] × N
            cmd_bytes.extend(bytes([0xa1, 0x00, 0x00, 0x00, len(hues)]))
            for h in hues:
                cmd_bytes.extend(bytes([0xa1, h, 100, 100]))
        else:
            cmd_bytes.append(0x00)  # sub_style = default
            cmd_bytes.append(0x00)  # num_bars
            cmd_bytes.append(speed)
            cmd_bytes.extend(bytes(17))  # padding
        await display.send(bytes(cmd_bytes))
        print(f"Configured waveform style {style} (brightness={brightness}, speed={speed})")
        if len(sys.argv) > 5:
            print(f"  Custom hues: {sys.argv[5]}")

    elif cmd == "wave":
        # ea 0f [style] 00 + 96 bytes (one amplitude per column, 0-100)
        # Demo: animated sine wave
        style_id = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x01
        print(f"Audio waveform demo, style {style_id:#x} (Ctrl+C to stop)")
        frame = 0
        try:
            while True:
                amplitudes = bytearray(96)
                for col in range(96):
                    phase = (col / 96 * 4 * math.pi) + (frame * 0.15)
                    val = math.sin(phase) * 0.5 + 0.5  # 0.0–1.0
                    amplitudes[col] = int(val * 100)
                await display.send(bytes([0xea, 0x0f, style_id, 0x00]) + bytes(amplitudes))
                print(f"\rFrame {frame}", end="", flush=True)
                frame += 1
                await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            # Send flat line to clear
            await display.send(bytes([0xea, 0x0f, style_id, 0x00]) + bytes(96))
            print(f"\nStopped at frame {frame}")

    elif cmd == "gif":
        path = sys.argv[2] if len(sys.argv) > 2 else None
        if not path:
            print("Usage: gif FILE [SPEED]")
            await display.disconnect()
            return
        speed = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        await display.show_gif_file(path, speed=speed)

    elif cmd == "img":
        from PIL import Image
        EFFECTS = {
            "static": 1, "scroll-r": 2, "scroll-l": 3, "flicker": 4,
            "breathe": 5, "snowflake": 6, "blend": 7, "sweep": 8,
            "bands": 9, "wipe": 10,
        }
        path = sys.argv[2] if len(sys.argv) > 2 else None
        if not path:
            print("Usage: img FILE [EFFECT] [SPEED]")
            print("  effects: static, scroll-r, scroll-l, flicker, breathe,")
            print("           snowflake, blend, sweep, bands, wipe (or 1-10)")
            await display.disconnect()
            return
        effect_arg = sys.argv[3] if len(sys.argv) > 3 else "static"
        effect = EFFECTS.get(effect_arg) or int(effect_arg)
        speed = int(sys.argv[4]) if len(sys.argv) > 4 else 50
        img = Image.open(path).convert('RGB').resize((DISPLAY_COLS, DISPLAY_ROWS), Image.LANCZOS)
        from surplife import compress_image, content_hash as _ch, image_to_framebuffer
        compressed = compress_image(img)
        payload = make_a2pl_payload(compressed)
        c_hash = _ch("a", payload)
        layer_id = str(uuid.uuid4())
        meta = json.dumps({
            "v": 1, "mant_type": 0, "enable_a2pl": 1,
            "layers": [{
                "nm": layer_id, "type": "a", "amt_pos": 0,
                "frame_num": 1, "amt_length": 6 + len(compressed), "amt_fmt": 0,
            }],
            "all_file_type": "a"
        }, separators=(',', ':')).encode('ascii')
        await display._upload_content(0x04, meta, payload,
                                      bytes([0xea, 0x06, 0x00, speed, effect]),
                                      c_hash=c_hash)
        name = {v: k for k, v in EFFECTS.items()}.get(effect, f"#{effect}")
        print(f"Image {path} sent ({name}, speed {speed}) hash={c_hash.hex()}")

    elif cmd == "wide":
        from PIL import Image
        import colorsys as _cs
        arg = sys.argv[2] if len(sys.argv) > 2 else None

        def parse_wide_opts(args, defaults=None):
            """Parse wide subcommand options from arg list.
            Returns dict with 'pages', 'speed', 'mode', plus any extra string args."""
            opts = {"pages": 6, "speed": 50, "mode": "a", "extra": []}
            if defaults:
                opts.update(defaults)
            for a in args:
                if a in ("a", "e") and len(a) == 1:
                    opts["mode"] = a
                elif a.isdigit():
                    n = int(a)
                    if n > 100:
                        opts["pages"] = n // DISPLAY_COLS or 1  # interpret as total cols
                    elif n > 20:
                        opts["speed"] = n
                    else:
                        # Could be pages or speed — pages if ≤20
                        if "pages_set" not in opts:
                            opts["pages"] = n
                            opts["pages_set"] = True
                        else:
                            opts["speed"] = n
                else:
                    opts["extra"].append(a)
            return opts

        if arg == "rainbow":
            o = parse_wide_opts(sys.argv[3:])
            num_cols = DISPLAY_COLS * 3
            img = Image.new('HSV', (num_cols, DISPLAY_ROWS))
            for x in range(num_cols):
                hue = int(x / num_cols * 255)
                for y in range(DISPLAY_ROWS):
                    img.putpixel((x, y), (hue, 255, 255))
            img = img.convert('RGB')
            print(f"Generated 3-page rainbow ({num_cols}x{DISPLAY_ROWS})")

        elif arg == "wolfram":
            # wide wolfram [RULE] [PAGES] [PALETTE] [SPEED]
            # e.g.: wide wolfram 30 6 snail 50
            rule = 30
            raw = sys.argv[3:]
            # First numeric arg = rule, second = pages, rest parsed normally
            nums = []
            strs = []
            for a in raw:
                if a in ("a", "e") and len(a) == 1:
                    strs.append(a)
                elif a.isdigit():
                    nums.append(int(a))
                else:
                    strs.append(a)
            rule = nums[0] if len(nums) > 0 else 30
            pages = nums[1] if len(nums) > 1 else 6
            speed = nums[2] if len(nums) > 2 else 50
            palette = "snail"
            mode = "a"
            for s in strs:
                if s in ("snail", "hue", "bw"):
                    palette = s
                elif s in ("a", "e"):
                    mode = s
            num_cols = DISPLAY_COLS * pages
            ruleset = [(rule >> i) & 1 for i in range(8)]

            rng = random.Random(42)
            cells = [rng.randint(0, 1) for _ in range(DISPLAY_ROWS)]
            img = Image.new('RGB', (num_cols, DISPLAY_ROWS))

            for col in range(num_cols):
                for row in range(DISPLAY_ROWS):
                    if cells[row]:
                        if palette == "hue":
                            h = col / num_cols
                            r, g, b = _cs.hsv_to_rgb(h, 0.9, 1.0)
                            img.putpixel((col, row), (int(r*255), int(g*255), int(b*255)))
                        elif palette == "snail":
                            t = col / num_cols
                            r = int(180 + 60 * math.sin(t * 3))
                            g = int(120 + 40 * math.sin(t * 5))
                            b = int(60 + 30 * math.sin(t * 7))
                            img.putpixel((col, row), (r, g, b))
                        else:
                            img.putpixel((col, row), (255, 255, 255))
                new = [0] * DISPLAY_ROWS
                for row in range(DISPLAY_ROWS):
                    left = cells[(row - 1) % DISPLAY_ROWS]
                    center = cells[row]
                    right = cells[(row + 1) % DISPLAY_ROWS]
                    idx = (left << 2) | (center << 1) | right
                    new[row] = ruleset[idx]
                cells = new
            o = {"speed": speed, "mode": mode}
            print(f"Generated Wolfram Rule {rule} ({num_cols}x{DISPLAY_ROWS}, {palette})")

        elif arg == "moire":
            o = parse_wide_opts(sys.argv[3:])
            num_cols = DISPLAY_COLS * o["pages"]
            img = Image.new('RGB', (num_cols, DISPLAY_ROWS))
            freq1, freq2 = 0.15, 0.17
            angle = 0.08
            for x in range(num_cols):
                for y in range(DISPLAY_ROWS):
                    v1 = math.sin(x * freq1 + y * 0.3)
                    v2 = math.sin(x * freq2 * math.cos(angle) + y * freq2 * math.sin(angle) + x * 0.02)
                    v = (v1 + v2) / 2.0
                    v = (v + 1) / 2
                    hue = (x / num_cols + v * 0.3) % 1.0
                    r, g, b = _cs.hsv_to_rgb(hue, 0.8 + 0.2 * v, v)
                    img.putpixel((x, y), (int(r*255), int(g*255), int(b*255)))
            print(f"Generated moiré pattern ({num_cols}x{DISPLAY_ROWS})")

        elif arg == "mandelbrot":
            o = parse_wide_opts(sys.argv[3:])
            num_cols = DISPLAY_COLS * o["pages"]
            re_min, re_max = -2.2, 0.8
            im_range = (re_max - re_min) * DISPLAY_ROWS / num_cols
            im_min, im_max = -im_range / 2, im_range / 2
            max_iter = 80
            img = Image.new('RGB', (num_cols, DISPLAY_ROWS))
            for x in range(num_cols):
                for y in range(DISPLAY_ROWS):
                    c = complex(
                        re_min + (x / num_cols) * (re_max - re_min),
                        im_min + (y / DISPLAY_ROWS) * (im_max - im_min))
                    z = 0
                    for it in range(max_iter):
                        z = z * z + c
                        if abs(z) > 2:
                            break
                    if it < max_iter - 1:
                        hue = (it / max_iter * 3) % 1.0
                        r, g, b = _cs.hsv_to_rgb(hue, 0.9, 1.0)
                        img.putpixel((x, y), (int(r*255), int(g*255), int(b*255)))
            print(f"Generated Mandelbrot ({num_cols}x{DISPLAY_ROWS}, {max_iter} iter)")

        elif arg == "julia":
            o = parse_wide_opts(sys.argv[3:])
            num_cols = DISPLAY_COLS * o["pages"]
            max_iter = 60
            img = Image.new('RGB', (num_cols, DISPLAY_ROWS))
            for x in range(num_cols):
                t = x / num_cols * 2 * math.pi
                c = complex(-0.7 + 0.27 * math.cos(t), 0.27 * math.sin(t))
                for y in range(DISPLAY_ROWS):
                    z = complex(-1.5 + 3.0 * 0.5,
                                -1.5 + (y / DISPLAY_ROWS) * 3.0)
                    for it in range(max_iter):
                        z = z * z + c
                        if abs(z) > 2:
                            break
                    if it < max_iter - 1:
                        hue = (it / max_iter * 2.5 + x / num_cols) % 1.0
                        r, g, b = _cs.hsv_to_rgb(hue, 0.85, 0.9)
                        img.putpixel((x, y), (int(r*255), int(g*255), int(b*255)))
            print(f"Generated Julia set ({num_cols}x{DISPLAY_ROWS})")

        elif arg == "plasma":
            o = parse_wide_opts(sys.argv[3:])
            num_cols = DISPLAY_COLS * o["pages"]
            img = Image.new('RGB', (num_cols, DISPLAY_ROWS))
            for x in range(num_cols):
                for y in range(DISPLAY_ROWS):
                    v1 = math.sin(x * 0.05 + y * 0.1)
                    v2 = math.sin(x * 0.03 - y * 0.15 + 2.0)
                    v3 = math.sin(math.sqrt((x * 0.07) ** 2 + (y * 0.2) ** 2) + 1.5)
                    v4 = math.sin(x * 0.02 + math.sin(y * 0.3 + x * 0.01))
                    v = (v1 + v2 + v3 + v4) / 4.0
                    hue = ((v + 1) / 2) % 1.0
                    sat = 0.7 + 0.3 * ((v1 + 1) / 2)
                    val = 0.6 + 0.4 * ((v3 + 1) / 2)
                    r, g, b = _cs.hsv_to_rgb(hue, sat, val)
                    img.putpixel((x, y), (int(r*255), int(g*255), int(b*255)))
            print(f"Generated plasma ({num_cols}x{DISPLAY_ROWS})")

        elif arg and not arg.startswith("-"):
            o = parse_wide_opts(sys.argv[3:])
            img = Image.open(arg)
            print(f"Uploading {arg} ({img.width}x{img.height})")
        else:
            print("Usage: wide PATTERN|IMAGE [PAGES] [SPEED] [MODE]")
            print("  Patterns: rainbow, wolfram [RULE] [PAGES] [snail|hue|bw],")
            print("            moire, mandelbrot, julia, plasma")
            print("  PAGES: number of 96-col pages (default 6)")
            print("  SPEED: scroll speed 1-100 (default 50)")
            print("  MODE: a = image protocol (default), e = text protocol")
            await display.disconnect()
            return
        await display.show_wide_image(img, speed=o.get("speed", 50), mode=o.get("mode", "a"))

    elif cmd == "playlist":
        entries = await display.get_playlist()
        if not entries:
            print("Playlist is empty")
        else:
            print(f"Playlist ({len(entries)} entries):")
            details = await display.get_playlist_details()
            detail_map = {h: (idx, f1, dur) for idx, f1, dur, h in details}
            for i, (flag, h) in enumerate(entries):
                d = detail_map.get(h)
                if d:
                    idx, f1, dur = d
                    state = f"#{idx}" if idx > 0 else "off"
                    print(f"  {i}: {h.hex()}  flag=0x{flag:02x}  [{dur}s, {state}]")
                else:
                    print(f"  {i}: {h.hex()}  flag=0x{flag:02x}")

    elif cmd == "playlist-add":
        hash_hex = sys.argv[2] if len(sys.argv) > 2 else None
        if not hash_hex:
            print("Usage: playlist-add HASH [DURATION]")
            await display.disconnect()
            return
        duration = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        new_hash = bytes.fromhex(hash_hex)
        assert len(new_hash) == 16, "Hash must be 16 bytes (32 hex chars)"

        entries = await display.get_playlist()
        hashes = [h for _, h in entries]
        hashes.append(new_hash)

        await display.set_playlist(hashes, byte3=0x00)
        await display.set_playlist_details(hashes, duration=duration)
        print(f"Added {hash_hex} to playlist ({len(hashes)} entries, {duration}s each)")

    elif cmd == "playlist-rm":
        hash_hex = sys.argv[2] if len(sys.argv) > 2 else None
        if not hash_hex:
            print("Usage: playlist-rm HASH")
            await display.disconnect()
            return
        rm_hash = bytes.fromhex(hash_hex)

        entries = await display.get_playlist()
        hashes = [h for _, h in entries if h != rm_hash]

        if len(hashes) == len(entries):
            print(f"Hash {hash_hex} not found in playlist")
        else:
            await display.set_playlist(hashes, byte3=0x01)
            if hashes:
                await display.set_playlist_details(hashes)
            print(f"Removed from playlist ({len(hashes)} entries remaining)")

    else:
        print(__doc__)
        await display.disconnect()
        return

    await asyncio.sleep(2.0)
    await display.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
