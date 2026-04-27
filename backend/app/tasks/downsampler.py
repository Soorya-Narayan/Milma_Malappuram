"""Time-bucketed aggregation + retention jobs.

Four jobs, all driven from scheduler.py:

* ``aggregate_minute()`` — roll completed minutes of ``readings`` into
  ``readings_1min``. Uses ``INSERT OR IGNORE`` so re-runs are harmless.
* ``aggregate_hour()`` — roll completed hours of ``readings_1min`` into
  ``readings_1h``. Then WAL-checkpoints (TRUNCATE) to cap WAL growth.
* ``apply_retention()`` — delete raw rows older than ``raw_days``,
  1-minute rows older than ``one_min_days``, 1-hour rows older than
  ``one_hour_years``. Runs ``PRAGMA optimize`` after, and ``VACUUM``
  only if reclaimable space exceeds ~25 % of the DB (VACUUM rewrites
  the whole file).
* ``catchup_on_boot()`` — on startup, aggregate any complete minute /
  hour buckets that were missed while the app was down.

All timestamps in the DB are unix epoch **milliseconds** (UTC). Bucket
boundaries are aligned to the floor of the bucket size.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import RetentionConfig

log = logging.getLogger(__name__)

_MS_MIN = 60_000
_MS_HOUR = 60 * _MS_MIN
_MS_DAY = 24 * _MS_HOUR

# Threshold for running VACUUM after retention sweep — CLAUDE.md §
# "Retention policy".
_VACUUM_RECLAIM_PCT = 25.0
_VACUUM_MIN_DELETES = 10_000


def _floor(ts_ms: int, bucket_ms: int) -> int:
    return (ts_ms // bucket_ms) * bucket_ms


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Aggregation queries. Parameterised by the [start, end) range of buckets
# to process, so the same SQL handles routine 1-minute runs and startup
# catchup.
# ---------------------------------------------------------------------------
_SQL_AGG_MINUTE = text(
    """
    INSERT OR IGNORE INTO readings_1min
        (bucket_ts, device_id, channel, pv_avg, pv_min, pv_max, sample_count)
    SELECT
        (ts / :bucket_ms) * :bucket_ms AS bucket_ts,
        device_id,
        channel,
        AVG(pv),
        MIN(pv),
        MAX(pv),
        SUM(CASE WHEN pv IS NOT NULL THEN 1 ELSE 0 END)
    FROM readings
    WHERE ts >= :start_ms AND ts < :end_ms
    GROUP BY bucket_ts, device_id, channel
    HAVING SUM(CASE WHEN pv IS NOT NULL THEN 1 ELSE 0 END) > 0
    """
)

_SQL_AGG_HOUR = text(
    """
    INSERT OR IGNORE INTO readings_1h
        (bucket_ts, device_id, channel, pv_avg, pv_min, pv_max, sample_count)
    SELECT
        (bucket_ts / :bucket_ms) * :bucket_ms AS hour_bucket_ts,
        device_id,
        channel,
        -- Weighted averages aren't worth the complexity here; sample_count is
        -- near-constant (≈60 per minute-bucket) so simple AVG is accurate.
        AVG(pv_avg),
        MIN(pv_min),
        MAX(pv_max),
        SUM(sample_count)
    FROM readings_1min
    WHERE bucket_ts >= :start_ms AND bucket_ts < :end_ms
    GROUP BY hour_bucket_ts, device_id, channel
    HAVING SUM(sample_count) > 0
    """
)


# ---------------------------------------------------------------------------
# Minute aggregation
# ---------------------------------------------------------------------------
async def aggregate_minute(
    session_factory: async_sessionmaker,
    *,
    now_ms: int | None = None,
    start_ms: int | None = None,
) -> int:
    """Aggregate all complete minute buckets in ``[start_ms, end_ms)``.

    * ``end_ms`` is derived as ``floor(now_ms, minute)`` — the current,
      incomplete minute is excluded.
    * If ``start_ms`` is None, only the single previous minute is processed.
    * Returns the number of rows inserted into ``readings_1min``.
    """
    now = now_ms if now_ms is not None else _now_ms()
    end_ms = _floor(now, _MS_MIN)
    begin_ms = start_ms if start_ms is not None else end_ms - _MS_MIN

    if begin_ms >= end_ms:
        return 0

    async with session_factory() as session:
        result = await session.execute(
            _SQL_AGG_MINUTE,
            {"bucket_ms": _MS_MIN, "start_ms": begin_ms, "end_ms": end_ms},
        )
        await session.commit()
        inserted = result.rowcount or 0

    log.debug(
        "aggregate_minute: start=%s end=%s inserted=%d",
        _iso(begin_ms), _iso(end_ms), inserted,
    )
    return inserted


# ---------------------------------------------------------------------------
# Hour aggregation
# ---------------------------------------------------------------------------
async def aggregate_hour(
    session_factory: async_sessionmaker,
    engine: AsyncEngine,
    *,
    now_ms: int | None = None,
    start_ms: int | None = None,
) -> int:
    """Roll completed hours from ``readings_1min`` into ``readings_1h``.

    After aggregation, runs ``PRAGMA wal_checkpoint(TRUNCATE)`` on a
    dedicated AUTOCOMMIT connection so the WAL file doesn't grow
    unbounded (CLAUDE.md § SQLite tuning).
    """
    now = now_ms if now_ms is not None else _now_ms()
    end_ms = _floor(now, _MS_HOUR)
    begin_ms = start_ms if start_ms is not None else end_ms - _MS_HOUR

    inserted = 0
    if begin_ms < end_ms:
        async with session_factory() as session:
            result = await session.execute(
                _SQL_AGG_HOUR,
                {"bucket_ms": _MS_HOUR, "start_ms": begin_ms, "end_ms": end_ms},
            )
            await session.commit()
            inserted = result.rowcount or 0

    # Checkpoint must run outside a transaction.
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE);")

    log.info(
        "aggregate_hour: start=%s end=%s inserted=%d; WAL checkpointed",
        _iso(begin_ms), _iso(end_ms), inserted,
    )
    return inserted


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------
async def apply_retention(
    session_factory: async_sessionmaker,
    engine: AsyncEngine,
    retention: RetentionConfig,
    *,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Delete rows older than the retention window per tier.

    Runs ``PRAGMA optimize`` afterwards. Conditionally runs ``VACUUM``
    only when a significant number of rows were deleted *and* the
    freelist/page_count ratio suggests > _VACUUM_RECLAIM_PCT is
    reclaimable — VACUUM rewrites the entire DB file and is expensive
    on an SBC.
    """
    now = now_ms if now_ms is not None else _now_ms()

    cut_raw = now - retention.raw_days * _MS_DAY
    cut_1m = now - retention.one_min_days * _MS_DAY
    cut_1h = now - retention.one_hour_years * 365 * _MS_DAY

    async with session_factory() as session:
        r1 = await session.execute(
            text("DELETE FROM readings WHERE ts < :cut"), {"cut": cut_raw}
        )
        r2 = await session.execute(
            text("DELETE FROM ambient_readings WHERE ts < :cut"), {"cut": cut_raw}
        )
        r3 = await session.execute(
            text("DELETE FROM readings_1min WHERE bucket_ts < :cut"), {"cut": cut_1m}
        )
        r4 = await session.execute(
            text("DELETE FROM readings_1h WHERE bucket_ts < :cut"), {"cut": cut_1h}
        )
        await session.commit()

    deleted = {
        "readings": r1.rowcount or 0,
        "ambient_readings": r2.rowcount or 0,
        "readings_1min": r3.rowcount or 0,
        "readings_1h": r4.rowcount or 0,
    }
    total_deleted = sum(deleted.values())
    log.info("retention sweep: deleted=%s", deleted)

    # Always run `PRAGMA optimize` — cheap, and it keeps the query
    # planner's stats fresh.
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("PRAGMA optimize;")

        if total_deleted >= _VACUUM_MIN_DELETES:
            # Decide whether to VACUUM. SQLite reports reclaimable pages
            # as `freelist_count`; total pages as `page_count`.
            fc = (await conn.exec_driver_sql("PRAGMA freelist_count;")).scalar_one()
            pc = (await conn.exec_driver_sql("PRAGMA page_count;")).scalar_one()
            reclaim_pct = (fc / pc * 100.0) if pc else 0.0

            if reclaim_pct >= _VACUUM_RECLAIM_PCT:
                log.info("VACUUM running (reclaim=%.1f%%)", reclaim_pct)
                await conn.exec_driver_sql("VACUUM;")
            else:
                log.info("skipping VACUUM (reclaim=%.1f%% below %.0f%%)",
                         reclaim_pct, _VACUUM_RECLAIM_PCT)

    return deleted


# ---------------------------------------------------------------------------
# Boot catchup — fills the gap between last aggregated bucket and "now"
# ---------------------------------------------------------------------------
async def catchup_on_boot(
    session_factory: async_sessionmaker,
    engine: AsyncEngine,
    *,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Aggregate any minute/hour buckets missed while the app was down."""
    now = now_ms if now_ms is not None else _now_ms()
    end_min = _floor(now, _MS_MIN)
    end_hr = _floor(now, _MS_HOUR)

    async with session_factory() as session:
        last_min_row = await session.execute(
            text("SELECT MAX(bucket_ts) FROM readings_1min")
        )
        last_min = last_min_row.scalar_one()

        last_hr_row = await session.execute(
            text("SELECT MAX(bucket_ts) FROM readings_1h")
        )
        last_hr = last_hr_row.scalar_one()

        # If no aggregates exist yet, fall back to the earliest raw row so
        # we don't try to aggregate the entire history in one pass.
        first_raw_row = await session.execute(text("SELECT MIN(ts) FROM readings"))
        first_raw = first_raw_row.scalar_one()

    min_start = (last_min + _MS_MIN) if last_min is not None else (
        _floor(first_raw, _MS_MIN) if first_raw is not None else end_min
    )
    hr_start = (last_hr + _MS_HOUR) if last_hr is not None else (
        _floor(first_raw, _MS_HOUR) if first_raw is not None else end_hr
    )

    min_inserted = await aggregate_minute(
        session_factory, now_ms=now, start_ms=min_start
    ) if min_start < end_min else 0

    hr_inserted = await aggregate_hour(
        session_factory, engine, now_ms=now, start_ms=hr_start
    ) if hr_start < end_hr else 0

    log.info(
        "boot catchup: minute_inserted=%d hour_inserted=%d",
        min_inserted, hr_inserted,
    )
    return {"minute": min_inserted, "hour": hr_inserted}


# ---------------------------------------------------------------------------
# Shared purge (used by the manual Settings → "Clear old data" endpoint
# and by the daily auto-purge scheduler job)
# ---------------------------------------------------------------------------
async def purge_older_than(
    session_factory: async_sessionmaker,
    days: int,
) -> tuple[int, dict[str, int]]:
    """Delete every row older than `days` days from all time-series tables.

    Runs all deletes in one transaction so a partial failure leaves the
    DB untouched. Returns (cutoff_ms, deleted_per_table).
    """
    from datetime import timedelta

    cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=days)
    cutoff_ms = int(cutoff_dt.timestamp() * 1000)

    deleted: dict[str, int] = {}
    async with session_factory() as session:
        for table, col in (
            ("readings", "ts"),
            ("ambient_readings", "ts"),
            ("alarm_acks", "ts"),
            ("readings_1min", "bucket_ts"),
            ("readings_1h", "bucket_ts"),
        ):
            r = await session.execute(
                text(f"DELETE FROM {table} WHERE {col} < :cutoff"),
                {"cutoff": cutoff_ms},
            )
            deleted[table] = int(r.rowcount or 0)
        await session.commit()

    # Best-effort housekeeping — don't fail the purge if this blips.
    async with session_factory() as session:
        try:
            await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
            await session.execute(text("PRAGMA optimize;"))
        except Exception:   # noqa: BLE001
            log.exception("post-purge housekeeping failed")

    return cutoff_ms, deleted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
