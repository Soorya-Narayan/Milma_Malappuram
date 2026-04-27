"""/ws/live — forwards every poll snapshot to connected clients as JSON.

Subscribers are registered with the in-process Broadcaster. Slow clients
won't block the poller: the Broadcaster drops oldest messages when a
subscriber's queue is full. If send to the socket itself blocks beyond
the ping timeout, Starlette raises, we clean up and exit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ws"])
log = logging.getLogger(__name__)


def _snapshot_to_json(snap) -> str:
    """Serialise a poller.Snapshot dataclass into the wire format."""
    d = dataclasses.asdict(snap)
    # ts_ms -> ISO string for clients; keep raw ms alongside for debugging.
    d["ts"] = datetime.fromtimestamp(d["ts_ms"] / 1000, tz=timezone.utc).isoformat()
    return json.dumps(d, default=str)


@router.websocket("/ws/live")
async def live_ws(ws: WebSocket) -> None:
    broadcaster = ws.app.state.broadcaster
    await ws.accept()
    log.info("ws/live connected peer=%s", ws.client)

    try:
        async with broadcaster.subscribe() as queue:
            while True:
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # keepalive — send a ping-style frame so idle intermediaries
                    # don't cut the connection.
                    await ws.send_text('{"type":"keepalive"}')
                    continue

                await ws.send_text(_snapshot_to_json(snap))
    except WebSocketDisconnect:
        log.info("ws/live disconnected peer=%s", ws.client)
    except Exception:   # noqa: BLE001
        log.exception("ws/live error peer=%s", ws.client)
        try:
            await ws.close()
        except Exception:   # pragma: no cover
            pass
