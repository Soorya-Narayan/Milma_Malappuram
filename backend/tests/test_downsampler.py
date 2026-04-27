"""Integration tests for app.tasks.downsampler.aggregate_minute.

Uses an in-memory-like SQLite file per test (tmp_path fixture) so we can
exercise the full async aiosqlite + SQLAlchemy pipeline without the real
/data volume.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.tasks.downsampler import aggregate_minute

pytestmark = pytest.mark.asyncio

_MS_MIN = 60_000
_MS_SEC = 1_000
_SCHEMA_SQL = Path(__file__).parent.parent / "app" / "schema.sql"


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "aime.db"
    # Apply schema directly with aiosqlite — same path init_db() uses.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_SCHEMA_SQL.read_text(encoding="utf-8"))
        await conn.commit()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # One device / channel row so FK-style querying behaves.
    async with factory() as s:
        await s.execute(
            _raw("INSERT INTO devices (id, name, host) VALUES (1, 'test', 'x')")
        )
        await s.execute(
            _raw("INSERT INTO channels (device_id, channel, name) VALUES (1, 1, 'ch1')")
        )
        await s.commit()

    yield factory
    await engine.dispose()


def _raw(sql):
    from sqlalchemy import text
    return text(sql)


async def _insert_reading(factory, ts_ms, pv, pv_error=0):
    async with factory() as s:
        await s.execute(
            _raw(
                "INSERT OR REPLACE INTO readings "
                "(ts, device_id, channel, pv, pv_error, alarms) "
                "VALUES (:ts, 1, 1, :pv, :err, 0)"
            ),
            {"ts": ts_ms, "pv": pv, "err": pv_error},
        )
        await s.commit()


async def _read_1min(factory):
    async with factory() as s:
        rows = (await s.execute(_raw("SELECT * FROM readings_1min"))).all()
    return rows


def _minute_floor(dt: datetime) -> int:
    ts_ms = int(dt.timestamp() * 1000)
    return (ts_ms // _MS_MIN) * _MS_MIN


class TestAggregateMinute:
    async def test_avg_min_max_over_one_minute(self, session_factory):
        bucket = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
        bucket_ms = _minute_floor(bucket)

        # 3 readings in [12:00:00, 12:01:00)
        for sec, pv in ((0, 10.0), (20, 20.0), (40, 30.0)):
            await _insert_reading(session_factory, bucket_ms + sec * _MS_SEC, pv)

        # Run at 12:01:30 so the minute 12:00-12:01 is "complete".
        now_ms = bucket_ms + 90 * _MS_SEC
        inserted = await aggregate_minute(session_factory, now_ms=now_ms)
        assert inserted == 1

        rows = await _read_1min(session_factory)
        assert len(rows) == 1
        r = rows[0]
        assert r.bucket_ts == bucket_ms
        assert r.pv_avg == pytest.approx(20.0)
        assert r.pv_min == pytest.approx(10.0)
        assert r.pv_max == pytest.approx(30.0)
        assert r.sample_count == 3

    async def test_failed_polls_excluded_from_count(self, session_factory):
        bucket_ms = _minute_floor(datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc))

        # 2 ok readings and 1 failed (pv=NULL, pv_error=99)
        await _insert_reading(session_factory, bucket_ms + 0, 15.0, pv_error=0)
        await _insert_reading(session_factory, bucket_ms + 10 * _MS_SEC, 25.0, pv_error=0)
        await _insert_reading(session_factory, bucket_ms + 20 * _MS_SEC, None, pv_error=99)

        now_ms = bucket_ms + 90 * _MS_SEC
        await aggregate_minute(session_factory, now_ms=now_ms)

        rows = await _read_1min(session_factory)
        assert len(rows) == 1
        assert rows[0].sample_count == 2
        assert rows[0].pv_avg == pytest.approx(20.0)

    async def test_all_failed_bucket_is_skipped(self, session_factory):
        bucket_ms = _minute_floor(datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc))
        for sec in (0, 15, 30):
            await _insert_reading(
                session_factory, bucket_ms + sec * _MS_SEC, None, pv_error=99
            )

        now_ms = bucket_ms + 90 * _MS_SEC
        inserted = await aggregate_minute(session_factory, now_ms=now_ms)
        assert inserted == 0
        assert await _read_1min(session_factory) == []

    async def test_incomplete_minute_excluded(self, session_factory):
        # Insert readings inside the *current* minute and run — should insert 0.
        bucket_ms = _minute_floor(datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc))
        await _insert_reading(session_factory, bucket_ms + 30 * _MS_SEC, 42.0)

        # Run still inside the same minute.
        now_ms = bucket_ms + 40 * _MS_SEC
        inserted = await aggregate_minute(session_factory, now_ms=now_ms)
        assert inserted == 0
        assert await _read_1min(session_factory) == []

    async def test_rerun_is_idempotent(self, session_factory):
        bucket_ms = _minute_floor(datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc))
        await _insert_reading(session_factory, bucket_ms + 10 * _MS_SEC, 5.0)

        now_ms = bucket_ms + 90 * _MS_SEC
        await aggregate_minute(session_factory, now_ms=now_ms)
        await aggregate_minute(session_factory, now_ms=now_ms)  # INSERT OR IGNORE

        rows = await _read_1min(session_factory)
        assert len(rows) == 1
