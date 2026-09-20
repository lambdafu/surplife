"""Scan a single red pixel across the display for visual verification."""

import asyncio
import sys

from surplife import SurplifeDisplay
from test_a2pl import build_single_pixel

ADDRESS = "92CFA184-8B61-2363-4E4A-A8BAEFB80D17"


async def draw_direct(display: SurplifeDisplay, pixel_data: bytes):
    """Send a2pl pixel data via ea 11 direct draw (no upload protocol)."""
    size = len(pixel_data)
    cmd = b"\xea\x11\x00\x00" + size.to_bytes(2, 'big') + pixel_data
    await display.send(cmd)


async def main():
    # Usage: test_a2pl_scan.py [delay] [start_x] [start_y]
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.3
    start_x = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    start_y = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    display = SurplifeDisplay(ADDRESS)
    display._on_notify = lambda s, d: None  # suppress notify prints
    await display.connect()
    await asyncio.sleep(0.5)
    await display.init()
    await asyncio.sleep(1.0)

    count = 0
    try:
        for x in range(max(1, start_x), 96):  # skip x=0, needs different format (3f)
            y_start = start_y if x == start_x else 0
            for y in range(y_start, 16):
                pixel_data = build_single_pixel(x, y, 0x01, 0xFF)
                await draw_direct(display, pixel_data)
                count += 1
                print(f"  ({x:2d},{y:2d})  #{count}", end="\r")
                await asyncio.sleep(delay)
    except KeyboardInterrupt:
        print(f"\nStopped at #{count}")

    await display.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
