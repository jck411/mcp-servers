#!/usr/bin/env bash
# setup-systemd.sh — Install systemd units and per-server port configs
#
# This creates per-instance .env files so the template unit knows which port
# to use for each server. Systemd can't do "${MCP_PORT_%i}" variable
# composition, so we use EnvironmentFile=/opt/mcp-servers/.env.<instance>
#
# Usage:
#   sudo ./deploy/setup-systemd.sh              # Install all servers
#   sudo ./deploy/setup-systemd.sh calculator    # Install specific server

set -euo pipefail

REPO_DIR="/opt/mcp-servers"

# Port map — must match .env.example
declare -A PORT_MAP=(
    [shell_control]=9001
    [housekeeping]=9002
    [calculator]=9003
    [calendar]=9004
    [gmail]=9005
    [gdrive]=9006
    [pdf]=9007
    [monarch]=9008
    [notes]=9009
    [spotify]=9010
    [playwright]=9011
    [kiosk_clock_tools]=9012
    [tv]=9013
    [rag]=9014
)

# Default servers to enable
DEFAULT_SERVERS=("housekeeping" "calculator" "shell_control" "playwright" "spotify" "gdrive" "gmail" "calendar" "notes" "pdf" "monarch" "tv" "rag")

# Use provided servers or defaults
if [[ $# -gt 0 ]]; then
    SERVERS=("$@")
else
    SERVERS=("${DEFAULT_SERVERS[@]}")
fi

echo "=== Installing systemd template ==="
cp "${REPO_DIR}/deploy/mcp-server@.service" /etc/systemd/system/
systemctl daemon-reload

echo "=== Creating per-server environment files ==="
for server in "${SERVERS[@]}"; do
    port="${PORT_MAP[$server]:-}"
    if [[ -z "$port" ]]; then
        echo "  ⚠️  Unknown server: ${server} — skipping"
        continue
    fi

    env_file="${REPO_DIR}/.env.${server}"
    echo "MCP_PORT=${port}" > "$env_file"
    chown mcp:mcp "$env_file" 2>/dev/null || true
    echo "  ✅ ${env_file} → port ${port}"
done

echo ""
echo "=== Enabling and starting servers ==="
for server in "${SERVERS[@]}"; do
    port="${PORT_MAP[$server]:-}"
    if [[ -z "$port" ]]; then
        continue
    fi

    unit="mcp-server@${server}"
    echo "  Enabling ${unit} (port ${port})..."
    systemctl enable --now "$unit"
done

echo ""
echo "=== Status ==="
for server in "${SERVERS[@]}"; do
    unit="mcp-server@${server}"
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        echo "  ✅ ${unit} — running"
    else
        echo "  ❌ ${unit} — not running"
        journalctl -u "$unit" -n 5 --no-pager 2>/dev/null || true
    fi
done

echo ""
echo "Done. Verify with: systemctl list-units 'mcp-server@*'"
