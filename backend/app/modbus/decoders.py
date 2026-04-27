"""Pure decoding functions for the AIME 8U register layout.

All logic mirrors `reference/aime8u_modbus_reader.py`. Kept pure (no I/O,
no pymodbus dependency) so unit tests can feed known byte patterns from
the manual and assert exact outputs.

Register map (from AIME 8U manual, Section 10):

    Address | FC | Type     | Description
    --------|----|----------|-------------------------------------------
      82    | 04 | INT16    | Ambient temperature (terminal block, CJC)
    1561..  | 04 | INT16    | PV for CH1..CH8 (scaled by resolution)
    1577..  | 04 | BITMAP16 | Alarm-1..Alarm-4 status (1 reg per alarm)
    2001..  | 04 | FLOAT32  | PV for CH1..CH8 (2 regs/channel, ABCD order)
"""

from __future__ import annotations

import struct

# ---------------------------------------------------------------------------
# Register addresses (raw Modbus per the manual). If a device returns
# "illegal data address", subtract 1 — some firmware uses 0-based addressing.
# ---------------------------------------------------------------------------
ADDR_AMBIENT_TEMP = 82
ADDR_PV_INT16_CH1 = 1561
ADDR_ALARM_BITMAP = 1577
ADDR_PV_FLOAT_CH1 = 2001

NUM_CHANNELS = 8
NUM_ALARMS = 4

# ---------------------------------------------------------------------------
# PV error sentinels — treat as NULL PV in the DB, not as numeric values.
# pv_error codes match the `readings.pv_error` column contract in CLAUDE.md:
#   0 = ok, 1 = under-range, 2 = over-range, 3 = sensor open, 99 = read fail
# ---------------------------------------------------------------------------
PV_ERR_UNDER_RANGE = -32768
PV_ERR_OVER_RANGE = 32752
PV_ERR_SENSOR_OPEN = 32767

PV_ERROR_OK = 0
PV_ERROR_UNDER = 1
PV_ERROR_OVER = 2
PV_ERROR_OPEN = 3
PV_ERROR_READ_FAIL = 99


def registers_to_floats(registers: list[int], word_order: str = "big") -> list[float]:
    """Decode consecutive 16-bit registers as IEEE-754 big-endian floats.

    AIME 8U uses `big` (high reg first, a.k.a. ABCD). `little` swaps word
    order (CDAB) — kept for compatibility with devices that reorder.
    """
    if len(registers) % 2 != 0:
        raise ValueError("need an even number of registers")
    out: list[float] = []
    for i in range(0, len(registers), 2):
        hi, lo = registers[i], registers[i + 1]
        if word_order == "little":
            hi, lo = lo, hi
        out.append(struct.unpack(">f", struct.pack(">HH", hi, lo))[0])
    return out


def decode_alarm_bitmap(reg: int) -> list[bool]:
    """Unpack one 16-bit alarm register into 8 per-channel booleans.

    Bit N (LSB-first) = alarm active on channel N+1. Upper 8 bits are
    unused on the 8U. Returned list is length 8, ordered CH1..CH8.
    """
    return [((reg >> bit) & 1) == 1 for bit in range(NUM_CHANNELS)]


def classify_pv_int(raw: int) -> tuple[float | None, int]:
    """Classify an INT16 PV reading as ok or one of three error states.

    Returns ``(pv_value, error_code)``. On error, ``pv_value`` is ``None``
    so the caller can write NULL to the DB. On success, ``pv_value`` is
    the raw integer cast to float — callers using the companion FLOAT32
    registers as the true PV will ignore this field.
    """
    if raw == PV_ERR_UNDER_RANGE:
        return (None, PV_ERROR_UNDER)
    if raw == PV_ERR_OVER_RANGE:
        return (None, PV_ERROR_OVER)
    if raw == PV_ERR_SENSOR_OPEN:
        return (None, PV_ERROR_OPEN)
    return (float(raw), PV_ERROR_OK)


def decode_ambient(raw_reg: int) -> float:
    """Decode the INT16 ambient/CJC register as signed, 0.1 °C resolution."""
    if raw_reg >= 0x8000:
        raw_reg -= 0x10000
    return raw_reg / 10.0


def pack_alarm_nibble(alarms_per_alarm_idx: list[bool]) -> int:
    """Compress a per-channel 4-alarm status into the `alarms` column.

    Input: list[bool] of length 4, ordered [AL1, AL2, AL3, AL4].
    Output: 4-bit integer AL4|AL3|AL2|AL1 (LSB=AL1). Matches the storage
    contract in CLAUDE.md: ``alarms INTEGER`` packs all four alarms for
    one (device, channel, ts) into a single column.
    """
    if len(alarms_per_alarm_idx) != NUM_ALARMS:
        raise ValueError(f"expected {NUM_ALARMS} alarm flags, got {len(alarms_per_alarm_idx)}")
    value = 0
    for idx, active in enumerate(alarms_per_alarm_idx):
        if active:
            value |= 1 << idx
    return value
