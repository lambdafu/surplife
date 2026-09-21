"""Content services for Surplife Matrix.

show_media (path/URL), show_camera (HA camera snapshot), show_text,
show_clock, draw_pixels, playlist ops, set_speed, activate. All commands
run over the runtime's persistent connection (serialized by the command
lock); the display keeps playing after each upload.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .assets import resolve_builtin
from .const import (
    DOMAIN,
    GIF_ACTIVATION_DWELL_S,
    SERVICE_ACTIVATE,
    SERVICE_DRAW_PIXELS,
    SERVICE_PLAYLIST_ADD,
    SERVICE_PLAYLIST_CLEAR,
    SERVICE_PLAYLIST_REMOVE,
    SERVICE_SET_SPEED,
    SERVICE_SHOW_CAMERA,
    SERVICE_SHOW_CLOCK,
    SERVICE_SHOW_MEDIA,
    SERVICE_SHOW_TEXT,
)
from .core import messages as m
from .core.a2pl import compress
from .core.color import image_to_framebuffer, rgb_to_display, rgb_to_meta_color
from .core.connection import _PlaylistEntryView
from .core.content import build_a2pl_payload, content_hash, fix_gif, validate_gif
from .core.fonts import render_text
from .core.media import frame_to_canvas
from .core.protocol import (
    FRAMEBUFFER_SIZE,
    PACED_SEGMENTS_THRESHOLD,
    SEGMENT_PACE_S,
    UPLOAD_SETTLE_S,
)
from .runtime import SurplifeRuntime

_LOGGER = logging.getLogger(__name__)

UPLOAD_KW = {
    "segment_pace_s": SEGMENT_PACE_S,
    "paced_threshold": PACED_SEGMENTS_THRESHOLD,
    "settle_s": UPLOAD_SETTLE_S,
}


def _resolve_runtime(hass: HomeAssistant, entity_id: str) -> SurplifeRuntime:
    """Map an entity_id to its config entry's runtime."""
    entity_map = hass.data[DOMAIN].get("entities", {})
    entry_id = entity_map.get(entity_id)
    if entry_id is None:
        raise HomeAssistantError(
            f"Entity {entity_id} is not a Surplife Matrix device")
    return hass.data[DOMAIN][entry_id]


def _resolve_ha_path(source: str) -> str:
    """Resolve a media path: absolute, /media/local, /config/www, or bare."""
    p = source
    if p.startswith(("/media/", "/config/")):
        return p
    for prefix in ("/media/local", "/config/www", "/config"):
        candidate = f"{prefix}/{p.lstrip('/')}"
        if os.path.exists(candidate):
            return candidate
    raise HomeAssistantError(f"File not found: {p}")


async def _fetch_media(hass: HomeAssistant, source: str) -> bytes:
    """Resolve a media source to raw image/GIF bytes.

    Sources: `builtin:<name>` (animations shipped with the integration),
    /media/local/... paths, http(s) URLs, or data: URIs.
    """
    if source.startswith("builtin:"):
        path = await hass.async_add_executor_job(resolve_builtin, source)

        def _read_builtin() -> bytes:
            with open(path, "rb") as f:
                return f.read()

        return await hass.async_add_executor_job(_read_builtin)
    if source.startswith(("http://", "https://")):
        import aiohttp

        async with aiohttp.ClientSession() as session, session.get(
            source, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            return await resp.read()
    if source.startswith("data:"):
        import base64

        return base64.b64decode(source.split(",", 1)[1])
    path = await hass.async_add_executor_job(_resolve_ha_path, source)

    def _read() -> bytes:
        with open(path, "rb") as f:
            return f.read()

    return await hass.async_add_executor_job(_read)


def _is_gif(data: bytes) -> bool:
    return data[:3] == b"GIF"


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register content services (once, guarded)."""
    if hass.services.has_service(DOMAIN, SERVICE_SHOW_MEDIA):
        return

    def _runtime(entity_id: str) -> SurplifeRuntime:
        entry_id = hass.data[DOMAIN].get("entities", {}).get(entity_id)
        if entry_id is None:
            raise HomeAssistantError(f"Entity {entity_id} is not a "
                                     f"Surplife Matrix device")
        return hass.data[DOMAIN][entry_id]

    # ── show_media ──────────────────────────────────────────────────
    show_media_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("source"): cv.string,
        vol.Optional("media_type", default="auto"): vol.In(
            ["auto", "image", "gif"]),
        vol.Optional("effect", default=1): vol.All(
            vol.Coerce(int), vol.Range(1, 10)),
        vol.Optional("speed", default=50): vol.All(
            vol.Coerce(int), vol.Range(1, 100)),
        vol.Optional("graffiti", default=False): cv.boolean,
        vol.Optional("add_to_playlist"): vol.All(
            vol.Coerce(int), vol.Range(1, 255)),
    })

    async def async_show_media(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        raw = await _fetch_media(hass, call.data["source"])
        media_type = call.data["media_type"]
        graffiti = call.data["graffiti"]
        effect = call.data["effect"]
        speed = call.data["speed"]

        if media_type == "gif" or (media_type == "auto"
                                   and _is_gif(raw)):
            c_hash = await _upload_gif(runtime, raw, graffiti, speed)
        else:
            c_hash = await _upload_image(runtime, raw, graffiti,
                                         effect, speed)

        duration = call.data.get("add_to_playlist")
        if duration:
            await runtime.connection.command(
                lambda _c, conn: _playlist_add_conn(conn, c_hash, duration))

    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_MEDIA, async_show_media,
        schema=show_media_schema)

    # ── show_camera ─────────────────────────────────────────────────
    show_camera_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("camera_entity"): cv.entity_id,
        vol.Optional("graffiti", default=False): cv.boolean,
    })

    async def async_show_camera(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        camera_entity = call.data["camera_entity"]

        # Fetch a JPEG snapshot from the camera entity
        from homeassistant.components.camera import (
            DOMAIN as CAMERA_DOMAIN,
        )

        result = await hass.services.async_call(
            CAMERA_DOMAIN, "snapshot",
            {ATTR_ENTITY_ID: camera_entity},
            blocking=True, return_response=True,
        )
        image = b"".join(result.values())
        _LOGGER.debug("Camera snapshot: %d bytes", len(image))

        # Convert to one full canvas and upload as static graffiti/image
        from PIL import Image

        img = Image.open(io.BytesIO(image))
        fb = frame_to_canvas(img)
        payload = compress(fb)
        meta = m.image_metadata("c" if call.data["graffiti"] else "a",
                                1, len(payload))
        c_type = "c" if call.data["graffiti"] else "a"
        c_hash = content_hash(c_type, payload)
        activate = (b"\xea\x09\x00\x50\x01" if call.data["graffiti"]
                    else b"\xea\x06\x00\x32\x01")
        cache_type = 0x00 if call.data["graffiti"] else 0x04
        await runtime.connection.command(
            lambda _c, conn: conn.upload_content(
                cache_type, meta, payload, activate, c_hash, **UPLOAD_KW))

    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_CAMERA, async_show_camera,
        schema=show_camera_schema)

    # ── show_text ───────────────────────────────────────────────────
    show_text_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("message"): cv.string,
        vol.Optional("colors"): vol.All(
            cv.ensure_list, [cv.template]),
        vol.Optional("speed", default=50): vol.All(
            vol.Coerce(int), vol.Range(1, 100)),
    })

    async def async_show_text(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        colors = []
        for tpl in call.data.get("colors", []):
            colors.append(_template_rgb(hass, tpl))
        c_hash = await _upload_text(runtime, call.data["message"],
                                    colors or None, call.data["speed"])
        _LOGGER.info("show_text: hash=%s", c_hash.hex())

    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_TEXT, async_show_text,
        schema=show_text_schema)

    # ── show_clock ──────────────────────────────────────────────────
    show_clock_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("style", default=0): vol.All(
            vol.Coerce(int), vol.Range(0, 7)),
        vol.Optional("12h", default=False): cv.boolean,
        vol.Optional("date", default=False): cv.boolean,
    })

    async def async_show_clock(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        await runtime.connection.command(
            lambda _c, conn: conn.show_clock(
                call.data["style"], not call.data["12h"],
                call.data["date"]))

    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_CLOCK, async_show_clock,
        schema=show_clock_schema)

    # ── draw_pixels ─────────────────────────────────────────────────
    pixel_schema = vol.Schema({
        vol.Required("x"): vol.All(vol.Coerce(int), vol.Range(0, 95)),
        vol.Required("y"): vol.All(vol.Coerce(int), vol.Range(0, 15)),
        vol.Optional("color"): cv.string,  # hex "ff0000"; absent = erase
    })
    draw_pixels_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("pixels"): vol.All(cv.ensure_list, [pixel_schema]),
    })

    async def async_draw_pixels(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        fb = bytearray(FRAMEBUFFER_SIZE)
        for px in call.data["pixels"]:
            col, row = px["x"], px["y"]
            if not (0 <= col < 96 and 0 <= row < 16):
                raise vol.Invalid(f"Pixel ({col}, {row}) outside 96x16")
            pos = col * 32 + row * 2
            if "color" in px:
                fb[pos], fb[pos + 1] = rgb_to_display(*_hex_rgb(px["color"]))
        stream = compress(bytes(fb))
        await runtime.connection.command(
            lambda _c, conn: conn.send(m.cmd_direct_draw(stream)))

    hass.services.async_register(
        DOMAIN, SERVICE_DRAW_PIXELS, async_draw_pixels,
        schema=draw_pixels_schema)

    # ── playlist_add / playlist_remove / playlist_clear ─────────────
    playlist_add_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("content_hash"): cv.string,
        vol.Optional("duration", default=10): vol.All(
            vol.Coerce(int), vol.Range(1, 255)),
    })

    async def async_playlist_add(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        c_hash = _parse_hash(call.data["content_hash"])
        await runtime.connection.command(
            lambda _c, conn: _playlist_add_conn(
                conn, c_hash, call.data["duration"]))

    hass.services.async_register(
        DOMAIN, SERVICE_PLAYLIST_ADD, async_playlist_add,
        schema=playlist_add_schema)

    playlist_rm_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("content_hash"): cv.string,
    })

    async def async_playlist_remove(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        c_hash = _parse_hash(call.data["content_hash"])
        async def _run(_client, conn):
            flags, _details = await conn.get_playlist_raw()
            entries = [
                _PlaylistEntryView(flag=flag, hash=bytes(h))
                for flag, h in flags
                if bytes(h) != c_hash
            ]
            await conn.set_playlist_raw(entries, editing=True)
            if entries:
                for i, e in enumerate(entries):
                    e.index = i + 1
                    if e.duration <= 0:
                        e.duration = 10
                await conn.set_playlist_details_raw(entries)
        await runtime.connection.command(_run)

    hass.services.async_register(
        DOMAIN, SERVICE_PLAYLIST_REMOVE, async_playlist_remove,
        schema=playlist_rm_schema)

    async def async_playlist_clear(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        await runtime.connection.command(
            lambda _c, conn: conn.set_playlist_raw([], editing=True))

    hass.services.async_register(
        DOMAIN, SERVICE_PLAYLIST_CLEAR, async_playlist_clear,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}))

    # ── set_speed ───────────────────────────────────────────────────
    set_speed_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("speed"): vol.All(vol.Coerce(int), vol.Range(1, 100)),
    })

    async def async_set_speed(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        await runtime.connection.command(
            lambda _c, conn: conn.set_speed(call.data["speed"]))

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SPEED, async_set_speed,
        schema=set_speed_schema)

    # ── activate ─────────────────────────────────────────────────────
    # Re-display previously uploaded content by hash. A cache hit needs
    # no upload at all — the instant building block for scene automations.
    activate_schema = vol.Schema({
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("content_hash"): cv.string,
        vol.Optional("speed"): vol.All(vol.Coerce(int), vol.Range(1, 100)),
        vol.Optional("effect"): vol.All(vol.Coerce(int), vol.Range(1, 10)),
    })

    async def async_activate(call: ServiceCall) -> None:
        runtime = _runtime(call.data[ATTR_ENTITY_ID])
        c_hash = _parse_hash(call.data["content_hash"])
        speed = call.data.get("speed")
        effect = call.data.get("effect")

        async def _run(_client, conn):
            # The device's cache check (ea 05) ignores the type byte — only
            # the 16-byte hash matters (verified live). So ONE probe tells
            # us whether the content is cached; the activation command
            # then comes from the client-side type map (recorded by this
            # integration's uploads) or, for foreign content (phone app),
            # the documented per-type activation attempts.
            resp = await conn.send_and_wait(
                m.cmd_cache_check(0x04, c_hash), m.match_cache_check)
            if not (len(resp) > 3 and resp[3] == 0x01):
                raise HomeAssistantError(
                    f"Content {c_hash.hex()} is not in the device cache — "
                    "upload it first (show_media).")

            stored_type = _CONTENT_TYPES.get((runtime.address, c_hash))
            if stored_type:
                activate = _ACTIVATE_FOR_TYPE[stored_type](speed, effect)
                await conn.send(activate)
                return
            # Unknown type (uploaded via the app): try activations from
            # safest to most side-effect-prone.
            for type_letter in _ACTIVATE_FALLBACK_ORDER:
                await conn.send(_ACTIVATE_FOR_TYPE[type_letter](speed, effect))
                break   # single attempt: one safe command per unknown content

        await runtime.connection.command(_run)

    hass.services.async_register(
        DOMAIN, SERVICE_ACTIVATE, async_activate,
        schema=activate_schema)


# ── Upload helpers ───────────────────────────────────────────────────

def _parse_hash(text: str) -> bytes:
    try:
        h = bytes.fromhex(text)
    except ValueError:
        raise vol.Invalid("content_hash must be hex")
    if len(h) != 16:
        raise vol.Invalid("content_hash must be 32 hex digits")
    return h


def _hex_rgb(text: str) -> tuple[int, int, int]:
    s = text.lstrip("#")
    if len(s) != 6:
        raise vol.Invalid("color must be 6 hex digits")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _template_rgb(hass, tpl) -> tuple[int, int, int]:
    from homeassistant.helpers import template as tpl_mod

    if isinstance(tpl, str) and len(tpl) == 6 and tpl.isalnum():
        return _hex_rgb(tpl)
    rendered = tpl_mod.Template(tpl, hass).async_render(parse_result=False)
    return _hex_rgb(str(rendered))


async def _upload_gif(runtime: SurplifeRuntime, raw: bytes, graffiti: bool,
                      speed: int, dwell: float = GIF_ACTIVATION_DWELL_S) -> bytes:
    report = validate_gif(raw, strict=False)
    if not report["ok"]:
        _LOGGER.info("Auto-fixing unsafe GIF: %s", report["violations"])
        raw = fix_gif(raw)
    if graffiti:
        meta = m.gif_metadata("d", len(raw))
        c_hash = content_hash("d", raw)
        await _do_upload(runtime, 0x02, meta, raw,
                         b"\xea\x0a\x00\x50\x01", c_hash)
        _CONTENT_TYPES[(runtime.address, c_hash)] = "d"
    else:
        meta = m.gif_metadata("b", len(raw))
        c_hash = content_hash("b", raw)
        await _do_upload(runtime, 0x01, meta, raw,
                         b"\xea\x07\x00" + bytes([speed]), c_hash)
        _CONTENT_TYPES[(runtime.address, c_hash)] = "b"
    # Verified-safe pattern from live testing: let the GIF's first frames
    # play before further traffic on the connection (the device is busy
    # starting playback right after activation).
    if dwell > 0:
        await asyncio.sleep(dwell)
    return c_hash


async def _upload_image(runtime: SurplifeRuntime, raw: bytes,
                        graffiti: bool, effect: int, speed: int) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    fb, num_cols = image_to_framebuffer(img)
    payload, frame_num, _ = build_a2pl_payload(fb, num_cols)
    c_type = "c" if graffiti else "a"
    meta = m.image_metadata(c_type, frame_num, len(payload))
    c_hash = content_hash(c_type, payload)
    if graffiti:
        await _do_upload(runtime, 0x00, meta, payload,
                         b"\xea\x09\x00\x50\x01", c_hash)
    else:
        await _do_upload(runtime, 0x04, meta, payload,
                         bytes([0xea, 0x06, 0x00, speed, effect]), c_hash)
    _CONTENT_TYPES[(runtime.address, c_hash)] = c_type
    return c_hash


async def _do_upload(runtime: SurplifeRuntime, cache_type: int, meta: bytes,
                     payload: bytes, activate: bytes, c_hash: bytes) -> None:
    await runtime.connection.command(
        lambda _c, conn: conn.upload_content(cache_type, meta, payload,
                                             activate, c_hash, **UPLOAD_KW))


async def _playlist_add_conn(conn, c_hash: bytes, duration: int) -> None:
    flags, details = await conn.get_playlist_raw()
    entries = []
    for flag, h in flags:
        entries.append(_PlaylistEntryView(flag=flag, hash=bytes(h)))
    for index, _f1, dur, h in details:
        for e in entries:
            if e.hash == bytes(h):
                e.index, e.duration = index, dur
    if any(e.hash == c_hash for e in entries):
        return
    max_index = max((e.index for e in entries), default=0) + 1
    entries.append(_PlaylistEntryView(flag=0x01, hash=c_hash,
                                      index=max_index, duration=duration))
    await conn.set_playlist_raw(entries, editing=False)
    await conn.set_playlist_details_raw(entries)

async def _upload_text(runtime: SurplifeRuntime, message: str,
                       colors: list | None, speed: int) -> bytes:
    """Render scrolling text and upload it (type "e")."""
    if colors is None:
        colors = [(255, 0, 0)]
    fb, num_cols = render_text(message, fg=colors[0], font_size=16)
    payload, frame_num, last_frame_cols = build_a2pl_payload(fb, num_cols)

    fg_colors = [rgb_to_meta_color(r, g, b) for r, g, b in colors]
    fg_attr = 0 if len(colors) > 1 else 1
    attr = {
        "speed": speed,
        "effect": "b" if frame_num > 1 else "a",
        "pause_t": 1,
        "bg_color": "000000",
        "fg_color": fg_colors,
        "fg_attr": fg_attr,
        "fg_dir": 1,
    }
    if frame_num > 1:
        attr["last_word_frame"] = last_frame_cols
    meta = m.text_metadata("e", frame_num, len(payload), attr)
    c_hash = content_hash("e", payload, attr)
    await _do_upload(runtime, 0x02, meta, payload, b"\xea\x24", c_hash)
    _CONTENT_TYPES[(runtime.address, c_hash)] = "e"
    return c_hash


# Activation commands per device content type (speed/effect only apply to
# types "a" and "b"). Keys: the type letter used in content_hash().
_ACTIVATE_FOR_TYPE = {
    "a": lambda speed, effect: bytes(
        [0xEA, 0x06, 0x01, speed or 50, effect or 1]),
    "b": lambda speed, effect: bytes(
        [0xEA, 0x07, 0x00, speed or 50]),
    "c": lambda speed, effect: b"\xEA\x09\x00\x50\x01",
    "d": lambda speed, effect: b"\xEA\x0a\x00\x50\x01",
    "e": lambda speed, effect: b"\xEA\x24",
}

# Fallback attempt order when the type isn't in the client-side map (content
# uploaded by the phone app): safest activation first. ea 24 (text) first —
# it is a no-op on GIF content; ea 0a last — it has documented side effects
# on text.
_ACTIVATE_FALLBACK_ORDER = ["e", "d", "a", "b", "c"]
_ACTIVATE_FALLBACK = {k: _ACTIVATE_FOR_TYPE[k] for k in _ACTIVATE_FALLBACK_ORDER}

# Client-side record of uploaded content types: (address, hash) -> type letter.
_CONTENT_TYPES: dict[tuple[str, bytes], str] = {}
