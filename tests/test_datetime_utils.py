"""Tests for shared.datetime_utils — timezone-aware date parsing."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from shared.datetime_utils import (
    parse_iso_time_string,
    parse_time_string,
)

EASTERN = ZoneInfo("America/New_York")
UTC = datetime.UTC


# ---------------------------------------------------------------------------
# parse_time_string — keyword resolution
# ---------------------------------------------------------------------------


class TestParseTimeStringKeywords:
    """Keywords must resolve relative to Eastern time, not UTC."""

    @freeze_time("2026-02-23T03:00:00", tz_offset=0)
    def test_tomorrow_evening(self):
        """At 10 PM Eastern (3 AM UTC Feb 23), 'tomorrow' → Feb 23."""
        result = parse_time_string("tomorrow")
        assert result == "2026-02-23T00:00:00Z"

    @freeze_time("2026-02-23T04:00:00", tz_offset=0)
    def test_tomorrow_late_evening(self):
        """At 11 PM Eastern (4 AM UTC next day), 'tomorrow' → still next Eastern day."""
        result = parse_time_string("tomorrow")
        assert result == "2026-02-23T00:00:00Z"

    @freeze_time("2026-02-23T04:00:00", tz_offset=0)
    def test_today_late_evening(self):
        """At 11 PM Eastern (4 AM UTC next day), 'today' → still the same Eastern day."""
        result = parse_time_string("today")
        assert result == "2026-02-22T00:00:00Z"

    @freeze_time("2026-02-22T17:00:00", tz_offset=0)
    def test_today_midday(self):
        """At noon Eastern, 'today' → Feb 22."""
        result = parse_time_string("today")
        assert result == "2026-02-22T00:00:00Z"

    @freeze_time("2026-02-22T17:00:00", tz_offset=0)
    def test_yesterday(self):
        result = parse_time_string("yesterday")
        assert result == "2026-02-21T00:00:00Z"

    @freeze_time("2026-02-22T17:00:00", tz_offset=0)
    def test_next_week(self):
        result = parse_time_string("next_week")
        assert result == "2026-03-01T00:00:00Z"

    @freeze_time("2026-02-22T17:00:00", tz_offset=0)
    def test_next_month(self):
        result = parse_time_string("next_month")
        assert result == "2026-03-01T00:00:00Z"

    @freeze_time("2026-02-22T17:00:00", tz_offset=0)
    def test_next_year(self):
        result = parse_time_string("next_year")
        assert result == "2027-02-22T00:00:00Z"

    @freeze_time("2026-02-23T04:59:00", tz_offset=0)
    def test_boundary_just_before_midnight_eastern(self):
        """At 11:59 PM Eastern (4:59 AM UTC), 'today' is still Feb 22."""
        result = parse_time_string("today")
        assert result == "2026-02-22T00:00:00Z"

    @freeze_time("2026-02-23T05:00:00", tz_offset=0)
    def test_boundary_midnight_eastern(self):
        """At midnight Eastern (5 AM UTC), 'today' flips to Feb 23."""
        result = parse_time_string("today")
        assert result == "2026-02-23T00:00:00Z"


# ---------------------------------------------------------------------------
# parse_time_string — non-keyword inputs
# ---------------------------------------------------------------------------


class TestParseTimeStringPassthrough:
    """ISO dates and datetimes pass through correctly."""

    def test_iso_date(self):
        assert parse_time_string("2026-02-23") == "2026-02-23T00:00:00Z"

    def test_none(self):
        assert parse_time_string(None) is None

    def test_empty(self):
        assert parse_time_string("") is None

    def test_unrecognized_string_returned_as_is(self):
        assert parse_time_string("not-a-date") == "not-a-date"

    def test_naive_datetime_treated_as_eastern(self):
        """A naive datetime like '2026-02-23T09:00:00' → date portion Feb 23."""
        result = parse_time_string("2026-02-23T09:00:00")
        assert result == "2026-02-23T00:00:00Z"

    def test_aware_datetime_preserves_date(self):
        """An aware datetime with Eastern offset → correct date."""
        result = parse_time_string("2026-02-23T09:00:00-05:00")
        assert result == "2026-02-23T00:00:00Z"


# ---------------------------------------------------------------------------
# parse_iso_time_string — event time normalization
# ---------------------------------------------------------------------------


class TestParseIsoTimeString:
    """Naive datetimes must be interpreted as Eastern, not UTC."""

    def test_none(self):
        assert parse_iso_time_string(None) is None

    def test_date_only_passthrough(self):
        """Date-only strings are returned as-is (for all-day events)."""
        assert parse_iso_time_string("2026-02-23") == "2026-02-23"

    def test_naive_datetime_as_eastern(self):
        """'2026-02-23T09:00:00' → 9 AM Eastern = 2 PM UTC (EST offset -5)."""
        result = parse_iso_time_string("2026-02-23T09:00:00")
        assert result == "2026-02-23T14:00:00Z"

    def test_naive_datetime_summer_edt(self):
        """'2026-06-15T09:00:00' → 9 AM Eastern = 1 PM UTC (EDT offset -4)."""
        result = parse_iso_time_string("2026-06-15T09:00:00")
        assert result == "2026-06-15T13:00:00Z"

    def test_utc_z_suffix_passthrough(self):
        """Already UTC → returned unchanged."""
        assert parse_iso_time_string("2026-02-23T14:00:00Z") == "2026-02-23T14:00:00Z"

    def test_explicit_offset_converted(self):
        """'-05:00' offset → converted to UTC."""
        result = parse_iso_time_string("2026-02-23T09:00:00-05:00")
        assert result == "2026-02-23T14:00:00Z"

    def test_explicit_positive_offset(self):
        """'+02:00' offset → converted to UTC."""
        result = parse_iso_time_string("2026-02-23T14:00:00+02:00")
        assert result == "2026-02-23T12:00:00Z"
