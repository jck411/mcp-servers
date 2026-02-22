#!/usr/bin/env bash
# deploy.sh — Deploy MCP servers: auto via SSH or print Proxmox console commands
#
# Usage:
#   ./deploy.sh                      # Deploy all servers (auto-detect SSH)
#   ./deploy.sh calendar             # Deploy specific server(s)
#   ./deploy.sh calendar spotify     # Deploy multiple specific servers
#   ./deploy.sh --remote calendar    # Force remote mode (Proxmox console instructions)
#   ./deploy.sh --local calendar     # Force SSH mode
#   ./deploy.sh --status             # Show server status
#   ./deploy.sh --no-push calendar   # Skip git commit/push (already pushed)

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
LXC_MCP=110
LXC_BACKEND=111
MCP_SSH="root@192.168.1.110"
BACKEND_SSH="root@192.168.1.111"
MCP_REPO="/opt/mcp-servers"
BACKEND_REFRESH_URL="https://127.0.0.1:8000/api/mcp/servers/refresh"

ALL_SERVERS=(
    housekeeping calculator shell_control playwright spotify
    gdrive gmail calendar notes pdf monarch tv rag
)

# ── Parse args ────────────────────────────────────────────────────────────────
MODE=""        # auto | local | remote
SKIP_PUSH=0
SHOW_STATUS=0
SERVERS=()

for arg in "$@"; do
    case "$arg" in
        --remote)  MODE="remote" ;;
        --local)   MODE="local" ;;
        --status)  SHOW_STATUS=1 ;;
        --no-push) SKIP_PUSH=1 ;;
        --help|-h)
            head -12 "$0" | tail -10
            exit 0
            ;;
        *)         SERVERS+=("$arg") ;;
    esac
done

[[ ${#SERVERS[@]} -eq 0 ]] && SERVERS=("${ALL_SERVERS[@]}")

# ── Helpers ───────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[32m'
YELLOW='\033[33m'
CYAN='\033[36m'
RESET='\033[0m'

banner() { echo -e "\n${BOLD}${CYAN}=== $1 ===${RESET}"; }
info()   { echo -e "${DIM}$1${RESET}"; }
warn()   { echo -e "${YELLOW}$1${RESET}"; }

detect_mode() {
    [[ -n "$MODE" ]] && return
    banner "Detecting connectivity"
    if ssh -o ConnectTimeout=2 -o BatchMode=yes "$MCP_SSH" "true" &>/dev/null; then
        MODE="local"
        info "  SSH reachable → automatic deploy"
    else
        MODE="remote"
        info "  SSH unreachable → Proxmox console mode"
    fi
}

# ── Git push ──────────────────────────────────────────────────────────────────
push_local() {
    cd "$REPO_ROOT"

    if [[ -n "$(git status --porcelain)" ]]; then
        banner "Committing local changes"
        git add -A
        git commit -m "deploy: update mcp-servers"
    else
        info "No local changes to commit."
    fi

    banner "Pushing to origin"
    git push origin master
}

# ── Status (works in both modes) ─────────────────────────────────────────────
show_status() {
    if [[ "$MODE" == "local" ]]; then
        banner "Server status (via SSH)"
        ssh "$MCP_SSH" "for s in ${SERVERS[*]}; do printf '%-20s ' \"\$s\"; systemctl is-active mcp-server@\$s 2>/dev/null || echo inactive; done"
    else
        echo ""
        echo -e "${BOLD}Paste into Proxmox console (root@pve):${RESET}"
        echo ""
        echo "pct exec ${LXC_MCP} -- bash -c 'for s in ${SERVERS[*]}; do printf \"%-20s \" \"\$s\"; systemctl is-active mcp-server@\$s 2>/dev/null || echo inactive; done'"
    fi
}

# ── Build restart + status commands for server list ───────────────────────────
build_server_cmds() {
    local cmds=""
    for server in "${SERVERS[@]}"; do
        cmds+="systemctl restart mcp-server@${server} && "
    done
    # Replace trailing " && " with "; " then add status checks
    cmds="${cmds% && } ; "
    for server in "${SERVERS[@]}"; do
        cmds+="echo '--- ${server} ---' && systemctl status mcp-server@${server} --no-pager -l 2>&1 | head -5 ; "
    done
    echo "$cmds"
}

# ── Local mode: SSH and do everything ─────────────────────────────────────────
deploy_local() {
    local restart_cmds
    restart_cmds="$(build_server_cmds)"

    banner "Deploying to LXC ${LXC_MCP} via SSH"
    ssh "$MCP_SSH" "export PATH=\"/root/.local/bin:/home/mcp/.local/bin:\$PATH\" && cd ${MCP_REPO} && git pull --ff-only && uv sync --extra all && ${restart_cmds}"

    banner "Refreshing backend discovery (LXC ${LXC_BACKEND})"
    ssh "$BACKEND_SSH" "curl -sk -X POST ${BACKEND_REFRESH_URL} -H 'Content-Type: application/json' -H 'Accept: application/json'" | python3 -m json.tool 2>/dev/null || true

    echo ""
    echo -e "${GREEN}${BOLD}Deploy complete — ${#SERVERS[@]} server(s)${RESET}"
}

# ── Remote mode: print Proxmox console commands ──────────────────────────────
deploy_remote() {
    local server_list="${SERVERS[*]}"
    local restart_cmds=""
    local status_cmds=""

    for server in "${SERVERS[@]}"; do
        restart_cmds+="systemctl restart mcp-server@${server} && "
    done
    restart_cmds="${restart_cmds% && }"

    for server in "${SERVERS[@]}"; do
        status_cmds+="systemctl status mcp-server@${server} --no-pager -l 2>&1 | head -5 ; "
    done

    cat <<EOF

${BOLD}════════════════════════════════════════════════════════════════${RESET}
${BOLD}  Paste these commands into Proxmox console (root@pve):${RESET}
${BOLD}════════════════════════════════════════════════════════════════${RESET}

${DIM}# Step 1: Pull code and install deps (LXC ${LXC_MCP})${RESET}
pct exec ${LXC_MCP} -- bash -c 'export PATH="/root/.local/bin:\$PATH" && cd ${MCP_REPO} && git pull --ff-only && uv sync --extra all'

${DIM}# Step 2: Restart server(s): ${server_list}${RESET}
pct exec ${LXC_MCP} -- bash -c '${restart_cmds}'

${DIM}# Step 3: Check status${RESET}
pct exec ${LXC_MCP} -- bash -c '${status_cmds}'

${DIM}# Step 4: Refresh backend discovery (LXC ${LXC_BACKEND})${RESET}
pct exec ${LXC_BACKEND} -- bash -c 'curl -sk -X POST ${BACKEND_REFRESH_URL} -H "Content-Type: application/json" -H "Accept: application/json"'

${BOLD}════════════════════════════════════════════════════════════════${RESET}
EOF
}

# ── Main ──────────────────────────────────────────────────────────────────────
detect_mode

if [[ $SHOW_STATUS -eq 1 ]]; then
    show_status
    exit 0
fi

if [[ $SKIP_PUSH -eq 0 ]]; then
    push_local
fi

if [[ "$MODE" == "local" ]]; then
    deploy_local
else
    deploy_remote
fi
