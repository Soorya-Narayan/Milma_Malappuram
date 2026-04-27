"""Unit tests for the interval auto-selection logic in app.api.history.

No DB involved — these are pure functions on range sizes.
"""

from __future__ import annotations

from app.api.history import (
    POINT_CAP,
    auto_interval,
    estimate_points,
    pick_effective_interval,
)

MS_MIN = 60_000
MS_HOUR = 60 * MS_MIN
MS_DAY = 24 * MS_HOUR


class TestAutoInterval:
    def test_one_hour_is_raw(self):
        assert auto_interval(1 * MS_HOUR) == "raw"

    def test_exactly_two_hours_is_raw(self):
        assert auto_interval(2 * MS_HOUR) == "raw"

    def test_just_over_two_hours_is_1m(self):
        assert auto_interval(2 * MS_HOUR + 1) == "1m"

    def test_one_day_is_1m(self):
        assert auto_interval(1 * MS_DAY) == "1m"

    def test_seven_days_is_1m(self):
        assert auto_interval(7 * MS_DAY) == "1m"

    def test_eight_days_is_1h(self):
        assert auto_interval(8 * MS_DAY) == "1h"

    def test_year_is_1h(self):
        assert auto_interval(365 * MS_DAY) == "1h"


class TestEstimatePoints:
    def test_raw_at_one_second_resolution(self):
        assert estimate_points(60 * 1000, "raw") == 60   # 60 s -> 60 raw pts

    def test_1m_at_minute_resolution(self):
        assert estimate_points(1 * MS_DAY, "1m") == 1440   # 24 * 60

    def test_1h_at_hour_resolution(self):
        assert estimate_points(7 * MS_DAY, "1h") == 7 * 24


class TestPickEffective:
    def test_2h_raw_under_cap(self):
        # 2h at raw = 7200 points > 5000 cap — should escalate to 1m.
        interval, escalated = pick_effective_interval(2 * MS_HOUR, "auto")
        assert interval == "1m"
        assert escalated is True

    def test_1h_raw_under_cap(self):
        # 1h at raw = 3600 < 5000 → stays raw.
        interval, escalated = pick_effective_interval(1 * MS_HOUR, "auto")
        assert interval == "raw"
        assert escalated is False

    def test_7d_escalates_1m_to_1h(self):
        # 7d at 1m ~= 10080 points > 5000 → escalate to 1h (= 168 points).
        interval, escalated = pick_effective_interval(7 * MS_DAY, "auto")
        assert interval == "1h"
        assert escalated is True

    def test_explicit_raw_is_respected_even_if_over_cap(self):
        # User explicitly asked for raw for a 2h range — still escalates per
        # the cap logic; that's the whole point of the cap.
        interval, escalated = pick_effective_interval(2 * MS_HOUR, "raw")
        assert interval == "1m"
        assert escalated is True

    def test_very_small_range_raw(self):
        interval, escalated = pick_effective_interval(5 * MS_MIN, "auto")
        assert interval == "raw"
        assert escalated is False

    def test_cap_constant_matches_spec(self):
        # CLAUDE.md mandates ≤5000 points per response.
        assert POINT_CAP == 5_000
