"""GET /api/devices, GET /api/devices/{id}/diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.schemas import ChannelMeta, DeviceDiagnostics, DeviceMeta, from_ms

router = APIRouter(prefix="/api", tags=["devices"])


@router.get("/devices", response_model=list[DeviceMeta])
async def list_devices() -> list[DeviceMeta]:
    """Return every device plus its channels in a single response."""
    async with AsyncSessionLocal() as session:
        devs = (
            await session.execute(
                text(
                    "SELECT id, name, host, port, unit_id, location, enabled "
                    "FROM devices ORDER BY id"
                )
            )
        ).all()
        chs = (
            await session.execute(
                text(
                    "SELECT device_id, channel, name, unit, enabled "
                    "FROM channels ORDER BY device_id, channel"
                )
            )
        ).all()

    by_device: dict[int, list[ChannelMeta]] = {}
    for row in chs:
        by_device.setdefault(row.device_id, []).append(
            ChannelMeta(
                channel=row.channel,
                name=row.name,
                unit=row.unit,
                enabled=bool(row.enabled),
            )
        )

    return [
        DeviceMeta(
            id=d.id,
            name=d.name,
            host=d.host,
            port=d.port,
            unit_id=d.unit_id,
            location=d.location,
            enabled=bool(d.enabled),
            channels=by_device.get(d.id, []),
        )
        for d in devs
    ]


@router.get("/devices/{device_id}/diagnostics", response_model=DeviceDiagnostics)
async def device_diagnostics(device_id: int) -> DeviceDiagnostics:
    """Poll success rate over the last hour + last successful timestamp."""
    async with AsyncSessionLocal() as session:
        exists = await session.execute(
            text("SELECT 1 FROM devices WHERE id = :id"), {"id": device_id}
        )
        if exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="device not found")

        # Success rate over the last hour is computed per-poll-cycle, not
        # per row: for a single (device, ts) all 8 channels either succeeded
        # or all failed. Use DISTINCT ts to count cycles.
        row = await session.execute(
            text(
                """
                SELECT
                  COUNT(DISTINCT ts) AS total,
                  COUNT(DISTINCT CASE WHEN pv_error = 99 THEN ts END) AS failed,
                  MAX(CASE WHEN pv_error != 99 THEN ts END) AS last_ok_ts
                FROM readings
                WHERE device_id = :id
                  AND ts >= (CAST(strftime('%s','now') AS INTEGER) * 1000) - 3600000
                """
            ),
            {"id": device_id},
        )
        r = row.one()

    total = int(r.total or 0)
    failed = int(r.failed or 0)
    success_rate = (1.0 - failed / total) if total else 0.0
    last_seen = from_ms(int(r.last_ok_ts)) if r.last_ok_ts else None

    return DeviceDiagnostics(
        device_id=device_id,
        connected=last_seen is not None,  # refined by the poller state if needed
        last_seen_ts=last_seen,
        total_polls_1h=total,
        failed_polls_1h=failed,
        success_rate_1h=round(success_rate, 4),
    )
