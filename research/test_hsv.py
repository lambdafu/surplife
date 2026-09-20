#!/usr/bin/env python3
"""Test HSV color encoding by cycling through diagnostic colors on the display."""

import asyncio
from surplife import SurplifeDisplay

# (H°, S%, V%, description)
TESTS = [
    # --- Pure primaries at full S/V ---
    (0,   100, 100, "Bright intense RED"),
    (120, 100, 100, "Bright intense GREEN"),
    (240, 100, 100, "Bright intense BLUE"),

    # --- Secondaries ---
    (60,  100, 100, "Bright YELLOW"),
    (180, 100, 100, "Bright CYAN"),
    (300, 100, 100, "Bright MAGENTA/PINK"),

    # --- Hue boundary tests (near quantization edges) ---
    (30,  100, 100, "ORANGE (between red and yellow)"),
    (90,  100, 100, "CHARTREUSE (between yellow and green)"),
    (150, 100, 100, "SPRING GREEN (between green and cyan)"),
    (210, 100, 100, "AZURE/SKY BLUE (between cyan and blue)"),
    (270, 100, 100, "VIOLET/PURPLE (between blue and magenta)"),
    (330, 100, 100, "ROSE/DEEP PINK (between magenta and red)"),

    # --- Saturation tests (red, decreasing S) ---
    (0,   100, 100, "RED full saturation"),
    (0,    50, 100, "PASTEL RED / salmon (half saturation, full bright)"),
    (0,     0, 100, "WHITE (zero saturation, full bright)"),

    # --- Value/brightness tests (red, decreasing V) ---
    (0,   100, 100, "RED full brightness"),
    (0,   100,  50, "DARK RED / maroon (full sat, half bright)"),
    (0,   100,   7, "VERY DARK RED (full sat, near-black)"),

    # --- S and V both reduced ---
    (0,    50,  50, "MUTED DARK RED (half sat, half bright)"),
    (120,  50,  50, "MUTED DARK GREEN"),
    (240,  50,  50, "MUTED DARK BLUE / slate"),

    # --- Near-black and near-white ---
    (0,     0,   0, "BLACK (or near-black)"),
    (0,     0, 100, "WHITE (S=0, V=max)"),
    (0,     0,  50, "GRAY (S=0, half bright)"),

    # --- Tricky hue values near wrap-around ---
    (355, 100, 100, "RED (just below 360° wrap)"),
    (5,   100, 100, "RED (just above 0°)"),

    # --- Mid-range combo ---
    (200, 80,  70,  "MEDIUM BLUE, slightly desaturated, slightly dim"),
]


async def main():
    address = "92CFA184-8B61-2363-4E4A-A8BAEFB80D17"
    display = SurplifeDisplay(address)
    await display.connect()
    await asyncio.sleep(0.5)
    await display.init()
    await display.power_on()
    await asyncio.sleep(1.0)

    print(f"Running {len(TESTS)} color tests (1 second each):\n")

    for i, (h, s, v, desc) in enumerate(TESTS):
        b1, b2 = display._encode_hsv(h, s, v)
        # Decode back for verification
        h_dec = (b1 >> 1) / 127 * 360
        s_dec = (((b1 & 1) << 3) | ((b2 >> 5) & 7)) / 15 * 100
        v_dec = (b2 & 0x1f) / 31 * 100
        print(f"  [{i+1:2d}/{len(TESTS)}] H={h:3d}° S={s:3d}% V={v:3d}%  "
              f"-> bytes({b1:02x},{b2:02x}) "
              f"-> decoded H={h_dec:5.1f}° S={s_dec:5.1f}% V={v_dec:5.1f}%  "
              f"| {desc}")
        await display.show_solid_color(h, s, v)
        await asyncio.sleep(1.0)

    print("\nDone! Did the colors match the descriptions?")
    await asyncio.sleep(2.0)
    await display.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
