#!/usr/bin/env bash
# setup-systemd.sh — Install systemd units and per-server port configs
#
# This creates per-instance .env files so the template unit knows which port
# to use for each server. Systemd can't do "${MCP_PORT_%i}" variable
# composition, so we use EnvironmentFile=/opt/mcp-servers/.env.<instance>
#
# Usage:
#   sudo ./deploy/setup-systemd.sh            # Install Knowledge services
#   sudo ./deploy/setup-systemd.sh knowledge  # Install a specific service
#
# NOTE: Private/account/home-control servers live on LXC 117 (mcp-accounts).

set -euo pipefail

REPO_DIR="/opt/mcp-servers"

# Ensure dependencies are installed before starting servers
echo "=== Syncing dependencies ==="
export PATH="/root/.local/bin:/home/mcp/.local/bin:$PATH"
cd "$REPO_DIR" && uv sync --extra all

# Port map
declare -A PORT_MAP=(
    [knowledge]=9017
    [knowledge_api]=9018
)

# Default servers to enable
DEFAULT_SERVERS=("knowledge" "knowledge_api")

# Use provided servers or defaults
if [[ $# -gt 0 ]]; then
    SERVERS=("$@")
else
    SERVERS=("${DEFAULT_SERVERS[@]}")
fi

INSTALL_WIKI_TIMER=false
if [[ $# -eq 0 ]]; then
    INSTALL_WIKI_TIMER=true
else
    for server in "${SERVERS[@]}"; do
        if [[ "$server" == "knowledge" ]]; then
            INSTALL_WIKI_TIMER=true
        fi
    done
fi

echo "=== Installing systemd template ==="
cp "${REPO_DIR}/deploy/mcp-server@.service" /etc/systemd/system/
if [[ "$INSTALL_WIKI_TIMER" == true ]]; then
    cp "${REPO_DIR}/deploy/mcp-wiki-maintain.service" /etc/systemd/system/
    cp "${REPO_DIR}/deploy/mcp-wiki-maintain.timer" /etc/systemd/system/
fi
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
if [[ "$INSTALL_WIKI_TIMER" == true ]]; then
    echo "  Enabling mcp-wiki-maintain.timer..."
    systemctl enable --now mcp-wiki-maintain.timer
fi

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
if [[ "$INSTALL_WIKI_TIMER" == true ]]; then
    if systemctl is-active --quiet mcp-wiki-maintain.timer 2>/dev/null; then
        echo "  ✅ mcp-wiki-maintain.timer — active"
    else
        echo "  ❌ mcp-wiki-maintain.timer — not active"
        journalctl -u mcp-wiki-maintain.timer -n 5 --no-pager 2>/dev/null || true
    fi
fi

echo ""
echo "Done. Verify with: systemctl list-units 'mcp-server@*'; systemctl list-timers 'mcp-wiki-*'"
