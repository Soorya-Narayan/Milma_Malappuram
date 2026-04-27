"""GET /settings — rename channels."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.devices import list_devices
from app.pages import templates
from app.runtime_settings import ALLOWED_LOG_INTERVALS_S

router = APIRouter(tags=["pages"])


# Human-readable labels for each allowed logging interval. Order matches
# ALLOWED_LOG_INTERVALS_S in runtime_settings.py.
_INTERVAL_LABELS: dict[float, str] = {
    1: "1-Sec",
    2: "2-Sec",
    5: "5-Sec",
    10: "10-Sec",
    30: "30-Sec",
    60: "1-Min",
    300: "5-Min",
    600: "10-Min",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    devices = await list_devices()
    device = next((d for d in devices if d.enabled), devices[0] if devices else None)
    channels = device.channels if device else []
    by_num = {c.channel: c for c in channels}
    rows = []
    for i in range(1, 9):
        c = by_num.get(i)
        rows.append({
            "channel": i,
            "name": c.name if c else f"CH{i}",
            "unit": (c.unit if c else "") or "",
            "enabled": c.enabled if c else True,
        })

    runtime = request.app.state.poller_runtime
    interval_options = [
        {"seconds": s, "label": _INTERVAL_LABELS.get(s, f"{s}-Sec")}
        for s in ALLOWED_LOG_INTERVALS_S
    ]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "device_id": device.id if device else 0,
            "rows": rows,
            "current_interval_s": runtime.interval_s,
            "interval_options": interval_options,
        },
    )
