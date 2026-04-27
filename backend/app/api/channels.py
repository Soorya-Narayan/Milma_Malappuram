"""Channel-level actions: rename + enable/disable."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import AsyncSessionLocal

router = APIRouter(prefix="/api", tags=["channels"])


class RenameRequest(BaseModel):
    device_id: int
    channel: int = Field(ge=1, le=8)
    name: str = Field(min_length=1, max_length=64)
    unit: str | None = None


@router.post("/channels/rename")
async def rename_channel(req: RenameRequest) -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                UPDATE channels
                   SET name = :name,
                       unit = COALESCE(:unit, unit)
                 WHERE device_id = :device_id
                   AND channel = :channel
                """
            ),
            {
                "device_id": req.device_id,
                "channel": req.channel,
                "name": req.name.strip(),
                "unit": req.unit,
            },
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="channel not found")
        await session.commit()
    return {"status": "ok"}


class SetEnabledRequest(BaseModel):
    device_id: int
    channel: int = Field(ge=1, le=8)
    enabled: bool


@router.post("/channels/set-enabled")
async def set_channel_enabled(
    req: SetEnabledRequest, request: Request
) -> dict[str, object]:
    """Enable or disable logging/display of a single channel.

    Disabled channels:
      * are not persisted to the `readings` table by the poller
      * are not included in Trends, History, or Excel export
      * render as a neutral "—" tile on Live (no alarm/error colouring)
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                UPDATE channels
                   SET enabled = :enabled
                 WHERE device_id = :device_id
                   AND channel = :channel
                """
            ),
            {
                "device_id": req.device_id,
                "channel": req.channel,
                "enabled": 1 if req.enabled else 0,
            },
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="channel not found")
        await session.commit()

    # Sync the in-memory poller cache so the change takes effect on the
    # next persist cycle without waiting for a restart.
    runtime = getattr(request.app.state, "poller_runtime", None)
    if runtime is not None:
        disabled = runtime.disabled_channels.setdefault(req.device_id, set())
        if req.enabled:
            disabled.discard(req.channel)
        else:
            disabled.add(req.channel)

    return {
        "status": "ok",
        "device_id": req.device_id,
        "channel": req.channel,
        "enabled": req.enabled,
    }
