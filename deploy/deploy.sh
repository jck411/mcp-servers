#!/usr/bin/env bash
# deploy.sh — Pull latest, install deps, restart MCP server services
#
# Usage:
#   ./deploy/deploy.sh              # Deploy all enabled servers
#   ./deploy/deploy.sh calculator   # Deploy specific server(s)
#   ./deploy/deploy.sh --status     # Show status of all servers
#
# Run on the Proxmox LXC (192.168.1.110) or wherever servers are deployed.
# Can be run as root or mcp user (uses sudo for systemctl if needed).

set -euo pipefail

REPO_DIR="/opt/mcp-servers"
SERVERS=("calculator" "shell_control" "playwright" "spotify")

# Ensure uv is on PATH
export PATH="/root/.local/bin:/home/mcp/.local/bin:$PATH"

cd "$REPO_DIR"

# --status flag: just show current state
if [[ "${1:-}" == "--status" ]]; then
    echo "=== MCP Server Status ==="
    systemctl list-units 'mcp-server@*' --no-pager --no-legend 2>/dev/null || true
    echo ""
    for server in "${SERVERS[@]}"; do
        port_var="MCP_PORT_${server}"
        port=$(grep "^${port_var}=" .env 2>/dev/null | cut -d= -f2 || echo "?")
        if systemctl is-active --quiet "mcp-server@${server}" 2>/dev/null; then
            echo "  ✅ ${server} (port ${port})"
        else
            echo "  ❌ ${server} (port ${port})"
        fi
    done
    exit 0
fi

echo "=== Pulling latest code ==="
git pull --ff-only

echo "=== Installing dependencies ==="
if command -v uv &>/dev/null; then
    uv sync --extra all
else
    echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Override server list if specific servers requested
if [[ $# -gt 0 ]]; then
    SERVERS=("$@")
fi

echo ""
RESTART_CMD="systemctl"
if [[ "$(id -u)" -ne 0 ]]; then
    RESTART_CMD="sudo systemctl"
fi

failed=0
for server in "${SERVERS[@]}"; do
    unit="mcp-server@${server}"
    echo "=== Restarting ${unit} ==="
    if $RESTART_CMD restart "$unit"; then
        $RESTART_CMD --no-pager status "$unit" || true
    else
        echo "  ⚠️  Failed to restart ${unit}"
        $RESTART_CMD --no-pager status "$unit" || true
        failed=$((failed + 1))
    fi
    echo ""
done

if [[ $failed -gt 0 ]]; then
    echo "=== Deploy complete with ${failed} failure(s) ==="
    exit 1
else
    echo "=== Deploy complete — ${#SERVERS[@]} server(s) restarted ==="
fi
