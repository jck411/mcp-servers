"""Tests for schedule computation and cross-domain signal detection."""

from datetime import date, datetime

import pytest

from servers.knowledge.schedule import (
    SCHEDULE_START,
    apply_overrides,
    availability_range,
    is_working,
    next_free_days,
    schedule_day,
)
from servers.knowledge.cross_domain import (
    detect_signals,
    extract_dates,
)


# ---------------------------------------------------------------------------
# schedule.py tests
# ---------------------------------------------------------------------------


class TestIsWorking:
    """Base schedule: 7 on (Thu-Wed) / 7 off, starting 2026-04-30."""

    def test_first_day_on(self):
        assert is_working(date(2026, 4, 30)) is True  # Thursday — first on day

    def test_last_on_day(self):
        assert is_working(date(2026, 5, 6)) is True  # Wednesday — day 7 of cycle

    def test_first_off_day(self):
        assert is_working(date(2026, 5, 7)) is False  # Thursday — first off day

    def test_last_off_day(self):
        assert is_working(date(2026, 5, 13)) is False  # Wednesday — day 14

    def test_second_cycle_on(self):
        assert is_working(date(2026, 5, 14)) is True  # Next on-cycle starts

    def test_today_may_22(self):
        # 2026-05-22 is a Friday. Days since start: 22. 22 % 14 = 8 → off week
        assert is_working(date(2026, 5, 22)) is False


class TestScheduleDay:
    def test_long_day(self):
        day = schedule_day(date(2026, 4, 30))  # Thursday — long day
        assert day["working"] is True
        assert day["shift"] == "long"
        assert "10:00 AM" in day["hours"]

    def test_short_day_wednesday(self):
        day = schedule_day(date(2026, 5, 6))  # Wednesday — short day
        assert day["working"] is True
        assert day["shift"] == "short"
        assert "2:00 PM" in day["hours"]

    def test_short_day_sunday(self):
        day = schedule_day(date(2026, 5, 3))  # Sunday — short day
        assert day["working"] is True
        assert day["shift"] == "short"

    def test_off_day(self):
        day = schedule_day(date(2026, 5, 8))  # off week
        assert day["working"] is False
        assert day.get("available") == "all day"

    def test_home_by_on_work_day(self):
        day = schedule_day(date(2026, 4, 30))
        assert "11" in day["home_by"]


class TestAvailabilityRange:
    def test_returns_correct_count(self):
        days = availability_range(date(2026, 5, 1), 7)
        assert len(days) == 7

    def test_transition_on_to_off(self):
        # Start from May 4 (Mon, on-week), look 7 days → should hit off week
        days = availability_range(date(2026, 5, 4), 7)
        on_count = sum(1 for d in days if d["working"])
        off_count = sum(1 for d in days if not d["working"])
        # May 4-6 on, May 7-10 off
        assert on_count == 3
        assert off_count == 4


class TestNextFreeDays:
    def test_finds_free_days(self):
        free = next_free_days(date(2026, 5, 1), count=3)
        assert len(free) == 3
        assert all(not d["working"] for d in free)

    def test_from_off_week(self):
        free = next_free_days(date(2026, 5, 8), count=2)
        assert len(free) == 2
        assert free[0]["date"] == "2026-05-08"


class TestApplyOverrides:
    def test_covering_for_jack_makes_off(self):
        days = [schedule_day(date(2026, 5, 1))]  # Thursday — normally on
        assert days[0]["working"] is True

        overrides = [
            {"key": "schedule_change_1_date", "value": "2026-05-01"},
            {"key": "schedule_change_1_covering_for", "value": "Jack"},
            {"key": "schedule_change_1_worker", "value": "Andison"},
        ]
        result = apply_overrides(days, overrides)
        assert result[0]["working"] is False
        assert "Andison" in result[0].get("override_reason", "")

    def test_jack_covering_makes_on(self):
        days = [schedule_day(date(2026, 5, 8))]  # off week
        assert days[0]["working"] is False

        overrides = [
            {"key": "schedule_change_1_date", "value": "2026-05-08"},
            {"key": "schedule_change_1_covering_for", "value": "Andison"},
            {"key": "schedule_change_1_worker", "value": "Jack"},
            {"key": "schedule_change_1_hours", "value": "1000-2230"},
        ]
        result = apply_overrides(days, overrides)
        assert result[0]["working"] is True

    def test_unrelated_override_ignored(self):
        days = [schedule_day(date(2026, 5, 1))]
        overrides = [
            {"key": "schedule_change_1_date", "value": "2026-06-15"},
            {"key": "schedule_change_1_covering_for", "value": "Jack"},
            {"key": "schedule_change_1_worker", "value": "Andison"},
        ]
        result = apply_overrides(days, overrides)
        assert result[0]["working"] is True  # unchanged


# ---------------------------------------------------------------------------
# cross_domain.py tests
# ---------------------------------------------------------------------------


class TestExtractDates:
    def test_iso_date(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("The date is 2026-06-04.", now)
        assert date(2026, 6, 4) in dates

    def test_month_day_year(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Scheduled for June 4, 2026.", now)
        assert date(2026, 6, 4) in dates

    def test_month_day_no_year(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Due on June 4.", now)
        assert date(2026, 6, 4) in dates

    def test_no_dates(self):
        now = datetime(2026, 5, 22)
        assert extract_dates("No dates here.", now) == []

    def test_multiple_dates(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Between 2026-06-03 and 2026-06-04.", now)
        assert len(dates) == 2


class TestDetectSignals:
    def _now(self):
        return datetime(2026, 5, 22, 21, 0, 0)

    def test_planning_query_triggers_scheduling(self):
        signals = detect_signals(
            "When should I prepare the yard?",
            results=[], facts=[], searched_domains=[], now=self._now(),
        )
        assert "scheduling" in signals
        assert signals["scheduling"]["is_planning_query"] is True

    def test_dates_in_results_trigger_scheduling(self):
        results = [{"content": "Confirmed date: June 4, 2026"}]
        signals = detect_signals(
            "Tell me about the landscaping project",
            results=results, facts=[], searched_domains=[], now=self._now(),
        )
        assert "scheduling" in signals
        assert "2026-06-04" in signals["scheduling"]["dates_found"]

    def test_no_scheduling_if_already_searched(self):
        signals = detect_signals(
            "When should I prepare?",
            results=[], facts=[], searched_domains=["work_schedule"],
            now=self._now(),
        )
        assert "scheduling" not in signals

    def test_people_detection(self):
        signals = detect_signals(
            "What about Sanja's schedule?",
            results=[], facts=[], searched_domains=[], now=self._now(),
        )
        assert "people" in signals
        assert "Sanja" in signals["people"]["names"]

    def test_outdoor_detection(self):
        signals = detect_signals(
            "yard work plans",
            results=[], facts=[], searched_domains=[], now=self._now(),
        )
        assert "outdoor" in signals
        assert signals["outdoor"]["suggest_weather"] is True

    def test_financial_detection(self):
        results = [{"content": "Total cost: $2,650"}]
        signals = detect_signals(
            "landscaping quote",
            results=results, facts=[], searched_domains=[], now=self._now(),
        )
        assert "financial" in signals

    def test_no_signals_for_simple_query(self):
        signals = detect_signals(
            "What is my blood type?",
            results=[], facts=[], searched_domains=[], now=self._now(),
        )
        assert len(signals) == 0
