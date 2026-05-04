#!/usr/bin/env bash
# deploy.sh — Deploy MCP servers to LXC CT 110 via Proxmox (auto-detects home vs remote)
#
# Both local (LAN) and tunnel (remote) modes go through the PVE host using
# `pct exec` so the execution path is identical regardless of location.
#
# Usage:
#   ./deploy/deploy.sh                      # Deploy all servers (auto-detect)
#   ./deploy/deploy.sh calendar             # Deploy specific server(s)
#   ./deploy/deploy.sh calendar spotify     # Deploy multiple
#   ./deploy/deploy.sh --local calendar     # Force home LAN mode (ssh root@192.168.1.11)
#   ./deploy/deploy.sh --tunnel calendar    # Force Cloudflare tunnel mode
#   ./deploy/deploy.sh --remote calendar    # No SSH — print pct exec commands to paste
#   ./deploy/deploy.sh --status             # Show server status
#   ./deploy/deploy.sh --no-push calendar   # Skip git commit/push

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
LXC_MCP=110
LXC_BACKEND=111
PVE_SSH="root@192.168.1.11"    # Proxmox host — reachable on home LAN
TUNNEL_SSH="proxmox-tunnel"    # Cloudflare tunnel alias — reachable from anywhere
MCP_REPO="/opt/mcp-servers"
BACKEND_REFRESH_URL="https://127.0.0.1:8000/api/mcp/servers/refresh"

# Port map — must stay in sync with deploy/setup-systemd.sh
declare -A PORT_MAP=(
    [shell_control]=9001   [calculator]=9003  [calendar]=9004
    [gmail]=9005           [gdrive]=9006      [pdf]=9007
    [monarch]=9008         [notes]=9009       [spotify]=9010
    [playwright]=9011      [tv]=9013          [rag]=9014
    [hue]=9015             [web_search]=9016  [knowledge]=9017
    [knowledge_api]=9018
)

ALL_SERVERS=(
    calculator shell_control playwright spotify
    gdrive gmail calendar notes pdf monarch tv rag hue web_search knowledge knowledge_api
)

# ── Parse args ────────────────────────────────────────────────────────────────
MODE=""
SKIP_PUSH=0
SHOW_STATUS=0
SERVERS=()

for arg in "$@"; do
    case "$arg" in
        --local)   MODE="local" ;;
        --tunnel)  MODE="tunnel" ;;
        --remote)  MODE="remote" ;;
        --status)  SHOW_STATUS=1 ;;
        --no-push) SKIP_PUSH=1 ;;
        --help|-h)
            head -18 "$0" | tail -16
            exit 0
            ;;
        *)         SERVERS+=("$arg") ;;
    esac
done

[[ ${#SERVERS[@]} -eq 0 ]] && SERVERS=("${ALL_SERVERS[@]}")

# ── Helpers ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[32m'
CYAN='\033[36m'
RESET='\033[0m'

banner() { echo -e "\n${BOLD}${CYAN}=== $1 ===${RESET}"; }
info()   { echo -e "${DIM}$1${RESET}"; }

# Run a command inside CT 110 via pct exec on the given PVE SSH host.
# Both local and tunnel use this — only the SSH host differs.
_pct_exec() {
    local host="$1" cmd="$2"
    local quoted; printf -v quoted '%q' "$cmd"
    ssh "$host" "pct exec ${LXC_MCP} -- bash -c ${quoted}"
}

# Run a command inside CT 111 (backend) via pct exec.
_pct_exec_backend() {
    local host="$1" cmd="$2"
    local quoted; printf -v quoted '%q' "$cmd"
    ssh "$host" "pct exec ${LXC_BACKEND} -- bash -c ${quoted}"
}

# ── Auto-detect ───────────────────────────────────────────────────────────────
detect_mode() {
    [[ -n "$MODE" ]] && return
    banner "Detecting connectivity"
    # Try PVE host directly on home LAN (fast, 3s timeout)
    if ssh -o ConnectTimeout=3 -o BatchMode=yes "$PVE_SSH" "true" &>/dev/null; then
        MODE="local"
        info "  PVE reachable at ${PVE_SSH} → local deploy"
    # Try Cloudflare tunnel (works from anywhere with internet)
    elif ssh -o ConnectTimeout=8 -o BatchMode=yes "$TUNNEL_SSH" "true" &>/dev/null 2>&1; then
        MODE="tunnel"
        info "  Cloudflare tunnel reachable → tunnel deploy"
    else
        MODE="remote"
        info "  SSH unreachable → Proxmox console mode (paste commands manually)"
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

# ── Status ────────────────────────────────────────────────────────────────────
show_status() {
    local status_cmd="for s in ${SERVERS[*]}; do printf '%-20s ' \"\$s\"; systemctl is-active mcp-server@\$s 2>/dev/null || echo inactive; done"
    local pct_cmd="pct exec ${LXC_MCP} -- bash -c 'for s in ${SERVERS[*]}; do printf \"%-20s \" \"\$s\"; systemctl is-active mcp-server@\$s 2>/dev/null || echo inactive; done'"

    if [[ "$MODE" == "local" ]]; then
        banner "Server status (via PVE ${PVE_SSH} → CT ${LXC_MCP})"
        _pct_exec "$PVE_SSH" "$status_cmd"
    elif [[ "$MODE" == "tunnel" ]]; then
        banner "Server status (via tunnel → CT ${LXC_MCP})"
        _pct_exec "$TUNNEL_SSH" "$status_cmd"
    else
        echo ""
        echo -e "${BOLD}Paste into Proxmox console (root@pve):${RESET}"
        echo ""
        echo "$pct_cmd"
    fi
}

# ── Build the remote command string ──────────────────────────────────────────
_build_run_cmd() {
    local cmds="export PATH=/root/.local/bin:/home/mcp/.local/bin:\$PATH && cd ${MCP_REPO} && git pull --ff-only && uv sync --extra all"

    # Write per-server port env files
    for server in "${SERVERS[@]}"; do
        local port="${PORT_MAP[$server]:-}"
        if [[ -n "$port" ]]; then
            cmds+=" && echo MCP_PORT=${port} > ${MCP_REPO}/.env.${server}"
        fi
    done

    # Kill any orphan process holding the port before restarting
    for server in "${SERVERS[@]}"; do
        local port="${PORT_MAP[$server]:-}"
        if [[ -n "$port" ]]; then
            cmds+=" && if command -v fuser >/dev/null 2>&1; then fuser -k ${port}/tcp 2>/dev/null || true; else pids=\$(ss -ltnp 'sport = :${port}' 2>/dev/null | sed -n 's/.*pid=\\([0-9][0-9]*\\).*/\\1/p'); [ -z \"\$pids\" ] || kill \$pids 2>/dev/null || true; fi"
        fi
    done

    # Restart services
    for server in "${SERVERS[@]}"; do
        cmds+=" && systemctl restart mcp-server@${server}"
    done

    # Poll each service up to 20s for it to become active
    for server in "${SERVERS[@]}"; do
        cmds+=" && echo '--- ${server} ---'"
        cmds+=" && for _poll in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do state=\$(systemctl is-active mcp-server@${server} 2>/dev/null || true); [ \"\$state\" = 'activating' ] || break; sleep 1; done"
        cmds+=" && systemctl is-active mcp-server@${server} 2>/dev/null"
    done

    echo "$cmds"
}

# ── Deploy (local and tunnel use the same pct exec path) ─────────────────────
deploy_via() {
    local ssh_host="$1" label="$2"
    local run_cmd; run_cmd="$(_build_run_cmd)"

    banner "Deploying to CT ${LXC_MCP} via ${label}"
    _pct_exec "$ssh_host" "$run_cmd"

    banner "Refreshing backend discovery (CT ${LXC_BACKEND})"
    _pct_exec_backend "$ssh_host" \
        "curl -sk --max-time 15 -X POST ${BACKEND_REFRESH_URL} -H 'Content-Type: application/json' -H 'Accept: application/json'" \
        | python3 -m json.tool 2>/dev/null || true

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
        status_cmds+="systemctl is-active mcp-server@${server} 2>/dev/null; "
    done

    echo ""
    echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}  Paste these commands into Proxmox console (root@pve):${RESET}"
    echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
    echo ""
    echo -e "${DIM}# Step 1: Pull code and install deps${RESET}"
    echo "pct exec ${LXC_MCP} -- bash -c 'export PATH=\"/root/.local/bin:\$PATH\" && cd ${MCP_REPO} && git pull --ff-only && uv sync --extra all'"
    echo ""
    echo -e "${DIM}# Step 2: Restart server(s): ${server_list}${RESET}"
    echo "pct exec ${LXC_MCP} -- bash -c '${restart_cmds}'"
    echo ""
    echo -e "${DIM}# Step 3: Check status${RESET}"
    echo "pct exec ${LXC_MCP} -- bash -c '${status_cmds}'"
    echo ""
    echo -e "${DIM}# Step 4: Refresh backend discovery${RESET}"
    echo "pct exec ${LXC_BACKEND} -- bash -c 'curl -sk --max-time 15 -X POST ${BACKEND_REFRESH_URL} -H \"Content-Type: application/json\" -H \"Accept: application/json\"'"
    echo ""
    echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
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
    deploy_via "$PVE_SSH" "PVE ${PVE_SSH}"
elif [[ "$MODE" == "tunnel" ]]; then
    deploy_via "$TUNNEL_SSH" "Cloudflare tunnel"
else
    deploy_remote
fi
