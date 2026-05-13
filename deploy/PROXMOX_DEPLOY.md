# Deploying MCP Servers to Proxmox

**Target:** LXC container on Proxmox (192.168.1.11)  
**Container:** CT 110, IP `192.168.1.110`  
**OS:** Debian 13 (matches all other containers)  
**Servers:** calculator (9003), hue (9015), knowledge (9017), and others — see `deploy/setup-systemd.sh` for the full list.

> **Remote access prerequisites.** Commands below that use `ssh proxmox-tunnel` require the Cloudflare Access service token wrapper described in [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md#deploying-from-remote-away-from-home). `ssh.jackshome.com` is fronted by Cloudflare Access (service token `mcp-servers-ssh`) and PVE has password auth disabled — key + token is mandatory.

---

## 1. Create the LXC Container

Run on the **Proxmox host** (`ssh root@192.168.1.11`):

```bash
# Download Debian 13 template if not already cached
pveam update
pveam download local debian-13-standard_13.0-1_amd64.tar.zst

# Create container
pct create 110 local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst \
  --hostname mcp-servers \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.1.110/24,gw=192.168.1.1 \
  --nameserver "1.1.1.1 8.8.8.8" \
  --searchdomain local \
  --features nesting=1 \
  --onboot 1 \
  --start 1 \
  --unprivileged 1

# Verify it's running
pct status 110
```

### Resource Notes

- **2 cores / 2 GB RAM** is generous for the current lightweight Python servers
- Increase to 4 cores / 4 GB if you add heavier servers (PDF/Knowledge)
- `nesting=1` required for systemd inside the container
- `onboot=1` ensures servers survive Proxmox reboots

---

## 2. Initial Container Setup

Enter the container:

```bash
# From Proxmox host
pct enter 110
# Or from elsewhere
ssh proxmox-tunnel 'pct exec 110 -- bash'
```

Install base packages:

```bash
apt update && apt upgrade -y
apt install -y \
  git \
  curl \
  ca-certificates \
  build-essential \
  python3 \
  python3-venv \
  sudo

# Create a service user (non-root)
useradd -m -s /bin/bash mcp
echo "mcp ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/mcp
```

---

## 3. Install uv (Python Package Manager)

```bash
# As root — install uv system-wide
curl -LsSf https://astral.sh/uv/install.sh | sh

# Add to PATH for all users
echo 'export PATH="/root/.local/bin:$PATH"' >> /etc/profile.d/uv.sh
source /etc/profile.d/uv.sh

# Verify
uv --version
```

---

## 4. Clone and Install the Repo

```bash
# Clone to /opt/mcp-servers
git clone https://github.com/jck411/mcp-servers.git /opt/mcp-servers
cd /opt/mcp-servers

# Set ownership to service user
chown -R mcp:mcp /opt/mcp-servers

# Install dependencies (as mcp user)
su - mcp -c "cd /opt/mcp-servers && uv sync --extra all"

# Verify Python works
su - mcp -c "/opt/mcp-servers/.venv/bin/python -c 'import fastmcp; print(fastmcp.__version__)'"
```

---

## 5. Configure Environment

`.env` is already symlinked on the LXC — no manual copy needed. Per-instance port files (`.env.<name>`) are created automatically by `setup-systemd.sh` in the next step.

Per-instance port files are created automatically by the setup script in step 6.

---

## 6. Install Systemd Units

```bash
# Run the setup script — creates service units + per-instance .env files
sudo /opt/mcp-servers/deploy/setup-systemd.sh

# Output will show:
#   ✅ /opt/mcp-servers/.env.calculator → port 9003
#   ✅ /opt/mcp-servers/.env.hue → port 9015
#   ✅ /opt/mcp-servers/.env.knowledge → port 9017

# Or install specific servers only:
# sudo /opt/mcp-servers/deploy/setup-systemd.sh calculator hue

# Verify they are running
systemctl status mcp-server@calculator --no-pager
systemctl status mcp-server@hue --no-pager
systemctl status mcp-server@knowledge --no-pager
```

### Checking Logs

```bash
# Live logs for a specific server
journalctl -u mcp-server@calculator -f

# All MCP server logs
journalctl -u 'mcp-server@*' --since "5 min ago"
```

---

## 7. Verify Servers Are Responding

From inside the container:

```bash
# Calculator
curl -s http://127.0.0.1:9003/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python3 -m json.tool

# Hue
curl -s http://127.0.0.1:9015/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python3 -m json.tool
```

From your **dev machine** (Dell XPS / 192.168.1.19):

```bash
# Test connectivity across the network
curl -s http://192.168.1.110:9003/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

---

## 8. Connect from Backend

Once servers are verified, tell Backend_FastAPI to connect:

```bash
# Calculator
curl -X POST http://localhost:8000/api/mcp/servers/connect \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://192.168.1.110:9003/mcp"}'

# Hue
curl -X POST http://localhost:8000/api/mcp/servers/connect \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://192.168.1.110:9015/mcp"}'

# Verify all connected
curl http://localhost:8000/api/mcp/servers/ | python3 -m json.tool
```

Or add them directly to `data/mcp_servers.json` in Backend_FastAPI:

```json
{
  "servers": [
    {
      "id": "calculator",
      "url": "http://192.168.1.110:9003/mcp",
      "enabled": true,
      "disabled_tools": []
    },
    {
      "id": "hue",
      "url": "http://192.168.1.110:9015/mcp",
      "enabled": true,
      "disabled_tools": []
    }
  ]
}
```

---

## 9. Add DHCP Reservation on Router

Add a reservation for the MCP server container on the NETGEAR RAXE500:

| Device | IP | MAC |
|--------|-----|-----|
| MCP Servers (CT 110) | 192.168.1.110 | *(get from `ip link show eth0` inside CT)* |

> The container uses a static IP in its LXC config, but a router reservation prevents conflicts if DHCP ever hands out .110 to something else.

```bash
# Get the MAC address
pct exec 110 -- ip link show eth0 | grep ether
```

---

## Ongoing Operations

### Deploying Updates

From your dev machine:

```bash
ssh proxmox-tunnel "pct exec 110 -- bash -lc 'cd /opt/mcp-servers && su - mcp -c \"./deploy/deploy.sh\"'"

# Or deploy a specific server only:
ssh proxmox-tunnel "pct exec 110 -- bash -lc 'cd /opt/mcp-servers && su - mcp -c \"./deploy/deploy.sh calculator\"'"
```

### Adding a New Server

1. Create `servers/<name>.py` in this repo
2. Add port to `.env` on Proxmox: `MCP_PORT_<name>=<port>`
3. Enable: `systemctl enable --now mcp-server@<name>`
4. Connect from backend: `POST /api/mcp/servers/connect {"url": "http://192.168.1.110:<port>/mcp"}`

### Monitoring

```bash
# Quick status of all MCP servers
ssh proxmox-tunnel "pct exec 110 -- systemctl list-units 'mcp-server@*' --no-pager"

# Resource usage
ssh proxmox-tunnel "pct exec 110 -- ps aux | grep 'servers\\.' | grep -v grep"

# Container resource limits from Proxmox host
ssh root@192.168.1.11 "pct config 110"
```

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Service won't start | `journalctl -u mcp-server@<name> -n 50` |
| Port already in use | `ss -tlnp | grep <port>` |
| Can't reach from network | `curl http://192.168.1.110:<port>/mcp` from Proxmox host |
| Python import errors | `/opt/mcp-servers/.venv/bin/python -m servers.<name>` manually |
| uv not found | `source /etc/profile.d/uv.sh` or reinstall uv |
| Permission denied | Check `/opt/mcp-servers` ownership: `chown -R mcp:mcp /opt/mcp-servers` |

---

## Optional: Cloudflare Tunnel

To expose MCP servers externally (use with your existing Cloudflare Tunnel on Proxmox host):

Add to your tunnel config on 192.168.1.11:

```yaml
ingress:
  - hostname: mcp-calculator.jackshome.com
    service: http://192.168.1.110:9003
  - hostname: mcp-hue.jackshome.com
    service: http://192.168.1.110:9015
  # ... etc
```

Then restart: `systemctl restart cloudflared`
