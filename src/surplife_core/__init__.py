"""surplife_core — pure protocol and encoding logic for Surplife displays.

No BLE transport dependencies: this package contains the reverse-engineered
command builders, response parsers, pixel encoders, and content validators.
Transport (BLE) is abstracted behind an interface so both the CLI (bleak)
and Home Assistant (bleak-retry-connector + shared scanner) can drive it.
"""

from .a2pl import compress, decompress  # noqa: F401
from .content import (  # noqa: F401
    build_a2pl_payload,
    content_hash,
    validate_gif,
)
from .messages import match  # noqa: F401
from .protocol import (  # noqa: F401
    BLE_MANUFACTURER_ID,
    BLE_NAME_PREFIX,
    BYTES_PER_PIXEL,
    DISPLAY_COLS,
    DISPLAY_ROWS,
    FRAMEBUFFER_SIZE,
    MAX_SEGMENT_SIZE,
    WRITE_UUID,
)

__version__ = "0.1.0"
