# APK Decompilation Analysis

## App Overview

The Surplife app is a **Flutter app** with Java BLE transport. The app supports many device
types (WiFi bulbs, mesh devices, BLE LED displays). Our device uses the **ZGHB** protocol
variant via the `com.zengge.hagallbjarkan` BLE library.

**Critical finding**: All protocol-level logic (JSON fields like `mant_type`, `enable_a2pl`,
`amt_fmt`, `fg_color`, `effect`, `layers`, `all_file_type`) is in **compiled Dart code**
(`libapp.so`), NOT in the Java sources. The Java side only handles BLE transport and device
lifecycle. This means we cannot decompile the actual command-building logic from Java.

App entry point: `com.zengge.magichome2.flutter.NewFlutterEngineActivity`
Dart entrypoint args: `["ZG002", "hb6cZv"]`

## Architecture

```
Flutter/Dart (libapp.so)           Java (decompiled)
========================           ==================
Protocol logic                     BLE transport
- JSON field construction          - GATT connection management
- Command bytes (0xe0, 0xea...)    - Packet fragmentation (MTU)
- Pixel encoding ("a2pl")          - Write/Read characteristics
- Color conversion                 - Sequence numbering
                                   - Multi-packet assembly
        |                                    |
        v                                    v
   CommInfo {hex, opcode}    --->    ZGHBDeviceHandler.write()
   (via Pigeon FlutterDeviceControlApi)      |
                                    ZGHBWriteUtils.write()
                                             |
                                    UpperTransportLayer
                                             |
                                    LowerTransportLayerEncoder
                                             |
                                    BLE GATT Write (char ff01)
```

## Command Flow (Dart -> BLE)

1. **Dart** builds raw command bytes + opcode
2. Sends via Pigeon bridge: `FlutterDeviceControlApi.pushCommand(CommInfo)`
3. `CommInfo.cmd` HashMap contains:
   - `"hex"` — raw byte payload (the protocol data we see in traces)
   - `"opcode"` — single byte (parsed as `(byte)(int)Double.parseDouble(...)`)
   - `"waitGattResponse"` — boolean
4. `BleDeviceManagerServer.handleCommand()` dispatches to `ZGHBDeviceHandler`
5. `ZGHBDeviceHandler.write(mac, opcode, hex_bytes, waitResponse)` wraps in transport layer

## BLE Transport Layer (fully understood from Java)

### UUIDs (`handler/zghb/Service.java`)
| UUID | Purpose |
|------|---------|
| `0000ffff-...-00805f9b34fb` | Main Service |
| `0000ff01-...-00805f9b34fb` | Write Characteristic |
| `0000ff02-...-00805f9b34fb` | Read/Notify Characteristic |
| `0000fe00-...-00805f9b34fb` | OTA Service |
| `0000ff11-...-00805f9b34fb` | OTA Write |
| `0000ff22-...-00805f9b34fb` | OTA Read |

### UpperTransportLayer (`protocol/zgble/UpperTransportLayer.java`)

Wraps app-level messages:
- `ack` (bool) — whether ACK is expected
- `protect` (bool) — protection flag
- `type` (byte) — 0=JSON, 1=HEX, 2=ACK_PACK
- `seq` (byte) — per-device sequence counter
- `cmdId` (byte) — the opcode from CommInfo
- `payload` (byte[]) — the hex data from CommInfo

Factory: `createUpper(ack=false, protect=false, seq, cmdId, payload)` for normal writes.

### LowerTransportLayerEncoder — Packet Fragmentation

**Version detection**: `bleVersion >= 8` → writeVersion=1 (V1), else writeVersion=0 (V0)

#### V0 Format (MTU <= 255)

**First packet** (8-byte header):
```
[0] ctrl: bit5=protect, bit4=ack, bit6=hasFrag, bits1-0=version(0)
[1] seq
[2] segNum_hi (0x00 for first, 0x80|idx for last)
[3] segNum_lo
[4] totalLen_hi    (payload length, BE 16-bit)
[5] totalLen_lo
[6] thisSegLen      (8-bit, = payloadInThisSeg + 1)
[7] cmdId
[8...] payload data
```

**Continuation packets** (5-byte header):
```
[0] ctrl (same as above, bit6=1 for frag)
[1] seq
[2] segNum_hi (0x80|idx if last segment)
[3] segNum_lo
[4] thisSegLen (8-bit)
[5...] payload data
```

#### V1 Format (MTU <= 512)

**First packet** (9-byte header):
```
[0] ctrl: bits same as V0, bits1-0=version(1)
[1] seq
[2] segNum_hi
[3] segNum_lo
[4] totalLen_hi    (BE 16-bit)
[5] totalLen_lo
[6] thisSegLen_hi  (BE 16-bit, = payloadInThisSeg + 1)
[7] thisSegLen_lo
[8] cmdId
[9...] payload data
```

**Continuation packets** (6-byte header):
```
[0] ctrl
[1] seq
[2] segNum_hi
[3] segNum_lo
[4] thisSegLen_hi  (BE 16-bit)
[5] thisSegLen_lo
[6...] payload data
```

### Ctrl Byte Bit Layout
```
bit 7: always 0
bit 6: hasMoreFragments (1=multi-packet, 0=single or last)
bit 5: protect
bit 4: ack
bit 3: unused (0)
bit 2: unused (0)
bit 1-0: version (0=V0, 1=V1)
```

**NOTE**: The `createCtrl` function explicitly clears bits 3-0 then ORs in `version & 3`.
The encoder sets bit6 via `z12` parameter (true for multi-packet first segment).
For single packets, bit6=0 and segNum=0x8000 (= "last segment, index 0").

### LowerTransportLayerDecoder — Reassembly

**Version detection on receive**: `(byte[0] & 3) == 1` → V1 format.
**Fragmentation detection**: `(byte[0] & 64) == 64` → multi-packet message.
**Last segment marker**: `(segNum_hi & 0x80) == 0x80`.

The decoder accumulates fragments, checking `seq` and `segNum` continuity.

### Write with ACK (`ZGHBWriteUtils.writeAck`)

For commands requiring acknowledgment:
1. Register a `ReceiveCallback` on the connection
2. Write packet to GATT
3. Wait up to 500ms for response via `ResultFuture.getValue(500)`
4. If write fails or no ACK: set bit 7 of `packet[0]` (= `0x80 | byte[0]`) and retry
5. On success or ACK received: remove callback, continue to next fragment

## Bitmap/Font Rendering (`wh/a.java` + `wh/b.java`)

Flutter platform channel `"bit_text"` provides `getCharDataByBitmap(char, size)`:

1. Loads font from `assets/fonts/12.TTF`
2. Creates a `size x size` ARGB bitmap
3. Draws character at position (6.0, 9.6) in white, centered
4. Converts to binary: pixel < 0 (has alpha/color) → 1, else 0
5. Returns `Map<Integer, byte[]>` (column index → column bytes)

This is the **Java-side** font rendering for the `bit_text` channel. The Dart side likely
uses this to generate the 1-bit pixel data that gets compressed into the "a2pl" format.

Font file: `decompiled-apk/resources/assets/fonts/12.TTF`

## What We Can and Cannot Learn from the APK

### Already Extracted (transport layer — fully understood):
- BLE UUIDs and service structure
- Packet fragmentation format (V0 and V1)
- Sequence numbering scheme
- ACK retry mechanism
- MTU negotiation (512 for V1, 255 for V0)
- OTA firmware update protocol

### Cannot Extract from Java (need traces or Dart disassembly):
- Command byte assignments (0xe0, 0xea subtypes)
- JSON payload structure (mant_type, enable_a2pl, etc.)
- Pixel data "a2pl" compression algorithm
- Color encoding (HSV?)
- Carousel/playlist command format
- Cache check / content hash algorithm

### Potentially Extractable (from libapp.so with Dart RE tools):
- All protocol constants and command builders
- The a2pl compression/decompression algorithm
- Color conversion functions
- Hash calculation for content caching

## Key Source Files

### Protocol/Transport (`com.zengge.hagallbjarkan`)
| File | Purpose |
|------|---------|
| `handler/zghb/Service.java` | BLE UUID constants |
| `handler/zghb/ZGHBDeviceHandler.java` | Main device handler, write/request dispatch |
| `handler/zghb/ZGHBWriteUtils.java` | Low-level BLE write with version awareness |
| `handler/zghb/ZGHBReceiveCallback.java` | Notification handler, decodes incoming packets |
| `protocol/zgble/UpperTransportLayer.java` | App message wrapper (seq, cmdId, payload) |
| `protocol/zgble/LowerTransportLayerEncoder.java` | Packet fragmentation (V0/V1) |
| `protocol/zgble/LowerTransportLayerDecoder.java` | Packet reassembly |
| `protocol/zgble/OTAProtocol.java` | Firmware update encoding |
| `utils/ByteUtil.java` | Bit manipulation helpers |

### App / Flutter Bridge (`com.zengge.magichome2` / `com.zengge.wifi`)
| File | Purpose |
|------|---------|
| `magichome2/server/devicemanager/ble/BleDeviceManagerServer.java` | Command dispatch, device lifecycle |
| `wifi/flutter/plugin/send_command/generate/ControlMessages.java` | Pigeon-generated Dart↔Java bridge |
| `wifi/flutter/plugin/send_command/MagicHome2SendCommand.java` | Command routing to device managers |
| `wifi/flutter/PluginManager.java` | Flutter plugin registration |
| `wh/a.java` | "bit_text" platform channel plugin |
| `wh/b.java` | Bitmap rendering (font → 1-bit pixel array) |

## Next Steps

1. **Dart/libapp.so reverse engineering** — Tools like `blutter` or `darter` can extract
   Dart snapshots from libapp.so. This would reveal the actual protocol command builders,
   a2pl compression, and JSON construction logic.

2. **Font file analysis** — The `12.TTF` font is used for text rendering. Extracting it
   might help understand expected character dimensions.

3. **Trace correlation** — Now that we understand the transport layer wrapping, we can
   strip it from traces to focus purely on the protocol payload bytes.
