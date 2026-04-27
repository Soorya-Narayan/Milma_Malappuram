"""FastAPI entry point.

Lifespan order (startup):
    1. init_db()              — create /data + apply schema.sql
    2. sync_devices_from_yaml — upsert devices/channels from devices.yaml
    3. Broadcaster()          — in-process pub/sub for live snapshots
    4. start_pollers()        — one asyncio task per enabled device

Shutdown reverses: stop_pollers → dispose_engine.

Downsampler + REST endpoints + WebSocket + HTMX pages arrive in later phases.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import alarms as alarms_api
from app.api import channels as channels_api
from app.api import devices as devices_api
from app.api import export as export_api
from app.api import history as history_api
from app.api import live as live_api
from app.config import get_settings
from app.db import AsyncSessionLocal, dispose_engine, engine, init_db
from app.modbus.poller import start_pollers, stop_pollers
from app.runtime_settings import (
    PollerRuntime,
    load_auto_purge,
    load_disabled_channels,
    load_log_interval,
)
from app.api import settings as settings_api
from app.api import usage as usage_api
from app.pages import about as about_page
from app.pages import history as history_page
from app.pages import live as live_page
from app.pages import settings as settings_page
from app.pages import trends as trends_page
from app.pages import usage as usage_page
from app.pubsub import Broadcaster
from app.startup import sync_devices_from_yaml
from app.tasks.scheduler import start_scheduler, stop_scheduler
from app.ws import live as live_ws

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)

    log.info("starting Milma AIME dashboard — data_dir=%s", settings.data_dir)

    await init_db()

    devices_file = settings.load_devices_file()
    async with AsyncSessionLocal() as session:
        await sync_devices_from_yaml(session, devices_file)

    broadcaster = Broadcaster()

    # Runtime-editable poll interval. Loaded from app_settings KV table,
    # falling back to the devices.yaml default on first boot.
    interval_s = await load_log_interval(
        AsyncSessionLocal, default_s=devices_file.poll_interval_s
    )
    disabled = await load_disabled_channels(AsyncSessionLocal)
    auto_purge = await load_auto_purge(AsyncSessionLocal, default=False)
    runtime = PollerRuntime(
        interval_s=interval_s,
        disabled_channels=disabled,
        auto_purge_enabled=auto_purge,
    )

    poller_tasks: list[asyncio.Task] = start_pollers(
        devices_file, runtime, broadcaster, AsyncSessionLocal
    )
    scheduler_task: asyncio.Task = start_scheduler(
        AsyncSessionLocal, engine, devices_file.retention, runtime
    )

    # Expose shared singletons on app.state for handlers/websocket/tests.
    _app.state.devices_file = devices_file
    _app.state.broadcaster = broadcaster
    _app.state.poller_tasks = poller_tasks
    _app.state.scheduler_task = scheduler_task
    _app.state.poller_runtime = runtime

    try:
        yield
    finally:
        log.info("shutting down")
        await stop_scheduler(scheduler_task)
        await stop_pollers(poller_tasks)
        await dispose_engine()


app = FastAPI(title="Milma AIME Dashboard", lifespan=lifespan)

app.include_router(devices_api.router)
app.include_router(live_api.router)
app.include_router(history_api.router)
app.include_router(export_api.router)
app.include_router(alarms_api.router)
app.include_router(channels_api.router)
app.include_router(settings_api.router)
app.include_router(usage_api.router)
app.include_router(live_ws.router)
app.include_router(live_page.router)
app.include_router(trends_page.router)
app.include_router(history_page.router)
app.include_router(settings_page.router)
app.include_router(about_page.router)
app.include_router(usage_page.router)

# Static assets (CSS/JS vendor + app). Directory is created in the image.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/api/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    db_path: Path = settings.data_dir / "aime.db"
    db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.exists() else 0.0

    async with AsyncSessionLocal() as session:
        row = await session.execute(text("SELECT COUNT(*) FROM devices WHERE enabled = 1"))
        devices_enabled = int(row.scalar_one())

    poller_tasks = getattr(app.state, "poller_tasks", [])
    devices_polling = sum(1 for t in poller_tasks if not t.done())

    scheduler_task = getattr(app.state, "scheduler_task", None)
    scheduler_alive = bool(scheduler_task and not scheduler_task.done())

    return {
        "status": "ok",
        "phase": "7",
        "devices_enabled": devices_enabled,
        "devices_polling": devices_polling,
        "scheduler_alive": scheduler_alive,
        "db_size_mb": db_size_mb,
    }
