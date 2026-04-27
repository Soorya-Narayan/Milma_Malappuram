"""Runtime-editable settings shared across tasks.

Backed by the `app_settings` KV table. One in-memory holder is created at
startup and shared by the pollers (readers) and the settings API (writer)
so changes take effect on the next poll cycle without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

# Allowed logging cycle times (seconds). Keep in lockstep with the UI
# dropdown in settings.html — the API validates against this set.
ALLOWED_LOG_INTERVALS_S: tuple[float, ...] = (1, 2, 5, 10, 30, 60, 300, 600)


@dataclass
class PollerRuntime:
    """Mutable holder the pollers read at the top of every cycle."""

    interval_s: float
    # device_id -> set of channel numbers (1..8) that should NOT be logged.
    # Mutated by POST /api/channels/set-enabled; read by the poller on every
    # persist cycle.
    disabled_channels: dict[int, set[int]] = field(default_factory=dict)
    # When True, the scheduler fires the 180-day purge once per day at the
    # retention hour (03:00 local). Mutated by POST /api/settings/auto-purge.
    auto_purge_enabled: bool = False


async def load_disabled_channels(
    session_factory: async_sessionmaker,
) -> dict[int, set[int]]:
    """Read the current disabled-channel set from the channels table."""
    out: dict[int, set[int]] = {}
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT device_id, channel FROM channels WHERE enabled = 0"
                )
            )
        ).all()
    for r in rows:
        out.setdefault(r.device_id, set()).add(r.channel)
    return out


async def load_log_interval(
    session_factory: async_sessionmaker, default_s: float
) -> float:
    """Fetch the stored interval; seed the row with `default_s` if missing."""
    async with session_factory() as session:
        row = await session.execute(
            text("SELECT value FROM app_settings WHERE key = 'log_interval_s'")
        )
        v = row.scalar_one_or_none()
        if v is None:
            await session.execute(
                text(
                    "INSERT INTO app_settings (key, value) VALUES "
                    "('log_interval_s', :v)"
                ),
                {"v": str(default_s)},
            )
            await session.commit()
            return default_s
    try:
        return float(v)
    except ValueError:
        return default_s


async def save_log_interval(
    session_factory: async_sessionmaker, seconds: float
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES "
                "('log_interval_s', :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"v": str(seconds)},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Auto-purge (delete data older than 6 months, fired daily by the scheduler)
# ---------------------------------------------------------------------------
async def load_auto_purge(
    session_factory: async_sessionmaker, default: bool = False
) -> bool:
    """Read the stored auto_purge flag; seed with `default` if missing."""
    async with session_factory() as session:
        row = await session.execute(
            text("SELECT value FROM app_settings WHERE key = 'auto_purge_enabled'")
        )
        v = row.scalar_one_or_none()
        if v is None:
            await session.execute(
                text(
                    "INSERT INTO app_settings (key, value) VALUES "
                    "('auto_purge_enabled', :v)"
                ),
                {"v": "1" if default else "0"},
            )
            await session.commit()
            return default
    return v.strip() in ("1", "true", "True", "yes", "on")


async def save_auto_purge(
    session_factory: async_sessionmaker, enabled: bool
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES "
                "('auto_purge_enabled', :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"v": "1" if enabled else "0"},
        )
        await session.commit()
