#!/usr/bin/env bash
# deploy.sh — Pull latest, install deps, restart MCP server services
#
# Usage:
#   ./deploy/deploy.sh              # Deploy all servers
#   ./deploy/deploy.sh calculator   # Deploy specific server
#
# Run on the Proxmox host (or wherever servers are deployed).

set -euo pipefail

REPO_DIR="/opt/mcp-servers"
SERVERS=("calculator" "shell_control" "playwright")

cd "$REPO_DIR"

echo "=== Pulling latest code ==="
git pull --ff-only

echo "=== Installing dependencies ==="
uv sync --extra all

if [[ $# -gt 0 ]]; then
    # Deploy specific server(s)
    SERVERS=("$@")
fi

for server in "${SERVERS[@]}"; do
    unit="mcp-server@${server}"
    echo "=== Restarting ${unit} ==="
    sudo systemctl restart "$unit"
    sudo systemctl --no-pager status "$unit"
    echo ""
done

echo "=== Deploy complete ==="
