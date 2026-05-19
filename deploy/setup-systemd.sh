#!/usr/bin/env bash
# setup-systemd.sh — Install systemd units and per-server port configs
#
# This creates per-instance .env files so the template unit knows which port
# to use for each server. Systemd can't do "${MCP_PORT_%i}" variable
# composition, so we use EnvironmentFile=/opt/mcp-servers/.env.<instance>
#
# Usage:
#   sudo ./deploy/setup-systemd.sh                           # Install services + enabled timer
#   sudo ENABLE_NIGHTLY=0 ./deploy/setup-systemd.sh knowledge # Install without starting nightly timer
#
# NOTE: Private/account/home-control servers live on LXC 117 (mcp-accounts).

set -euo pipefail

REPO_DIR="/opt/mcp-servers"
ENABLE_NIGHTLY="${ENABLE_NIGHTLY:-1}"

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

echo "=== Installing systemd template ==="
cp "${REPO_DIR}/deploy/mcp-server@.service" /etc/systemd/system/
cp "${REPO_DIR}/deploy/mcp-knowledge-nightly.service" /etc/systemd/system/
cp "${REPO_DIR}/deploy/mcp-knowledge-nightly.timer" /etc/systemd/system/
chmod +x "${REPO_DIR}/deploy/backup.sh" "${REPO_DIR}/deploy/healthcheck.sh"
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
    echo "  Enabling and restarting ${unit} (port ${port})..."
    systemctl enable "$unit"
    systemctl restart "$unit"
done
systemctl disable --now mcp-wiki-maintain.timer 2>/dev/null || true
if [[ "$ENABLE_NIGHTLY" == 1 ]]; then
    echo "  Enabling mcp-knowledge-nightly.timer..."
    systemctl enable --now mcp-knowledge-nightly.timer
else
    echo "  Installing mcp-knowledge-nightly.timer disabled because ENABLE_NIGHTLY=0..."
    systemctl disable --now mcp-knowledge-nightly.timer 2>/dev/null || true
fi

echo ""
echo "=== Removing legacy cron entries ==="
tmp_cron="$(mktemp)"
if crontab -l > "$tmp_cron" 2>/dev/null; then
    grep -Ev '/opt/mcp-servers/deploy/(backup|healthcheck)\.sh|mcp-wiki-maintain' \
        "$tmp_cron" > "${tmp_cron}.new" || true
    crontab "${tmp_cron}.new"
    echo "  Removed old backup/healthcheck/wiki cron entries"
else
    echo "  No root crontab found"
fi
rm -f "$tmp_cron" "${tmp_cron}.new"

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
if systemctl is-active --quiet mcp-knowledge-nightly.timer 2>/dev/null; then
    echo "  ✅ mcp-knowledge-nightly.timer — active"
else
    echo "  mcp-knowledge-nightly.timer — installed, not active"
    journalctl -u mcp-knowledge-nightly.timer -n 5 --no-pager 2>/dev/null || true
fi

echo ""
echo "Done. Verify with: systemctl list-units 'mcp-server@*'; systemctl list-timers --all '*knowledge*'"
