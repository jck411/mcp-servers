"""MCP server exposing kiosk clock utilities (alarms, timers, etc.).

STATUS: DRAFT — not deployed. Ported from Backend_FastAPI but alarm CRUD
still depends on the backend API at runtime (the backend owns alarm state
and the kiosk display). Time-context dependency is fully standalone via
shared.time_context.

TODO before deploying:
  - Decide alarm storage: keep backend API proxy OR implement local storage
  - If local: add SQLite/JSON alarm store + WebSocket push to kiosk
  - Wire into deploy scripts, pyproject.toml, etc.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastmcp import FastMCP

from shared.time_context import (
    EASTERN_TIMEZONE,
    EASTERN_TIMEZONE_NAME,
    create_time_snapshot,
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9012

# Backend API URL (configurable via env)
# Default to HTTPS since backend uses SSL in production
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "https://127.0.0.1:8000")

mcp = FastMCP("kiosk-clock-tools")


def _parse_alarm_time(alarm_time: str, now_eastern: datetime) -> datetime | None:
    """Parse alarm time string into a datetime.

    Return None if parsing fails.
    """
    formats_to_try = [
        ("%H:%M", False),
        ("%H:%M:%S", True),
        ("%I:%M %p", False),
        ("%I:%M:%S %p", True),
    ]

    for fmt, has_seconds in formats_to_try:
        try:
            parsed = datetime.strptime(alarm_time.strip(), fmt)
            result = now_eastern.replace(
                hour=parsed.hour,
                minute=parsed.minute,
                second=parsed.second if has_seconds else 0,
                microsecond=0,
            )
            if result <= now_eastern:
                result = result + timedelta(days=1)
            return result
        except ValueError:
            continue

    try:
        result = datetime.fromisoformat(alarm_time.strip())
        if result.tzinfo is None:
            result = result.replace(tzinfo=EASTERN_TIMEZONE)
        return result
    except ValueError:
        pass

    return None


def _format_time_until(total_seconds: int) -> str:
    """Format seconds into a human-readable string."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0 and hours == 0:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    return ", ".join(parts) if parts else "now"


@mcp.tool(
    "kiosk_set_alarm",
    description=(
        "Set an alarm for a specific time. The alarm will trigger at the specified "
        "time and can include an optional label/message. Times are interpreted in "
        "Eastern Time (ET/EDT) unless otherwise specified. The alarm will play audio "
        "and show a visual notification on the kiosk display."
    ),
)
async def set_alarm(
    alarm_time: str,
    label: str = "Alarm",
) -> dict[str, Any]:
    """Set an alarm for the specified time.

    Args:
        alarm_time: The time to set the alarm for. Accepts formats like:
            - "HH:MM" (e.g., "14:30" for 2:30 PM)
            - "HH:MM:SS" (e.g., "14:30:00")
            - "7:30 AM" or "2:30 PM" (12-hour format)
            - ISO format (e.g., "2026-01-03T14:30:00")
        label: Optional label or message for the alarm.

    Returns:
        A dictionary containing the alarm details and confirmation.
    """
    snapshot = create_time_snapshot(EASTERN_TIMEZONE_NAME, fallback=EASTERN_TIMEZONE)
    now_eastern = snapshot.eastern

    parsed_time = _parse_alarm_time(alarm_time, now_eastern)

    if parsed_time is None:
        return {
            "success": False,
            "error": (
                f"Could not parse alarm time '{alarm_time}'. "
                "Please use formats like '14:30', '2:30 PM', '7:00 AM', or ISO format."
            ),
        }

    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=EASTERN_TIMEZONE)

    time_until = parsed_time - now_eastern
    total_seconds = int(time_until.total_seconds())

    if total_seconds < 0:
        return {
            "success": False,
            "error": "Alarm time is in the past. Please specify a future time.",
        }

    time_until_str = _format_time_until(total_seconds)

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.post(
                f"{BACKEND_API_URL}/api/alarms",
                json={
                    "alarm_time": parsed_time.isoformat(),
                    "label": label,
                },
            )

            if response.status_code in (200, 201):
                alarm_data = response.json()
                return {
                    "success": True,
                    "alarm": {
                        "alarm_id": alarm_data["alarm_id"],
                        "label": label,
                        "scheduled_time_iso": alarm_data["alarm_time"],
                        "scheduled_time_display": parsed_time.strftime(
                            "%a %b %d %Y %I:%M:%S %p %Z"
                        ),
                        "time_until": time_until_str,
                        "time_until_seconds": total_seconds,
                    },
                    "current_time": {
                        "iso": now_eastern.isoformat(),
                        "display": now_eastern.strftime("%a %b %d %Y %I:%M:%S %p %Z"),
                    },
                    "message": (
                        f"Alarm '{label}' set for {parsed_time.strftime('%I:%M %p %Z')} "
                        f"({time_until_str} from now)."
                    ),
                }
            else:
                return {
                    "success": False,
                    "error": (
                        f"Backend returned error: {response.status_code} - {response.text}"
                    ),
                }

    except httpx.ConnectError:
        return {
            "success": False,
            "error": "Could not connect to backend service. Is the server running?",
        }
    except httpx.TimeoutException:
        return {
            "success": False,
            "error": "Request to backend timed out.",
        }
    except Exception as exc:
        import traceback

        error_msg = str(exc) or type(exc).__name__
        return {
            "success": False,
            "error": f"Unexpected error: {error_msg}",
            "traceback": traceback.format_exc(),
        }


@mcp.tool(
    "kiosk_list_alarms",
    description="List all pending alarms that haven't fired yet.",
)
async def list_alarms() -> dict[str, Any]:
    """List all pending alarms."""
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.get(f"{BACKEND_API_URL}/api/alarms")

            if response.status_code == 200:
                alarms = response.json()
                if not alarms:
                    return {
                        "success": True,
                        "alarms": [],
                        "message": "No pending alarms.",
                    }

                return {
                    "success": True,
                    "alarms": alarms,
                    "count": len(alarms),
                    "message": f"{len(alarms)} pending alarm(s).",
                }
            else:
                return {
                    "success": False,
                    "error": f"Backend returned error: {response.status_code}",
                }

    except httpx.ConnectError:
        return {
            "success": False,
            "error": "Could not connect to backend service.",
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Unexpected error: {exc}",
        }


@mcp.tool(
    "kiosk_cancel_alarm",
    description="Cancel a pending alarm by its ID.",
)
async def cancel_alarm(alarm_id: str) -> dict[str, Any]:
    """Cancel a pending alarm.

    Args:
        alarm_id: The ID of the alarm to cancel.

    Returns:
        Confirmation of cancellation or error.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.delete(f"{BACKEND_API_URL}/api/alarms/{alarm_id}")

            if response.status_code == 200:
                return {
                    "success": True,
                    "alarm_id": alarm_id,
                    "message": f"Alarm {alarm_id} has been cancelled.",
                }
            elif response.status_code == 400:
                return {
                    "success": False,
                    "error": "Alarm could not be cancelled (may not be pending or not found).",
                }
            elif response.status_code == 404:
                return {
                    "success": False,
                    "error": f"Alarm {alarm_id} not found.",
                }
            else:
                return {
                    "success": False,
                    "error": f"Backend returned error: {response.status_code}",
                }

    except httpx.ConnectError:
        return {
            "success": False,
            "error": "Could not connect to backend service.",
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Unexpected error: {exc}",
        }


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = DEFAULT_HTTP_PORT,
) -> None:
    """Run the MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:
    """CLI entrypoint for the kiosk clock tools server."""
    parser = argparse.ArgumentParser(description="Kiosk Clock Tools MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind HTTP server to")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_HTTP_PORT, help="Port for HTTP server"
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()


__all__ = ["mcp", "run", "main", "DEFAULT_HTTP_PORT", "set_alarm", "list_alarms", "cancel_alarm"]
