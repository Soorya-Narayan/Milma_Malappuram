"""
AIME 8U RTU - Modbus TCP/IP Reader  (PPI Process Precision Instruments)
-----------------------------------------------------------------------
Continuously polls the 8 analog input channels of the AIME 8U over
Modbus TCP/IP and refreshes a dashboard-style table every N seconds.

Requirements:
    pip install "pymodbus>=3.6"

Register map (from AIME 4U / 8U manual, Section 10):

    Address | FC | Type     | Description
    --------|----|----------|-------------------------------------------
      82    | 04 | INT16    | Ambient temperature (terminal block, CJC)
    1561..  | 04 | INT16    | PV for CH1..CH8 (scaled by resolution)
    1577..  | 04 | BITMAP16 | Alarm-1..Alarm-4 status (1 reg per alarm)
    2001..  | 04 | FLOAT32  | PV for CH1..CH8 (2 regs per ch, ABCD order)

Ambient temp note:
    This is the temperature AT the module's terminal block, used for
    Cold Junction Compensation of thermocouple inputs. It's effectively
    the ambient inside the panel - not the module's CPU temperature.
"""

import logging
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse


# ----------------------------------------------------------------------
# Logging - less chatty by default for continuous mode
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,           # switch to INFO if you want per-cycle logs
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("AIME8U")


# ----------------------------------------------------------------------
# Register map constants (raw Modbus protocol addresses per the manual)
# If you get "illegal data address" errors, subtract 1 from each.
# ----------------------------------------------------------------------
ADDR_AMBIENT_TEMP   = 82
ADDR_PV_INT16_CH1   = 1561
ADDR_ALARM_BITMAP   = 1577
ADDR_PV_FLOAT_CH1   = 2001
NUM_CHANNELS        = 8

PV_ERR_UNDER_RANGE  = -32768
PV_ERR_OVER_RANGE   =  32752
PV_ERR_SENSOR_OPEN  =  32767


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
@dataclass
class ModbusConfig:
    host: str = "192.168.1.20"
    port: int = 502
    unit_id: int = 1
    timeout: float = 2.0             # keep short so a dead poll doesn't stall the dashboard
    retries: int = 1


@dataclass
class Snapshot:
    """One complete poll of the module."""
    timestamp: datetime
    pv_floats: List[float]
    pv_ints:   List[int]
    alarms:    List[List[bool]]      # 4 alarms x 8 channels
    ambient:   float
    ok:        bool = True
    error:     str = ""


# ----------------------------------------------------------------------
# Modbus client wrapper
# ----------------------------------------------------------------------
class AIME8UClient:
    def __init__(self, config: ModbusConfig):
        self.cfg = config
        self.client: Optional[ModbusTcpClient] = None

    # ---- connection lifecycle ----
    def connect(self) -> bool:
        """Open the TCP socket. Returns True on success, False on failure."""
        self.close()
        self.client = ModbusTcpClient(
            host=self.cfg.host,
            port=self.cfg.port,
            timeout=self.cfg.timeout,
            retries=self.cfg.retries,
        )
        return self.client.connect()

    def close(self) -> None:
        if self.client and self.client.connected:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    # ---- low-level read (handles pymodbus API variations) ----
    def _read_input(self, address: int, count: int) -> List[int]:
        if not self.connected:
            raise ConnectionException("Client not connected.")
        try:
            rsp = self.client.read_input_registers(
                address=address, count=count, device_id=self.cfg.unit_id
            )
        except TypeError:
            rsp = self.client.read_input_registers(
                address=address, count=count, slave=self.cfg.unit_id
            )
        if rsp.isError():
            if isinstance(rsp, ExceptionResponse):
                raise ModbusIOException(
                    f"Modbus exception code={rsp.exception_code} (0x{rsp.exception_code:02X})"
                )
            raise ModbusIOException(f"Read failed: {rsp}")
        return list(rsp.registers)

    # ---- one complete poll of the module ----
    def poll(self) -> Snapshot:
        pv_regs    = self._read_input(ADDR_PV_FLOAT_CH1, NUM_CHANNELS * 2)
        pv_ints    = self._read_input(ADDR_PV_INT16_CH1, NUM_CHANNELS)
        alarm_regs = self._read_input(ADDR_ALARM_BITMAP, 4)
        amb_raw    = self._read_input(ADDR_AMBIENT_TEMP, 1)[0]

        pv_floats = registers_to_floats(pv_regs, word_order="big")

        alarms = [
            [((reg >> bit) & 1) == 1 for bit in range(NUM_CHANNELS)]
            for reg in alarm_regs
        ]

        # ambient is signed INT16 with 0.1 deg resolution
        if amb_raw >= 0x8000:
            amb_raw -= 0x10000
        ambient = amb_raw / 10.0

        return Snapshot(
            timestamp=datetime.now(),
            pv_floats=pv_floats,
            pv_ints=pv_ints,
            alarms=alarms,
            ambient=ambient,
        )


# ----------------------------------------------------------------------
# Float decoding (no pymodbus.payload dependency)
# ----------------------------------------------------------------------
def registers_to_floats(registers: List[int], word_order: str = "big") -> List[float]:
    if len(registers) % 2 != 0:
        raise ValueError("need an even number of registers")
    out: List[float] = []
    for i in range(0, len(registers), 2):
        hi, lo = registers[i], registers[i + 1]
        if word_order == "little":
            hi, lo = lo, hi
        out.append(struct.unpack(">f", struct.pack(">HH", hi, lo))[0])
    return out


# ----------------------------------------------------------------------
# Display helpers
# ----------------------------------------------------------------------
def describe_pv_int(raw: int) -> str:
    if raw == PV_ERR_UNDER_RANGE: return "UNDER-RANGE"
    if raw == PV_ERR_OVER_RANGE:  return "OVER-RANGE"
    if raw == PV_ERR_SENSOR_OPEN: return "SENSOR-OPEN"
    return str(raw)


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def render(snap: Snapshot, cfg: ModbusConfig, cycle: int, interval: float) -> None:
    clear_screen()
    bar = "=" * 64
    print(bar)
    print(f" AIME 8U Live Dashboard   |   {cfg.host}:{cfg.port}   |   unit {cfg.unit_id}")
    print(f" Cycle #{cycle}   |   {snap.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
          f"   |   {interval:.1f} s interval")
    print(bar)

    if not snap.ok:
        print(f"\n  [!] Poll failed: {snap.error}\n")
        print(bar)
        return

    print(f"{'CH':<4}{'PV (float)':<14}{'PV (int16)':<14}"
          f"{'AL1':<5}{'AL2':<5}{'AL3':<5}{'AL4':<5}")
    print("-" * 64)
    for i in range(NUM_CHANNELS):
        pv_f = snap.pv_floats[i]
        pv_i = snap.pv_ints[i]
        a1, a2, a3, a4 = (snap.alarms[k][i] for k in range(4))
        # Show dashes for errored channels so the float column stays readable
        pv_f_str = "---" if pv_i in (PV_ERR_UNDER_RANGE,
                                     PV_ERR_OVER_RANGE,
                                     PV_ERR_SENSOR_OPEN) else f"{pv_f:.3f}"
        print(f"{i+1:<4}{pv_f_str:<14}{describe_pv_int(pv_i):<14}"
              f"{'ON' if a1 else '-':<5}"
              f"{'ON' if a2 else '-':<5}"
              f"{'ON' if a3 else '-':<5}"
              f"{'ON' if a4 else '-':<5}")
    print("-" * 64)
    print(f" Ambient (terminal block / CJC): {snap.ambient:.1f} C")
    print(bar)
    print(" Press Ctrl+C to stop.")


# ----------------------------------------------------------------------
# Continuous polling loop
# ----------------------------------------------------------------------
class MonitorApp:
    def __init__(self, cfg: ModbusConfig, interval: float = 1.0):
        self.cfg = cfg
        self.interval = interval
        self.rtu = AIME8UClient(cfg)
        self.running = True
        self.cycle = 0

    def stop(self, *_):
        self.running = False

    def _ensure_connection(self) -> bool:
        if self.rtu.connected:
            return True
        log.warning("Connecting to %s:%s ...", self.cfg.host, self.cfg.port)
        ok = self.rtu.connect()
        if ok:
            log.warning("Connected.")
        else:
            log.error("Connect failed; will retry next cycle.")
        return ok

    def run(self) -> int:
        # Handle Ctrl+C cleanly on both Windows and *nix
        signal.signal(signal.SIGINT, self.stop)
        try:
            signal.signal(signal.SIGTERM, self.stop)
        except (AttributeError, ValueError):
            pass  # SIGTERM not available on all Windows Python builds

        while self.running:
            cycle_start = time.monotonic()
            self.cycle += 1

            snap = Snapshot(
                timestamp=datetime.now(),
                pv_floats=[0.0] * NUM_CHANNELS,
                pv_ints=[0] * NUM_CHANNELS,
                alarms=[[False] * NUM_CHANNELS for _ in range(4)],
                ambient=0.0,
            )

            try:
                if not self._ensure_connection():
                    snap.ok = False
                    snap.error = "no TCP connection"
                else:
                    snap = self.rtu.poll()
            except (ConnectionException, TimeoutError, OSError) as e:
                snap.ok = False
                snap.error = f"connection lost: {e}"
                self.rtu.close()          # force reconnect next cycle
            except ModbusIOException as e:
                snap.ok = False
                snap.error = f"modbus error: {e}"
            except Exception as e:
                snap.ok = False
                snap.error = f"unexpected: {e}"
                log.exception("Unexpected error in poll loop")

            render(snap, self.cfg, self.cycle, self.interval)

            # Sleep for the remainder of the interval (no drift)
            elapsed = time.monotonic() - cycle_start
            remaining = self.interval - elapsed
            if remaining > 0:
                # break sleep into short chunks so Ctrl+C responds quickly
                end = time.monotonic() + remaining
                while self.running and time.monotonic() < end:
                    time.sleep(min(0.1, end - time.monotonic()))

        self.rtu.close()
        print("\nStopped.")
        return 0


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> int:
    cfg = ModbusConfig(host="192.168.1.20", port=502, unit_id=1)
    app = MonitorApp(cfg, interval=1.0)      # <-- change interval here
    return app.run()


if __name__ == "__main__":
    sys.exit(main())