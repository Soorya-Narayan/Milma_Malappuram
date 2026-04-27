"""GET /api/history — time-series with auto interval selection + cap.

Interval picking logic is extracted as pure functions at the top of the
module so ``tests/test_history_query.py`` can exercise it without a DB.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.schemas import HistoryPoint, HistoryResponse
from app.schemas import from_ms as ts_to_dt

router = APIRouter(prefix="/api", tags=["history"])


# ---------------------------------------------------------------------------
# Interval selection (pure — tested in isolation)
# ---------------------------------------------------------------------------
_MS_SEC = 1_000
_MS_MIN = 60 * _MS_SEC
_MS_HOUR = 60 * _MS_MIN
_MS_DAY = 24 * _MS_HOUR

POINT_CAP = 5_000
_INTERVALS: tuple[Literal["raw", "1m", "1h"], ...] = ("raw", "1m", "1h")
_BUCKET_MS: dict[str, int] = {"raw": _MS_SEC, "1m": _MS_MIN, "1h": _MS_HOUR}


def auto_interval(range_ms: int) -> Literal["raw", "1m", "1h"]:
    """CLAUDE.md default: ≤2h raw, ≤7d 1m, else 1h."""
    if range_ms <= 2 * _MS_HOUR:
        return "raw"
    if range_ms <= 7 * _MS_DAY:
        return "1m"
    return "1h"


def estimate_points(range_ms: int, interval: str) -> int:
    return max(1, range_ms // _BUCKET_MS[interval])


def pick_effective_interval(
    range_ms: int,
    requested: str,
    cap: int = POINT_CAP,
) -> tuple[Literal["raw", "1m", "1h"], bool]:
    """Resolve requested -> effective interval; escalate until under cap.

    Returns (effective_interval, escalated_from_request).
    """
    chosen = auto_interval(range_ms) if requested == "auto" else requested
    if chosen not in _INTERVALS:
        raise ValueError(f"unknown interval: {chosen}")

    escalated = False
    idx = _INTERVALS.index(chosen)   # type: ignore[arg-type]
    while idx < len(_INTERVALS) - 1 and estimate_points(range_ms, _INTERVALS[idx]) > cap:
        idx += 1
        escalated = True
    return _INTERVALS[idx], escalated


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------
def _build_query(interval: str) -> str:
    if interval == "raw":
        return (
            "SELECT ts AS bucket_ts, pv AS pv_avg, pv AS pv_min, pv AS pv_max, "
            "       1 AS sample_count "
            "FROM readings "
            "WHERE device_id = :device_id AND channel = :channel "
            "  AND ts >= :from_ms AND ts < :to_ms "
            "ORDER BY ts ASC"
        )
    table = "readings_1min" if interval == "1m" else "readings_1h"
    return (
        f"SELECT bucket_ts, pv_avg, pv_min, pv_max, sample_count "
        f"FROM {table} "
        f"WHERE device_id = :device_id AND channel = :channel "
        f"  AND bucket_ts >= :from_ms AND bucket_ts < :to_ms "
        f"ORDER BY bucket_ts ASC"
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.get("/history", response_model=HistoryResponse)
async def history(
    device_id: int = Query(..., gt=0),
    channel: int = Query(..., ge=1, le=8),
    from_: datetime = Query(..., alias="from", description="ISO-8601 start"),
    to: datetime = Query(..., description="ISO-8601 end (exclusive)"),
    interval: Literal["auto", "raw", "1m", "1h"] = "auto",
) -> HistoryResponse:
    if to <= from_:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")

    from_ms = int(from_.timestamp() * 1000)
    to_ms = int(to.timestamp() * 1000)
    range_ms = to_ms - from_ms

    effective, escalated = pick_effective_interval(range_ms, interval)
    sql = _build_query(effective)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(sql),
                {"device_id": device_id, "channel": channel, "from_ms": from_ms, "to_ms": to_ms},
            )
        ).all()

    points = [
        HistoryPoint(
            ts=ts_to_dt(r.bucket_ts),
            pv_avg=r.pv_avg,
            pv_min=r.pv_min,
            pv_max=r.pv_max,
            sample_count=r.sample_count,
        )
        for r in rows
    ]

    return HistoryResponse(
        device_id=device_id,
        channel=channel,
        from_ts=from_,
        to_ts=to,
        requested_interval=interval,
        effective_interval=effective,
        point_cap_exceeded=escalated,
        point_count=len(points),
        points=points,
    )


