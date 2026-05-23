"""Tests for cross-domain signal detection."""

from datetime import datetime

import pytest

from servers.knowledge.cross_domain import (
    detect_signals,
    extract_dates,
)


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------


class TestExtractDates:
    def test_iso_date(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("The date is 2026-06-04.", now)
        assert "2026-06-04" in dates

    def test_month_day_year(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Scheduled for June 4, 2026.", now)
        assert "2026-06-04" in dates

    def test_month_day_no_year(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Due on June 4.", now)
        assert "2026-06-04" in dates

    def test_no_dates(self):
        now = datetime(2026, 5, 22)
        assert extract_dates("No dates here.", now) == []

    def test_multiple_dates(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Between 2026-06-03 and 2026-06-04.", now)
        assert len(dates) == 2
        assert dates == ["2026-06-03", "2026-06-04"]


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


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

    def test_people_in_facts(self):
        facts = [{"key": "swap_worker", "value": "Andison covers for Jack"}]
        signals = detect_signals(
            "schedule changes",
            results=[], facts=facts, searched_domains=[], now=self._now(),
        )
        assert "people" in signals
        assert "Andison" in signals["people"]["names"]

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

    def test_financial_not_duplicated(self):
        """If finances was already searched, don't signal again."""
        results = [{"content": "Total cost: $2,650"}]
        signals = detect_signals(
            "landscaping quote",
            results=results, facts=[], searched_domains=["finances"],
            now=self._now(),
        )
        assert "financial" not in signals

    def test_no_signals_for_simple_query(self):
        signals = detect_signals(
            "What is my blood type?",
            results=[], facts=[], searched_domains=[], now=self._now(),
        )
        assert len(signals) == 0

    def test_multiple_signals(self):
        """A yard planning question with dates should trigger scheduling + outdoor."""
        results = [{"content": "Dan arrives June 4, 2026. Cost: $2,650"}]
        signals = detect_signals(
            "When should I prepare the yard before Dan starts?",
            results=results, facts=[], searched_domains=[], now=self._now(),
        )
        assert "scheduling" in signals
        assert "outdoor" in signals
        assert "financial" in signals
        assert "people" in signals
        assert "Dan" in signals["people"]["names"]

    def test_best_day_triggers_planning(self):
        signals = detect_signals(
            "What's the best day to do this?",
            results=[], facts=[], searched_domains=[], now=self._now(),
        )
        assert "scheduling" in signals
        assert signals["scheduling"]["is_planning_query"] is True

    def test_dates_in_facts_trigger_scheduling(self):
        facts = [{"key": "confirmed_date", "value": "June 4, 2026"}]
        signals = detect_signals(
            "landscaping project details",
            results=[], facts=facts, searched_domains=[], now=self._now(),
        )
        assert "scheduling" in signals
        assert "2026-06-04" in signals["scheduling"]["dates_found"]
