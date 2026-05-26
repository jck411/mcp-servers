"""Tests for cross-domain signal detection."""

from datetime import datetime

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

    def test_multiple_dates_sorted(self):
        now = datetime(2026, 5, 22)
        dates = extract_dates("Between 2026-06-03 and 2026-06-04.", now)
        assert dates == ["2026-06-03", "2026-06-04"]


# ---------------------------------------------------------------------------
# Signal detection — no hardcoded domains or servers
# ---------------------------------------------------------------------------


class TestDetectSignals:
    def _now(self):
        return datetime(2026, 5, 22, 21, 0, 0)

    def test_planning_query_triggers_scheduling(self):
        signals = detect_signals(
            "When should I prepare the yard?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "scheduling" in names

    def test_scheduling_has_enrichment_query(self):
        signals = detect_signals(
            "When should I do this?",
            results=[], facts=[], now=self._now(),
        )
        sched = next(s for s in signals if s["name"] == "scheduling")
        assert sched["enrichment_query"]  # non-empty query string
        assert isinstance(sched["enrichment_query"], str)

    def test_dates_in_results_trigger_scheduling(self):
        results = [{"content": "Confirmed date: June 4, 2026"}]
        signals = detect_signals(
            "Tell me about the landscaping project",
            results=results, facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "scheduling" in names
        sched = next(s for s in signals if s["name"] == "scheduling")
        assert "2026-06-04" in sched["meta"]["dates_found"]

    def test_dates_in_facts_trigger_scheduling(self):
        facts = [{"key": "confirmed_date", "value": "June 4, 2026"}]
        signals = detect_signals(
            "landscaping project details",
            results=[], facts=facts, now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "scheduling" in names

    def test_outdoor_detection(self):
        signals = detect_signals(
            "yard work plans",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "outdoor" in names

    def test_outdoor_has_tool_hint(self):
        signals = detect_signals(
            "yard work plans",
            results=[], facts=[], now=self._now(),
        )
        outdoor = next(s for s in signals if s["name"] == "outdoor")
        assert outdoor["tool_hint"]
        assert "weather" in outdoor["tool_hint"].lower()

    def test_financial_detection(self):
        results = [{"content": "Total cost: $2,650"}]
        signals = detect_signals(
            "landscaping quote",
            results=results, facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "financial" in names

    def test_no_signals_for_simple_query(self):
        signals = detect_signals(
            "What is my favorite color?",
            results=[], facts=[], now=self._now(),
        )
        assert len(signals) == 0

    def test_health_detection(self):
        signals = detect_signals(
            "What is my blood type?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "health" in names

    def test_people_detection(self):
        signals = detect_signals(
            "Who is my coworker on night shift?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "people" in names

    def test_project_detection(self):
        signals = detect_signals(
            "What is the status of my homelab project?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "projects" in names

    def test_transport_detection(self):
        signals = detect_signals(
            "When is my car inspection due?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "transport" in names

    def test_task_detection(self):
        signals = detect_signals(
            "What tasks are overdue?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "tasks" in names

    def test_multiple_signals(self):
        """A yard planning question with money should trigger multiple signals."""
        results = [{"content": "Dan arrives June 4, 2026. Cost: $2,650"}]
        signals = detect_signals(
            "When should I prepare the yard before Dan starts?",
            results=results, facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "scheduling" in names
        assert "outdoor" in names
        assert "financial" in names

    def test_no_hardcoded_domain_names(self):
        """Signal dicts should not contain hardcoded domain names."""
        results = [{"content": "June 4, 2026. Cost: $2,650"}]
        signals = detect_signals(
            "When should I prepare the yard?",
            results=results, facts=[], now=self._now(),
        )
        for signal in signals:
            # Signals should have enrichment_query (a search string),
            # not search_domains (hardcoded domain list)
            assert "search_domains" not in signal

    def test_tool_hints_are_capability_based(self):
        """Tool hints should describe capabilities, not name specific servers."""
        signals = detect_signals(
            "When should I prepare the yard?",
            results=[], facts=[], now=self._now(),
        )
        for signal in signals:
            hint = signal.get("tool_hint") or ""
            # Should not reference specific MCP server names or ports
            assert "9004" not in hint
            assert "9017" not in hint
            assert "mcp-" not in hint.lower()

    def test_best_day_triggers_planning(self):
        signals = detect_signals(
            "What's the best day to do this?",
            results=[], facts=[], now=self._now(),
        )
        names = [s["name"] for s in signals]
        assert "scheduling" in names
