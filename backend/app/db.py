"""Async SQLite layer.

Exposes:
  * engine            — shared AsyncEngine (aiosqlite)
  * AsyncSessionLocal — AsyncSession factory
  * init_db()         — creates /data (if missing) and applies schema.sql
  * dispose_engine()  — awaits engine.dispose() for clean shutdown

Every new connection runs the PRAGMA block from CLAUDE.md. The engine
registers a sync `connect` event listener (fires with the underlying
DBAPI/aiosqlite-sync connection), which is the idiomatic SQLAlchemy
place for this. init_db() bypasses the engine and uses aiosqlite
directly because it needs to run `executescript` (multi-statement DDL),
which SQLAlchemy's async connection doesn't expose cleanly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

log = logging.getLogger(__name__)

# Pragmas applied on every new connection. See CLAUDE.md § "SQLite tuning".
_CONNECT_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA cache_size=-4000;",       # 4 MB page cache
    "PRAGMA mmap_size=67108864;",     # 64 MB mmap
    "PRAGMA temp_store=MEMORY;",
    "PRAGMA busy_timeout=5000;",      # 5 s lock wait
    "PRAGMA foreign_keys=ON;",
)

_SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"


def _db_path() -> Path:
    return get_settings().data_dir / "aime.db"


def _db_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _attach_pragma_listener(engine: AsyncEngine) -> None:
    """Run _CONNECT_PRAGMAS on every new DBAPI connection via the engine."""

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            for pragma in _CONNECT_PRAGMAS:
                cur.execute(pragma)
        finally:
            cur.close()


def _build_engine() -> AsyncEngine:
    eng = create_async_engine(
        _db_url(_db_path()),
        echo=False,
        future=True,
        pool_pre_ping=False,
    )
    _attach_pragma_listener(eng)
    return eng


# Module-level singletons. Safe because we run one uvicorn worker (see
# CLAUDE.md § "Things to NOT do"), so there is exactly one event loop.
engine: AsyncEngine = _build_engine()
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db() -> None:
    """Create the data directory (if missing) and apply schema.sql.

    Uses aiosqlite directly — SQLAlchemy's async Connection does not expose
    `executescript`, and schema.sql contains multiple DDL statements.
    Pragmas are set here too so WAL mode is enabled before the first write
    (the engine's connect listener also applies them for all other sessions).
    """
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    schema_sql = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    db_path = _db_path()

    async with aiosqlite.connect(db_path) as conn:
        for pragma in _CONNECT_PRAGMAS:
            await conn.execute(pragma)
        await conn.executescript(schema_sql)
        await _apply_column_migrations(conn)
        await conn.commit()

    log.info("schema applied at %s", db_path)


async def _apply_column_migrations(conn: aiosqlite.Connection) -> None:
    """Add columns that were introduced after the initial CREATE TABLE.

    SQLite's CREATE TABLE IF NOT EXISTS won't patch pre-existing tables,
    so new columns need explicit ALTER TABLE ADD COLUMN guarded by a
    PRAGMA table_info() check.
    """
    cursor = await conn.execute("PRAGMA table_info(channels)")
    cols = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "enabled" not in cols:
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
        log.info("migrated: channels.enabled column added")


async def dispose_engine() -> None:
    """Dispose the engine; call from FastAPI lifespan shutdown."""
    await engine.dispose()
