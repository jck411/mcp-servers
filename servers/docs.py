"""Docs MCP server — live filesystem access to ~/REPOS for LLM context.

Runs locally via stdio. Never deployed to an LXC.
Gives any AI agent an always-up-to-date view of repos, infrastructure,
env layout, and the ability to read/search any file.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from fastmcp import FastMCP

REPOS = Path(os.environ.get("DOCS_REPOS_ROOT", Path.home() / "REPOS"))
SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", "uv.lock",
}
# Filenames / paths that must never be returned by read_file / search, even though the
# stdio MCP technically runs with the user's filesystem permissions. These hold real
# secrets and should not leak into any LLM transcript.
SECRET_DIR_NAMES = {"credentials", "certs", "secrets"}
SECRET_FILENAMES = {".env"}
SECRET_FILENAME_PREFIXES = (".env.",)
SECRET_FILENAME_ALLOW = {".env.example"}
SECRET_NAME_SUBSTRINGS = ("secret", "credential", "client_secret", "id_rsa", "id_ed25519")
WRITEABLE_TEXT_EXTENSIONS = {
    ".adoc",
    ".cfg",
    ".conf",
    ".csv",
    ".ini",
    ".json",
    ".markdown",
    ".md",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
DEFAULT_HTTP_PORT = 9019

mcp = FastMCP("docs")


def _resolve_repos_path(path: str) -> tuple[Path | None, str | None]:
    if not path.strip():
        return None, "Error: path is required"
    root = REPOS.resolve()
    target = (REPOS / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, "Error: path must be inside ~/REPOS"
    return target, None


def _is_secret_path(target: Path) -> bool:
    """Return True if the path looks like a secret file or sits in a secret directory."""
    name = target.name
    if name in SECRET_FILENAME_ALLOW:
        return False
    if name in SECRET_FILENAMES:
        return True
    if name.startswith(SECRET_FILENAME_PREFIXES) and name not in SECRET_FILENAME_ALLOW:
        return True
    lowered = name.lower()
    if any(token in lowered for token in SECRET_NAME_SUBSTRINGS):
        return True
    for part in target.parts:
        if part in SECRET_DIR_NAMES:
            return True
    return False


@mcp.tool("docs_overview")
async def overview() -> str:
    """High-level overview: repos, LXC/VM services, DHCP devices, and env key counts."""
    parts: list[str] = []

    # Repos
    repos = []
    for d in sorted(REPOS.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        readme = d / "README.md"
        first = ""
        if readme.exists():
            for line in readme.read_text(errors="replace").splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    first = line[:120]
                    break
        repos.append(f"  {d.name}: {first}" if first else f"  {d.name}")
    parts.append("## Repos\n" + "\n".join(repos))

    # LXC / VM fleet
    svc = REPOS / "NETWORK/lxc/services.json"
    if svc.exists():
        rows = []
        for s in json.loads(svc.read_text()):
            ports = ",".join(str(p) for p in s.get("ports", []))
            rows.append(
                f"  {s['name']:20s}  ip={s['ip']:16s}  ports={ports:12s}  "
                f"id={s['id']}  repo={s.get('owner_repo') or '—'}"
            )
        parts.append("## Infrastructure (LXC/VM)\n" + "\n".join(rows))

    # DHCP devices
    dhcp = REPOS / "NETWORK/dhcp/reservations.json"
    if dhcp.exists():
        rows = [
            f"  {d['name']:25s}  ip={d['ip']:16s}  mac={d.get('mac', '—')}"
            for d in json.loads(dhcp.read_text())
        ]
        parts.append("## Devices (DHCP)\n" + "\n".join(rows))

    return "\n\n".join(parts)


@mcp.tool("docs_read_file")
async def read_file(path: str) -> str:
    """Read a file from ~/REPOS by relative path (e.g. 'NETWORK/infrastructure.md').
    If path is a directory, lists its contents."""
    target, error = _resolve_repos_path(path)
    if error:
        return error
    assert target is not None
    if not target.exists():
        return f"Error: {path} not found"
    if target.is_dir():
        entries = []
        for p in sorted(target.iterdir()):
            if p.name in SKIP_DIRS:
                continue
            kind = "dir/" if p.is_dir() else f"{p.stat().st_size:,}b"
            entries.append(f"  {p.name:40s}  {kind}")
        return f"Directory: {path}/\n" + "\n".join(entries)
    if _is_secret_path(target):
        return f"Error: refusing to read secret file {path}."
    return target.read_text(errors="replace")[:50_000]


@mcp.tool("docs_write_file")
async def write_file(
    path: str, content: str, mode: str = "replace", create_dirs: bool = False
) -> str:
    """Write text docs/config files inside ~/REPOS.
    mode: replace (default) or append."""
    if mode not in {"replace", "append"}:
        return "Error: mode must be 'replace' or 'append'"
    if len(content) > 500_000:
        return "Error: content too large (max 500000 chars)"

    target, error = _resolve_repos_path(path)
    if error:
        return error
    assert target is not None

    if target.exists() and target.is_dir():
        return "Error: target path is a directory"
    if target.suffix.lower() not in WRITEABLE_TEXT_EXTENSIONS:
        allowed = ", ".join(sorted(WRITEABLE_TEXT_EXTENSIONS))
        return f"Error: writes limited to text/documentation files ({allowed})"

    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        return f"Error: parent directory does not exist: {target.parent}"

    open_mode = "a" if mode == "append" else "w"
    with target.open(open_mode, encoding="utf-8") as handle:
        handle.write(content)

    try:
        rel_path = target.relative_to(REPOS.resolve())
    except ValueError:
        rel_path = target
    return f"OK: wrote {len(content)} chars to {rel_path} ({mode})"


@mcp.tool("docs_search")
async def search(query: str, file_pattern: str = "") -> str:
    """Grep across all repos. Returns matching lines (max 50). Optional file_pattern like '*.py'."""
    cmd = ["grep", "-rnI", "--color=never", "-m", "50"]
    for skip in SKIP_DIRS:
        cmd += [f"--exclude-dir={skip}"]
    for secret_dir in SECRET_DIR_NAMES:
        cmd += [f"--exclude-dir={secret_dir}"]
    cmd += ["--exclude=.env"]
    if file_pattern:
        cmd += [f"--include={file_pattern}"]
    cmd += [query, str(REPOS)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        root_prefix = str(REPOS) + "/"
        filtered: list[str] = []
        for line in r.stdout.splitlines():
            rel = line.removeprefix(root_prefix)
            path_part = rel.split(":", 1)[0] if ":" in rel else rel
            candidate = REPOS / path_part
            if _is_secret_path(candidate):
                continue
            filtered.append(rel)
            if len(filtered) >= 50:
                break
        return "\n".join(filtered) if filtered else "No results."
    except subprocess.TimeoutExpired:
        return "Search timed out."


# ── Entrypoint ────────────────────────────────────────────────────────────────

def run(transport: str = "stdio", host: str = "0.0.0.0", port: int = DEFAULT_HTTP_PORT) -> None:
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port,
                json_response=True, stateless_http=True,
                uvicorn_config={"access_log": False})
    else:
        mcp.run(transport="stdio")


def main() -> None:
    p = argparse.ArgumentParser(description="Docs MCP Server — live ~/REPOS context")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = p.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()

__all__ = ["mcp", "run", "overview", "read_file", "write_file", "search"]
