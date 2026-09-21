# surplife

Python library and CLI for controlling Surplife 96x16 LED matrix displays over BLE.

Reverse-engineered protocol — not affiliated with Surplife or iPixel.

## Requirements

- Python 3.11+
- macOS or Linux with BLE support
- A Surplife LED display (BLE name: `IOTBT*`)

## Installation

```
pip install -e .
```

## CLI

### Scan for displays

```
surplife scan
```

### One-off commands

Commands auto-discover the nearest display, or target a specific one with `-d`:

```
surplife brightness 80
surplife -d D98 power off
surplife speed 50
surplife clock 3 --date
surplife image photo.png -e breathe
surplife gif animation.gif -s 80
surplife text "Hello World!" -c ff0000 -c 00ff00 -c 0000ff
surplife pixel 5,3 9,5 -c ff0000
surplife graffiti drawing.png
surplife graffiti-gif doodle.gif
surplife playlist list
surplife playlist add <hash> -t 15
surplife playlist rm <hash>
```

Content commands print the 16-byte content hash the device caches the
upload under; the playlist (the device's carousel of cached content) is
managed with those hashes.

The `-d` flag accepts a short name (`D98`), full BLE name (`IOTBTD98`),
macOS UUID, or Linux MAC address.

Use `--force` on content commands to bypass device cache during testing.

### Interactive shell

For repeated commands without reconnecting each time:

```
surplife shell
surplife -d D98 shell
```

The shell supports multiple simultaneous connections:

```
> connect D98
Connected to IOTBTD98 [1]  power=on brightness=100% speed=50 96x16
> connect 905
Connected to IOTBT905 [2]  power=on brightness=80% speed=50 96x16
> select 1
> image assets/rainbow.png
> select 2
> text "Hello!" ff0000,00ff00,0000ff
> gif assets/cat_flattened.gif
> playlist-add last 15
> playlist
> playlist-rm e006
> brightness 50
> draw 5,3 ff0000
> draw 9,5
> draw-clear
> clock 7 date
> status
> list
> help
> quit
```

Tab completion works for commands, file paths, and effect names.

## Library

```python
import asyncio
from surplife.display import SurplifeDisplay

async def main():
    async with SurplifeDisplay() as display:
        # Images (static or with effects)
        await display.show_image_file("photo.png", effect=5)

        # GIF animations
        await display.show_gif_file("animation.gif", speed=80)

        # Scrolling text with gradient
        await display.show_text("Hello!", colors=[(255,0,0), (0,255,0)])

        # Graffiti (device graffiti category)
        await display.show_graffiti_file("drawing.png")

        # Direct pixel drawing (replaces the whole display)
        await display.draw_pixels({(5, 3): (255, 0, 0), (9, 5): (0, 255, 0)})

        # Playlist (carousel of cached content)
        c_hash = await display.show_gif_file("animation.gif")
        await display.playlist_add(c_hash, duration=15)
        for entry in await display.get_playlist():
            print(entry.index, entry.duration, entry.hash.hex())

        # Clock, brightness, power
        await display.show_clock(style=3, show_date=True)
        await display.set_brightness(80)
        await display.power_off()

asyncio.run(main())
```

### Scanning

```python
from surplife.scanner import discover, discover_one

# Find all nearby displays
devices = await discover(timeout=10.0)

# Find the nearest one (fast, returns on first match)
device = await discover_one()
```

## Image effects

Static images support 10 display effects via `-e` / `--effect`:

| ID | Name | Description |
|----|------|-------------|
| 1 | static | No animation |
| 2 | scroll-r | Scroll right to left |
| 3 | scroll-l | Scroll left to right |
| 4 | flicker | Rapid on/off flashing |
| 5 | breathe | Smooth fade in and out |
| 6 | snowflake | Lines fall from top, stacking to build image |
| 7 | blend | Image builds left to right with blending |
| 8 | sweep | Image reveals column by column |
| 9 | bands | Shows rows in bands, top to bottom |
| 10 | wipe | Image builds line by line |

## Status

Work in progress. Currently implemented:

- Device discovery and connection (auto-discover, by name, by address)
- Brightness, speed, power on/off
- Static image upload with 10 display effects (a2pl compression)
- GIF animation upload
- Scrolling text with solid color or RGB gradient
- Firmware clock display (8 styles, 12h/24h, date)
- Time sync
- Content caching (device-side, with `--force` bypass)
- Interactive shell with multi-device support and tab completion
- Playlist / carousel management (add, remove, reorder, list)
- Graffiti upload (static image + animation GIF, device graffiti category)
- Direct pixel drawing (`ea 11` live draw, stateless CLI / per-device canvas in shell)
- Waveform (audio visualizer) style configuration and streaming (experimental, shell only)

## Home Assistant integration

A HACS-installable integration (`custom_components/surplife_matrix/`) controls
the display inside Home Assistant using HA's shared Bluetooth stack. It
provides a light entity (power/brightness) plus services:

```
surplife_matrix.show_media   — upload an image/GIF from /media, a URL, or data: URI
surplife_matrix.show_camera  — show a frame from any HA camera
surplife_matrix.show_text    — scrolling text (solid or gradient colors)
surplife_matrix.show_clock   — firmware clock (styles, 12h/24h, date)
surplife_matrix.draw_pixels  — direct-draw pixels (ea 11)
surplife_matrix.playlist_add/remove/clear — device carousel management
surplife_matrix.set_speed    — GIF speed
surplife_matrix.activate     — re-display cached content by hash (instant, no upload)
```

Unsafe GIFs are auto-converted into the device-safe envelope (single global
palette, >=100ms frame delay) before upload.

**Architecture (wedge-safe by design):** the integration holds one
**persistent, always-connected BLE session** per device — the device firmware
wedges after ~6–7 connect cycles (see the device wedge warning below), so
commands are never re-connected per action. All traffic is serialized through
a command lock (interleaved uploads would corrupt the device's segment
reassembly), state comes from command ACKs instead of polling, and GIF
uploads observe a 3 s post-activation dwell. A watchdog detects the wedge
signature and marks the entity unavailable until the display is power-cycled.
See [PROTOCOL.md "BLE Connect-Cycle Limitation"](PROTOCOL.md#ble-connect-cycle-limitation--confirmed--crash-tested)
and ["Cache Check Semantics"](PROTOCOL.md#cache-check-semantics--confirmed--live-verified).
See `hacs.json` for the
repository layout; the vendored protocol core in
`custom_components/surplife_matrix/core/` is synced from `src/surplife_core`
via `scripts/sync_core.sh`.

## Shipped animations

The repo and the HA integration ship a curated set of **device-safe**
animations (every file passes `validate_gif()`; produced by
`scripts/generate_animations.py`, which pushes each one through the
auto-fix pipeline and asserts the safe envelope):

| `builtin:` name | Visual |
|---|---|
| `rainbow_wave` | hue-drift rainbow with a soft vertical brightness wave |
| `plasma` | additive sine plasma, warm-cool palette |
| `fire` | bottom-up flickering flames |
| `ocean` | layered sine waves, blue-teal palette |
| `starfield` | twinkling stars drifting on black |
| `police_light` | alternating red/blue sweep |
| `color_cycle`, `scroll_text`, `d20_flames`, `popcorn2`, `spaceship`, `wizard_fireball` | device-safe copies of the original assets |

Use them in the Home Assistant integration without any setup —
`show_media` accepts a `builtin:` source:

```
service: surplife_matrix.show_media
data:
  entity_id: light.surplife_matrix
  source: builtin:rainbow_wave
```

To regenerate (or add) animations: `python3 scripts/generate_animations.py`
(writes to `assets/animations/` and the integration's `animations/` folder;
`--list` shows the available generators). A committed test asserts every
bundled GIF stays device-safe.

## Device-safe GIFs

The device's GIF decoder is fragile — see
[PROTOCOL.md "GIF Device Limitations"](PROTOCOL.md#gif-device-limitations--confirmed--crash-tested).
GIFs with local color tables or frame delays under 100 ms crash the firmware
after the upload completes. All GIF uploads are validated by default
(`validate_gif()`); pass `check=False` to override.

```python
from surplife.display import validate_gif

report = validate_gif(open("animation.gif", "rb").read(), strict=False)
print(report)   # {'ok': True, 'frames': 64, ...} or violation list
```

To render arbitrary RGB animations safely, quantize all frames against a
single shared 128-color palette, use a frame delay of 150 ms, and verify
with `validate_gif()` before upload.

## Device wedge warning

The device firmware leaks a resource per BLE connection: after **~6–7 connect
cycles since boot** it wedges (panel freezes, init handshake never completes)
and only recovers with a **manual power cycle**. Session *duration* and
in-session command count are safe — batch commands into one session and never
poll by connecting. See
[PROTOCOL.md "BLE Connect-Cycle Limitation"](PROTOCOL.md#ble-connect-cycle-limitation--confirmed--crash-tested).
The CLI and the Home Assistant integration already follow this pattern
(single session per invocation / persistent connection respectively).

## Docker

The container talks to the host's BlueZ stack over the system D-Bus socket.
This requires a **Linux host** with bluetoothd running (Docker Desktop on
macOS/Windows cannot expose the host Bluetooth adapter), and a user in the
`bluetooth` group (or root).

```
docker compose build
docker compose run --rm surplife scan
docker compose run --rm surplife -d D98 brightness 80
docker compose run --rm surplife shell
docker compose run --rm surplife -d D98 pixel 5,3 9,5 -c ff0000
```

## Protocol documentation

- [PROTOCOL.md](PROTOCOL.md) — the reverse-engineered BLE protocol: commands, content upload, metadata
- [A2PL.md](A2PL.md) — the a2pl pixel compression format
- [BLE.md](BLE.md) — an introduction to BLE reverse engineering for beginners

## Research material

`research/` holds the material the protocol documentation was derived from:
BTSnoop HCI captures (`research/traces/`), the trace analysis and capture parsing
scripts, the a2pl decompressor test suite, notes on the APK decompilation, the
graffiti direct-draw replay test (`research/test_graffiti_replay.py`), and the
original monolithic `surplife.py` script that preceded this package. Core logic
tests live in `tests/test_core.py` (pytest).

## License

MIT, see [LICENSE](LICENSE). The bundled [Spleen](https://github.com/fcambus/spleen)
font is BSD-2-Clause, see `src/surplife/fonts/LICENSE.spleen`.
