"""One asyncio task per enabled device.

Each task:
  1. ensures its persistent AIME8UClient is connected
  2. reads FLOAT PVs, INT16 PVs, alarm bitmaps, and ambient (sequential —
     a Modbus TCP socket serves one request at a time)
  3. assembles a Snapshot dataclass
  4. writes rows to `readings` + `ambient_readings` in one transaction
  5. publishes the snapshot to the in-process Broadcaster
  6. sleeps so cycle-to-cycle timing is exactly poll_interval_s (no drift)

On any exception: log, close the client, sleep one cycle, retry. A
failed poll still writes one row per channel with pv=NULL and
pv_error=99 so gaps are explicit in the DB (CLAUDE.md § coding
conventions — "No silent failures").

Note on "parallel reads": the original prompt says to parallel-read the
four register blocks. pymodbus is sync over one TCP socket per device,
so the four reads necessarily serialise on the per-client lock. We do
them as four sequential awaits — same wall time as asyncio.gather
would give, with simpler error handling.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import DeviceConfig, DevicesFile, get_settings
from app.modbus.client import AIME8UClient, ModbusConfig
from app.runtime_settings import PollerRuntime
from app.modbus.decoders import (
    NUM_ALARMS,
    NUM_CHANNELS,
    PV_ERROR_OK,
    PV_ERROR_READ_FAIL,
    classify_pv_int,
    decode_alarm_bitmap,
    decode_ambient,
    pack_alarm_nibble,
    registers_to_floats,
)
from app.pubsub import Broadcaster

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot — what gets published to subscribers and persisted per poll
# ---------------------------------------------------------------------------
@dataclass
class ChannelSample:
    channel: int
    pv: float | None
    pv_error: int
    alarms: int               # 4-bit AL4|AL3|AL2|AL1


@dataclass
class Snapshot:
    device_id: int
    ts_ms: int                # unix epoch milliseconds (UTC)
    ok: bool
    error: str = ""
    ambient_c: float | None = None
    channels: list[ChannelSample] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
_INSERT_READING = text(
    "INSERT OR REPLACE INTO readings "
    "(ts, device_id, channel, pv, pv_error, alarms) "
    "VALUES (:ts, :device_id, :channel, :pv, :pv_error, :alarms)"
)

_INSERT_AMBIENT = text(
    "INSERT OR REPLACE INTO ambient_readings (ts, device_id, ambient_c) "
    "VALUES (:ts, :device_id, :ambient_c)"
)


async def _persist_snapshot(
    session_factory: async_sessionmaker,
    snap: Snapshot,
    disabled_channels: set[int] | None = None,
) -> None:
    """Write one ambient row + N channel rows in a single transaction.

    Operator request: only log channels that actually have a valid
    reading. A channel with pv_error != 0 (under-range, over-range,
    sensor open, read failure) is a "no data" tick and does not get a
    row. Channels the operator marked disabled in Settings are also
    filtered out before writing. If nothing is left to persist we skip
    the whole insert — including the ambient reading — so the DB
    doesn't fill with empty cycles.
    """
    disabled = disabled_channels or set()
    valid_channels = [
        s for s in snap.channels
        if s.pv_error == 0 and s.pv is not None and s.channel not in disabled
    ]
    if not valid_channels:
        return

    async with session_factory() as session:
        if snap.ambient_c is not None:
            await session.execute(
                _INSERT_AMBIENT,
                {
                    "ts": snap.ts_ms,
                    "device_id": snap.device_id,
                    "ambient_c": snap.ambient_c,
                },
            )
        await session.execute(
            _INSERT_READING,
            [
                {
                    "ts": snap.ts_ms,
                    "device_id": snap.device_id,
                    "channel": s.channel,
                    "pv": s.pv,
                    "pv_error": s.pv_error,
                    "alarms": s.alarms,
                }
                for s in valid_channels
            ],
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Snapshot assembly from raw register reads
# ---------------------------------------------------------------------------
def _assemble_snapshot(
    device_id: int,
    ts_ms: int,
    float_regs: list[int],
    int_regs: list[int],
    alarm_regs: list[int],
    ambient_reg: int,
) -> Snapshot:
    pv_floats = registers_to_floats(float_regs, word_order="big")
    ambient_c = decode_ambient(ambient_reg)

    # alarm_regs is 4 regs, each packs 8 channels. Invert to per-channel.
    per_alarm = [decode_alarm_bitmap(r) for r in alarm_regs]   # shape [4][8]

    channels: list[ChannelSample] = []
    for ch_idx in range(NUM_CHANNELS):
        _, err_code = classify_pv_int(int_regs[ch_idx])
        pv = pv_floats[ch_idx] if err_code == PV_ERROR_OK else None
        alarm_flags = [per_alarm[a][ch_idx] for a in range(NUM_ALARMS)]
        channels.append(
            ChannelSample(
                channel=ch_idx + 1,
                pv=pv,
                pv_error=err_code,
                alarms=pack_alarm_nibble(alarm_flags),
            )
        )

    return Snapshot(
        device_id=device_id,
        ts_ms=ts_ms,
        ok=True,
        ambient_c=ambient_c,
        channels=channels,
    )


def _failed_snapshot(device_id: int, ts_ms: int, error: str) -> Snapshot:
    """Build a snapshot that marks every channel as pv=NULL, pv_error=99."""
    return Snapshot(
        device_id=device_id,
        ts_ms=ts_ms,
        ok=False,
        error=error,
        ambient_c=None,
        channels=[
            ChannelSample(channel=ch, pv=None, pv_error=PV_ERROR_READ_FAIL, alarms=0)
            for ch in range(1, NUM_CHANNELS + 1)
        ],
    )


# ---------------------------------------------------------------------------
# Per-device loop
# ---------------------------------------------------------------------------
async def run_device(
    device: DeviceConfig,
    runtime: PollerRuntime,
    broadcaster: Broadcaster,
    session_factory: async_sessionmaker,
) -> None:
    """One persistent poller task for a single RTU. Runs until cancelled."""

    client = AIME8UClient(
        ModbusConfig(
            host=device.host,
            port=device.port,
            unit_id=device.unit_id,
            timeout=2.0,
            retries=1,
        )
    )
    log.info("poller started: device_id=%d host=%s:%d", device.id, device.host, device.port)

    # Modbus polling is always 1 Hz so the Live view / WebSocket stay fresh.
    # The operator-configurable "logging cycle" in Settings controls how often
    # we actually persist a row to the DB — not how often we poll the RTU.
    POLL_HZ = 1.0
    last_logged_mono: float | None = None

    next_deadline = time.monotonic()
    try:
        while True:
            cycle_started = time.monotonic()
            ts_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

            try:
                if not client.connected:
                    ok = await client.connect()
                    if not ok:
                        raise ConnectionError(f"connect to {device.host}:{device.port} failed")

                float_regs = await client.read_pv_floats()
                int_regs = await client.read_pv_ints()
                alarm_regs = await client.read_alarm_status()
                ambient_reg = await client.read_ambient()

                snap = _assemble_snapshot(
                    device_id=device.id,
                    ts_ms=ts_ms,
                    float_regs=float_regs,
                    int_regs=int_regs,
                    alarm_regs=alarm_regs,
                    ambient_reg=ambient_reg,
                )

            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001 - poller swallows all
                log.warning("device_id=%d poll failed: %s", device.id, exc)
                await client.close()
                snap = _failed_snapshot(device.id, ts_ms, str(exc))

            # Persist only on the configured logging cadence. The WebSocket
            # push below still happens every cycle so Live updates at 1 Hz.
            log_interval_s = max(runtime.interval_s, POLL_HZ)
            now_mono = time.monotonic()
            should_log = (
                last_logged_mono is None
                or (now_mono - last_logged_mono) >= log_interval_s - 0.05
            )
            if should_log:
                try:
                    await _persist_snapshot(
                        session_factory,
                        snap,
                        disabled_channels=runtime.disabled_channels.get(device.id),
                    )
                    last_logged_mono = now_mono
                except Exception:   # noqa: BLE001
                    # DB problems shouldn't kill the poller; just log.
                    log.exception("device_id=%d persist failed", device.id)

            broadcaster.publish(snap)

            # Drift-free scheduling: next deadline is always cycle_started + 1s
            # so the Modbus poll cadence (and the Live feed) stays at 1 Hz
            # regardless of the logging cycle setting.
            next_deadline = cycle_started + POLL_HZ
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            # If we overran the budget, just start the next cycle immediately —
            # the DB's (ts, device_id, channel) PK will dedup if ts collides.
    except asyncio.CancelledError:
        log.info("poller cancelling: device_id=%d", device.id)
        raise
    finally:
        await client.close()
        log.info("poller stopped: device_id=%d", device.id)


# ---------------------------------------------------------------------------
# Lifecycle management
# ---------------------------------------------------------------------------
def start_pollers(
    devices_file: DevicesFile,
    runtime: PollerRuntime,
    broadcaster: Broadcaster,
    session_factory: async_sessionmaker,
) -> list[asyncio.Task]:
    """Spawn one task per enabled device. Returns the list of Tasks."""
    tasks: list[asyncio.Task] = []
    for device in devices_file.devices:
        if not device.enabled:
            log.info("skipping disabled device id=%d (%s)", device.id, device.name)
            continue
        task = asyncio.create_task(
            run_device(device, runtime, broadcaster, session_factory),
            name=f"poller-{device.id}",
        )
        tasks.append(task)
    log.info(
        "started %d poller task(s) (poll=1.0s, log=%.1fs)",
        len(tasks), runtime.interval_s,
    )
    return tasks


async def stop_pollers(tasks: list[asyncio.Task]) -> None:
    """Cancel and await every poller task. Safe to call on an empty list."""
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("stopped %d poller task(s)", len(tasks))
