"""GET /history — wide pivoted table (ts + CH1..CH8) with local-time formatting.

Single-device dashboard, so the device selector has been removed. The
pivot is done in SQL using conditional MAX() aggregation over the
(ts, channel) grid. Timestamps are formatted in the container TZ
(Asia/Kolkata by default) so operators see local clock time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.devices import list_devices
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.pages import templates

router = APIRouter(tags=["pages"])

PAGE_SIZE = 100


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().tz)
    except Exception:   # noqa: BLE001 — fall back if the env TZ is unparseable
        return ZoneInfo("UTC")


async def _default_device_id() -> int:
    devices = await list_devices()
    for d in devices:
        if d.enabled:
            return d.id
    return devices[0].id if devices else 0


@dataclass
class _Row:
    ts_local: str
    ch: list[float | None]   # 8 values, None where missing


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    devices = await list_devices()
    device = next((d for d in devices if d.enabled), devices[0] if devices else None)
    channels = device.channels if device else []
    # Only render columns for channels the operator has enabled. Disabled
    # channels are hidden from the table entirely — matches the "no logging,
    # no trends, no export" rule set in Settings.
    by_num = {c.channel: c for c in channels if c.enabled}
    enabled_channels = sorted(by_num.keys())
    headers = [by_num[i].name for i in enabled_channels]
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "device_id": device.id if device else 0,
            "headers": headers,
            "headers_json": json.dumps(headers),
            "enabled_channels": enabled_channels,
            "enabled_channels_json": json.dumps(enabled_channels),
            "tz_label": get_settings().tz,
        },
    )


@router.get("/partials/history-rows", response_class=HTMLResponse)
async def history_rows(
    request: Request,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    page: int = Query(1, ge=1),
    device_id: int | None = Query(None),
) -> HTMLResponse:
    if to <= from_:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")

    did = device_id if device_id is not None else await _default_device_id()
    if did == 0:
        raise HTTPException(status_code=404, detail="no device configured")

    # Resolve which channels are currently enabled for this device so we only
    # render those columns. Disabled channels are not shown on the page and
    # not included in the row values.
    devices = await list_devices()
    device = next((d for d in devices if d.id == did), None)
    enabled_channels = sorted(
        c.channel for c in (device.channels if device else []) if c.enabled
    )
    if not enabled_channels:
        enabled_channels = list(range(1, 9))  # fallback: show all

    from_ms = int(from_.timestamp() * 1000)
    to_ms = int(to.timestamp() * 1000)
    offset = (page - 1) * PAGE_SIZE

    # Pivot: one row per ts, one column per channel. LIMIT+1 to detect has_next.
    sql = text(
        """
        SELECT ts,
            MAX(CASE WHEN channel=1 THEN pv END) AS ch1,
            MAX(CASE WHEN channel=2 THEN pv END) AS ch2,
            MAX(CASE WHEN channel=3 THEN pv END) AS ch3,
            MAX(CASE WHEN channel=4 THEN pv END) AS ch4,
            MAX(CASE WHEN channel=5 THEN pv END) AS ch5,
            MAX(CASE WHEN channel=6 THEN pv END) AS ch6,
            MAX(CASE WHEN channel=7 THEN pv END) AS ch7,
            MAX(CASE WHEN channel=8 THEN pv END) AS ch8
        FROM readings
        WHERE device_id = :d
          AND ts >= :f AND ts < :t
        GROUP BY ts
        ORDER BY ts DESC
        LIMIT :lim OFFSET :off
        """
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sql,
            {"d": did, "f": from_ms, "t": to_ms, "lim": PAGE_SIZE + 1, "off": offset},
        )
        rows = result.all()

    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    tz = _local_tz()
    out: list[_Row] = []
    for r in rows:
        dt_local = datetime.fromtimestamp(r.ts / 1000, tz=timezone.utc).astimezone(tz)
        all_vals = [r.ch1, r.ch2, r.ch3, r.ch4, r.ch5, r.ch6, r.ch7, r.ch8]
        out.append(
            _Row(
                ts_local=dt_local.strftime("%Y-%m-%d %H:%M:%S"),
                ch=[all_vals[c - 1] for c in enabled_channels],
            )
        )

    # Properly URL-encode so `+` in the tz offset doesn't get decoded as space
    # on the next-page request (which previously broke pagination).
    query_str = urlencode({
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "device_id": did,
    })
    return templates.TemplateResponse(
        request,
        "partials/history_rows.html",
        {
            "rows": out,
            "page": page,
            "has_next": has_next,
            "query_str": query_str,
            "colspan": 1 + len(enabled_channels),
        },
    )
