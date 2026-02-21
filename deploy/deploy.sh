#!/usr/bin/env bash
# deploy.sh — Commit, push, pull latest, install deps, restart MCP server services
#
# Usage (from home LAN or on server):
#   ./deploy/deploy.sh              # Deploy all enabled servers
#   ./deploy/deploy.sh calculator   # Deploy specific server(s)
#   ./deploy/deploy.sh --status     # Show status of all servers
#
# From home LAN: commits local changes, pushes, SSHes into LXC 110 to deploy.
# On the server: pulls latest, installs deps, restarts services.

set -euo pipefail

SERVER_HOST="root@192.168.1.110"
REPO_DIR="/opt/mcp-servers"
SERVERS=("housekeeping" "calculator" "shell_control" "playwright" "spotify" "gdrive" "gmail" "calendar" "notes" "pdf" "monarch" "tv" "rag")

# --- Detect if we're running locally (not on the server) ---
if [[ ! -d "$REPO_DIR" ]]; then
    # Running locally — commit, push, then SSH to server
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    LOCAL_ROOT="$(dirname "$SCRIPT_DIR")"

    echo "=== Local deploy: commit & push, then remote deploy ==="

    cd "$LOCAL_ROOT"
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "=== Committing local changes ==="
        git add -A
        git commit -m "${1:-deploy: update mcp-servers}"
    else
        echo "No local changes to commit."
    fi

    echo "=== Pushing to origin ==="
    git push

    # Forward args to the remote script
    echo "=== SSHing into ${SERVER_HOST} to deploy ==="
    ssh "$SERVER_HOST" "${REPO_DIR}/deploy/deploy.sh $*"
    exit $?
fi

# --- Running on the server ---
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
