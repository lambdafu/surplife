# Surplife LED Display — BLE Protocol

Reverse-engineered BLE protocol for the Surplife 96x16 LED display.
Based on analysis of 97 BTSnoop HCI captures.

## Device

- **BLE Name:** `IOTBT<suffix>` (suffix = last 3 hex chars of MAC, e.g. IOTBT996, IOTBTD98)
- **Manufacturer ID:** `0x005a`
- **Display:** 96x16 pixels, 16-bit HSV color (2 bytes/pixel)
- **App:** Surplife / iPixel Color (iOS/Android)
- **MTU:** 512

Not a standard iPixel device — uses its own wrapper protocol, not the
documented `fa02`/`fa03` UUIDs.

## BLE Services

| Service | Characteristic | Function |
|---------|---------------|----------|
| `0000ffff` | `ff01` | Write — all commands |
| `0000ffff` | `ff02` | Notify — all responses |
| `0000fe00` | `ff11` | Write — unused by app |
| `0000fe00` | `ff22` | Notify — unused by app |

## Packet Format

### Request (App → Device)

```
[SEQ_HI][SEQ_LO][0x80][0x00][LEN-1 (BE16)][LEN (BE16)][0x0a][PAYLOAD...]
```

- Bytes 0–1: sequence number (increments, high byte starts at `0x01`)
- Bytes 2–3: flags, always `0x80 0x00`
- Bytes 4–5: payload length minus 1 (big-endian)
- Bytes 6–7: payload length (big-endian)
- Byte 8: always `0x0a`
- Bytes 9+: command payload

### Response (Device → App)

Same 8-byte header format. The first byte of the inner payload is a prefix:

| Prefix | Observed context |
|--------|-----------------|
| `0x15` | First `ea 81` during init, cache check (`ea 05`), upload ACKs (`e0 30`, `e0 33`) |
| `0x16` | Second `ea 81` during init, status updates after `e0 01` commands |

The exact semantics of `0x15` vs `0x16` are not fully understood. During init,
both prefixes appear on `ea 81` responses (see Init Sequence below). For
practical purposes, both should be accepted when waiting for responses.

## Content Types

| Type | Description | Payload format | `amt_fmt` | `frame_num` | Cache byte | Activation |
|------|-------------|---------------|-----------|-------------|------------|------------|
| `"a"` | Image (static or scrolling) | a2pl (6-byte header + compressed) | `0` | 1 or N pages | `0x04` | `ea 06` |
| `"b"` | GIF animation | Raw GIF file | `2` | `0` | `0x01` | `ea 07` |
| `"c"` | Graffiti (static) | a2pl (6-byte header + compressed) | `0` | `1` | `0x00` | `ea 09` |
| `"d"` | Graffiti animation | Raw GIF file | `2` | `0` | `0x02` | `ea 0a` |
| `"e"` | Scrolling text | a2pl (6-byte header + compressed) | `0` | N pages | `0x02` | `ea 24` |
| `"f"` | Waveform animation | a2pl multi-frame | `0` | N frames | ? | ? |

Types `"b"` and `"d"` are functionally identical on the protocol level — both
upload raw GIF data. The type field likely just tells the app which UI category
the content belongs to. Both type `"c"` and `"d"` are confirmed by traces 14–62;
the library exposes them as `show_graffiti()` and `show_graffiti_gif()`.

**Type `"a"` multi-frame scrolling:** Type `"a"` supports `frame_num > 1` with
the same multi-frame a2pl payload format as type `"e"`. Combined with `ea 06`
effect 2 (scroll-r), the device scrolls across all pages with true pixel colors.
This is the correct way to display wide scrolling images. Type `"e"` also scrolls
multi-frame content, but replaces all non-black pixel colors with the `fg_color`
gradient from the JSON metadata (it treats the pixel data as a mask).

### Payload Formats

**a2pl format** (`amt_fmt: 0`): 6-byte header + LZ77-compressed pixel data.
See [A2PL.md](A2PL.md) for compression details.

```
[00 00 00 00] [size BE16] [compressed pixel data]
```

**GIF format** (`amt_fmt: 2`): Standard GIF87a/GIF89a file, 96x16 pixels.
The device decodes GIF natively. No header prefix.

## Commands

### `e0` — Display Control

| Command | ACK | Function |
|---------|-----|----------|
| `e0 01 00 01 00 00 [val] 00 [val] 00 00 00 00 00` | `16 ea 81 [status]` | Set brightness (0–100) |
| `e0 01 00 23 [10x 00]` | `16 ea 81 [status]` | Power ON |
| `e0 01 00 24 [10x 00]` | `16 ea 81 [status]` | Power OFF |
| `e0 0e 01` | — | Init / refresh |
| `e0 1e 00` | — | Screen prepare (always sent 2x before activation) |
| `e0 30 [size BE32] ea 23` | `15 e0 30 00 01` | Frame header (size = total content bytes) |
| `e0 32 [data]` | — | Data segment (max ~490 bytes per segment) |
| `e0 33` | `15 e0 33 00` | End marker |

### `ea` — Content & Activation

| Command | ACK | Function |
|---------|-----|----------|
| `ea 05 [cache_byte] [16B hash]` | `15 ea 05 00` (new) / `01` (cached) | Cache check (type byte ignored — see Cache Check Semantics) |
| `ea 06 [source] [speed] [effect]` | `15 ea 24 00` | Activate image with effect (type "a") |
| `ea 07 00 [speed]` | — | Activate GIF + set speed (type "b") |
| `ea 09 00 50 01` | `15 ea 24 00` | Activate graffiti (type "c") |
| `ea 0a 00 50 01` | `15 ea 24 00` | Activate graffiti animation (type "d") |
| `ea 0b [count] [byte3] [entries]` | `15 ea 0b 00` | Set playlist (see below) |
| `ea 0c` | `15 ea 0c [count] [entries]` | Get playlist (see below) |
| `ea 0d [count] [entries]` | `15 ea 0d 00` | Set playlist details (see below) |
| `ea 0e` | `15 ea 0e [count] [entries]` | Get playlist details (see below) |
| `ea 11 00 00 00 [len] [data]` | — | Direct draw (see `ea 11` section below) |
| `ea 24` | `15 ea 24 00` | Activate text (type "e") |
| `ea 10 [style] [fmt] [date]` | — | Show firmware clock (see below) |
| `ea 14 01` | — | Sent by app after init (purpose unclear) |
| `ea 81 8a 8b 59` | `15/16 ea 81 [status]` | Device hash exchange (see Init Sequence) |

**Note on `ea 07`:** While documented as "Activate GIF + set speed," sending
`ea 07` to non-GIF content has side effects: scrolling text switches to blinking,
scrolling images become static. Speed for text and images is set during upload
(JSON metadata or `ea 06` activation command). Similar mismatches exist for
other cross-type activations: `ea 24` (text activation) is a no-op on GIF
content, and `ea 0a` (graffiti-animation activation) on text has side effects.
When activating content of unknown type (e.g. uploaded via the phone app), try
activations from safest to most side-effect-prone: `ea 24`, `ea 0a`, `ea 06`,
`ea 07`, `ea 09` — one attempt only, then re-probe on the next command cycle.

### `ea 06` — Image Activation with Effects [CONFIRMED]

```
ea 06 [source] [speed] [effect_id]
```

| Byte | Values | Meaning |
|------|--------|---------|
| source | `00`/`01` | `00` = freshly uploaded, `01` = from cache |
| speed | `01`–`64` | Animation speed (1–100) |
| effect_id | `01`–`0a` | Display effect (see tables below) |

Can also be sent standalone (without uploading) to change the effect on
an already-cached image. Use `ea 05` first to confirm cache hit (`01`),
then `ea 06 01 [speed] [effect]`.

### Cache Check Semantics [CONFIRMED — live-verified]

The `ea 05` cache check **ignores the type byte** — only the 16-byte
content hash matters. Verified live: the same content hash answered
`01` (cached) under type bytes `0x04`, `0x01`, and `0x02`.

Implications:

- One probe is enough to learn whether content is cached; no need to
  probe per type.
- The type byte cannot be used to *learn* the content's type. To pick
  the correct activation command (image vs GIF vs text vs graffiti),
  the controlling side must remember the type it uploaded itself, or
  fall back to documented activations for foreign content (phone app):
  safest first — `ea 24` is a no-op on GIF content, whereas `ea 0a` on
  text has documented side effects (see the `ea 07` note below).
- The type byte in the *upload* sequence (`ea 05` before `e0 30`) and
  the content-type table above still describe what the app sends; the
  device just does not key its cache on it.

**App effects (accessible in UI):**

| ID | Name | Description |
|----|------|-------------|
| 1 | static | No animation |
| 2 | scroll-r | Scroll right to left |
| 3 | scroll-l | Scroll left to right |
| 4 | flicker | Rapid on/off flashing |
| 5 | breathe | Smooth fade in and out |
| 6 | snowflake | Lines fall from top, stacking to build image |

**Hidden effects (not in app UI, functional):**

| ID | Name | Description |
|----|------|-------------|
| 7 | blend | Image builds left to right with column blending/fade-in |
| 8 | sweep | Image reveals column by column, left to right |
| 9 | bands | Shows ~4 rows at a time, top to bottom |
| 10 | wipe | Image builds line by line, top to bottom |

### `ea 09` / `ea 0a` — Graffiti Activation [CONFIRMED]

```
ea 09 00 50 01        (static graffiti, type "c", cache byte 0x00)
ea 0a 00 50 01        (graffiti animation, type "d", cache byte 0x02)
```

Fixed payload. ACK: `15 ea 24 00`. The `50` is likely speed and `01`
possibly a layer count, but the app always sends these values.

### `ea 11` — Direct Draw [CONFIRMED]

```
ea 11 00 00 00 [len] [a2pl stream]
```

Immediate framebuffer update without a content upload. Used by the app's
live graffiti mode while the user draws (trace 60: 15 draws, one pixel
added at a time).

| Byte | Meaning |
|------|---------|
| `ea 11` | Command |
| `00 00 00` | Fixed prefix |
| `len` | Payload length in bytes (observed 24–44; 255 max as a 1-byte field) |
| payload | **Raw a2pl stream of the full 3072-byte framebuffer** |

Key properties (verified by decompressing all trace-60 draws):

- No 6-byte header and no offset table — the payload starts directly
  with the first a2pl command byte and decompresses to exactly 3072 bytes.
- Every draw replaces the **entire** canvas. The app accumulates pixels
  client-side and re-sends the full framebuffer on each stroke; there is
  no incremental pixel command.
- Colors are the standard 16-bit HSV encoding (see HSV Color Encoding).
- No ACK.
- The payload must end with trailing literal bytes (the same firmware
  off-by-one bug as uploads — see [A2PL.md](A2PL.md#stream-ending)).

### `ea 10` — Clock [CONFIRMED]

```
ea 10 [style] [time_fmt] [date_flag]
```

| Byte | Values | Meaning |
|------|--------|---------|
| style | `00`–`07` | Clock face style (8 styles) |
| time_fmt | `01`/`02` | `01` = 12-hour, `02` = 24-hour |
| date_flag | `00`/`01` | `00` = time only, `01` = show date |

No pixel data — firmware renders the clock natively.

### `ea 0f` — Audio Waveform Streaming [CONFIRMED]

```
ea 0f [style_id] 00 [96 amplitude bytes]
```

- `style_id`: active waveform style (set via `e1 05`)
- 96 bytes, one per column, values 0–100
- Phone streams at ~10 fps; device renders bars natively
- Short form `ea 0f ff 00` (no amplitudes) = start/reset

### `e1 05` — Waveform Style Config [CONFIRMED]

```
e1 05 [b2] [b3] [style] [b5] [b6] [b7] [17x00] [color_block]
```

| Offset | Values | Meaning |
|--------|--------|---------|
| 2 | `00` | Always zero |
| 3 | `50`/`64` | Possibly brightness (0x50=80, 0x64=100) — not confirmed |
| 4 | `01`–`0d`, `ff` | Style index |
| 5 | `00`/`01`/`02` | Color mode: `00` = static gradient, `01` = color cycling (flat), `02` = scrolling gradient |
| 6 | `05`/`06`/`00` | Decay direction (bar styles): `05` = top-down (bars up), `06` = bottom-down (bars hang). `00` for non-bar styles. |
| 7 | `54`/`64` | Possibly decay/animation speed (0x54=84, 0x64=100) — not confirmed |
| 8–24 | zeros | Padding (byte 24 = `01` in one trace with custom colors) |
| 25+ | color block | Color list (see below) |

Color block: `a1 00 00 00 [N]` + N colors as `a1 [H] [S] [V]`
(H: 0–150 maps to 0–360°, S/V: 0–100).

The color list serves different purposes depending on the style: as a
gradient (styles 1-4), as a palette for random pixel colors (style 7),
or as a palette for background flashes (styles 8, 12).

### Waveform Styles

The device renders bars/effects based on 96 amplitude values (0–100)
streamed via `ea 0f`. Bars automatically decay to zero when streaming stops.

**Bar styles** (amplitude = bar height):

| ID | Gradient | Decay direction | Description |
|----|----------|----------------|-------------|
| 1 | Left → right (cycling) | Top → bottom | Classic bar graph |
| 2 | Bottom → top (cycling) | Top → bottom | Vertical gradient bars |
| 3 | Left → right (cycling) | Top+bottom → middle | Bars from center |
| 4 | Middle → top+bottom (mirrored) | Top+bottom → middle | Mirrored gradient, bars from center |

**Sparkle/flash styles:**

| ID | Amplitude | Description |
|----|-----------|-------------|
| 7 | Aggregate (sum/avg of all 96 values) = pixel count | Random colored pixels (from color list) fade in/out on black. Per-column data is ignored — only total intensity matters. |
| 8 | Aggregate = intensity | Low amp: white pixel fade. High amp: background flashes in random color from list, white stars fade in/out. Strobe-like. |
| 12 | Threshold (~3) | Screen fills with random colors from list, fades to black. Triggers above threshold, intensity doesn't vary. |
| 13 | Threshold (~3) | Picks a color from palette, flashes it, dims to black. Triggers above threshold, intensity doesn't vary. |

**Non-functional IDs** (tested, no visible output): 5, 6, 9, 10, 11.

**Custom style** (`0xff`): Uses uploaded type "f" content (multi-frame a2pl).
Requires content upload before streaming, plus `ea 0f ff 00` reset.

Known style IDs from traces: 1, 2, 3, 4, 7, 8, 12, 13, `0xff`.

### Playlist / Carousel Commands [CONFIRMED]

The device maintains a playlist of cached content that cycles automatically.
All operations use read-modify-write: read the current state, modify locally,
write the full list back. There are no incremental add/remove commands.

**`ea 0c` — Get playlist:**

Response: `15 ea 0c [count] [count × 17B entries]`

Each entry: `[flag 1B] [hash 16B]`
- `flag = 0x12` may indicate the currently displayed entry; `0x00` otherwise.

**`ea 0b` — Set playlist:**

```
ea 0b [count] [byte3] [count × 17B entries]
```

Each entry: `[flag 1B] [hash 16B]`.
- `byte3 = 0x00` when adding entries to the playlist (trace 07). The app
  echoes the flags it read via `ea 0c` for the existing entries (`0x12` on
  the first one) and sends `flag = 0x01` for the newly appended entry.
- `byte3 = 0x01` when removing entries or reordering (trace 08). The app
  sends `flag = 0x00` for every entry.

The meaning of the flag byte is not known; the values above are what the
app sends.

**`ea 0e` — Get playlist details:**

Response: `15 ea 0e [count] [count × 19B entries]`

Each entry: `[index 1B] [flag1 1B] [duration 1B] [hash 16B]`
- `index`: 1-based position in rotation (`0` = content exists but not active).
- `duration`: display time in seconds (e.g. `0x0a` = 10 seconds).
- `flag1`: always `0x00` in observed traces.

**`ea 0d` — Set playlist details:**

```
ea 0d [count] [count × 19B entries]
```

Same entry format as `ea 0e`: `[index 1B] [flag1 1B] [duration 1B] [hash 16B]`.
Note: no `byte3` field (unlike `ea 0b`).

**Typical sequence — add to playlist:**
```
1. ea 0c                  → get current playlist (N entries)
2. ea 0b [N+1] 00 [...]  → set new playlist (old entries + new hash)
3. ea 0d [N+1] [...]     → set details (1-based indices + duration)
```

In trace 07 the app sent only steps 1–2; the new entry then read back via
`ea 0e` with `index = 0` and `duration = 0` (trace 08), i.e. not in rotation.
Step 3 is what the library does to put the new entry into rotation.

**Typical sequence — remove from playlist:**
```
1. ea 0c                  → get current playlist
2. ea 0e                  → get details (optional, to preserve durations)
3. ea 0b [N-1] 01 [...]  → set reduced playlist
4. ea 0d [N-1] [...]     → set details with new indices
```

### `10 14` — Device Config / Time Sync [CONFIRMED]

Sent once at init to set the device clock. The app sends current local time
so the firmware can render clock faces and timestamps.

```
10 14 [YY] [MM] [DD] [hh] [mm] [ss] [dow] 00 0f [checksum]
```

| Offset | Values | Meaning |
|--------|--------|---------|
| 2 | `0x1a` = 26 | Year minus 2000 (2026) |
| 3 | `0x01`–`0x0c` | Month (1–12) |
| 4 | `0x01`–`0x1f` | Day (1–31) |
| 5 | `0x00`–`0x17` | Hour (0–23) |
| 6 | `0x00`–`0x3b` | Minute (0–59) |
| 7 | `0x00`–`0x3b` | Second (0–59) |
| 8 | `0x01`–`0x07` | Day of week (1=Monday .. 7=Sunday, ISO 8601) |
| 9 | `0x00` | Reserved (always zero) |
| 10 | `0x0f` | Fixed value |
| 11 | | Checksum: `sum(bytes[0:11]) & 0xFF` |

Verified against capture timestamps: the config command time matches the
BTSnoop capture time when accounting for UTC vs local timezone (CET/CEST).

### Init Sequence

The connection init consists of three commands, followed by a wait for two
device responses before the device is ready to accept further commands.

```
1. 0x0c (raw, no 0x0a prefix)       → init trigger
2. 10 14 [time sync]                → set device clock (see above)
3. ea 81 8a 8b 59                   → device hash exchange
   << 15 ea 81 [status payload]     → first response (immediate)
   << 16 ea 81 [status payload]     → second response (~1.5s later)
4. ea 14 01                         → sent by app after init (purpose unclear)
```

The device displays a blue "(-)" indicator during init.

**Observation:** Commands (e.g. brightness) sent after only the first `ea 81`
response don't take effect. Waiting for the second response before sending
commands resolves this. The role of the `0x15` vs `0x16` prefix byte in
determining readiness is not fully understood — it may be coincidental with
the timing. See response prefix table above.

## Upload Sequence

All content uploads follow the same pattern:

```
1. ea 05 [cache_byte] [16B hash]  → check cache
   If cached (ACK 01):
     → send activation command, done
   If new (ACK 00):
2. e0 30 [frame header]           → declare total size
3. e0 32 [data] ...               → data segments (repeated)
4. e0 33                          → end marker
5. e0 1e 00 (2x)                  → screen prepare
6. [activation command]            → type-specific activation
```

**Partial-upload abort behavior:** if the transport breaks mid-upload
(segments lost, connection dropped), the device keeps the partial blob in
its upload buffer. Activating that content can fail, and the next complete
upload replaces it. If the end marker (`e0 33`) is never acknowledged, the
content is incomplete on the device — do not activate cached content from
that upload. After activation, GIF playback needs a brief settle (~3 s
verified live) before further traffic on the connection while the first
frames start playing.

### Content Blob Structure

The content uploaded via `e0 32` segments is a single blob with this layout:

```
[ea 23 01 03]              ← content header (4 bytes, constant)
[16-byte content hash]     ← MD5 hash for device-side caching
[json_length BE16]         ← length of JSON metadata
[json bytes]               ← JSON metadata (see below)
[payload]                  ← pixel data (a2pl) or raw GIF
```

The blob is split into `e0 32` segments of up to 490 bytes each (the MTU
limit minus command overhead). The device reassembles them by segment index.

### Segmentation (`e0 32`)

Each segment has a 9-byte command header before the chunk data:

```
e0 32 [chunk_len BE16] [00 00 00 00] [seg_idx] [chunk data...]
```

- `chunk_len`: bytes of chunk data in this segment (BE16)
- `00 00 00 00`: purpose unknown (possibly a BE32 offset, or padding)
- `seg_idx`: 0-based segment index (1 byte in all observed traces, giving
  a theoretical maximum of 256 segments x 490 bytes = ~125KB; the actual
  device limit is unknown)

### a2pl Payload Format

For a2pl content (types "a", "c", "e", "f"), the payload contains an offset
table followed by compressed frame data. Each "frame" is one 96-column page
of the display, independently compressed.

**Single frame** (`frame_num: 1`):

```
[00 00 00 00] [frame0_size BE16]
[frame0 compressed a2pl data]
```

The leading 4 zero bytes are consistent with a BE32 offset of 0 (frame 0
starts at the beginning of the data section).

**Multi-frame** (`frame_num > 1`):

```
[00 00 00 00] [frame0_size BE16]                   ← header (6 bytes)
[cumul_offset_1 BE32] [frame1_size BE16]           ← offset table entry
[cumul_offset_2 BE32] [frame2_size BE16]           ← (frame_num - 1 entries)
...
[frame0 compressed data]                           ← frame data section
[frame1 compressed data]
...
```

- `cumul_offset_N` = byte offset from the start of the frame data section
  to frame N. Equals the sum of all previous frame sizes.
  Verified as BE32 (4 bytes) against trace 91 (9 frames, 6204 bytes of
  compressed data across 95 printable ASCII characters).
- `frame_size` = compressed size of each individual frame (BE16).
- Each frame decompresses to a full 3072-byte framebuffer (96 cols x 16 rows
  x 2 bytes/pixel), except potentially the last frame of scrolling content.
- `amt_length` in JSON = total payload size (header + table + all frame data).

The number of frames is not encoded in the binary payload — the device reads
`frame_num` from the JSON metadata to know how many offset table entries to
expect (the table has `frame_num - 1` entries, since frame 0's offset is
implicitly 0).

The offset table allows the device to seek to any frame without decompressing
preceding frames.

**GIF payload** (`amt_fmt: 2`): No header or offset table. The payload is the
raw GIF file bytes. The device decodes GIF natively.

### GIF Device Limitations [CONFIRMED — crash-tested]

The device's GIF decoder is fragile. GIFs outside the app's envelope crash
the firmware (complete freeze, missing init responses, reboot cycle after
~2 min). Verified safe envelope (matching the app's own GIFs, e.g. trace 04):

| Property | Safe value | Crashes at |
|----------|-----------|------------|
| Color table | **Single global table** (128 or 256 entries) | Local color tables in image blocks |
| Frame delay | **≥ 100 ms** (6–10 fps) | 60–90 ms delays (11–16 fps) — wedges render loop |
| Frame count | **~60 frames** max observed safe | (not exhaustively tested) |
| Size | **≤ 52 KB** observed safe; 60–98 KB with other violations crashed | combined with the above |

Crash characteristics: upload *completes* and the content hash is cached,
then the device stops answering (only one `ea 81` during init, incomplete
GATT service discovery) and reboots after ~2 minutes. Recovery: physical
power cycle, or wait out the reboot.

Also avoid: reconnecting over BLE while a GIF is playing — this independently
triggers the same wedge. The app never reconnects mid-playback. See the
[BLE Connect-Cycle Limitation](#ble-connect-cycle-limitation--confirmed--crash-tested)
below for the root cause and integration guidance.

`e0 32` segments themselves need no pacing (the app writes back-to-back,
median 0.4 ms gaps, and a 107-segment upload is fine).

### BLE Connect-Cycle Limitation [CONFIRMED — crash-tested]

The firmware leaks a resource (suspected RAM) per BLE connection. After
**~6–7 connect cycles since boot**, the device wedges — regardless of what
the sessions did:

| Observed | Value |
|----------|-------|
| Connect cycles tolerated per boot | ~6–7 |
| Wedge signature | Panel freezes on the last visual; device still advertises (`IOTBT*`) but init never completes (single `15 ea 81`, second `16 ea 81` never arrives, GATT service discovery incomplete) |
| Self-recovery | **None** — manual power cycle required |
| Recovery window | ~2 min dark after power cycle before advertising resumes |
| Depends on content? | No — read-only sessions (brightness) wedge identically |
| Depends on session duration? | **No** — 15+ commands over one connection are fine (trace 60; the app's own pattern) |
| Depends on in-session command count? | No — 10+ commands per session verified safe |

Wedge trigger matrix (all reproduced live):

- 7th connect after boot, even with 15–60 s idle gaps between sessions
- Connecting while a GIF plays (independent trigger, see above)

**Implications for integration/control authors:**

- **Batch all commands into one session.** One session ≈ one user action.
  A long-lived, always-connected session is safe and is exactly what the
  official app does.
- **Never poll by connecting.** A 60 s status poll wedges the device within
  minutes. Maintain state from command ACKs (`e0 01` responses carry
  power/brightness/speed) and keep the connection open.
- After a wedge: the device must be power-cycled manually. Hammering a
  wedged device with reconnect attempts extends the wedge — back off and
  warn the user instead.

**Concurrent commands:** a single connection carries strictly serialized
commands. The device reassembles `e0 32` upload chunks by segment index into
ONE blob — two interleaved uploads corrupt each other's data. Any concurrent
command pair also races the request/response matching (both sides match the
first notification that arrives). Control implementations must hold a command
lock around every write/await sequence on a shared connection; a command only
"ends" once its final ACK (or the response timeout) has been processed.

### JSON Metadata

The JSON metadata precedes the payload in the content blob.

**Static image (type "a"):**
```json
{"v":1,"mant_type":0,"enable_a2pl":1,
 "layers":[{"nm":"uuid","type":"a","amt_pos":0,
            "frame_num":1,"amt_length":798,"amt_fmt":0}],
 "all_file_type":"a"}
```

**GIF animation (type "b" / "d"):**
```json
{"v":1,"mant_type":0,"enable_a2pl":1,
 "layers":[{"nm":"uuid","type":"b","amt_pos":0,
            "frame_num":0,"amt_length":378,"amt_fmt":2}],
 "all_file_type":"b"}
```

**Scrolling text (type "e"):**
```json
{"v":1,"mant_type":0,"enable_a2pl":1,
 "layers":[{"nm":"uuid","type":"e",
            "attr":[{"speed":83,"effect":"b","pause_t":1,
                     "bg_color":"000000",
                     "fg_color":["006464","1E6464",...],
                     "fg_attr":0,"fg_dir":1,
                     "last_word_frame":76}],
            "amt_pos":0,"frame_num":9,
            "amt_length":6258,"amt_fmt":0}],
 "all_file_type":"e"}
```

### Text Color Attributes [CONFIRMED]

Text colors are **device-rendered** — the a2pl pixel data contains the text shape
in a placeholder color, and the firmware applies the gradient at render time based
on metadata. This means the same pixel data works with any color scheme.

| Field | Values | Meaning |
|-------|--------|---------|
| `fg_color` | list of hex strings | Color stops in `"HHSSVV"` format (see below) |
| `fg_attr` | `0` / `1` | `0` = gradient mode, `1` = solid color |
| `fg_dir` | `1` | Gradient direction (1 = left-to-right) |
| `bg_color` | hex string | Background in `"HHSSVV"` format (`"000000"` = black) |

**`fg_color` format:** Each entry is `"HHSSVV"` where:
- `HH`: Hue, 0x00–0x96 (0–150 decimal) maps to 0–360°
- `SS`: Saturation, 0x00–0x64 (0–100 decimal)
- `VV`: Value/brightness, 0x00–0x64 (0–100 decimal)

Examples:
- `["006464"]` — solid red (H=0°, S=100%, V=100%)
- `["006464","1E6464","3C6464","5A6464","786464","966464"]` — full rainbow
- `["5A6464","786464","966464"]` — cool gradient (cyan → blue → magenta)

The device interpolates between color stops across the display width. The gradient
stays fixed in position — when text scrolls, it moves through the stationary color
field. Arbitrary numbers of color stops are supported.

**Waveform custom animation (type "f"):**
```json
{"v":1,"mant_type":0,"enable_a2pl":1,
 "layers":[{"nm":"uuid","type":"f","amt_pos":0,
            "frame_num":2,"amt_length":1381,"amt_fmt":0}],
 "all_file_type":"f"}
```

## HSV Color Encoding

16-bit packed HSV with an unusual bit layout:

```
byte1:  H6 H5 H4 H3 H2 H1 H0 S3
byte2:  S2 S1 S0 V4 V3 V2 V1 V0
```

| Field | Bits | Range | Resolution |
|-------|------|-------|------------|
| Hue | 7 | 0–127 | ~2.8° |
| Saturation | 4 | 0–15 | ~6.7% |
| Value | 5 | 0–31 | ~3.2% |

**Encoding:**
```python
hue_7 = round(H_deg / 360 * 127) & 0x7F
s_4   = round(S_pct / 100 * 15)  & 0xF
v_5   = round(V_pct / 100 * 31)  & 0x1F
byte1 = (hue_7 << 1) | ((s_4 >> 3) & 1)
byte2 = ((s_4 & 0x7) << 5) | v_5
```

## Device Status Response (`ea 81`)

The device sends `ea 81` status notifications during init (see Init Sequence)
and in response to commands like `e0 01` (brightness, power). The payload
(offsets relative to the inner response, 0-indexed after the 8-byte wrapper):

```
15 ea 81 00 00 dd 06 23 75 01 64 00 00 00 64 00 00 60 10 02 00 dd 02 63 00
 0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24
```

| Offset | Example | Meaning |
|--------|---------|---------|
| 0 | `15`/`16` | Response prefix (see init sequence) |
| 1–2 | `ea 81` | Command echo |
| 7 | `23`/`24` | Power state: `0x23` = on, `0x24` = off (echoes `e0 01` subcommand) |
| 10 | `0a`–`64` | Animation speed (1–100), verified by set/reconnect/read |
| 14 | `64` | Current brightness (0–100) |
| 17 | `60` | Display columns (96) |
| 18 | `10` | Display rows (16) |

Other fields (e.g. bytes 5–6, 8–9, 11–13, 21–24) vary across devices and
sessions but are not yet understood.

Note: `ea 07` (set speed) does not trigger a notification response.
The speed value only appears in `ea 81` status on reconnect.

## Traces

97 BTSnoop HCI captures in `research/traces/`, covering:
- Power on/off, brightness (01–03)
- GIF animation upload and speed control (04–08)
- Playlist/carousel management (07–08)
- Text upload with scrolling (12–13)
- Graffiti pixel drawing (14–61)
- Graffiti animation (62)
- Text color gradients (64)
- All 55 stock images (90)
- All printable characters scrolling text (91)
- Stock image recapture (92)
- Clock display (93)
- Audio waveform (94)
- All 8 clock styles +/- date (95)
- Clock 12h/24h toggle (96)
- Waveform styles and custom colors (97)
