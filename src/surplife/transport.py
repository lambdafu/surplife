"""Bleak transport for SurplifeConnection (CLI / standalone use).

Implements the transport interface from surplife_core.connection:
  - write(packet): GATT write without response
  - feed_notification hook for the notify characteristic
"""

from __future__ import annotations

import logging

from bleak import BleakClient

from surplife_core.protocol import WRITE_UUID

log = logging.getLogger(__name__)


class BleakTransport:
    """Adapter between bleak and the SurplifeConnection transport interface."""

    def __init__(self, client: BleakClient,
                 notify_uuid: str = "0000ff02-0000-1000-8000-00805f9b34fb",
                 write_uuid: str = WRITE_UUID):
        self._client = client
        self._notify_uuid = notify_uuid
        self._write_uuid = write_uuid
        self._sink = None  # callable set by the connection

    @property
    def client(self) -> BleakClient:
        return self._client

    def set_sink(self, sink) -> None:
        """Set the notification sink: callable(raw: bytes) -> None."""
        self._sink = sink

    async def start(self) -> None:
        async def _on_notify(_sender: int, data: bytearray) -> None:
            if self._sink:
                self._sink(bytes(data))
        await self._client.start_notify(self._notify_uuid, _on_notify)

    async def stop(self) -> None:
        try:
            await self._client.stop_notify(self._notify_uuid)
        except Exception:
            pass

    async def write(self, packet: bytes) -> None:
        log.debug(">> %s", packet.hex())
        await self._client.write_gatt_char(self._write_uuid, packet,
                                           response=False)


def wrap_packet_with_seq(inner: bytes, seq: int) -> bytes:
    """Backwards-compatible wrapper for sequence-framed packets."""
    from surplife_core.messages import wrap_packet
    return wrap_packet(inner, seq)
