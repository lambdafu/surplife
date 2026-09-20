# Bluetooth Low Energy — A Practical Introduction

A gentle introduction to BLE for anyone poking at embedded devices with a packet
sniffer and a hex editor. We'll use examples from the Surplife LED display
reverse engineering project, but everything here applies to BLE in general.

## What is BLE?

Bluetooth Low Energy (BLE, also called Bluetooth Smart) is a wireless protocol
designed for devices that need to exchange small amounts of data while sipping
power — think sensors, wearables, LED displays, smart locks.

BLE is **not** Classic Bluetooth. They share a name, a radio band (2.4 GHz),
and an entry in your phone's settings, but the protocols are completely different:

| | Classic Bluetooth | BLE |
|--|-------------------|-----|
| Designed for | Audio streaming, file transfer | Sensor data, control messages |
| Power | ~30 mA average | ~1 uA idle, ~15 mA peak |
| Connection model | Continuous stream | Short bursts, sleeps between |
| Throughput | ~2 Mbps | ~100–250 Kbps |
| Battery life | Hours to days | Months to years on coin cell |

A BLE device can also support Classic Bluetooth (called "dual-mode"), but the
two stacks are independent. When you're sniffing BLE traffic, you're looking at
an entirely separate protocol from the Bluetooth audio in your headphones.

## The Cast of Characters

BLE defines four roles. In practice, you'll care about two:

- **Peripheral** — the device. Advertises its presence, accepts connections,
  holds the data. Our Surplife display is a peripheral.
- **Central** — the phone/computer. Scans for peripherals, initiates connections,
  reads and writes data. The Surplife app running on your phone is the central.

The other two roles (Broadcaster and Observer) are for beacons and listeners that
never form a connection. We won't need them here.

## How a Connection Happens

### 1. Advertising

A peripheral broadcasts advertising packets on three dedicated channels (37, 38, 39)
spread across the 2.4 GHz band. These packets say "I exist, here's my name and
some basic info." The Surplife display advertises as `IOTBT` followed by the last 3 hex digits
of its MAC address (e.g. `IOTBT996`, `IOTBTD98`).

### 2. Scanning

The central listens on those same three channels. When it hears an advertisement
it's interested in, it can request more details (active scanning) or just connect.

### 3. Connection

The central sends a connection request. Both sides agree on timing parameters
(how often to wake up and talk) and switch to the 37 data channels, hopping
frequencies to avoid interference.

From this point on, the central and peripheral exchange data in periodic
**connection events** — brief windows where both radios are awake. Between events,
both sides sleep. This is where BLE's low power comes from.

## The Data Model: GATT

Once connected, BLE uses a layered data model called **GATT** (Generic Attribute
Profile). Think of it as a tiny database on the device:

```
Server (the device)
  Service: "LED Control" (UUID 0000ffff)
    Characteristic: "Command" (UUID ff01)  [Write]
    Characteristic: "Response" (UUID ff02) [Notify]
  Service: "Unknown" (UUID 0000fe00)
    Characteristic: ff11  [Write]
    Characteristic: ff22  [Notify]
```

That's the actual GATT table of the Surplife display. Let's break it down:

**Services** are logical groupings of related functionality. Each has a UUID
(universally unique identifier). The Surplife uses service `0000ffff` for all
its LED control — commands go in, status comes back.

**Characteristics** are the actual data endpoints within a service. Each has:
- A **UUID** — identifies what it is
- **Properties** — what you can do with it (read, write, notify, etc.)
- A **value** — the actual bytes

The Surplife display is simple: one characteristic to write commands to (`ff01`),
one that sends notifications back (`ff02`). Many devices are more complex, with
dozens of characteristics for different sensors, settings, and data streams.

### Properties

Each characteristic declares what operations it supports:

| Property | Direction | ACK? | Use case |
|----------|-----------|------|----------|
| Read | Client ← Server | Yes | Read a sensor value |
| Write | Client → Server | Yes | Send a command, get confirmation |
| Write Without Response | Client → Server | No | Stream data fast, no waiting |
| Notify | Client ← Server | No | Device pushes updates |
| Indicate | Client ← Server | Yes | Like Notify, but confirmed |

**Write** vs **Write Without Response** is an important distinction. Write (with
response) is like certified mail — you send a packet, then wait for the device
to acknowledge before sending the next. Write Without Response is fire-and-forget:
faster throughput, but no delivery guarantee. The Surplife app uses Write Without
Response for bulk data transfer (pixel uploads), and the display confirms receipt
through separate Notify messages.

**Notify** vs **Indicate** is the same trade-off in reverse. The Surplife display
uses Notify — it pushes status updates without waiting for the phone to confirm.

### Subscribing to Notifications

A quirk that trips up beginners: notifications don't just happen. The client must
explicitly **subscribe** by writing to a special descriptor called the **CCCD**
(Client Characteristic Configuration Descriptor, UUID `0x2902`) attached to the
characteristic. Writing `0x01 0x00` enables notifications, `0x00 0x00` disables
them. Until you do this, the device won't send you anything.

## UUIDs

UUIDs identify services and characteristics. They come in two flavors:

**16-bit short UUIDs** are assigned by the Bluetooth SIG for standardized services.
They're shorthand for a full 128-bit UUID using the Bluetooth Base UUID:

```
0000XXXX-0000-1000-8000-00805F9B34FB
     ^^^^
     your 16-bit UUID goes here
```

So service `0x180F` (Battery Service) is really
`0000180F-0000-1000-8000-00805F9B34FB`.

**128-bit vendor UUIDs** are what most devices use for their custom stuff.
The Surplife display uses `0000ffff` and `0000fe00` as service UUIDs — these
*happen* to fit the 16-bit format but aren't SIG-assigned. Most BLE tools will
display them as short UUIDs anyway.

In our protocol docs, we use the short form for readability:

| Full UUID | Short form |
|-----------|------------|
| `0000ffff-0000-1000-8000-00805f9b34fb` | `0000ffff` |
| `0000ff01-0000-1000-8000-00805f9b34fb` | `ff01` |

## Handles

Under the hood, GATT is built on the **ATT** (Attribute Protocol) layer. ATT
doesn't know about services or characteristics — it just sees a flat table of
**attributes**, each identified by a 16-bit **handle** (think: array index).

Services, characteristics, their values, and descriptors are all just attributes
with sequential handles. When you write to characteristic `ff01`, your BLE stack
looks up which handle corresponds to that characteristic's value and issues an
ATT Write to that handle number.

You'll see handles in packet captures. They're assigned sequentially by the device
and stay stable for the duration of a connection (and usually across reconnections,
unless the device's firmware changes its GATT table).

## MTU

The **MTU** (Maximum Transmission Unit) determines how much data fits in a single
ATT packet.

- **Default MTU**: 23 bytes. After subtracting 3 bytes of ATT header, that's
  **20 bytes** of usable payload. Not much.
- **Negotiated MTU**: Right after connecting, the client can request a larger MTU
  via an Exchange MTU Request. The effective MTU is the minimum of what both sides
  support.

The Surplife display negotiates **MTU 512**, giving us ~509 bytes per packet.
This matters because we're uploading kilobytes of pixel data — at the default
20 bytes per packet, a single image would take over 150 packets. At MTU 512,
it takes about 6.

MTU negotiation is one of the first things you'll see in a packet capture after
connection establishment.

## What's in a Packet Capture?

When you sniff BLE traffic (via Apple's PacketLogger, Android's HCI log, or
Wireshark with a sniffer dongle), you're capturing **HCI** (Host Controller
Interface) traffic — the boundary between the Bluetooth stack and the radio
hardware. This includes:

- **HCI commands/events** — connection management, scanning, etc.
- **ACL data packets** — the actual L2CAP/ATT data

A typical session looks like:

```
1. HCI: LE Create Connection (central connects to peripheral)
2. HCI: Connection Complete
3. ATT: Exchange MTU Request (client: 512) → Response (server: 512)
4. ATT: Read By Group Type (discover services)
5. ATT: Read By Type (discover characteristics)
6. ATT: Write (subscribe to notifications on ff02)
7. ATT: Write Command → ff01 (app sends a command)
8. ATT: Handle Value Notification ← ff02 (device responds)
   ... (repeat 7-8 for the rest of the session)
```

In the Surplife traces, step 7 is where all the interesting stuff happens:
display commands, pixel data uploads, and activation sequences — all sent as
Write Commands to characteristic `ff01`. The display's responses come back as
notifications on `ff02`.

### A Real Packet

Here's an actual command from our traces — setting display brightness to 100%:

```
Write Command → ff01:
  01 12 80 00 00 0e 00 0f 0a e0 01 00 01 00 00 64 00 64 00 00 00 00 00
  ├─┤                              ├────────────────────────────────────┤
  seq                              command payload (e0 01 ... brightness)
       ├──┤
       flags (0x80 0x00)
             ├──┤ ├──┤
             len-1 len
                        ├┤
                        0x0a marker
```

The Surplife protocol adds its own framing on top of BLE — sequence numbers,
length fields, a marker byte — before the actual command bytes. This layering
is typical: the BLE transport delivers raw bytes, and the device defines its
own application protocol on top.

## Tools of the Trade

**Capturing traffic:**

- **Apple PacketLogger** (macOS) — free, from Apple's "Additional Tools for
  Xcode" download. Records all Bluetooth HCI traffic from your Mac. This is
  what we used for the Surplife traces.
- **Android HCI snoop log** — enable in Developer Options, captures to a file.
  Open with Wireshark.
- **Wireshark** — opens both formats. Great for filtering and protocol dissection.
  Filter `btatt` to see only ATT operations, `btatt.handle == 0x0012` for a
  specific characteristic.
- **nRF Sniffer** (Nordic) — hardware dongle that captures actual over-the-air
  packets, including advertising. Plugs into Wireshark.

**Interactive exploration:**

- **nRF Connect** (Nordic, iOS/Android/Desktop) — scan for devices, browse their
  GATT tables, read/write characteristics interactively. Essential for poking at
  a device before writing code.
- **LightBlue** (iOS/macOS) — similar to nRF Connect.
- **`bluetoothctl`** (Linux) — command-line BLE tool from BlueZ.

**Programmatic access:**

- **Bleak** (Python) — cross-platform BLE library. Async, clean API, works on
  macOS/Windows/Linux. This is what this Surplife third-party library uses.
- **CoreBluetooth** (macOS/iOS) — Apple's native framework.
- **Web Bluetooth** — browser-based BLE access (Chrome).

## Tips for Reverse Engineering

1. **Start with nRF Connect.** Before capturing anything, connect to the device
   and browse its GATT table. Note down services, characteristics, and their
   properties. This tells you where data flows.

2. **Capture the app doing its thing.** Use PacketLogger or Android HCI logging
   while the official app talks to the device. Each action (set brightness,
   upload image, change mode) produces a distinct sequence of writes and
   notifications.

3. **Isolate and compare.** Capture the same action multiple times with small
   variations (e.g., brightness 50% vs 100%). Diff the packets to find which
   bytes change.

4. **Look for framing patterns.** Most devices wrap their protocol in some kind
   of header — length fields, sequence numbers, command codes. The Surplife
   uses a 9-byte header before every command. Once you find the framing, you
   can parse any packet.

5. **Watch the notifications.** Device responses often encode status, error
   codes, or acknowledgments that help you understand the request/response
   protocol.

6. **MTU matters for uploads.** If the device uploads large payloads (images,
   firmware), the data will be split across multiple ATT packets. You'll need
   to reassemble them — your packet capture tool may or may not do this for you.

## Further Reading

- [PROTOCOL.md](PROTOCOL.md) — the Surplife display protocol we reverse-engineered
  using these techniques
- [A2PL.md](A2PL.md) — the pixel data compression format
- The [Bluetooth Core Specification](https://www.bluetooth.com/specifications/specs/core-specification/)
  (free PDF, ~3000 pages — you won't read it cover to cover, but it's the
  definitive reference when you need to look something up)
- Nordic's [BLE Fundamentals](https://academy.nordicsemi.com/courses/bluetooth-low-energy-fundamentals/) —
  free online course, excellent
