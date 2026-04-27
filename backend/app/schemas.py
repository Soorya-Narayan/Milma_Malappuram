"""Pydantic v2 response models for the public API.

All DB timestamps are UTC epoch milliseconds (INTEGER). These schemas
convert to timezone-aware ISO8601 strings at the boundary — internal
code never passes ``datetime`` objects around.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

Interval = Literal["raw", "1m", "1h"]


def from_ms(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Devices + channels
# ---------------------------------------------------------------------------
class ChannelMeta(BaseModel):
    channel: int
    name: str
    unit: str | None = None
    enabled: bool = True


class DeviceMeta(BaseModel):
    id: int
    name: str
    host: str
    port: int
    unit_id: int
    location: str | None = None
    enabled: bool
    channels: list[ChannelMeta] = Field(default_factory=list)


class DeviceDiagnostics(BaseModel):
    device_id: int
    connected: bool
    last_seen_ts: datetime | None = None
    total_polls_1h: int = 0
    failed_polls_1h: int = 0
    success_rate_1h: float = 0.0


# ---------------------------------------------------------------------------
# Live snapshots
# ---------------------------------------------------------------------------
class ChannelSnapshot(BaseModel):
    channel: int
    pv: float | None
    pv_error: int
    alarms: int


class DeviceSnapshot(BaseModel):
    device_id: int
    ts: datetime
    ok: bool = True
    ambient_c: float | None = None
    channels: list[ChannelSnapshot] = Field(default_factory=list)


class LiveResponse(BaseModel):
    devices: list[DeviceSnapshot]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class HistoryPoint(BaseModel):
    ts: datetime
    pv_avg: float | None = None
    pv_min: float | None = None
    pv_max: float | None = None
    sample_count: int | None = None


class HistoryResponse(BaseModel):
    device_id: int
    channel: int
    from_ts: datetime
    to_ts: datetime
    requested_interval: Interval | Literal["auto"]
    effective_interval: Interval
    point_cap_exceeded: bool
    point_count: int
    points: list[HistoryPoint]
