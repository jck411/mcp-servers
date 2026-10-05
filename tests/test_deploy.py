"""Deployment entry point must never mutate a guest or silently select services."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def deny_external_side_effects(tmp_path, monkeypatch):
    # Even a future regression to the retired script must not reach Git/SSH.
    bin_dir = tmp_path / "denied-bin"
    bin_dir.mkdir()
    for name in ("git", "ssh", "systemctl", "pct", "curl", "fuser", "kill"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\necho 'External command denied in tests' >&2\nexit 99\n")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def run(*args):
    return subprocess.run(
        ["bash", str(ROOT / "deploy/deploy.sh"), *args],
        capture_output=True, text=True, timeout=10,
    )


def test_explicit_read_only_plan():
    result = run("--dry-run", "calendar", "hue")
    assert result.returncode == 0
    assert "CT117" in result.stdout
    assert "calendar: port 9004" in result.stdout
    assert "hue: port 9015" in result.stdout
    assert "No changes" in result.stdout


def test_no_implicit_service_selection():
    result = run()
    assert result.returncode != 0
    assert "explicit" in result.stderr


@pytest.mark.parametrize("args", [
    ("--dry-run",), ("--preflight",), ("--no-push", "hue"),
    ("--status", "hue"), ("--dry-run", "web_search"),
    ("--dry-run", "hue;touch /bad"),
])
def test_invalid_scope_or_legacy_options_rejected(args):
    assert run(*args).returncode != 0


def test_every_supported_service_and_duplicates():
    result = run("--dry-run", "calendar", "gmail", "gdrive", "monarch", "spotify", "tv",
                 "hue", "hue")
    assert result.returncode == 0
    assert result.stdout.count("hue: port 9015") == 1
    assert "spotify: port 9010" in result.stdout


def load_preflight():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "deploy/preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("dirty,origin,active,listener,expected", [
    ("", "https://github.com/jck411/mcp-servers.git", "yes", "yes", 0),
    (" M README.md", "https://github.com/jck411/mcp-servers.git", "yes", "yes", 1),
    ("?? private-state", "https://github.com/jck411/mcp-servers.git", "yes", "yes", 1),
    ("", "PRIVATE-URL", "yes", "yes", 1),
    ("", "https://github.com/jck411/mcp-servers.git", "no", "yes", 1),
    ("", "https://github.com/jck411/mcp-servers.git", "yes", "no", 1),
])
def test_guest_gate_synthetic(tmp_path, dirty, origin, active, listener, expected):
    # Exercise the actual remote shell, never contact Proxmox in regressions.
    runtime = tmp_path / ".venv/bin"
    runtime.mkdir(parents=True)
    (runtime / "python").write_text("#!/bin/sh\nexit 0\n")
    (runtime / "python").chmod(0o700)
    commands = {
        "git": '''case "$1" in
config) printf '%s' "$TEST_ORIGIN";;
rev-parse) echo c2fe6b0b04c19516081f0678b2ea6af914fff646;;
status) printf '%s' "$TEST_DIRTY";;
*) exit 99;; esac''',
        "systemctl": '''case "$1" in
is-active) [ "$TEST_ACTIVE" = yes ];;
show) case "$4" in WorkingDirectory) echo /opt/mcp-accounts;;
User|Group) echo mcp;; *) exit 99;; esac;;
*) exit 99;; esac''',
        "ss": '[ "$TEST_LISTENER" != yes ] || echo LISTEN',
    }
    for name, body in commands.items():
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o700)
    script = load_preflight().remote_script(["calendar", "hue"])
    script = script.replace("cd /opt/mcp-accounts", f"cd '{tmp_path}'")
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "TEST_DIRTY": dirty, "TEST_ORIGIN": origin,
                                 "TEST_ACTIVE": active, "TEST_LISTENER": listener})
    assert result.returncode == expected
    assert "PRIVATE-URL" not in result.stdout
    assert "private-state" not in result.stdout


def test_remote_commands_are_read_only():
    script = load_preflight().remote_script(["hue"])
    for forbidden in ("git fetch", "git reset", "git clean", "git commit", "git push",
                      "restart", "fuser", "kill ", "uv sync", "EnvironmentFile", "journalctl"):
        assert forbidden not in script


def test_preflight_transport_failure_is_sanitized():
    result = run("--preflight", "calendar")
    assert result.returncode == 1
    assert "preflight failed" in result.stdout
    assert "External command denied" not in result.stdout + result.stderr


def test_bootstrap_fails_closed():
    result = subprocess.run(["bash", str(ROOT / "deploy/setup-systemd.sh"), "calendar"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "Bootstrap disabled" in result.stderr


