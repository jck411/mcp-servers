#!/usr/bin/env bash
# refresh.sh — Push local changes and restart MCP servers on LXC
#
# Usage:
#   ./refresh.sh              # Push and restart all servers
#   ./refresh.sh housekeeping # Push and restart specific server(s)
#   ./refresh.sh --status     # Show server status (no push)

set -euo pipefail

LXC_HOST="192.168.1.110"
LXC_USER="root"
REPO_PATH="/opt/mcp-servers"

# Status check only
if [[ "${1:-}" == "--status" ]]; then
    ssh "${LXC_USER}@${LXC_HOST}" "${REPO_PATH}/deploy/deploy.sh --status"
    exit 0
fi

# Commit and push if there are changes
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "=== Local changes detected, committing ==="
    git add -A
    git commit -m "Auto-commit before refresh" || true
fi

echo "=== Pushing to origin ==="
git push origin master

echo "=== Deploying to LXC (${LXC_HOST}) ==="
ssh "${LXC_USER}@${LXC_HOST}" "cd ${REPO_PATH} && git pull --ff-only && ${REPO_PATH}/deploy/deploy.sh $*"
