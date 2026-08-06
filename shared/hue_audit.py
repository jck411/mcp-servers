"""Best-effort audit wrapper for Hue MCP commands."""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from shared.hue_store import HueStore

logger = logging.getLogger(__name__)


def audited_hue_command(
    tool_name: str, *, record_result: bool = True
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Record command intent and outcome without making logging a control dependency."""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind_partial(*args, **kwargs)
            request = dict(bound.arguments)
            command_id: int | None = None
            try:
                with HueStore() as store:
                    command_id = store.start_command(tool_name, request)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not start Hue command audit: %s", exc)

            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                _finish(command_id, "error", error=repr(exc))
                raise

            recorded_result = str(result)[:2000] if record_result else "<redacted>"
            _finish(command_id, "completed", result=recorded_result)
            return result

        return wrapper

    return decorator


def _finish(
    command_id: int | None,
    outcome: str,
    *,
    result: str | None = None,
    error: str | None = None,
) -> None:
    if command_id is None:
        return
    try:
        with HueStore() as store:
            store.finish_command(command_id, outcome, result=result, error=error)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not finish Hue command audit: %s", exc)
