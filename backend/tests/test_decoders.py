"""Pure-function tests for app.modbus.decoders.

No DB, no sockets, no pymodbus — these functions take integers in and
return integers/floats/lists out, so every assertion is a fixed bit
pattern from the AIME 8U manual.
"""

from __future__ import annotations

import math

import pytest

from app.modbus.decoders import (
    NUM_ALARMS,
    NUM_CHANNELS,
    PV_ERROR_OK,
    PV_ERROR_OPEN,
    PV_ERROR_OVER,
    PV_ERROR_UNDER,
    classify_pv_int,
    decode_alarm_bitmap,
    decode_ambient,
    pack_alarm_nibble,
    registers_to_floats,
)


# ---------------------------------------------------------------------------
# registers_to_floats — IEEE-754 bit pattern from the manual
# ---------------------------------------------------------------------------
class TestRegistersToFloats:
    def test_123_4_big_endian(self):
        # 123.4f = 0x42F6CCCD (per IEEE-754 single precision, big endian)
        out = registers_to_floats([0x42F6, 0xCCCD], word_order="big")
        assert len(out) == 1
        assert math.isclose(out[0], 123.4, rel_tol=1e-6)

    def test_zero_and_negative(self):
        # 0.0 -> 0x00000000, -1.0 -> 0xBF800000
        out = registers_to_floats([0x0000, 0x0000, 0xBF80, 0x0000], word_order="big")
        assert out == [0.0, -1.0]

    def test_little_word_order_swaps(self):
        # Same bytes, swapped word order -> different float
        big = registers_to_floats([0x42F6, 0xCCCD], word_order="big")[0]
        little = registers_to_floats([0x42F6, 0xCCCD], word_order="little")[0]
        assert big != little
        # And "little" is equivalent to swapping the words manually.
        assert math.isclose(
            little,
            registers_to_floats([0xCCCD, 0x42F6], word_order="big")[0],
            rel_tol=1e-9,
        )

    def test_odd_register_count_raises(self):
        with pytest.raises(ValueError):
            registers_to_floats([0x0001], word_order="big")


# ---------------------------------------------------------------------------
# classify_pv_int — error sentinels from the manual
# ---------------------------------------------------------------------------
class TestClassifyPvInt:
    def test_ok(self):
        pv, err = classify_pv_int(1234)
        assert err == PV_ERROR_OK
        assert pv == 1234.0

    def test_under_range(self):
        pv, err = classify_pv_int(-32768)
        assert err == PV_ERROR_UNDER
        assert pv is None

    def test_over_range(self):
        pv, err = classify_pv_int(32752)
        assert err == PV_ERROR_OVER
        assert pv is None

    def test_sensor_open(self):
        pv, err = classify_pv_int(32767)
        assert err == PV_ERROR_OPEN
        assert pv is None

    def test_value_near_sentinel_is_ok(self):
        # 32751 is one below OVER-RANGE — should stay ok.
        pv, err = classify_pv_int(32751)
        assert err == PV_ERROR_OK
        assert pv == 32751.0


# ---------------------------------------------------------------------------
# decode_alarm_bitmap — LSB = CH1
# ---------------------------------------------------------------------------
class TestDecodeAlarmBitmap:
    def test_all_clear(self):
        assert decode_alarm_bitmap(0x0000) == [False] * NUM_CHANNELS

    def test_all_set(self):
        assert decode_alarm_bitmap(0x00FF) == [True] * NUM_CHANNELS

    def test_ch1_only(self):
        out = decode_alarm_bitmap(0x0001)
        assert out[0] is True
        assert out[1:] == [False] * 7

    def test_ch8_only(self):
        out = decode_alarm_bitmap(0x0080)
        assert out[:7] == [False] * 7
        assert out[7] is True

    def test_upper_byte_ignored(self):
        # Only the lower 8 bits matter on the 8U.
        assert decode_alarm_bitmap(0xFF00) == [False] * NUM_CHANNELS


# ---------------------------------------------------------------------------
# decode_ambient — signed INT16, 0.1 °C resolution
# ---------------------------------------------------------------------------
class TestDecodeAmbient:
    def test_positive(self):
        assert decode_ambient(245) == pytest.approx(24.5)

    def test_zero(self):
        assert decode_ambient(0) == 0.0

    def test_negative_via_twos_complement(self):
        # -50 (signed) encoded as 0xFFCE (65486 unsigned) -> -5.0 °C
        assert decode_ambient(65486) == pytest.approx(-5.0)

    def test_extreme_negative(self):
        # 0x8001 (32769 unsigned) -> -32767 -> -3276.7 °C (nonsense but decodes)
        assert decode_ambient(0x8001) == pytest.approx(-3276.7)


# ---------------------------------------------------------------------------
# pack_alarm_nibble — compresses 4 alarms into a 4-bit int
# ---------------------------------------------------------------------------
class TestPackAlarmNibble:
    def test_none(self):
        assert pack_alarm_nibble([False, False, False, False]) == 0

    def test_al1_only(self):
        assert pack_alarm_nibble([True, False, False, False]) == 0b0001

    def test_al4_only(self):
        assert pack_alarm_nibble([False, False, False, True]) == 0b1000

    def test_all(self):
        assert pack_alarm_nibble([True, True, True, True]) == 0b1111

    def test_wrong_length(self):
        with pytest.raises(ValueError):
            pack_alarm_nibble([True, False, True])

    def test_length_matches_num_alarms(self):
        assert NUM_ALARMS == 4
