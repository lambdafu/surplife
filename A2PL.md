# a2pl Pixel Data Format

Compression format used by the Surplife display for pixel data.
See [PROTOCOL.md](PROTOCOL.md) for the BLE protocol, commands, and metadata.

**Status:** Fully decoded. Compressor and decompressor verified against all
traces (55 stock images, scrolling text with >800 columns, 100% roundtrip).

## Display Geometry

- 96 columns x 16 rows
- 2 bytes per pixel (16-bit HSV)
- Total framebuffer: 96 x 16 x 2 = 3072 bytes
- Column-major: pixel at `(col, row)` is at byte offset `col * 32 + row * 2`
- Display does NOT clear between writes — must always write full framebuffer
- Display signals errors by showing red pixels in column 1

### Wide Framebuffers and Multi-Frame

For scrolling text and wide images, the content is wider than 96 columns.
The wide framebuffer is split into 96-column pages ("frames"), each
independently compressed with a2pl. Each frame decompresses to 3072 bytes.

The frames are stitched together with an offset table in the upload payload
that lets the device seek to any frame. The last frame may use fewer than
96 columns — the actual column count is specified in the JSON metadata
(`last_word_frame`).

See [PROTOCOL.md — a2pl Payload Format](PROTOCOL.md#a2pl-payload-format) for
the binary layout of the offset table and frame data.

## Compression Format

LZ77-style compression. Each command byte encodes two things in its nibbles:
- **Upper nibble** = literal byte count
- **Lower nibble** = back-reference copy length

Both use the **same extension pattern**: `0x0`–`0xE` = direct value,
`0xF` = base + sum-encoded extension.

### Command Layout

```
[opcode] [sum_lit?] [literal bytes] [LE16 distance] [sum_copy?]
```

- `sum_lit` present only when upper nibble = `0xF`
- `sum_copy` present only when lower nibble = `0xF`

| Nibble value | Literal count (upper) | Copy length (lower) |
|--------------|----------------------|---------------------|
| `0x0`–`0xE` | N bytes | N + 4 bytes |
| `0xF` | 15 + sum(N) bytes | 19 + sum(N) bytes |

### Literals

The upper nibble specifies how many literal bytes follow the opcode
(or sum-encoding). These bytes are written directly to the framebuffer.

### Back-References

After the literals, a back-reference copies from earlier in the output:

- **LE16 distance**: how far back to start copying
- **Copy length**: determined by lower nibble (see table above)

When `copy_length > distance`, LZ77 wraps — the source repeats, creating
tiling patterns:

| Distance | Effect |
|----------|--------|
| 1 | Repeats last byte: `AA AA AA...` |
| 2 | Tiles last 2 bytes: `AB AB AB...` |

### Sum-Encoding

Variable-length integer encoding, used when nibble = `0xF`:
- Byte `0x00`–`0xFE`: value is the byte itself (terminates)
- Byte `0xFF`: add 255 and continue reading
- Example: `FF 0D` = 255 + 13 = 268

### Stream Ending

There is no explicit terminator. The stream ends when data runs out.

**Important firmware quirk:** The device's decompressor has an off-by-one
bug — if the last back-reference lands exactly on the framebuffer boundary,
the final pixels are not written. The app works around this by ensuring
every stream ends with trailing literal bytes whose back-reference distance
is never reached:

```
[50 XX XX XX XX XX]     5 literal bytes → stream ends before backref
[70 XX XX XX XX XX XX XX]  7 literal bytes → stream ends before backref
```

Compressors **must** ensure the stream never ends on a backref boundary.
Shorten the last backref by a few bytes and emit the remainder as a
trailing literal command.

## Example

### Solid Fill (clear display)

```
2f 00 00 02 00 ff ff ff ff ff ff ff ff ff ff ff f1 50 00 00 00 00 00
```

| Bytes | Meaning |
|-------|---------|
| `2f 00 00` | 2 literal bytes: black pixel (HSV `00 00`) |
| `02 00` | Backref distance = 2 |
| `ff`×11 + `f1` | Sum-encoded length: 255×11 + 241 = 3046, copy = 3046+19 = 3065 bytes |
| `50 00 00 00 00 00` | 5 literal bytes (trailing, backref never reached) |

Total decompressed: 2 + 3065 + 5 = 3072 bytes (full framebuffer).
