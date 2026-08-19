# mcp-servers

Standalone account and home-control MCP servers deployed to LXC 117 with
systemd.

## Layout

```text
servers/          MCP server modules
shared/           Shared helpers
tests/            Focused service tests
```

## Production services

| Server | Port | Unit |
|---|---:|---|
| `calendar` | 9004 | `mcp-server@calendar` |
| `gmail` | 9005 | `mcp-server@gmail` |
| `gdrive` | 9006 | `mcp-server@gdrive` |
| `monarch` | 9008 | `mcp-server@monarch` |
| `spotify` | 9010 | `mcp-server@spotify` |
| `tv` | 9013 | `mcp-server@tv` |
| `hue` | 9015 | `mcp-server@hue` |

## Local Setup

```bash
uv sync --extra all --extra dev
uv run pytest
```

Production deployment and verification are defined by the `mcp-accounts`
entry in `../NETWORK/deploy/registry.yml`.
