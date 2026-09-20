#!/usr/bin/env python3
"""Replay S-series graffiti traces to the display for visual verification."""

import asyncio
import json
import uuid

from surplife import SurplifeDisplay

# S-series pixel data extracted from traces (byte2 varies, V=max)
# Sorted by S value (top 3 bits) descending: 7, 4, 3, 1, 0
SAMPLES = [
    ("S=7 (max)  - 23-all-pink",     bytes.fromhex("2fc9ff0200fffffffffffffffffffffff150ffc9ffc9ff")),
    ("S=4 (57%)  - 26-pink-75s",     bytes.fromhex("2fc89f0200fffffffffffffffffffffff1509fc89fc89f")),
    ("S=3 (43%)  - 24-pink-25s",     bytes.fromhex("2fc97f0200fffffffffffffffffffffff1507fc97fc97f")),
    ("S=1 (14%)  - 26-pink-95s",     bytes.fromhex("2fc83f0200fffffffffffffffffffffff1503fc83fc83f")),
    ("S=0 (0%)   - 25-pink-50s",     bytes.fromhex("2fc91f0200fffffffffffffffffffffff1501fc91fc91f")),
]


async def upload_color_data(display: SurplifeDisplay, pixel_data: bytes):
    """Upload type 'c' pixel data to the display."""
    layer_id = str(uuid.uuid4())
    meta = json.dumps({
        "v": 1, "mant_type": 0, "enable_a2pl": 1,
        "layers": [{
            "nm": layer_id, "type": "c", "amt_pos": 0,
            "frame_num": 1, "amt_length": 6 + len(pixel_data), "amt_fmt": 0,
        }],
        "all_file_type": "c"
    }, separators=(',', ':'))

    payload = b"\x00\x00\x00\x00" + len(pixel_data).to_bytes(2, 'big') + pixel_data
    await display._upload_content(0x00, meta.encode('ascii'), payload, b"\xea\x09\x00\x50\x01")


async def main():
    address = "92CFA184-8B61-2363-4E4A-A8BAEFB80D17"
    display = SurplifeDisplay(address)
    await display.connect()
    await asyncio.sleep(0.5)
    await display.init()
    await display.power_on()
    await asyncio.sleep(1.0)

    print("Replaying S-series (sorted by decoded S value, high to low):")
    print("Watch the display — saturation should decrease with each step.\n")

    for i, (label, pixel_data) in enumerate(SAMPLES):
        print(f"  [{i+1}/5] {label}")
        await upload_color_data(display, pixel_data)
        await asyncio.sleep(3.0)

    print("\nDone. Did saturation decrease monotonically?")
    await asyncio.sleep(2.0)
    await display.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
