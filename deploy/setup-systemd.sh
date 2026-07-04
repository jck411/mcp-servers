#!/usr/bin/env bash
# setup-systemd.sh - Install CT 110 MCP units and per-server port configs.

set -euo pipefail

REPO_DIR="/opt/mcp-servers"

echo "=== Syncing dependencies ==="
export PATH="/root/.local/bin:/home/mcp/.local/bin:$PATH"
cd "$REPO_DIR" && uv sync --extra all

declare -A PORT_MAP=(
    [web_search]=9016
)

if [[ $# -gt 0 ]]; then
    SERVERS=("$@")
else
    SERVERS=(web_search)
fi

echo "=== Installing systemd template ==="
cp "${REPO_DIR}/deploy/mcp-server@.service" /etc/systemd/system/
systemctl daemon-reload

echo "=== Verifying server names ==="
for server in "${SERVERS[@]}"; do
    if [[ "$server" == *-* ]]; then
        echo "  ${server}: use the Python module name with underscores" >&2
        exit 1
    fi
    if [[ -z "${PORT_MAP[$server]:-}" ]]; then
        echo "  ${server}: not configured for CT 110" >&2
        exit 1
    fi
    if ! "${REPO_DIR}/.venv/bin/python" -c "from importlib import import_module; import_module('servers.${server}')" 2>/dev/null; then
        echo "  servers.${server}: module not found" >&2
        exit 1
    fi
    echo "  servers.${server}"
done

echo "=== Creating per-server environment files ==="
for server in "${SERVERS[@]}"; do
    env_file="${REPO_DIR}/.env.${server}"
    echo "MCP_PORT=${PORT_MAP[$server]}" > "$env_file"
    chown mcp:mcp "$env_file" 2>/dev/null || true
    echo "  ${env_file} -> port ${PORT_MAP[$server]}"
done

echo "=== Enabling and starting servers ==="
for server in "${SERVERS[@]}"; do
    unit="mcp-server@${server}"
    echo "  Restarting ${unit}..."
    systemctl enable "$unit"
    systemctl restart "$unit"
done

echo "=== Status ==="
for server in "${SERVERS[@]}"; do
    unit="mcp-server@${server}"
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        echo "  ${unit} running"
    else
        echo "  ${unit} not running"
        journalctl -u "$unit" -n 5 --no-pager 2>/dev/null || true
    fi
done

echo "Done. Verify with: systemctl list-units 'mcp-server@*'"
