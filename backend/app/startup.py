"""Boot-time helpers that run inside the FastAPI lifespan.

Currently: upsert devices.yaml into the `devices` and `channels` tables.
Devices that disappear from the YAML are left in the DB (historical data
still references them). A device is marked `enabled=0` if its YAML entry
sets `enabled: false`, which the poller respects. Channels removed from
YAML are pruned — they are metadata only, not historical.
"""

from __future__ import annotations

import logging

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DevicesFile

log = logging.getLogger(__name__)


_UPSERT_DEVICE = text(
    """
    INSERT INTO devices (id, name, host, port, unit_id, location, enabled)
    VALUES (:id, :name, :host, :port, :unit_id, :location, :enabled)
    ON CONFLICT(id) DO UPDATE SET
        name     = excluded.name,
        host     = excluded.host,
        port     = excluded.port,
        unit_id  = excluded.unit_id,
        location = excluded.location,
        enabled  = excluded.enabled
    """
)

_UPSERT_CHANNEL = text(
    """
    INSERT INTO channels (device_id, channel, name, unit)
    VALUES (:device_id, :channel, :name, :unit)
    ON CONFLICT(device_id, channel) DO NOTHING
    """
)

_PRUNE_CHANNELS = text(
    "DELETE FROM channels WHERE device_id = :device_id AND channel NOT IN :keep"
).bindparams(bindparam("keep", expanding=True))


async def sync_devices_from_yaml(
    session: AsyncSession,
    devices_file: DevicesFile,
) -> None:
    """Upsert every device + its channels. Prune channels that disappeared."""
    n_devices = 0
    n_channels = 0

    for dev in devices_file.devices:
        await session.execute(
            _UPSERT_DEVICE,
            {
                "id": dev.id,
                "name": dev.name,
                "host": dev.host,
                "port": dev.port,
                "unit_id": dev.unit_id,
                "location": dev.location,
                "enabled": 1 if dev.enabled else 0,
            },
        )
        n_devices += 1

        kept_channels = [ch.channel for ch in dev.channels]
        for ch in dev.channels:
            await session.execute(
                _UPSERT_CHANNEL,
                {
                    "device_id": dev.id,
                    "channel": ch.channel,
                    "name": ch.name,
                    "unit": ch.unit,
                },
            )
            n_channels += 1

        if kept_channels:
            await session.execute(
                _PRUNE_CHANNELS,
                {"device_id": dev.id, "keep": kept_channels},
            )

    await session.commit()
    log.info("devices.yaml synced: %d devices, %d channels", n_devices, n_channels)
