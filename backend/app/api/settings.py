"""Runtime settings + maintenance actions for the Settings page.

Endpoints:
  GET  /api/settings/log-interval
  POST /api/settings/log-interval     body: {"seconds": <float>}
  GET  /api/settings/auto-purge
  POST /api/settings/auto-purge       body: {"enabled": <bool>}
  POST /api/maintenance/purge-old     deletes rows older than 6 months
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import AsyncSessionLocal
from app.runtime_settings import (
    ALLOWED_LOG_INTERVALS_S,
    save_auto_purge,
    save_log_interval,
)
from app.tasks.downsampler import purge_older_than

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])

# 6 months ≈ 180 days. Fixed; not configurable from the UI.
PURGE_OLDER_THAN_DAYS = 180


# ---------------------------------------------------------------------------
# Log interval
# ---------------------------------------------------------------------------
class LogIntervalResponse(BaseModel):
    seconds: float
    allowed: list[float]


class LogIntervalRequest(BaseModel):
    seconds: float = Field(gt=0)


@router.get("/settings/log-interval", response_model=LogIntervalResponse)
async def get_log_interval(request: Request) -> LogIntervalResponse:
    runtime = request.app.state.poller_runtime
    return LogIntervalResponse(
        seconds=runtime.interval_s,
        allowed=list(ALLOWED_LOG_INTERVALS_S),
    )


@router.post("/settings/log-interval", response_model=LogIntervalResponse)
async def set_log_interval(
    req: LogIntervalRequest, request: Request
) -> LogIntervalResponse:
    if req.seconds not in ALLOWED_LOG_INTERVALS_S:
        raise HTTPException(
            status_code=400,
            detail=(
                f"seconds must be one of {list(ALLOWED_LOG_INTERVALS_S)}"
            ),
        )
    await save_log_interval(AsyncSessionLocal, req.seconds)
    # Mutate the shared holder — pollers pick this up on the next cycle.
    runtime = request.app.state.poller_runtime
    runtime.interval_s = float(req.seconds)
    log.info("log interval updated to %.3fs", req.seconds)
    return LogIntervalResponse(
        seconds=runtime.interval_s,
        allowed=list(ALLOWED_LOG_INTERVALS_S),
    )


# ---------------------------------------------------------------------------
# Purge data older than 6 months
# ---------------------------------------------------------------------------
class PurgeResponse(BaseModel):
    cutoff_ts: int
    cutoff_iso: str
    deleted: dict[str, int]


@router.post("/maintenance/purge-old", response_model=PurgeResponse)
async def purge_old_data() -> PurgeResponse:
    """Delete rows older than 6 months from every time-series table."""
    cutoff_ms, deleted = await purge_older_than(
        AsyncSessionLocal, PURGE_OLDER_THAN_DAYS
    )
    cutoff_iso = datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).isoformat()
    total = sum(deleted.values())
    log.info(
        "purge_old_data: deleted %d rows older than %s (6 months)",
        total, cutoff_iso,
    )
    return PurgeResponse(
        cutoff_ts=cutoff_ms,
        cutoff_iso=cutoff_iso,
        deleted=deleted,
    )


# ---------------------------------------------------------------------------
# Auto-purge toggle (scheduler fires the daily job when this is enabled)
# ---------------------------------------------------------------------------
class AutoPurgeResponse(BaseModel):
    enabled: bool
    retention_days: int = PURGE_OLDER_THAN_DAYS


class AutoPurgeRequest(BaseModel):
    enabled: bool


@router.get("/settings/auto-purge", response_model=AutoPurgeResponse)
async def get_auto_purge(request: Request) -> AutoPurgeResponse:
    runtime = request.app.state.poller_runtime
    return AutoPurgeResponse(enabled=bool(runtime.auto_purge_enabled))


@router.post("/settings/auto-purge", response_model=AutoPurgeResponse)
async def set_auto_purge(
    req: AutoPurgeRequest, request: Request
) -> AutoPurgeResponse:
    await save_auto_purge(AsyncSessionLocal, req.enabled)
    # Mutate the shared holder so the scheduler picks it up at the next
    # 03:00 tick without a restart.
    runtime = request.app.state.poller_runtime
    runtime.auto_purge_enabled = bool(req.enabled)
    log.info("auto_purge_enabled set to %s", runtime.auto_purge_enabled)
    return AutoPurgeResponse(enabled=runtime.auto_purge_enabled)
