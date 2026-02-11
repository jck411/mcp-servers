"""Standalone housekeeping MCP server.

Exposes utility tools (time, echo) via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

The chat_history tool from the original Backend_FastAPI version is omitted
because it requires ChatRepository and other backend-only services.

Run:
    python -m servers.housekeeping --transport streamable-http --host 0.0.0.0 --port 9002
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from typing import Any, Literal

from fastmcp import FastMCP

from shared.time_context import (
    EASTERN_TIMEZONE,
    EASTERN_TIMEZONE_NAME,
    build_context_lines,
    create_time_snapshot,
    format_timezone_offset,
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9002

mcp = FastMCP("housekeeping")


@dataclass
class EchoResult:
    message: str
    uppercase: bool


@mcp.tool("test_echo")
async def test_echo(message: str, uppercase: bool = False) -> dict[str, Any]:
    """Return the message, optionally uppercased, for integration testing."""
    payload = message.upper() if uppercase else message
    return asdict(EchoResult(message=payload, uppercase=uppercase))


@mcp.tool(
    "current_time",
    description=(
        "Retrieve the current moment with precise Unix timestamps plus UTC and Eastern Time "
        "(ET/EDT) ISO formats. Use this whenever the conversation needs an up-to-date clock "
        "reference or time zone comparison."
    ),
)
async def current_time(format: Literal["iso", "unix"] = "iso") -> dict[str, Any]:
    """Return the current time with UTC and Eastern Time representations."""
    print(
        f"[HOUSEKEEPING-DEBUG] current_time called with format={format}",
        file=sys.stderr,
        flush=True,
    )

    snapshot = create_time_snapshot(EASTERN_TIMEZONE_NAME, fallback=EASTERN_TIMEZONE)
    eastern = snapshot.eastern

    if format == "iso":
        rendered = snapshot.iso_utc
    elif format == "unix":
        rendered = str(snapshot.unix_seconds)
    else:  # pragma: no cover - guarded by Literal
        raise ValueError(f"Unsupported format: {format}")

    offset = format_timezone_offset(eastern.utcoffset())
    context_lines = list(build_context_lines(snapshot))
    context_summary = "\n".join(context_lines)

    result = {
        "format": format,
        "value": rendered,
        "utc_iso": snapshot.iso_utc,
        "utc_unix": str(snapshot.unix_seconds),
        "utc_unix_precise": snapshot.unix_precise,
        "eastern_iso": eastern.isoformat(),
        "eastern_abbreviation": eastern.tzname(),
        "eastern_display": eastern.strftime("%a %b %d %Y %I:%M:%S %p %Z"),
        "eastern_offset": offset,
        "timezone": EASTERN_TIMEZONE_NAME,
        "context_lines": context_lines,
        "context_summary": context_summary,
    }

    print("[HOUSEKEEPING-DEBUG] current_time returning result", file=sys.stderr, flush=True)
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the housekeeping MCP server with the specified transport."""
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


def main() -> None:  # pragma: no cover - CLI helper
    import argparse

    parser = argparse.ArgumentParser(description="Housekeeping MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["mcp", "run", "main", "test_echo", "current_time"]
