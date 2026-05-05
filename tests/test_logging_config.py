"""Tests for shared.logging_config.

Covers logger creation, the @logged_tool decorator's success/error paths,
and the result-summarization helper.
"""

from __future__ import annotations

import logging

import pytest

from shared.logging_config import _summarize_result, get_logger, logged_tool


def test_get_logger_returns_logger():
    log = get_logger("tests.shared.logging_config")
    assert isinstance(log, logging.Logger)
    assert log.name == "tests.shared.logging_config"


def test_get_logger_idempotent_on_root_handler():
    # Calling get_logger many times must not stack handlers.
    for _ in range(5):
        get_logger("tests.shared.logging_config.idemp")
    root = logging.getLogger()
    stderr_handlers = [h for h in root.handlers
                       if isinstance(h, logging.StreamHandler)]
    # At most one stderr handler from us; uvicorn / pytest may add their own
    # but we never add more than one.
    assert sum(1 for h in stderr_handlers
               if getattr(h, "_from_logging_config", False)) <= 1


def test_summarize_result_dict_with_success_and_count():
    out = _summarize_result({"success": True, "count": 5, "results": [...]})
    assert "success=True" in out
    assert "count=5" in out


def test_summarize_result_dict_with_error():
    out = _summarize_result({"success": False, "error": "boom"})
    assert "success=False" in out
    assert "boom" in out


def test_summarize_result_non_dict():
    out = _summarize_result(["a", "b"])
    assert "result_type=list" in out


def test_summarize_result_truncates_long_error():
    long = "x" * 500
    out = _summarize_result({"success": False, "error": long})
    # Error string is capped (we cap at 120 chars).
    assert len(out) < 200


# ---------------------------------------------------------------------------
# logged_tool decorator
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_log(caplog):
    caplog.set_level(logging.DEBUG, logger="tests.logged_tool")
    return caplog


async def test_logged_tool_logs_success(captured_log):
    log = get_logger("tests.logged_tool")

    @logged_tool(log)
    async def my_tool(x: int) -> dict:
        return {"success": True, "count": x}

    result = await my_tool(7)
    assert result == {"success": True, "count": 7}

    msgs = [r.getMessage() for r in captured_log.records
            if r.name == "tests.logged_tool"]
    assert any("tool=my_tool" in m and "status=ok" in m for m in msgs)
    assert any("count=7" in m for m in msgs)


async def test_logged_tool_logs_exception_and_reraises(captured_log):
    log = get_logger("tests.logged_tool")

    @logged_tool(log)
    async def busted_tool() -> dict:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await busted_tool()

    msgs = [r.getMessage() for r in captured_log.records
            if r.name == "tests.logged_tool"]
    assert any("status=error" in m and "tool=busted_tool" in m for m in msgs)


async def test_logged_tool_includes_duration(captured_log):
    log = get_logger("tests.logged_tool")

    @logged_tool(log)
    async def quick() -> dict:
        return {"success": True}

    await quick()
    msgs = [r.getMessage() for r in captured_log.records
            if r.name == "tests.logged_tool"]
    assert any("duration_ms=" in m for m in msgs)


async def test_logged_tool_preserves_function_name():
    log = get_logger("tests.logged_tool")

    @logged_tool(log)
    async def my_named_tool() -> dict:
        return {"success": True}

    assert my_named_tool.__name__ == "my_named_tool"
