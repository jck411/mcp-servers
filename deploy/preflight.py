#!/usr/bin/env python3
"""Read-only CT117 gate. No fetch, dependency sync, source writes or restarts."""
import argparse
import shlex
import subprocess

PORTS = {
    "calendar": 9004, "gmail": 9005, "gdrive": 9006, "monarch": 9008,
    "spotify": 9010, "tv": 9013, "hue": 9015,
}


def remote_script(services):
    # Only allowlisted names are interpolated. Never print unit env or Git URLs.
    checks = """set -eu
export GIT_OPTIONAL_LOCKS=0
cd /opt/mcp-accounts
blocked=0
if [ "$(git config --get remote.origin.url)" != 'https://github.com/jck411/mcp-servers.git' ]; then
    echo 'BLOCKED: origin mismatch'; blocked=1
fi
printf 'Live HEAD: '; git rev-parse HEAD
status=$(git status --porcelain --untracked-files=normal)
if [ -n "$status" ]; then
    echo 'BLOCKED: dirty checkout; preserve and reconcile separately'; blocked=1
fi
if [ ! -x .venv/bin/python ]; then echo 'BLOCKED: runtime missing'; blocked=1; fi
"""
    for service in services:
        unit = f"mcp-server@{service}.service"
        checks += f"""
if [ "$(systemctl show {unit} -p WorkingDirectory --value)" != /opt/mcp-accounts ] ||
   [ "$(systemctl show {unit} -p User --value)" != mcp ] ||
   [ "$(systemctl show {unit} -p Group --value)" != mcp ]; then
    echo 'BLOCKED: {service} unit identity mismatch'; blocked=1
fi
if ! systemctl is-active --quiet {unit}; then
    echo 'BLOCKED: {service} not active'; blocked=1
fi
if [ -z "$(ss -H -ltn 'sport = :{PORTS[service]}')" ]; then
    echo 'BLOCKED: {service} listener missing'; blocked=1
fi
echo 'Checked {service}: unit identity, active state, expected listener'
"""
    checks += """
if ! systemctl is-active --quiet hue-event-collector.service; then
    echo 'BLOCKED: Hue collector not active'; blocked=1
fi
echo 'No changes made; this is not deployment authorization or provider acceptance'
exit "$blocked"
"""
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="offline scoped plan")
    mode.add_argument("--preflight", action="store_true",
                      help="read-only live gate via ssh proxmox")
    parser.add_argument("services", nargs="*", choices=list(PORTS))
    args = parser.parse_args()
    if not args.services:
        parser.error("explicit service selection required")
    services = list(dict.fromkeys(args.services))
    print("CT117 /opt/mcp-accounts; explicit scope:", flush=True)
    for service in services:
        print(f"{service}: port {PORTS[service]}, mcp-server@{service}.service", flush=True)
    print("Shared code/dependencies and Hue collector impact require review; see README.md.",
          flush=True)
    if args.dry_run:
        print("No changes and no network access; use --preflight for live checks.")
        return 0
    command = "pct exec 117 -- bash -c " + shlex.quote(remote_script(services))
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "proxmox", command],
            capture_output=True, text=True, timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("BLOCKED: preflight transport unavailable")
        return 1
    print(result.stdout, end="")
    # Suppress raw errors; even Git/SSH errors may contain private configuration.
    if result.returncode:
        print("BLOCKED: preflight failed; no deployment attempted")
        return 1
    print("Read-only checks passed; manual review/approval gates still apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
