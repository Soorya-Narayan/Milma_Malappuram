"""Tiny in-process scheduler (no APScheduler).

One asyncio task that sleeps to the next minute boundary in local time,
runs the minute aggregator, and fires the hourly + daily jobs when a
new hour / day begins.

Why local time? The retention job fires "at 03:00" per CLAUDE.md,
which is operator-local (Asia/Kolkata). Minute/hour bucket *contents*
are always aligned on UTC ms boundaries in the DB — only the trigger
cadence is local.

Started from the FastAPI lifespan after pollers are running. On
shutdown, cancel the returned Task and `await asyncio.gather` it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import RetentionConfig
from app.runtime_settings import PollerRuntime
from app.tasks.downsampler import (
    aggregate_hour,
    aggregate_minute,
    apply_retention,
    catchup_on_boot,
    purge_older_than,
)

log = logging.getLogger(__name__)

_RETENTION_HOUR_LOCAL = 3  # 03:00 local time
# 6 months ≈ 180 days — fired once per day at _RETENTION_HOUR_LOCAL when
# the operator has enabled "Auto-delete old data" in Settings.
_AUTO_PURGE_DAYS = 180

# Safety margin past the minute boundary so all poll writes from the
# previous minute have committed before we try to aggregate them.
_MINUTE_DELAY_S = 2.0


async def _sleep_until_next_minute() -> None:
    """Sleep until the next wall-clock minute boundary (+ small delay)."""
    now = datetime.now().astimezone()
    seconds_into_minute = now.second + now.microsecond / 1_000_000
    sleep_s = 60.0 - seconds_into_minute + _MINUTE_DELAY_S
    if sleep_s > 0:
        await asyncio.sleep(sleep_s)


async def _scheduler_loop(
    session_factory: async_sessionmaker,
    engine: AsyncEngine,
    retention: RetentionConfig,
    runtime: PollerRuntime,
) -> None:
    # Boot catchup runs once before the periodic loop starts.
    try:
        await catchup_on_boot(session_factory, engine)
    except Exception:   # noqa: BLE001
        log.exception("boot catchup failed")

    # Track the last hour/day we fired so we only fire once per boundary.
    last_hour: int | None = None
    last_day: int | None = None

    while True:
        await _sleep_until_next_minute()

        now = datetime.now().astimezone()

        try:
            await aggregate_minute(session_factory)
        except Exception:   # noqa: BLE001
            log.exception("aggregate_minute failed")

        # Hour roll-over: fire when we observe a new hour.
        current_hour_key = (now.year, now.month, now.day, now.hour)
        current_hour_hash = hash(current_hour_key)
        if last_hour is None:
            last_hour = current_hour_hash
        elif current_hour_hash != last_hour:
            last_hour = current_hour_hash
            try:
                await aggregate_hour(session_factory, engine)
            except Exception:   # noqa: BLE001
                log.exception("aggregate_hour failed")

        # Daily retention at _RETENTION_HOUR_LOCAL (local clock).
        current_day_key = (now.year, now.month, now.day)
        current_day_hash = hash(current_day_key)
        if (
            now.hour == _RETENTION_HOUR_LOCAL
            and (last_day is None or last_day != current_day_hash)
        ):
            last_day = current_day_hash
            try:
                await apply_retention(session_factory, engine, retention)
            except Exception:   # noqa: BLE001
                log.exception("apply_retention failed")

            # Operator-controlled auto-purge: if enabled in Settings,
            # drop everything older than 6 months at the same daily
            # 03:00 tick. Keeps the DB from growing without bound.
            if runtime.auto_purge_enabled:
                try:
                    cutoff_ms, deleted = await purge_older_than(
                        session_factory, _AUTO_PURGE_DAYS
                    )
                    total = sum(deleted.values())
                    log.info(
                        "auto_purge: deleted %d rows older than 6 months "
                        "(cutoff_ms=%d, per_table=%s)",
                        total, cutoff_ms, deleted,
                    )
                except Exception:   # noqa: BLE001
                    log.exception("auto_purge failed")


def start_scheduler(
    session_factory: async_sessionmaker,
    engine: AsyncEngine,
    retention: RetentionConfig,
    runtime: PollerRuntime,
) -> asyncio.Task:
    """Spawn the scheduler task. Returns the Task for lifespan shutdown."""
    task = asyncio.create_task(
        _scheduler_loop(session_factory, engine, retention, runtime),
        name="scheduler",
    )
    log.info("scheduler started (retention run at %02d:00 local)", _RETENTION_HOUR_LOCAL)
    return task


async def stop_scheduler(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:   # noqa: BLE001
        log.exception("scheduler task ended with error")
    log.info("scheduler stopped")
