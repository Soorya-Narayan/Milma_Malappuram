"""GET /trends — chart page. Data is fetched client-side from /api/history."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.devices import list_devices
from app.pages import templates

router = APIRouter(tags=["pages"])

PALETTE = [
    "#6ea8fe", "#e0a04a", "#77dd77", "#e06a6a",
    "#c397e6", "#5cc7c7", "#e0c977", "#b07a7a",
]


@router.get("/trends", response_class=HTMLResponse)
async def trends_page(request: Request) -> HTMLResponse:
    devices = await list_devices()
    # Single-device dashboard: use the first (enabled) device.
    device = next((d for d in devices if d.enabled), devices[0] if devices else None)
    # Hide channels the operator has disabled in Settings. They should not
    # appear as toggle buttons, chart series, or anywhere else on Trends.
    channels = [c for c in (device.channels if device else []) if c.enabled]
    return templates.TemplateResponse(
        request,
        "trends.html",
        {
            "device_id": device.id if device else 0,
            "channels": channels,
            "channels_json": json.dumps([c.model_dump() for c in channels]),
            "palette": PALETTE,
            "palette_json": json.dumps(PALETTE),
        },
    )
