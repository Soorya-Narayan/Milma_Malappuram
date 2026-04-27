"""GET /api/live — most recent reading per (device, channel) + ambient."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.schemas import (
    ChannelSnapshot,
    DeviceSnapshot,
    LiveResponse,
    from_ms,
)

router = APIRouter(prefix="/api", tags=["live"])


# Latest row per (device_id, channel). The earlier ROW_NUMBER() window
# ranked the entire readings table (millions of rows over the 7-day raw
# retention) before filtering — that was adding seconds to Live first paint.
# GROUP BY + JOIN lets SQLite use the idx_readings_dev_ch_ts covering index
# to resolve MAX(ts) per group without scanning row bodies.
_LATEST_READINGS_SQL = text(
    """
    WITH latest AS (
        SELECT device_id, channel, MAX(ts) AS ts
        FROM readings
        GROUP BY device_id, channel
    )
    SELECT r.ts, r.device_id, r.channel, r.pv, r.pv_error, r.alarms
    FROM latest l
    JOIN readings r
      ON r.device_id = l.device_id
     AND r.channel   = l.channel
     AND r.ts        = l.ts
    ORDER BY r.device_id, r.channel
    """
)

_LATEST_AMBIENT_SQL = text(
    """
    WITH latest AS (
        SELECT device_id, MAX(ts) AS ts
        FROM ambient_readings
        GROUP BY device_id
    )
    SELECT a.ts, a.device_id, a.ambient_c
    FROM latest l
    JOIN ambient_readings a
      ON a.device_id = l.device_id
     AND a.ts        = l.ts
    """
)


@router.get("/live", response_model=LiveResponse)
async def live_snapshot() -> LiveResponse:
    async with AsyncSessionLocal() as session:
        ch_rows = (await session.execute(_LATEST_READINGS_SQL)).all()
        amb_rows = (await session.execute(_LATEST_AMBIENT_SQL)).all()

    ambient_by_dev = {r.device_id: (r.ts, r.ambient_c) for r in amb_rows}

    by_dev: dict[int, list[ChannelSnapshot]] = {}
    latest_ts_by_dev: dict[int, int] = {}
    for r in ch_rows:
        by_dev.setdefault(r.device_id, []).append(
            ChannelSnapshot(
                channel=r.channel,
                pv=r.pv,
                pv_error=r.pv_error,
                alarms=r.alarms,
            )
        )
        latest_ts_by_dev[r.device_id] = max(latest_ts_by_dev.get(r.device_id, 0), r.ts)

    devices: list[DeviceSnapshot] = []
    for dev_id, channels in by_dev.items():
        amb_ts, amb_c = ambient_by_dev.get(dev_id, (latest_ts_by_dev[dev_id], None))
        devices.append(
            DeviceSnapshot(
                device_id=dev_id,
                ts=from_ms(max(latest_ts_by_dev[dev_id], amb_ts or 0)),
                ok=True,
                ambient_c=amb_c,
                channels=sorted(channels, key=lambda c: c.channel),
            )
        )

    devices.sort(key=lambda d: d.device_id)
    return LiveResponse(devices=devices)
