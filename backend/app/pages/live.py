"""GET / — live dashboard page (HTMX + WebSocket).

Server renders a first-paint grid of device cards with the most recent
snapshot per channel + per-device diagnostics. After load, the client
subscribes to /ws/live and updates tiles in place via Alpine.js.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.devices import list_devices
from app.api.live import live_snapshot
from app.pages import templates

router = APIRouter(tags=["pages"])


@router.get("/", response_class=HTMLResponse)
async def live_page(request: Request) -> HTMLResponse:
    devices = await list_devices()
    live = await live_snapshot()
    snap_by_device = {d.device_id: d for d in live.devices}

    # Per-device diagnostics are no longer shown on this page (the CONNECTED /
    # last-seen / success-rate strip was removed). Skip the per-device SQL scan
    # over readings — it was adding 3-5 s to first paint with no visible effect.
    diag_by_device = {d.id: None for d in devices}

    now = datetime.now(tz=timezone.utc)
    last24h_to = now.isoformat()
    last24h_from = (now - timedelta(hours=24)).isoformat()

    return templates.TemplateResponse(
        request,
        "live.html",
        {
            "devices": devices,
            "snap_by_device": snap_by_device,
            "diag_by_device": diag_by_device,
            "last24h_from": last24h_from,
            "last24h_to": last24h_to,
        },
    )


@router.get("/partials/live-tiles", response_class=HTMLResponse)
async def live_tiles_partial(request: Request) -> HTMLResponse:
    """Re-render the device grid; HTMX fallback when the WS is unavailable."""
    devices = await list_devices()
    live = await live_snapshot()
    snap_by_device = {d.device_id: d for d in live.devices}
    # Diagnostics strip is gone — skip the per-device readings scan.
    diag_by_device = {d.id: None for d in devices}

    now = datetime.now(tz=timezone.utc)
    ctx = {
        "devices": devices,
        "snap_by_device": snap_by_device,
        "diag_by_device": diag_by_device,
        "last24h_from": (now - timedelta(hours=24)).isoformat(),
        "last24h_to": now.isoformat(),
    }
    # Render just the grid using a minimal inline wrapper.
    html_parts = ['<div class="device-grid">']
    for device in devices:
        frag = templates.get_template("partials/device_card.html").render(
            device=device,
            snap=snap_by_device.get(device.id),
            diag=diag_by_device.get(device.id),
            last24h_from=ctx["last24h_from"],
            last24h_to=ctx["last24h_to"],
        )
        html_parts.append(frag)
    html_parts.append("</div>")
    return HTMLResponse("".join(html_parts))
