# Deployment

How to deploy any MCP server in this repo to LXC CT 110 (`192.168.1.110`).

---

## Quick reference

```bash
# Deploy one server (auto-detects local/tunnel/console)
./deploy/deploy.sh spotify

# Deploy multiple servers
./deploy/deploy.sh knowledge knowledge_api

# Deploy all servers
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

## What deploy does (steps 1–5)

1. **Push** — commits any dirty local files and runs `git push origin master` (skip with `--no-push`)
2. **Pull** — SSHs into CT 110 and runs `git pull --ff-only` + `uv sync --extra all`
3. **Port file** — writes `/opt/mcp-servers/.env.<server>` containing `MCP_PORT=<port>`
4. **Orphan kill** — `fuser -k <port>/tcp` to free the port before restart
5. **Restart + poll** — `systemctl restart mcp-server@<server>`, polls up to 20 s for `active`
6. **Backend refresh** — pokes LXC 111 to refresh its MCP server discovery list

---

## Deploying from home (local LAN)

SSHes to PVE at `root@192.168.1.11`, then `pct exec 110` into the LXC.

```bash
./deploy/deploy.sh spotify
./deploy/deploy.sh knowledge knowledge_api
./deploy/deploy.sh --no-push hue        # code already pushed
./deploy/deploy.sh --local calendar     # force local mode
```

---

## Deploying from remote (away from home)

Requires `proxmox-tunnel` in `~/.ssh/config` and `cloudflared` installed. Auto-detected — no flags needed.

```
# ~/.ssh/config entry required:
Host proxmox-tunnel
    HostName ssh.jackshome.com
    User root
    ProxyCommand cloudflared access ssh --hostname %h
    StrictHostKeyChecking no
```

Check: `which cloudflared && cloudflared version`

```bash
./deploy/deploy.sh spotify
# Internally: ssh proxmox-tunnel 'pct exec 110 -- bash -c "…"'
```

---

## When SSH is unreachable (Proxmox console)

```bash
./deploy/deploy.sh --remote spotify
```

Prints three `pct exec` blocks to paste into the Proxmox web console at `https://proxmox.jackshome.com → CT 110 → Console`:

```
# Step 1: Pull + sync
pct exec 110 -- bash -c 'cd /opt/mcp-servers && git pull --ff-only && uv sync --extra all'

# Step 2: Restart
pct exec 110 -- bash -c 'systemctl restart mcp-server@spotify'

# Step 3: Check status
pct exec 110 -- bash -c 'systemctl is-active mcp-server@spotify'
```

---

## Adding a new server

1. Create `servers/<name>.py` following the pattern in an existing server
2. Add `[name]=<port>` to `PORT_MAP` in both `deploy/deploy.sh` and `deploy/setup-systemd.sh`
3. Add the name to `ALL_SERVERS` in `deploy/deploy.sh` and `DEFAULT_SERVERS` in `deploy/setup-systemd.sh`
4. Add any extra pip packages to `pyproject.toml` under `[project.optional-dependencies]` and add to the `all` group
5. Add the port to the Port Assignments table in `.github/copilot-instructions.md`
6. Deploy: `./deploy/deploy.sh <name>`

---

## Updating the systemd unit file

`deploy/mcp-server@.service` is **not** copied automatically by `deploy.sh`. Copy it manually after changes:

```bash
# From home (local)
ssh root@192.168.1.110 '
  cd /opt/mcp-servers && git pull &&
  cp deploy/mcp-server@.service /etc/systemd/system/ &&
  systemctl daemon-reload
'

# From remote (tunnel)
ssh proxmox-tunnel 'pct exec 110 -- bash -c "
  cd /opt/mcp-servers && git pull &&
  cp deploy/mcp-server@.service /etc/systemd/system/ &&
  systemctl daemon-reload
"'
```

Then restart whichever services need it: `./deploy/deploy.sh --no-push <name>`.

---

## Port assignments

| Server | Port |
|--------|------|
| shell_control | 9001 |
| calculator | 9003 |
| calendar | 9004 |
| gmail | 9005 |
| gdrive | 9006 |
| pdf | 9007 |
| monarch | 9008 |
| notes | 9009 |
| spotify | 9010 |
| playwright | 9011 |
| tv | 9013 |
| rag | 9014 |
| hue | 9015 |
| web_search | 9016 |
| knowledge | 9017 |
| knowledge_api | 9018 |

Retired (do not reuse): 9002, 9012. Next available: 9019.

---

## Debugging

**Check all server statuses:**
```bash
./deploy/deploy.sh --status
```

**Check a single server:**
```bash
# From home (via PVE)
ssh root@192.168.1.11 'pct exec 110 -- systemctl is-active mcp-server@spotify'

# From remote (via tunnel)
ssh proxmox-tunnel 'pct exec 110 -- systemctl is-active mcp-server@spotify'
```

**View logs:**
```bash
# From home
ssh root@192.168.1.11 'pct exec 110 -- journalctl -u mcp-server@spotify -n 50 --no-pager'

# From remote
ssh proxmox-tunnel 'pct exec 110 -- journalctl -u mcp-server@spotify -n 50 --no-pager'
```

**Smoke-test a server's tool list:**
```bash
# Replace port as needed (see port table above)
ssh root@192.168.1.11 'pct exec 110 -- curl -s http://127.0.0.1:9010/mcp \
  -X POST -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"'
```

**Shell escaping rules for manual `pct exec` commands:**
- Outer `ssh` uses single quotes `'...'`
- Inside `pct exec -- bash -c "..."`, use double quotes
- Variable expansion inside remote bash: escape `$` as `\$`
- If SSH isn't available: use `root@pve → Shell` in the Proxmox web console at `https://proxmox.jackshome.com`
