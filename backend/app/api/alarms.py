"""Alarm acknowledgement — dashboard-side only.

Per CLAUDE.md Phase 7: "does NOT silence on the device, just on the
dashboard." We record one row per ack with the set of currently-active
alarm bits at the moment of acknowledgement, so operators can audit
who dismissed what and when.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db import AsyncSessionLocal

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


class AckRequest(BaseModel):
    device_id: int
    channel: int | None = None   # None = all channels on the device
    operator: str | None = None


class AckResponse(BaseModel):
    acked: int
    ts: datetime


@router.post("/ack", response_model=AckResponse)
async def ack(req: AckRequest) -> AckResponse:
    ts_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    async with AsyncSessionLocal() as session:
        exists = await session.execute(
            text("SELECT 1 FROM devices WHERE id = :d"), {"d": req.device_id}
        )
        if exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="device not found")

        # Grab the most recent alarms bitfield per (device, channel) — the
        # snapshot the operator saw on screen.
        params: dict[str, Any] = {"d": req.device_id}
        where_ch = ""
        if req.channel is not None:
            params["c"] = req.channel
            where_ch = " AND channel = :c"
        latest = (
            await session.execute(
                text(
                    f"""
                    WITH latest AS (
                        SELECT ts, device_id, channel, alarms,
                               ROW_NUMBER() OVER (PARTITION BY device_id, channel
                                                  ORDER BY ts DESC) AS rn
                        FROM readings WHERE device_id = :d{where_ch}
                    )
                    SELECT channel, alarms FROM latest WHERE rn = 1 AND alarms != 0
                    """
                ),
                params,
            )
        ).all()

        if not latest:
            return AckResponse(acked=0, ts=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc))

        await session.execute(
            text(
                "INSERT INTO alarm_acks (ts, device_id, channel, alarms_mask, operator) "
                "VALUES (:ts, :d, :c, :m, :op)"
            ),
            [
                {"ts": ts_ms, "d": req.device_id, "c": r.channel,
                 "m": r.alarms, "op": req.operator}
                for r in latest
            ],
        )
        await session.commit()

    return AckResponse(
        acked=len(latest),
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
    )


@router.get("/recent")
async def recent_acks(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT ts, device_id, channel, alarms_mask, operator "
                    "FROM alarm_acks ORDER BY ts DESC LIMIT :lim"
                ),
                {"lim": limit},
            )
        ).all()
    return [
        {
            "ts": datetime.fromtimestamp(r.ts / 1000, tz=timezone.utc).isoformat(),
            "device_id": r.device_id,
            "channel": r.channel,
            "alarms_mask": r.alarms_mask,
            "operator": r.operator,
        }
        for r in rows
    ]
