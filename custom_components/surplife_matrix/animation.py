"""Animation catalog and asynchronous upload manager.

The integration ships device-safe GIFs in its `animations/` folder (see
`assets.py`). This module adds the HA-facing layer on top:

- `AnimationCatalog`: the list of bundled animations with metadata
  (name, path, size, frame count) and the per-device mapping of
  content hash -> animation name, so uploads can be referred to by
  name instead of an opaque 32-hex-digit hash.
- `AnimationUpload`: a fire-and-forget upload of one animation. The
  caller gets a handle immediately; the upload runs in the background
  on the runtime's command lock (so it never interleaves with other
  traffic) and reports its state through the handle.

Why asynchronous: the bundled GIFs are up to ~41 KB and take several
seconds to push over BLE. A synchronous service call would block the
HA service worker (and the frontend spinner) for that time, so the
UI-facing paths (select entity, light effect) start the upload and
return at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from .assets import list_animations
from .core import messages as m
from .core.content import content_hash

_LOGGER = logging.getLogger(__name__)

# Upload states reported by AnimationUpload.
STATE_IDLE = "idle"
STATE_UPLOADING = "uploading"
STATE_ACTIVE = "active"
STATE_ERROR = "error"


@dataclass(frozen=True)
class Animation:
    """One bundled animation."""

    name: str
    path: str
    size: int
    frames: int


def _probe_animation(name: str, path: str) -> Animation:
    """Build an Animation descriptor, reading size/frame count from disk."""
    size = 0
    frames = 0
    try:
        with open(path, "rb") as f:
            raw = f.read()
        size = len(raw)
        frames = _count_frames(raw)
    except OSError as err:  # pragma: no cover - unreadable file
        _LOGGER.warning("Cannot read bundled animation %s: %s", name, err)
    return Animation(name=name, path=path, size=size, frames=frames)


def _count_frames(raw: bytes) -> int:
    """Count image descriptors in a GIF without full decoding."""
    if raw[:6] not in (b"GIF87a", b"GIF89a"):
        return 0
    packed = raw[10]
    gct_entries = 2 << (packed & 7) if (packed >> 7) & 1 else 0
    idx = 13 + (3 * gct_entries if gct_entries else 0)
    frames = 0
    while idx < len(raw):
        b = raw[idx]
        if b == 0x21:  # extension
            idx += 2
            while idx < len(raw) and raw[idx] != 0:
                idx += raw[idx] + 1
            idx += 1
        elif b == 0x2C:  # image descriptor
            frames += 1
            ip = raw[idx + 9]
            entries = 2 ** ((ip & 7) + 1) if (ip >> 7) & 1 else 0
            idx += 10 + (3 * entries if entries else 0) + 1
        elif b == 0x3B:  # trailer
            break
        else:
            idx += 1
    return frames


class AnimationCatalog:
    """The bundled animations plus the per-device name/hash mapping.

    The name -> hash mapping is what lets `playlist_add` and
    `activate` refer to content by a readable name. It is kept in
    `hass.data` so it survives entity reloads.
    """

    def __init__(self) -> None:
        self._animations: dict[str, Animation] = {}
        self._names: dict[tuple[str, bytes], str] = {}

    @property
    def animations(self) -> dict[str, Animation]:
        """Bundled animations by name (lazily loaded)."""
        if not self._animations:
            for name, path in list_animations().items():
                self._animations[name] = _probe_animation(name, path)
        return self._animations

    @property
    def names(self) -> list[str]:
        """Sorted list of animation names."""
        return sorted(self.animations)

    def get(self, name: str) -> Animation | None:
        """Look up one animation by name."""
        return self.animations.get(name.strip().lower())

    def resolve(self, name: str) -> Animation:
        """Look up one animation, raising a helpful error when unknown."""
        anim = self.get(name)
        if anim is None:
            available = ", ".join(self.names) if self.animations else "(none)"
            raise KeyError(
                f"Unknown animation '{name}'. Available: {available}")
        return anim

    def record(self, address: str, c_hash: bytes, name: str) -> None:
        """Remember which animation a content hash refers to."""
        self._names[(address, bytes(c_hash))] = name

    def name_for(self, address: str, c_hash: bytes) -> str | None:
        """Name of a content hash, or None for foreign content."""
        return self._names.get((address, bytes(c_hash)))

    def hash_for(self, address: str, name: str) -> bytes | None:
        """Content hash of a named animation, if it was uploaded."""
        for (addr, c_hash), anim_name in self._names.items():
            if addr == address and anim_name == name:
                return c_hash
        return None


@dataclass
class AnimationUpload:
    """Handle for one in-flight animation upload."""

    name: str
    state: str = STATE_IDLE
    content_hash: bytes | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        """True when the upload finished (successfully or not)."""
        return self.state in (STATE_ACTIVE, STATE_ERROR)

    def as_dict(self) -> dict:
        """Serialisable view (used for service responses)."""
        return {
            "animation": self.name,
            "state": self.state,
            "content_hash": self.content_hash.hex() if self.content_hash else None,
            "error": self.error,
        }


class AnimationManager:
    """Starts animation uploads in the background for one device.

    One manager per config entry; it owns the catalog and the mapping
    of content hashes to animation names for that device.
    """

    def __init__(self, runtime, catalog: AnimationCatalog | None = None) -> None:
        self._runtime = runtime
        self._catalog = catalog or AnimationCatalog()
        self._uploads: dict[str, AnimationUpload] = {}
        self._current: str | None = None
        self._speed: int = 50
        self._on_update = None

    @property
    def catalog(self) -> AnimationCatalog:
        """The animation catalog."""
        return self._catalog

    @property
    def names(self) -> list[str]:
        """Available animation names."""
        return self._catalog.names

    @property
    def current(self) -> str | None:
        """Name of the animation that is currently displayed."""
        return self._current

    @property
    def speed(self) -> int:
        """Speed used for the last upload."""
        return self._speed

    def upload_state(self, name: str) -> str:
        """State of the last upload of `name`."""
        upload = self._uploads.get(name)
        return upload.state if upload else STATE_IDLE

    def set_update_callback(self, callback) -> None:
        """Register the callback invoked when an upload finishes.

        The select entity uses this to refresh its state once the
        background upload completed, without polling.
        """
        self._on_update = callback

    def set_current(self, name: str | None) -> None:
        """Record which animation is now displayed."""
        self._current = name

    def cancel_pending(self) -> None:
        """Cancel uploads that are still running (called on unload)."""
        for upload in self._uploads.values():
            if upload.task is not None and not upload.task.done():
                upload.task.cancel()

    def start(self, name: str, speed: int | None = None) -> AnimationUpload:
        """Start an upload in the background and return its handle.

        The upload is queued on the runtime's command lock, so it never
        interleaves with other traffic. A second upload of the same
        animation while one is in flight is ignored.
        """
        anim = self._catalog.resolve(name)
        if speed is not None:
            self._speed = speed

        existing = self._uploads.get(anim.name)
        if existing is not None and existing.state == STATE_UPLOADING:
            return existing

        upload = AnimationUpload(name=anim.name, state=STATE_UPLOADING)
        self._uploads[anim.name] = upload
        upload.task = asyncio.create_task(self._run(anim, upload))
        return upload

    async def _run(self, anim: Animation, upload: AnimationUpload) -> None:
        """Upload one animation and activate it."""
        try:
            raw = await self._runtime.hass.async_add_executor_job(
                _read_file, anim.path)
            c_hash = content_hash("b", raw)
            meta = m.gif_metadata("b", len(raw))
            activate = b"\xea\x07\x00" + bytes([self._speed])

            async def _upload(_client, conn):
                await conn.upload_content(
                    0x01, meta, raw, activate, c_hash,
                    segment_pace_s=0.025, paced_threshold=8, settle_s=0.5)

            await self._runtime.connection.command(_upload)
            self._catalog.record(self._runtime.address, c_hash, anim.name)
            upload.content_hash = c_hash
            upload.state = STATE_ACTIVE
            self._current = anim.name
            _LOGGER.info(
                "Animation '%s' uploaded to %s (hash=%s)",
                anim.name, self._runtime.address, c_hash.hex())
        except Exception as err:
            upload.state = STATE_ERROR
            upload.error = f"{type(err).__name__}: {err}"
            _LOGGER.exception(
                "Animation upload failed for '%s' on %s",
                anim.name, self._runtime.address)
        finally:
            if self._on_update is not None:
                self._on_update()

    def step(self, delta: int) -> AnimationUpload | None:
        """Move `delta` steps through the catalog and start that upload.

        Returns None when there are no animations to step through.
        """
        names = self.names
        if not names:
            return None
        if self._current in names:
            index = (names.index(self._current) + delta) % len(names)
        else:
            index = 0 if delta >= 0 else len(names) - 1
        return self.start(names[index], self._speed)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def animation_names() -> list[str]:
    """Sorted names of all bundled animations (for YAML generation)."""
    if not os.path.isdir(os.path.join(os.path.dirname(__file__), "animations")):
        return []
    return sorted(list_animations())
