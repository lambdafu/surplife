"""BLE protocol constants and packet framing for Surplife displays.

All Surplife displays advertise as IOTBT<suffix> over BLE and share:
  - GATT service 0000ffff: main control (ff01 write, ff02 notify)
  - GATT service 0000fe00: OTA firmware updates (ff11 write, ff22 notify)
  - Manufacturer data under company ID 0x005a
"""

# GATT characteristic UUIDs
WRITE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# Display geometry
DISPLAY_COLS = 96
DISPLAY_ROWS = 16
BYTES_PER_PIXEL = 2
FRAMEBUFFER_SIZE = DISPLAY_COLS * DISPLAY_ROWS * BYTES_PER_PIXEL  # 3072

# BLE discovery
BLE_NAME_PREFIX = "IOTBT"
BLE_MANUFACTURER_ID = 0x005A

# Upload limits
MAX_SEGMENT_SIZE = 490
