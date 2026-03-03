#!/bin/bash
# Launch one or more MCP servers locally for development.
#
# Usage:
#   ./dev.sh spotify              # single server
#   ./dev.sh spotify calculator   # multiple servers
#   ./dev.sh --list               # show available servers + ports
#
# Servers run on 127.0.0.1 at their assigned port.
# Connect from the frontend: Settings → Server status → http://127.0.0.1:<port>/mcp

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Server → port mapping (must match deploy/setup-systemd.sh)
declare -A PORTS=(
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

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

list_servers() {
    echo -e "${BOLD}Available servers:${NC}"
    for name in $(echo "${!PORTS[@]}" | tr ' ' '\n' | sort); do
        echo -e "  ${CYAN}${name}${NC}  →  port ${PORTS[$name]}  →  http://127.0.0.1:${PORTS[$name]}/mcp"
    done
}

if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
    echo "Usage: ./dev.sh <server> [server ...]"
    echo "       ./dev.sh --list"
    echo ""
    list_servers
    exit 0
fi

if [[ "$1" == "--list" || "$1" == "-l" ]]; then
    list_servers
    exit 0
fi

# Ensure venv exists
if [[ ! -d .venv ]]; then
    echo -e "${CYAN}Creating venv...${NC}"
    uv sync
fi

PIDS=()

cleanup() {
    echo ""
    echo -e "${RED}Stopping servers...${NC}"
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

for name in "$@"; do
    port="${PORTS[$name]}"
    if [[ -z "$port" ]]; then
        echo -e "${RED}Unknown server: ${name}${NC}"
        echo "Run ./dev.sh --list to see available servers."
        exit 1
    fi

    echo -e "${GREEN}Starting ${name} on http://127.0.0.1:${port}/mcp${NC}"
    .venv/bin/python -m "servers.${name}" \
        --transport streamable-http \
        --host 127.0.0.1 \
        --port "$port" &
    PIDS+=($!)
done

echo ""
echo -e "${BOLD}All servers running. Ctrl+C to stop.${NC}"
wait
