#!/bin/bash
# Launch MCP servers locally for development.
#
# Usage:
#   ./dev.sh                      # interactive menu to pick servers
#   ./dev.sh spotify              # single server (skip menu)
#   ./dev.sh spotify calculator   # multiple servers (skip menu)
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
    [hue]=9015
    [knowledge]=9017
)

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

# Sorted server names (reused everywhere)
SORTED_NAMES=($(echo "${!PORTS[@]}" | tr ' ' '\n' | sort))

list_servers() {
    echo -e "${BOLD}Available servers:${NC}"
    for name in "${SORTED_NAMES[@]}"; do
        echo -e "  ${CYAN}${name}${NC}  →  port ${PORTS[$name]}  →  http://127.0.0.1:${PORTS[$name]}/mcp"
    done
}

pick_servers() {
    echo -e "${BOLD}Select servers to launch:${NC}"
    echo ""
    local i=1
    for name in "${SORTED_NAMES[@]}"; do
        printf "  ${CYAN}%2d${NC})  %-20s  port %s\n" "$i" "$name" "${PORTS[$name]}"
        ((i++))
    done
    echo ""
    printf "  ${YELLOW} a${NC})  All servers\n"
    echo ""
    echo -e "Enter numbers separated by spaces (e.g. ${CYAN}1 3 5${NC}), or ${YELLOW}a${NC} for all:"
    read -rp "> " selection

    if [[ -z "$selection" || "$selection" == "a" || "$selection" == "A" ]]; then
        SERVERS=("${SORTED_NAMES[@]}")
        return
    fi

    SERVERS=()
    for token in $selection; do
        if [[ "$token" =~ ^[0-9]+$ ]] && (( token >= 1 && token <= ${#SORTED_NAMES[@]} )); then
            SERVERS+=("${SORTED_NAMES[$((token - 1))]}")
        else
            echo -e "${RED}Invalid selection: ${token}${NC}"
            exit 1
        fi
    done

    if [[ ${#SERVERS[@]} -eq 0 ]]; then
        echo -e "${RED}No servers selected.${NC}"
        exit 1
    fi
}

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    echo "Usage: ./dev.sh                     # interactive menu"
    echo "       ./dev.sh spotify calculator   # launch specific servers"
    echo "       ./dev.sh --list               # show available servers + ports"
    echo ""
    list_servers
    exit 0
fi

if [[ "$1" == "--list" || "$1" == "-l" ]]; then
    list_servers
    exit 0
fi

# Load .env if present (export vars for child processes)
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

# Ensure venv exists
if [[ ! -d .venv ]]; then
    echo -e "${CYAN}Creating venv...${NC}"
    uv sync
fi

# Determine which servers to launch
if [[ $# -eq 0 ]]; then
    pick_servers
else
    SERVERS=()
    for name in "$@"; do
        if [[ -z "${PORTS[$name]}" ]]; then
            echo -e "${RED}Unknown server: ${name}${NC}"
            echo "Run ./dev.sh --list to see available servers."
            exit 1
        fi
        SERVERS+=("$name")
    done
fi

# Kill anything already listening on the ports we need
for name in "${SERVERS[@]}"; do
    port="${PORTS[$name]}"
    if [[ -z "$port" ]]; then
        continue
    fi
    existing=$(lsof -ti :"$port" 2>/dev/null || true)
    if [[ -n "$existing" ]]; then
        echo -e "${RED}Killing existing process on port ${port}...${NC}"
        echo "$existing" | xargs kill -9 2>/dev/null || true
    fi
done
sleep 0.5

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

for name in "${SERVERS[@]}"; do
    port="${PORTS[$name]}"

    echo -e "${GREEN}Starting ${name} on http://127.0.0.1:${port}/mcp  ${YELLOW}(auto-reload)${NC}"
    .venv/bin/watchfiles \
        ".venv/bin/python -m servers.${name} --transport streamable-http --host 127.0.0.1 --port ${port}" \
        servers/ shared/ &
    PIDS+=($!)
done

echo ""
echo -e "${BOLD}${#PIDS[@]} servers running. Ctrl+C to stop.${NC}"
wait
