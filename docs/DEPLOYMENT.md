# Deployment

How to deploy the Knowledge services in this repo to LXC CT 110
(`192.168.1.110`).

Private/account/home-control MCPs (`calendar`, `gmail`, `gdrive`, `monarch`,
`spotify`, `tv`, `hue`) use the same codebase but run as `mcp-accounts` on LXC
CT 117. Deploy those through `NETWORK/deploy/registry.yml`, not this script.

---

## Quick reference

```bash
# Deploy one Knowledge service (auto-detects local/tunnel/console)
./deploy/deploy.sh knowledge

# Deploy both Knowledge services
./deploy/deploy.sh knowledge knowledge_api

# Deploy all services managed by this script
./deploy/deploy.sh

# Already pushed? Skip the git commit/push step
./deploy/deploy.sh --no-push knowledge_api

# Check what's running
./deploy/deploy.sh --status
```

---

## How auto-detection works

Both local and tunnel modes go through the **PVE host** using `pct exec 110`, so the execution path inside the LXC is identical regardless of where you run the script from. Only the SSH hop differs.

| Priority | Mode | SSH target | How |
|----------|------|------------|-----|
| 1 | **local** | `root@192.168.1.11` (PVE, 3 s timeout) | Direct SSH on home LAN |
| 2 | **tunnel** | `proxmox-tunnel` (8 s timeout) | Cloudflare tunnel from anywhere |
| 3 | **remote/console** | — | Prints `pct exec` commands to paste manually |

Force a specific mode with `--local`, `--tunnel`, or `--remote`.

---

## What deploy does (steps 1–6)

1. **Push** — commits any dirty local files and runs `git push origin main` (skip with `--no-push`)
2. **Reset code** — SSHs into CT 110, runs `git fetch origin main`, resets tracked files to `origin/main`, then runs `uv sync --extra all`. Runtime/untracked folders such as `credentials/`, `logs/`, `data/`, and `knowledge/` are left alone.
3. **Port file** — writes `/opt/mcp-servers/.env.<server>` containing `MCP_PORT=<port>`
4. **Orphan kill** — `fuser -k <port>/tcp` to free the port before restart
5. **Restart + poll** — `systemctl restart mcp-server@<server>`, polls up to 20 s for `active`
6. **Backend refresh** — pokes LXC 111 to refresh the chat-backend MCP discovery list. If this fails, deploy exits non-zero because chat-backend may still have a stale tool list.

LibreChat is separate: it connects directly from LXC 115 using `librechat-config/librechat.yaml`. Restart LibreChat's `api` container after Knowledge MCP tool changes.

---

## Deploying from home (local LAN)

SSHes to PVE at `root@192.168.1.11`, then `pct exec 110` into the LXC.

```bash
./deploy/deploy.sh knowledge knowledge_api
./deploy/deploy.sh --no-push knowledge_api  # code already pushed
./deploy/deploy.sh --local knowledge        # force local mode
```

---

## Deploying from remote (away from home)

Requires `cloudflared` installed locally, the `proxmox-tunnel` Host in `~/.ssh/config`, and the Cloudflare Access service token wired into a wrapper script. Auto-detected — no flags needed.

`ssh.jackshome.com` is fronted by the Cloudflare Access app **"Proxmox SSH"** with a non-identity policy that requires service token `mcp-servers-ssh`. PVE itself only accepts SSH keys (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`). The wrapper script injects the service-token headers before cloudflared dials the SSH origin.

```
# ~/.ssh/config entry:
Host proxmox-tunnel
    HostName ssh.jackshome.com
    User root
    ProxyCommand /home/jack/.ssh/cloudflared-access-ssh.sh %h
    StrictHostKeyChecking no

# ~/.ssh/cloudflared-access-ssh.sh (chmod 700)
#!/usr/bin/env bash
set -euo pipefail
. "$HOME/.config/cloudflared/access-tokens.env"
export TUNNEL_SERVICE_TOKEN_ID TUNNEL_SERVICE_TOKEN_SECRET
exec cloudflared access ssh --hostname "$1"

# ~/.config/cloudflared/access-tokens.env (chmod 600)
# Values stored in the shared ~/REPOS/symlinked-env/.env as
#   PROXMOX_SSH_CF_ACCESS_CLIENT_ID / PROXMOX_SSH_CF_ACCESS_CLIENT_SECRET
TUNNEL_SERVICE_TOKEN_ID=<client_id>.access
TUNNEL_SERVICE_TOKEN_SECRET=<client_secret>
```

Check: `which cloudflared && cloudflared version`

```bash
./deploy/deploy.sh knowledge
# Internally: ssh proxmox-tunnel 'pct exec 110 -- bash -c "…"'
```

---

## When SSH is unreachable (Proxmox console)

```bash
./deploy/deploy.sh --remote knowledge
```

Prints three `pct exec` blocks to paste into the Proxmox web console at `https://proxmox.jackshome.com → CT 110 → Console`:

```
# Step 1: Reset tracked code + sync
pct exec 110 -- bash -c 'cd /opt/mcp-servers && git fetch origin main && git reset --hard origin/main && uv sync --extra all'

# Step 2: Restart
pct exec 110 -- bash -c 'systemctl restart mcp-server@knowledge'

# Step 3: Check status
pct exec 110 -- bash -c 'systemctl is-active mcp-server@knowledge'
```

---

## Adding a new Knowledge service

1. Create `servers/<name>.py` following the pattern in an existing server
2. Add `[name]=<port>` to `PORT_MAP` in both `deploy/deploy.sh` and `deploy/setup-systemd.sh`
3. Add the name to `ALL_SERVERS` in `deploy/deploy.sh` and `DEFAULT_SERVERS` in `deploy/setup-systemd.sh`
4. Add any extra pip packages to `pyproject.toml` under `[project.optional-dependencies]` and add to the `all` group
5. Document the port in `README.md` or the relevant deploy docs if it is user-facing
6. Deploy: `./deploy/deploy.sh <name>`

For private/account/home-control MCPs, add the service to the `mcp-accounts`
deployment path in `NETWORK/deploy/registry.yml` instead.

---

## Updating the systemd unit file

`deploy/mcp-server@.service` is **not** copied automatically by `deploy.sh`. Copy it manually after changes:

```bash
ssh proxmox-tunnel 'pct exec 110 -- bash -c "
  cd /opt/mcp-servers && git fetch origin main && git reset --hard origin/main &&
  cp deploy/mcp-server@.service /etc/systemd/system/ &&
  systemctl daemon-reload
"'
```

Then restart whichever services need it: `./deploy/deploy.sh --no-push <name>`.

---

## Port assignments

| Server | Port |
|--------|------|
| knowledge | 9017 |
| knowledge_api | 9018 |

The private/home MCP ports are listed in the README and managed by the CT 117
`mcp-accounts` deployment path. Retired ports (do not reuse): 9002, 9003,
9007, 9012, 9016. Next available: 9019.

---

## Debugging

**Check all server statuses:**
```bash
./deploy/deploy.sh --status
```

**Check a single server:**
```bash
# From home (via PVE)
ssh root@192.168.1.11 'pct exec 110 -- systemctl is-active mcp-server@knowledge'

# From remote (via tunnel)
ssh proxmox-tunnel 'pct exec 110 -- systemctl is-active mcp-server@knowledge'
```

**View logs:**
```bash
# From home
ssh root@192.168.1.11 'pct exec 110 -- journalctl -u mcp-server@knowledge -n 50 --no-pager'

# From remote
ssh proxmox-tunnel 'pct exec 110 -- journalctl -u mcp-server@knowledge -n 50 --no-pager'
```

**Smoke-test a server's tool list:**
```bash
ssh root@192.168.1.11 'pct exec 110 -- bash -lc '\''source /opt/mcp-servers/.env && curl -s http://127.0.0.1:9017/mcp \
  -X POST -H "Authorization: Bearer ${MCP_KNOWLEDGE_BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"'\'''
```

**Verify chat-backend sees the refreshed Knowledge tools:**
```bash
ssh proxmox-tunnel "pct exec 111 -- bash -lc \
  'curl -sk https://127.0.0.1:8000/api/mcp/servers/ | grep -o knowledge_curation_create'"
```

**Verify LibreChat sees the refreshed Knowledge tools:**
```bash
ssh proxmox-tunnel "pct exec 115 -- bash -lc \
  'cd /opt/LibreChat && docker logs LibreChat --since 10m 2>&1 | grep knowledge_curation_create'"
```

**Shell escaping rules for manual `pct exec` commands:**
- Outer `ssh` uses single quotes `'...'`
- Inside `pct exec -- bash -c "..."`, use double quotes
- Variable expansion inside remote bash: escape `$` as `\$`
- If SSH isn't available: use `root@pve → Shell` in the Proxmox web console at `https://proxmox.jackshome.com`
