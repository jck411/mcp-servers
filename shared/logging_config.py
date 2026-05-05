"""Shared logging helpers for MCP servers.

One-line setup so every server emits structured, level-filtered logs to
stderr. The format is journalctl-friendly: ISO timestamp, level, logger name,
message. Configure verbosity with the ``LOG_LEVEL`` environment variable
(default ``INFO``). Tool-call instrumentation lives in :func:`logged_tool`.

Typical use::

    from shared.logging_config import get_logger, logged_tool

    log = get_logger(__name__)
    log.info("starting up")

    @mcp.tool("server_do_thing")
    @logged_tool(log)
    async def do_thing(...): ...
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_CONFIGURED = False
_DEFAULT_LEVEL = "INFO"
_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def _configure_root_once() -> None:
    """Install one stderr handler on the root logger. Safe to call repeatedly."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("LOG_LEVEL", _DEFAULT_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    # Don't double-attach if something else (e.g. uvicorn) already configured.
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
               for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. Safe to call from any module at import time."""
    _configure_root_once()
    return logging.getLogger(name)


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def logged_tool(log: logging.Logger) -> Callable[[F], F]:
    """Decorate an async MCP tool function to log start/duration/errors.

    Logs at ``INFO`` on success and ``EXCEPTION`` on failure. Includes elapsed
    milliseconds and a one-line summary of the result type/size when it's a
    dict (the common MCP shape) — without dumping full payloads.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            tool = func.__name__
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                log.exception(
                    "tool=%s status=error duration_ms=%.1f error=%r",
                    tool, elapsed_ms, exc,
                )
                raise
            elapsed_ms = (time.monotonic() - start) * 1000
            summary = _summarize_result(result)
            log.info(
                "tool=%s status=ok duration_ms=%.1f %s",
                tool, elapsed_ms, summary,
            )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _summarize_result(result: Any) -> str:
    """Build a short, log-safe summary of an MCP tool result."""
    if not isinstance(result, dict):
        return f"result_type={type(result).__name__}"
    parts: list[str] = []
    success = result.get("success")
    if success is not None:
        parts.append(f"success={success}")
    for key in ("count", "fact_count", "chunks", "chunks_stored"):
        if key in result:
            parts.append(f"{key}={result[key]}")
    if "error" in result and result.get("success") is False:
        err = str(result["error"])[:120]
        parts.append(f'error="{err}"')
    return " ".join(parts) if parts else "result_type=dict"
