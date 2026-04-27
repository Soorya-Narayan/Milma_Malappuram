"""Async wrapper around pymodbus's sync ModbusTcpClient.

One instance per RTU. Holds a persistent TCP connection for the
lifetime of the poller task (CLAUDE.md: at most 2 concurrent clients
per AIME 8U, so we keep exactly one).

Every blocking pymodbus call is dispatched to a worker thread via
`asyncio.to_thread` — pymodbus is synchronous. An internal asyncio.Lock
serialises access: Modbus TCP is request/response over a single socket,
and issuing two requests at once on one connection would corrupt the
frame stream.

Each read raises on failure; the caller (poller) decides what to do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

from app.modbus.decoders import (
    ADDR_ALARM_BITMAP,
    ADDR_AMBIENT_TEMP,
    ADDR_PV_FLOAT_CH1,
    ADDR_PV_INT16_CH1,
    NUM_ALARMS,
    NUM_CHANNELS,
)

log = logging.getLogger(__name__)


@dataclass
class ModbusConfig:
    host: str
    port: int = 502
    unit_id: int = 1
    timeout: float = 2.0
    retries: int = 1


class AIME8UClient:
    """Async front-end for the sync pymodbus client.

    Intended usage:
        client = AIME8UClient(config)
        await client.connect()
        try:
            while True:
                regs = await client.read_pv_floats()
                ...
        finally:
            await client.close()
    """

    def __init__(self, config: ModbusConfig) -> None:
        self.cfg = config
        self._client: ModbusTcpClient | None = None
        # Serialises _all_ reads + connect/close on this client. One
        # socket, one in-flight request at a time.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    async def connect(self) -> bool:
        """Open the TCP socket. Returns True on success, False on failure."""
        async with self._lock:
            return await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> bool:
        self._close_sync()
        self._client = ModbusTcpClient(
            host=self.cfg.host,
            port=self.cfg.port,
            timeout=self.cfg.timeout,
            retries=self.cfg.retries,
        )
        return self._client.connect()

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        if self._client is not None:
            try:
                if self._client.connected:
                    self._client.close()
            except Exception:   # pragma: no cover - best-effort cleanup
                log.warning("error closing modbus client to %s:%d", self.cfg.host, self.cfg.port)
        self._client = None

    # ------------------------------------------------------------------
    # Low-level read (handles pymodbus API variations)
    # ------------------------------------------------------------------
    def _read_input_sync(self, address: int, count: int) -> list[int]:
        """Sync read executed inside a worker thread. Runs under self._lock."""
        if self._client is None or not self._client.connected:
            raise ConnectionException("Client not connected.")

        # pymodbus renamed the slave kwarg across 3.x. Try the newer name
        # first; fall back to the older one on TypeError. (CLAUDE.md shim.)
        try:
            rsp = self._client.read_input_registers(
                address=address, count=count, device_id=self.cfg.unit_id
            )
        except TypeError:
            rsp = self._client.read_input_registers(
                address=address, count=count, slave=self.cfg.unit_id
            )

        if rsp.isError():
            if isinstance(rsp, ExceptionResponse):
                raise ModbusIOException(
                    f"Modbus exception code={rsp.exception_code} (0x{rsp.exception_code:02X})"
                )
            raise ModbusIOException(f"Read failed: {rsp}")

        return list(rsp.registers)

    async def _read_input(self, address: int, count: int) -> list[int]:
        async with self._lock:
            return await asyncio.to_thread(self._read_input_sync, address, count)

    # ------------------------------------------------------------------
    # Typed reads — each raises on failure.
    # ------------------------------------------------------------------
    async def read_pv_floats(self) -> list[int]:
        """Return the 16 raw registers for CH1..CH8 FLOAT32 PVs."""
        return await self._read_input(ADDR_PV_FLOAT_CH1, NUM_CHANNELS * 2)

    async def read_pv_ints(self) -> list[int]:
        """Return the 8 raw INT16 PV registers for CH1..CH8."""
        return await self._read_input(ADDR_PV_INT16_CH1, NUM_CHANNELS)

    async def read_alarm_status(self) -> list[int]:
        """Return the 4 raw alarm bitmap registers (AL1..AL4)."""
        return await self._read_input(ADDR_ALARM_BITMAP, NUM_ALARMS)

    async def read_ambient(self) -> int:
        """Return the single raw INT16 ambient/CJC register."""
        regs = await self._read_input(ADDR_AMBIENT_TEMP, 1)
        return regs[0]
