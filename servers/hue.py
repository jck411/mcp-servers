"""Thin MCP facade for Hue control, status, and indexed event history."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from shared.hue_commands import (
    activate_scene,
    all_off,
    identify,
    register,
    set_light,
    set_room,
    toggle_automation,
)
from shared.hue_log_tools import log_activity, log_recent, log_search, log_status
from shared.hue_queries import (
    bridge_info,
    list_automations,
    list_devices,
    list_lights,
    list_rooms,
    list_scenes,
    sensor_status,
)

DEFAULT_HTTP_PORT = 9015

mcp = FastMCP(
    "hue",
    instructions=(
        "Use live Hue tools for current state and indexed hue_log_* tools for history. "
        "Bridge events cannot identify Hue app or Alexa actors; command history only "
        "attributes changes made through this MCP server."
    ),
)

TOOLS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("hue_list_lights", list_lights),
    ("hue_list_rooms", list_rooms),
    ("hue_list_scenes", list_scenes),
    ("hue_list_devices", list_devices),
    ("hue_set_light", set_light),
    ("hue_set_room", set_room),
    ("hue_activate_scene", activate_scene),
    ("hue_sensor_status", sensor_status),
    ("hue_list_automations", list_automations),
    ("hue_toggle_automation", toggle_automation),
    ("hue_bridge_info", bridge_info),
    ("hue_register", register),
    ("hue_all_off", all_off),
    ("hue_identify", identify),
    ("hue_log_recent", log_recent),
    ("hue_log_status", log_status),
    ("hue_log_activity", log_activity),
    ("hue_log_search", log_search),
)

for tool_name, handler in TOOLS:
    mcp.tool(tool_name)(handler)


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the MCP server with STDIO or stateless streamable HTTP transport."""
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
    parser = argparse.ArgumentParser(description="Hue Lights MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "run",
    "main",
    "DEFAULT_HTTP_PORT",
    "list_lights",
    "list_rooms",
    "list_scenes",
    "list_devices",
    "set_light",
    "set_room",
    "activate_scene",
    "sensor_status",
    "list_automations",
    "toggle_automation",
    "bridge_info",
    "register",
    "all_off",
    "identify",
    "log_recent",
    "log_status",
    "log_activity",
    "log_search",
]
