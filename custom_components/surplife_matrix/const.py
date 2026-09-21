"""Constants for the Surplife Matrix integration."""

from __future__ import annotations

DOMAIN = "surplife_matrix"

PLATFORMS = ["light"]

# NOTE: no periodic status polling. The device firmware wedges after ~6-7
# BLE connect cycles per boot (see PROTOCOL.md "BLE Connect-Cycle
# Limitation"); a polling coordinator would brick the display within
# minutes. State is maintained from command ACKs + optimistic updates on
# a persistent, always-connected session instead.

# Persistent-connection watchdog: when a write/wait times out this many
# times consecutively, the device is presumed firmware-wedged. The
# integration then marks the entity unavailable, warns the user, and
# stops reconnect attempts for RECONNECT_COOLDOWN seconds (hammering a
# wedged device extends the wedge).
WEDGE_THRESHOLD = 2
WEDGE_COOLDOWN_S = 300

# Reconnect backoff for a dropped (but healthy) connection.
RECONNECT_MIN_BACKOFF_S = 5.0
RECONNECT_MAX_BACKOFF_S = 60.0

# Post-activation dwell after GIF uploads: let the device start playback
# before further traffic on the connection (verified-safe pattern from
# live device testing).
GIF_ACTIVATION_DWELL_S = 3.0

# Content services
SERVICE_SHOW_MEDIA = "show_media"
SERVICE_SHOW_CAMERA = "show_camera"
SERVICE_SHOW_TEXT = "show_text"
SERVICE_SHOW_CLOCK = "show_clock"
SERVICE_DRAW_PIXELS = "draw_pixels"
SERVICE_PLAYLIST_ADD = "playlist_add"
SERVICE_PLAYLIST_REMOVE = "playlist_remove"
SERVICE_PLAYLIST_CLEAR = "playlist_clear"
SERVICE_SET_SPEED = "set_speed"
SERVICE_ACTIVATE = "activate"

# Effects (ea 06) for static images
EFFECTS = {
    "static": 1,
    "scroll-r": 2,
    "scroll-l": 3,
    "flicker": 4,
    "breathe": 5,
    "snowflake": 6,
    "blend": 7,
    "sweep": 8,
    "bands": 9,
    "wipe": 10,
}
