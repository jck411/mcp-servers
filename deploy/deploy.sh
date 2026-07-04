#!/usr/bin/env bash
# deploy.sh - Deploy CT 110 MCP services via Proxmox.

set -euo pipefail

LXC_MCP=110
LXC_BACKEND=111
PVE_SSH="root@192.168.1.11"
TUNNEL_SSH="proxmox-tunnel"
MCP_REPO="/opt/mcp-servers"
BACKEND_REFRESH_URL="https://127.0.0.1:8000/api/mcp/servers/refresh"

declare -A PORT_MAP=(
    [web_search]=9016
)

ALL_SERVERS=(web_search)

MODE=""
SKIP_PUSH=0
SHOW_STATUS=0
SERVERS=()

usage() {
    sed -n '1,18p' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --local) MODE="local" ;;
        --tunnel) MODE="tunnel" ;;
        --remote) MODE="remote" ;;
        --status) SHOW_STATUS=1 ;;
        --no-push) SKIP_PUSH=1 ;;
        --help|-h)
            usage
            exit 0
            ;;
        *) SERVERS+=("$arg") ;;
    esac
done

[[ ${#SERVERS[@]} -eq 0 ]] && SERVERS=("${ALL_SERVERS[@]}")

for server in "${SERVERS[@]}"; do
    if [[ -z "${PORT_MAP[$server]:-}" ]]; then
        echo "Unknown CT 110 server: ${server}" >&2
        echo "Configured servers: ${ALL_SERVERS[*]}" >&2
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

banner() { printf '\n=== %s ===\n' "$1"; }
info() { printf '%s\n' "$1"; }

_pct_exec() {
    local host="$1" cmd="$2"
    local quoted
    printf -v quoted '%q' "$cmd"
    ssh "$host" "pct exec ${LXC_MCP} -- bash -c ${quoted}"
}

_pct_exec_backend() {
    local host="$1" cmd="$2"
    local quoted
    printf -v quoted '%q' "$cmd"
    ssh "$host" "pct exec ${LXC_BACKEND} -- bash -c ${quoted}"
}

detect_mode() {
    [[ -n "$MODE" ]] && return
    banner "Detecting connectivity"
    if ssh -o ConnectTimeout=3 -o BatchMode=yes "$PVE_SSH" "true" &>/dev/null; then
        MODE="local"
        info "PVE reachable at ${PVE_SSH}"
    elif ssh -o ConnectTimeout=8 -o BatchMode=yes "$TUNNEL_SSH" "true" &>/dev/null 2>&1; then
        MODE="tunnel"
        info "Cloudflare tunnel reachable"
    else
        MODE="remote"
        info "SSH unreachable; printing Proxmox console commands"
    fi
}

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
    git push origin main
}

show_status() {
    local status_cmd="for s in ${SERVERS[*]}; do unit=mcp-server@\$s; printf '%-20s ' \"\$s\"; systemctl is-active \"\$unit\" 2>/dev/null || echo inactive; done"
    local pct_cmd="pct exec ${LXC_MCP} -- bash -c '$status_cmd'"

    if [[ "$MODE" == "local" ]]; then
        banner "Server status via ${PVE_SSH}"
        _pct_exec "$PVE_SSH" "$status_cmd"
    elif [[ "$MODE" == "tunnel" ]]; then
        banner "Server status via ${TUNNEL_SSH}"
        _pct_exec "$TUNNEL_SSH" "$status_cmd"
    else
        printf '%s\n' "$pct_cmd"
    fi
}

_build_run_cmd() {
    local cmds="export PATH=/root/.local/bin:/home/mcp/.local/bin:\$PATH && cd ${MCP_REPO} && git fetch origin main && git reset --hard origin/main && uv sync --extra all"

    for server in "${SERVERS[@]}"; do
        local port="${PORT_MAP[$server]}"
        cmds+=" && echo MCP_PORT=${port} > ${MCP_REPO}/.env.${server}"
        cmds+=" && if command -v fuser >/dev/null 2>&1; then fuser -k ${port}/tcp 2>/dev/null || true; else pids=\$(ss -ltnp 'sport = :${port}' 2>/dev/null | sed -n 's/.*pid=\\([0-9][0-9]*\\).*/\\1/p'); [ -z \"\$pids\" ] || kill \$pids 2>/dev/null || true; fi"
        cmds+=" && systemctl restart mcp-server@${server}"
        cmds+=" && echo '--- ${server} ---'"
        cmds+=" && for _poll in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do state=\$(systemctl is-active mcp-server@${server} 2>/dev/null || true); [ \"\$state\" = 'activating' ] || break; sleep 1; done"
        cmds+=" && systemctl is-active mcp-server@${server} 2>/dev/null"
    done

    echo "$cmds"
}

deploy_via() {
    local ssh_host="$1" label="$2"
    local run_cmd
    run_cmd="$(_build_run_cmd)"

    banner "Deploying to CT ${LXC_MCP} via ${label}"
    _pct_exec "$ssh_host" "$run_cmd"
    sleep 3

    banner "Refreshing backend discovery"
    local refresh_output
    if ! refresh_output="$(_pct_exec_backend "$ssh_host" \
        "curl -skS --fail --max-time 30 -X POST ${BACKEND_REFRESH_URL} -H 'Content-Type: application/json' -H 'Accept: application/json'")"; then
        echo "Backend MCP refresh failed. Clients may still have a stale tool list." >&2
        return 1
    fi
    if [[ -n "$refresh_output" ]]; then
        printf '%s\n' "$refresh_output" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$refresh_output"
    else
        info "Refresh succeeded with an empty response."
    fi

    banner "Deploy complete"
}

deploy_remote() {
    local restart_cmds=""
    local status_cmds=""

    for server in "${SERVERS[@]}"; do
        restart_cmds+="systemctl restart mcp-server@${server} && "
        status_cmds+="systemctl is-active mcp-server@${server} 2>/dev/null; "
    done
    restart_cmds="${restart_cmds% && }"

    printf '\nPaste into Proxmox console:\n\n'
    printf 'pct exec %s -- bash -c '\''export PATH="/root/.local/bin:$PATH" && cd %s && git fetch origin main && git reset --hard origin/main && uv sync --extra all'\''\n' "$LXC_MCP" "$MCP_REPO"
    printf 'pct exec %s -- bash -c '\''%s'\''\n' "$LXC_MCP" "$restart_cmds"
    printf 'pct exec %s -- bash -c '\''%s'\''\n' "$LXC_MCP" "$status_cmds"
    printf 'pct exec %s -- bash -c '\''curl -sk --max-time 15 -X POST %s -H "Content-Type: application/json" -H "Accept: application/json"'\''\n' "$LXC_BACKEND" "$BACKEND_REFRESH_URL"
}

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
    deploy_via "$TUNNEL_SSH" "$TUNNEL_SSH"
else
    deploy_remote
fi
