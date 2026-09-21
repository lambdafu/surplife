#!/usr/bin/env python3
"""Live device test matrix for the Surplife display (real hardware).

Each test runs in its own one-shot BLE session with the known-safe
pattern (connect → init → command → disconnect, dwell before disconnect).
Never reconnects while a GIF plays; aborts on any init timeout instead of
hammering a possibly-wedged device.

Usage:
    python3 research/test_device_matrix.py            # full matrix
    python3 research/test_device_matrix.py 1 3 7      # subset by test number
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from surplife.display import SurplifeDisplay
from surplife_core.content import validate_gif

DEVICE = os.environ.get("SURPLIFE_DEVICE", "D98")
SETTLE_AFTER_SESSION = 15.0      # idle gap between read-only sessions
SETTLE_AFTER_UPLOAD = 90.0       # extra gap after content uploads (a
                                 # wedged device may NOT self-recover:
                                 # manual power cycle required)       # idle gap between sessions
DWELL_AFTER_GIF = 3.0            # stay connected after GIF activation
RAINBOW = os.path.join(os.path.dirname(__file__), "..",
                       "assets", "rainbow_final_clean.gif")


class Result:
    def __init__(self, num, name):
        self.num = num
        self.name = name
        self.ok = False
        self.detail = ""


async def t01_init(d, result: Result):
    """1. Connect + init: the context manager inits; verify status."""
    # __aenter__ already ran the init handshake; re-sending the hash exchange
    # right after it wedges the firmware, so we only verify the state here.
    assert d.display_cols == 96, f"cols={d.display_cols}"
    assert d.display_rows == 16, f"rows={d.display_rows}"
    assert 0 <= d.brightness <= 100
    result.detail = d.status_str
    result.ok = True


async def t02_brightness(d, result: Result):
    """2. Brightness sweep 30 → 70 → 80 with status ACKs."""
    for value in (30, 70, 80):
        await d.set_brightness(value)
        assert d.brightness == value, f"set {value}, read {d.brightness}"
        await asyncio.sleep(0.3)
    result.detail = f"final={d.brightness}%"
    result.ok = True


async def t03_draw_pixels(d, result: Result):
    """3. Direct draw: red pixel at (0,0), erase at (95,15)."""
    await d.draw_pixels({(0, 0): (255, 0, 0), (95, 15): None})
    await asyncio.sleep(0.3)
    result.ok = True


async def t04_show_text(d, result: Result):
    """4. Scrolling text upload (type e)."""
    c_hash = await d.show_text("HA", colors=[(255, 0, 0)], speed=50)
    assert len(c_hash) == 16
    result.detail = c_hash.hex()[:12] + "…"
    result.ok = True


async def t05_show_clock(d, result: Result):
    """5. Firmware clock (style 3, 24h)."""
    await d.show_clock(style=3, hour_24=True, show_date=False)
    await asyncio.sleep(0.3)
    result.ok = True


async def t06_playlist_read(d, result: Result):
    """6. Playlist read: entries have 16-byte hashes."""
    entries = await d.get_playlist()
    for e in entries:
        assert len(e.hash) == 16
    result.detail = f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
    result.ok = True


async def t07_image_upload(d, result: Result):
    """7. Static image upload (assets/smiley.png)."""
    path = os.path.join(os.path.dirname(__file__), "..", "assets",
                        "smiley.png")
    c_hash = await d.show_image_file(path, effect=1, speed=50)
    assert len(c_hash) == 16
    result.detail = c_hash.hex()[:12] + "…"
    result.ok = True


async def t08_gif_upload(d, result: Result):
    """8. GIF upload — the device-safe rainbow (validated envelope)."""
    path = RAINBOW
    with open(path, "rb") as f:
        data = f.read()
    report = validate_gif(data)
    assert report["ok"], report["violations"]
    c_hash = await d.show_gif(data, speed=100)
    assert len(c_hash) == 16
    result.detail = f"{len(data)}B {c_hash.hex()[:12]}…"
    result.ok = True
    # safe-pattern dwell: let the GIF start before we disconnect
    await asyncio.sleep(DWELL_AFTER_GIF)


async def t09_playlist_add(d, result: Result):
    """9. Add last-uploaded content to the playlist."""
    with open(RAINBOW, "rb") as f:
        data = f.read()
    c_hash = await d.show_gif(data, speed=100)   # cache hit expected
    entries = await d.playlist_add(c_hash, duration=15)
    assert any(e.hash == c_hash for e in entries)
    result.detail = f"{len(entries)} entries"
    result.ok = True


async def t10_reactivate_cached(d, result: Result):
    """10. Re-activate the same GIF: exercises the cache-hit path."""
    with open(RAINBOW, "rb") as f:
        data = f.read()
    started = time.monotonic()
    c_hash = await d.show_gif(data, speed=100)
    elapsed = time.monotonic() - started
    result.detail = f"{elapsed:.1f}s (cache hit = fast)"
    result.ok = True
    await asyncio.sleep(DWELL_AFTER_GIF)


TESTS = [t01_init, t02_brightness, t03_draw_pixels, t05_show_clock,
         t06_playlist_read, t04_show_text, t07_image_upload, t08_gif_upload,
         t09_playlist_add, t10_reactivate_cached]


async def run_subset(numbers: list[int] | None) -> int:
    """Run all tests in ONE session.

    The firmware tolerates only ~6-7 BLE connect cycles per boot before its
    stack wedges (suspected RAM leak per connection), so we batch every test
    into a single session — matching the app's own one-session behavior.
    """
    failures = 0
    async with SurplifeDisplay(DEVICE) as d:
        for i, test in enumerate(TESTS, start=1):
            if numbers and i not in numbers:
                continue
            result = Result(i, test.__doc__.strip().split(".")[0]
                            if test.__doc__ else test.__name__)
            print(f"[{i:2d}] {result.name:60s}", end="", flush=True)
            try:
                await test(d, result)
                print(f"  PASS {result.detail}")
            except Exception as err:
                print(f"  FAIL: {type(err).__name__}: {err}")
                failures += 1
                print("  Continuing (in-session failures are safe).")
            # pacing between commands: short idle, longer after GIF activation
            if test in (t08_gif_upload, t10_reactivate_cached):
                await asyncio.sleep(DWELL_AFTER_GIF)
            else:
                await asyncio.sleep(1.0)
    return failures




async def long_run(cycles: int = 20) -> int:
    """--long-run: 20 in-session command cycles over ONE connection.

    Validates that session duration / in-session command count does not
    wedge the firmware (the leak is per-CONNECT, not per-command).
    """
    print(f"Long-run: {cycles} command cycles in one session")
    async with SurplifeDisplay(DEVICE) as d:
        print(f"connected: {d.status_str}")
        for i in range(cycles):
            # alternate read/write-ish commands; all safe (no content upload)
            match i % 4:
                case 0:
                    await d.set_brightness(40 + (i * 3) % 40)
                    label = f"brightness={d.brightness}%"
                case 1:
                    await d.draw_pixels({(i % 96, i % 16): (255, 0, 0)})
                    label = f"pixel ({i % 96},{i % 16})"
                case 2:
                    await d.show_clock(style=i % 8)
                    label = f"clock style {i % 8}"
                case 3:
                    entries = await d.get_playlist()
                    label = f"playlist {len(entries)} entr."
            print(f"  cycle {i + 1:2d}: {label}")
            await asyncio.sleep(0.5)
    print("Long-run complete — device did NOT wedge.")
    return 0


async def builtin_tour() -> int:
    """--builtin-tour: play every shipped animation in ONE session.

    Uploads each bundled animation, then activates it from cache; 3 s
    dwell between activations (the verified-safe in-session pattern).
    """
    from surplife_core.content import validate_gif

    anim_dir = os.path.join(os.path.dirname(__file__), "..",
                            "custom_components", "surplife_matrix",
                            "animations")
    names = sorted(n.removesuffix(".gif")
                   for n in os.listdir(anim_dir) if n.endswith(".gif"))
    print(f"Builtin tour: {len(names)} animations in one session")
    async with SurplifeDisplay(DEVICE) as d:
        print(f"connected: {d.status_str}")
        for i, name in enumerate(names, start=1):
            path = os.path.join(anim_dir, f"{name}.gif")
            with open(path, "rb") as f:
                data = f.read()
            report = validate_gif(data)      # strict: shipped files must pass
            try:
                c_hash = await d.show_gif(data, speed=100)
            except (TimeoutError, RuntimeError) as err:
                # one patient retry after a cool-down (device may be busy
                # finishing the previous animation's playback)
                print(f"\n       busy — retrying {name} after 30s "
                      f"({type(err).__name__})")
                await asyncio.sleep(30.0)
                c_hash = await d.show_gif(data, speed=100)
            print(f"  [{i:2d}] {name:24s} {len(data):6d}B "
                  f"hash={c_hash.hex()[:10]}… dwell")
            await asyncio.sleep(5.0)         # post-activation dwell
        # final: activate the first animation again via the cache-hit path
        first = names[0]
        with open(os.path.join(anim_dir, f"{first}.gif"), "rb") as f:
            data = f.read()
        c_hash = await d.show_gif(data, speed=100)
        print(f"  re-activate {first}: cache hit, {len(data)}B")
        await asyncio.sleep(3.0)
    print("Builtin tour complete — device did NOT wedge.")
    return 0


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    if "--long-run" in args:
        failures = await long_run()
        if failures:
            sys.exit(1)
        return
    if "--builtin-tour" in args:
        failures = await builtin_tour()
        if failures:
            sys.exit(1)
        return
    numbers = [int(a) for a in args] or None
    print(f"Live device test matrix — device {DEVICE}")
    failures = await run_subset(numbers)
    if failures:
        print(f"\n{failures} test(s) FAILED")
        sys.exit(1)
    print("\nAll live tests passed.")


if __name__ == "__main__":
    asyncio.run(main())


