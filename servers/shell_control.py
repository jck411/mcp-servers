"""MCP server exposing shell control utilities for executing commands."""

from __future__ import annotations

import asyncio
import atexit
import glob as glob_module
import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio.subprocess import Process

from fastmcp import FastMCP

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9001

mcp: FastMCP = FastMCP("shell-control")  # type: ignore


OUTPUT_TAIL_BYTES = 4 * 1024  # For success: last 4KB is usually enough
OUTPUT_HEAD_BYTES = 2 * 1024  # For failure: first 2KB (context)
OUTPUT_FAIL_TAIL_BYTES = 4 * 1024  # For failure: last 4KB (error details)
LOG_RETENTION_HOURS = 48
DELTAS_RETENTION_DAYS = 30
DELTAS_MAX_ENTRIES = 100
HOST_PROFILE_ENV = "HOST_PROFILE_ID"
HOST_ROOT_ENV = "HOST_ROOT_PATH"
SETTINGS_PANELS_ENV = "SHELL_CONTROL_SETTINGS_PANELS"

# Shell session settings
SESSION_IDLE_TIMEOUT = 300  # 5 minutes idle = cleanup
SESSION_MAX_AGE = 3600  # 1 hour max session lifetime
SESSION_MAX_COUNT = 5  # Max concurrent sessions

# Validation pattern for host_id to prevent path traversal attacks
_VALID_HOST_ID = re.compile(r"^[a-zA-Z0-9_-]+$")

# Cleanup throttling (avoid scanning files on every command)
CLEANUP_THROTTLE_SECONDS = 300  # 5 minutes between cleanups
_last_log_cleanup: float = 0.0
_last_delta_cleanup: float = 0.0

# Cached package manager detection (set lazily on first use)
_cached_package_manager: str | None = None

# Input validation bounds
TIMEOUT_MIN_SECONDS = 1
TIMEOUT_MAX_SECONDS = 600  # 10 minutes max

# Progress feedback threshold (seconds) - warn if command takes longer
PROGRESS_FEEDBACK_THRESHOLD = 10

# Patterns for commands that need --noconfirm or equivalent flags
# Format: (regex pattern, flag to add, insert position: 'after_cmd' or 'end')
_NOCONFIRM_PATTERNS: list[tuple[str, str, str]] = [
    # Arch: pacman, yay, paru
    (r"^(sudo\s+)?(pacman|yay|paru)\s+(-S|-R|-U)", "--noconfirm", "end"),
    # Fedora/RHEL: dnf, yum
    (r"^(sudo\s+)?(dnf|yum)\s+(install|remove|erase|upgrade|update)", "-y", "end"),
    # Debian/Ubuntu: apt, apt-get
    (r"^(sudo\s+)?(apt|apt-get)\s+(install|remove|purge|upgrade|dist-upgrade)", "-y", "end"),
    # Flatpak
    (r"^(sudo\s+)?flatpak\s+(install|uninstall|update)", "-y", "end"),
    # Snap
    (r"^(sudo\s+)?snap\s+(install|remove)", "--yes", "end"),
    # pip/pipx (for completeness)
    (r"^(sudo\s+)?pip3?\s+(install|uninstall)", "-y", "end"),
    (r"^(sudo\s+)?pipx\s+(install|uninstall)", "--force", "end"),
]

# Commands that are inherently interactive and should be rejected/warned
_INTERACTIVE_COMMANDS: list[tuple[str, str]] = [
    (r"^\s*vim\s", "vim requires interactive input; use 'sed' or 'cat <<EOF' for file edits"),
    (r"^\s*nano\s", "nano requires interactive input; use 'sed' or 'cat <<EOF' for file edits"),
    (r"^\s*vi\s", "vi requires interactive input; use 'sed' or 'cat <<EOF' for file edits"),
    (r"^\s*less\s", "less requires interactive input; use 'cat' or 'head/tail' instead"),
    (r"^\s*more\s", "more requires interactive input; use 'cat' or 'head/tail' instead"),
    (r"^\s*top$", "top requires interactive input; use 'top -bn1' for non-interactive"),
    (r"^\s*htop$", "htop requires interactive input; use 'top -bn1' for system stats"),
    (r"^\s*(sudo\s+)?visudo", "visudo requires interactive input; use 'echo ... | sudo tee' for sudoers"),
    (r"^\s*passwd(\s|$)", "passwd requires interactive input"),
    (r"^\s*ssh\s+(?!.*-o\s*BatchMode)", "ssh may prompt for password; use ssh-agent or BatchMode=yes"),
    (r"\|\s*less(\s|$)", "piping to less requires interaction; remove '| less'"),
    (r"\|\s*more(\s|$)", "piping to more requires interaction; remove '| more'"),
]


def _make_command_noninteractive(command: str) -> tuple[str, list[str]]:
    """Transform a command to be non-interactive for headless automation.

    Returns: (modified_command, list_of_warnings)

    - Adds --noconfirm/-y flags to package managers
    - Warns about inherently interactive commands
    """
    warnings: list[str] = []
    modified = command.strip()

    # Check for interactive commands that can't be automated
    for pattern, warning in _INTERACTIVE_COMMANDS:
        if re.search(pattern, modified, re.IGNORECASE):
            warnings.append(f"⚠️ {warning}")

    # Add non-interactive flags to package managers
    for pattern, flag, position in _NOCONFIRM_PATTERNS:
        if re.search(pattern, modified, re.IGNORECASE):
            # Check if flag is already present
            if flag not in modified:
                if position == "end":
                    modified = f"{modified} {flag}"
                else:
                    # Insert after the matched command
                    match = re.search(pattern, modified, re.IGNORECASE)
                    if match:
                        insert_pos = match.end()
                        modified = f"{modified[:insert_pos]} {flag}{modified[insert_pos:]}"
                warnings.append(f"ℹ️ Auto-added '{flag}' for headless operation")
            break  # Only apply one pattern

    return modified, warnings


def _get_package_manager() -> str:
    """Detect and cache the system package manager.

    Returns: 'pacman', 'dnf', 'apt', or 'unknown'
    """
    global _cached_package_manager

    if _cached_package_manager is not None:
        return _cached_package_manager

    # Check in order of preference
    pkg_managers = [
        ("pacman", "/usr/bin/pacman"),
        ("dnf", "/usr/bin/dnf"),
        ("apt", "/usr/bin/apt"),
    ]

    for name, path in pkg_managers:
        if os.path.exists(path):
            _cached_package_manager = name
            return name

    _cached_package_manager = "unknown"
    return "unknown"


# =============================================================================
# Persistent Shell Session Management
# =============================================================================


@dataclass
class ShellSession:
    """A persistent bash shell session."""

    session_id: str
    process: "Process"
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    cwd: str = field(default_factory=lambda: os.path.expanduser("~"))
    command_count: int = 0

    def is_alive(self) -> bool:
        """Check if the shell process is still running."""
        return self.process.returncode is None

    def is_expired(self) -> bool:
        """Check if session should be cleaned up."""
        now = time.time()
        idle_expired = (now - self.last_used) > SESSION_IDLE_TIMEOUT
        age_expired = (now - self.created_at) > SESSION_MAX_AGE
        return idle_expired or age_expired or not self.is_alive()


# Global session store
_sessions: dict[str, ShellSession] = {}
_sessions_lock = asyncio.Lock()


async def _cleanup_expired_sessions() -> None:
    """Remove expired sessions."""
    async with _sessions_lock:
        expired = [sid for sid, sess in _sessions.items() if sess.is_expired()]
        for sid in expired:
            sess = _sessions.pop(sid, None)
            if sess and sess.is_alive():
                sess.process.terminate()
                try:
                    await asyncio.wait_for(sess.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    sess.process.kill()
                    # Reap the process to avoid zombies
                    try:
                        await asyncio.wait_for(sess.process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass  # Process truly stuck, nothing more we can do


async def _get_or_create_session(session_id: str | None = None) -> ShellSession:
    """Get existing session or create a new one."""
    await _cleanup_expired_sessions()

    async with _sessions_lock:
        # If session_id provided, try to find it
        if session_id and session_id in _sessions:
            sess = _sessions[session_id]
            if sess.is_alive() and not sess.is_expired():
                sess.last_used = time.time()
                return sess
            # Session dead/expired, remove it
            _sessions.pop(session_id, None)

        # Enforce max sessions
        if len(_sessions) >= SESSION_MAX_COUNT:
            # Remove oldest session
            oldest_id = min(_sessions, key=lambda s: _sessions[s].last_used)
            old_sess = _sessions.pop(oldest_id)
            if old_sess.is_alive():
                old_sess.process.terminate()

        # Create new session
        new_id = session_id or uuid.uuid4().hex[:12]
        shell_env = _build_shell_env()

        # Use non-interactive bash to avoid command echoing
        process = await asyncio.create_subprocess_shell(
            "bash --norc --noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout
            env=shell_env,
            start_new_session=True,
        )

        sess = ShellSession(session_id=new_id, process=process)
        _sessions[new_id] = sess

        return sess


async def _run_in_session(
    sess: ShellSession,
    command: str,
    timeout_seconds: int = 30,
) -> tuple[str, int, str]:
    """Run a command in an existing session.

    Returns: (output, exit_code, new_cwd)
    """
    if not sess.is_alive():
        raise RuntimeError("Session is no longer alive")

    if not sess.process.stdin or not sess.process.stdout:
        raise RuntimeError("Session has no stdin/stdout")

    # Use a unique end marker for this command
    end_marker = f"___END_{uuid.uuid4().hex[:8]}___"

    # Build a script that:
    # 1. Runs the user command
    # 2. Captures exit code
    # 3. Prints marker with exit code and cwd on a single line
    wrapped_cmd = f"""{command}
__ec__=$?
echo ""
echo "{end_marker}:$__ec__:$(pwd)"
"""

    sess.process.stdin.write(wrapped_cmd.encode())
    await sess.process.stdin.drain()

    # Read output until we see the end marker
    output_lines: list[str] = []
    exit_code = -1
    new_cwd = sess.cwd
    start = time.time()

    while True:
        if time.time() - start > timeout_seconds:
            # Timeout - try to interrupt
            sess.process.send_signal(2)  # SIGINT
            raise TimeoutError(f"Command timed out after {timeout_seconds}s")

        try:
            line_bytes = await asyncio.wait_for(
                sess.process.stdout.readline(), timeout=0.5
            )
        except asyncio.TimeoutError:
            continue

        if not line_bytes:
            # EOF - process died
            raise RuntimeError("Shell process terminated unexpectedly")

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")

        # Check for our end marker
        if line.startswith(end_marker):
            # Parse: ___END_xxxx___:<exit_code>:<cwd>
            parts = line.split(":", 2)
            if len(parts) >= 3:
                try:
                    exit_code = int(parts[1])
                except ValueError:
                    exit_code = -1
                new_cwd = parts[2]
            break

        # Skip empty lines at the start (from echo "")
        if output_lines or line:
            output_lines.append(line)

    # Remove trailing empty line if present
    while output_lines and not output_lines[-1]:
        output_lines.pop()

    sess.cwd = new_cwd
    sess.command_count += 1
    sess.last_used = time.time()

    return "\n".join(output_lines), exit_code, new_cwd


async def _close_session(session_id: str) -> bool:
    """Close a session explicitly."""
    async with _sessions_lock:
        sess = _sessions.pop(session_id, None)
        if not sess:
            return False
        if sess.is_alive():
            sess.process.terminate()
            try:
                await asyncio.wait_for(sess.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                sess.process.kill()
        return True


def _get_repo_root() -> Path:
    """Return the project root (same logic as logging helpers)."""

    return Path(__file__).resolve().parents[3]


def _get_log_dir() -> Path:
    """Return the directory used to store shell execution logs."""

    log_dir = _get_repo_root() / "logs" / "shell"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _cleanup_old_logs() -> None:
    """Remove shell logs older than LOG_RETENTION_HOURS.

    Throttled to run at most once every CLEANUP_THROTTLE_SECONDS.
    """
    global _last_log_cleanup

    now = time.time()
    if now - _last_log_cleanup < CLEANUP_THROTTLE_SECONDS:
        return  # Throttled, skip this cleanup
    _last_log_cleanup = now

    log_dir = _get_log_dir()
    cutoff = now - (LOG_RETENTION_HOURS * 3600)

    for log_file in log_dir.glob("*.json"):
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
        except OSError:
            pass  # File may have been deleted concurrently


def _get_host_root() -> Path:
    """Return the root directory containing host profiles.

    Raises:
        RuntimeError: If HOST_ROOT_PATH is not set.
    """
    host_root = os.environ.get(HOST_ROOT_ENV, "").strip()
    if not host_root:
        raise RuntimeError(
            f"{HOST_ROOT_ENV} environment variable is required. "
            "Set it to the host profiles directory (e.g., '/home/jack/gdrive/host_profiles')."
        )
    path = Path(host_root).expanduser()
    if not path.exists():
        raise RuntimeError(f"Host profiles directory does not exist: {path}")
    return path


def _get_host_id() -> str:
    """Return the active host identifier from the environment.

    Raises:
        RuntimeError: If HOST_PROFILE_ID is not set.
    """
    env_value = os.environ.get(HOST_PROFILE_ENV, "").strip()
    if not env_value:
        raise RuntimeError(
            f"{HOST_PROFILE_ENV} environment variable is required. "
            "Set it in the MCP server config (e.g., 'xps13')."
        )
    return env_value


def _get_host_id_safe() -> str:
    """Return the host identifier or 'unknown' if not set."""
    return os.environ.get(HOST_PROFILE_ENV, "").strip() or "unknown"


def _get_host_dir(host_id: str | None = None) -> Path:
    """Return (and create) the directory for the given or active host.

    Raises:
        ValueError: If host_id contains invalid characters (path traversal prevention).
    """
    resolved_id = host_id or _get_host_id()

    # Validate host_id to prevent path traversal attacks
    if not _VALID_HOST_ID.match(resolved_id):
        raise ValueError(
            f"Invalid host_id: {resolved_id!r}. Must be alphanumeric with - or _"
        )

    host_dir = _get_host_root() / resolved_id
    host_dir.mkdir(parents=True, exist_ok=True)
    return host_dir


def _get_profile_path(host_id: str | None = None) -> Path:
    """Return the profile.json path for the given or active host."""

    return _get_host_dir(host_id) / "profile.json"


def _get_deltas_path(host_id: str | None = None) -> Path:
    """Return the deltas.log path for the given or active host."""

    return _get_host_dir(host_id) / "deltas.log"


def _get_inventory_path(host_id: str | None = None) -> Path:
    """Return the inventory.json path for the given or active host."""

    return _get_host_dir(host_id) / "inventory.json"


def _load_inventory() -> dict:
    """Load the system inventory; return empty dict if missing."""

    path = _get_inventory_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def _save_inventory(inventory: dict) -> None:
    """Persist system inventory to disk atomically."""

    path = _get_inventory_path()
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(path)


def _load_profile() -> dict:
    """Load the current host profile; raise if it is missing or invalid."""

    path = _get_profile_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        host_id = _get_host_id()
        raise FileNotFoundError(
            f"Host profile not found for id '{host_id}'. Expected at: {path}"
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"Host profile at {path} is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Host profile at {path} must contain a JSON object")

    return payload


def _save_profile(profile: dict) -> None:
    """Persist host profile to disk atomically."""

    path = _get_profile_path()
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(path)


def _deep_merge(base: dict, updates: dict) -> dict:
    """Recursively merge updates into base dict (returns new dict).

    Special handling:
    - None values DELETE the key from base
    - Nested dicts are merged recursively
    - All other values replace the existing value
    """

    result = base.copy()
    for key, value in updates.items():
        if value is None:
            # None means "delete this key"
            result.pop(key, None)
        elif (
            key in result and isinstance(result[key], dict) and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _cleanup_old_deltas(path: Path) -> None:
    """Remove delta entries older than DELTAS_RETENTION_DAYS or exceeding DELTAS_MAX_ENTRIES.

    Throttled to run at most once every CLEANUP_THROTTLE_SECONDS.
    """
    global _last_delta_cleanup

    now = time.time()
    if now - _last_delta_cleanup < CLEANUP_THROTTLE_SECONDS:
        return  # Throttled, skip this cleanup
    _last_delta_cleanup = now

    if not path.exists():
        return

    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return

    if not lines:
        return

    cutoff = now - (DELTAS_RETENTION_DAYS * 24 * 3600)
    kept: list[str] = []

    for line in lines:
        try:
            entry = json.loads(line)
            if entry.get("ts", 0) >= cutoff:
                kept.append(line)
        except json.JSONDecodeError:
            pass  # Skip corrupted entries

    # Also enforce max entries (keep most recent)
    if len(kept) > DELTAS_MAX_ENTRIES:
        kept = kept[-DELTAS_MAX_ENTRIES:]

    # Rewrite file atomically
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text("\n".join(kept) + "\n" if kept else "", encoding="utf-8")
    tmp_path.replace(path)


def _append_delta(delta_type: str, changes: dict, reason: str | None = None) -> None:
    """Append a change record to the deltas log for audit purposes."""

    path = _get_deltas_path()

    # Periodic cleanup (run before appending)
    _cleanup_old_deltas(path)

    entry = {
        "ts": time.time(),
        "type": delta_type,
        "changes": changes,
        "reason": reason,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# Patterns that trigger state snapshots
_SNAPSHOT_TRIGGERS: list[tuple[str, str]] = [
    # Package managers (Arch)
    (r"(pacman|yay|paru)\s+-S\s", "packages"),
    (r"(pacman|yay|paru)\s+-R", "packages"),
    (r"pacman\s+-Syu", "packages"),
    # Package managers (Fedora/RHEL)
    (r"dnf\s+(install|remove|erase)\s", "packages"),
    (r"dnf\s+(upgrade|update)(\s|$)", "packages"),
    (r"dnf\s+group\s+(install|remove)\s", "packages"),
    # Universal package managers
    (r"flatpak\s+(install|uninstall)", "packages"),
    (r"snap\s+(install|remove)", "packages"),
    (r"pipx\s+(install|uninstall)", "packages"),
    # Services
    (r"systemctl\s+(enable|disable)\s", "services"),
    # Default apps
    (r"xdg-settings\s+set\s", "defaults"),
    (r"xdg-mime\s+default\s", "defaults"),
    (r"update-alternatives\s+--set\s", "defaults"),
]

# Settings panels to open after certain commands (pattern -> panel)
# Supports: gnome-control-center, systemsettings (KDE), or generic commands
_SETTINGS_PANELS: dict[str, dict[str, str | None]] = {
    # Bluetooth
    r"bluetooth(ctl)?\s+(connect|pair|trust|power|scan)": {
        "gnome": "gnome-control-center bluetooth",
        "kde": "systemsettings kcm_bluetooth",
        "generic": "blueman-manager",
    },
    r"rfkill\s+(un)?block\s+bluetooth": {
        "gnome": "gnome-control-center bluetooth",
        "kde": "systemsettings kcm_bluetooth",
        "generic": "blueman-manager",
    },
    # Audio/Volume
    r"(pactl|pamixer|amixer)\s+.*(volume|mute|sink|source)": {
        "gnome": "gnome-control-center sound",
        "kde": "systemsettings kcm_pulseaudio",
        "generic": "pavucontrol",
    },
    r"wpctl\s+set-(volume|mute)": {
        "gnome": "gnome-control-center sound",
        "kde": "systemsettings kcm_pulseaudio",
        "generic": "pavucontrol",
    },
    # Display/Monitor
    r"(xrandr|wlr-randr|gnome-randr)\s+": {
        "gnome": "gnome-control-center display",
        "kde": "systemsettings kcm_kscreen",
        "generic": "arandr",
    },
    # Network/WiFi
    r"nmcli\s+(dev|device|con|connection)\s+(wifi|connect|up|down|modify)": {
        "gnome": "gnome-control-center wifi",
        "kde": "systemsettings kcm_networkmanagement",
        "generic": "nm-connection-editor",
    },
    r"nmcli\s+radio\s+wifi": {
        "gnome": "gnome-control-center wifi",
        "kde": "systemsettings kcm_networkmanagement",
        "generic": "nm-connection-editor",
    },
    # Power settings
    r"(powerprofilesctl|power-profiles-daemon)": {
        "gnome": "gnome-control-center power",
        "kde": "systemsettings kcm_powerdevilprofilesconfig",
        "generic": None,
    },
    r"(tlp|auto-cpufreq)": {
        "gnome": "gnome-control-center power",
        "kde": "systemsettings kcm_powerdevilprofilesconfig",
        "generic": None,
    },
    # Appearance/Theme
    r"gsettings\s+set\s+org\.gnome\.(desktop\.interface|shell\.extensions)": {
        "gnome": "gnome-control-center appearance",
        "kde": None,
        "generic": None,
    },
    r"plasma-apply-(lookandfeel|colorscheme|desktoptheme)": {
        "gnome": None,
        "kde": "systemsettings kcm_lookandfeel",
        "generic": None,
    },
    # Keyboard
    r"(setxkbmap|localectl\s+set-x11-keymap)": {
        "gnome": "gnome-control-center keyboard",
        "kde": "systemsettings kcm_keyboard",
        "generic": None,
    },
    # Mouse/Touchpad
    r"(xinput|libinput).*set-prop": {
        "gnome": "gnome-control-center mouse",
        "kde": "systemsettings kcm_touchpad",
        "generic": None,
    },
    # Printers
    r"(lpadmin|lpstat|cupsenable|cupsdisable)": {
        "gnome": "gnome-control-center printers",
        "kde": "systemsettings kcm_printer_manager",
        "generic": "system-config-printer",
    },
    # Default applications
    r"xdg-(settings|mime)\s+(set|default)": {
        "gnome": "gnome-control-center default-apps",
        "kde": "systemsettings kcm_componentchooser",
        "generic": None,
    },
    # Night light / color temperature
    r"(gsettings.*night-light|redshift|gammastep)": {
        "gnome": "gnome-control-center display",
        "kde": "systemsettings kcm_nightcolor",
        "generic": None,
    },
}

# Categories of packages to track (grep patterns for pacman -Qe)
_TRACKED_CATEGORIES: dict[str, list[str]] = {
    "browsers": [
        "brave",
        "firefox",
        "chromium",
        "google-chrome",
        "vivaldi",
        "microsoft-edge",
        "librewolf",
        "floorp",
        "zen-browser",
    ],
    "editors": [
        "code",
        "visual-studio-code",
        "neovim",
        "vim",
        "emacs",
        "sublime-text",
        "atom",
        "gedit",
        "kate",
        "helix",
    ],
    "terminals": [
        "alacritty",
        "kitty",
        "wezterm",
        "foot",
        "konsole",
        "gnome-terminal",
        "tilix",
        "terminator",
    ],
    "system_tools": [
        "earlyoom",
        "timeshift",
        "tlp",
        "auto-cpufreq",
        "zram-generator",
        "preload",
        "thermald",
        "power-profiles-daemon",
    ],
    "media": [
        "spotify",
        "vlc",
        "mpv",
        "obs-studio",
        "kdenlive",
        "audacity",
        "gimp",
        "inkscape",
        "krita",
    ],
    "dev_tools": [
        "docker",
        "podman",
        "nodejs",
        "npm",
        "yarn",
        "python",
        "go",
        "rust",
        "cargo",
        "git",
        "github-cli",
    ],
}


async def _snapshot_tracked_packages() -> dict[str, list[str]]:
    """Snapshot only the tracked package categories (fast, ~100 tokens).

    Supports: pacman (Arch), dnf (Fedora/RHEL)
    """
    pkg_manager = _get_package_manager()

    # Build a single grep pattern for all tracked packages
    all_patterns = []
    for packages in _TRACKED_CATEGORIES.values():
        all_patterns.extend(packages)

    # Escape special chars and join with |
    grep_pattern = "|".join(f"^{p}" for p in all_patterns)

    # Select command based on package manager
    if pkg_manager == "pacman":
        # Output: "package-name version"
        cmd = f"pacman -Qe 2>/dev/null | grep -E '{grep_pattern}' || true"
    elif pkg_manager == "dnf":
        # Output: "package-name.arch version repo"
        # Use awk to normalize output to "package-name version"
        cmd = (
            f"dnf list installed 2>/dev/null | grep -E '{grep_pattern}' | "
            f"awk '{{split($1,a,\".\"); print a[1], $2}}' || true"
        )
    else:
        return {}

    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        output = stdout.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, Exception):
        return {}

    if not output:
        return {}

    # Parse output and categorize
    # Both pacman and dnf (after awk) output: "package-name version"
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if not parts:
            continue
        pkg_name = parts[0]
        pkg_with_ver = line.strip()

        # Find which category this belongs to
        for category, patterns in _TRACKED_CATEGORIES.items():
            for pattern in patterns:
                if pkg_name.startswith(pattern) or pkg_name == pattern:
                    if category not in result:
                        result[category] = []
                    result[category].append(pkg_with_ver)
                    break

    return result


async def _snapshot_enabled_services() -> list[str]:
    """Snapshot user-enabled systemd services (fast, ~20 tokens)."""

    # Only get user-enabled services, not all system services
    tracked_services = [
        "earlyoom",
        "tlp",
        "thermald",
        "auto-cpufreq",
        "docker",
        "podman",
        "libvirtd",
        "bluetooth",
        "cups",
        "sshd",
        "power-profiles-daemon",
        "timeshift-autosnap",
    ]
    pattern = "|".join(tracked_services)

    try:
        process = await asyncio.create_subprocess_shell(
            f"systemctl list-unit-files --state=enabled --type=service 2>/dev/null "
            f"| grep -E '{pattern}' | awk '{{print $1}}' || true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        output = stdout.decode("utf-8", errors="replace").strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        return []

    return [s.strip() for s in output.splitlines() if s.strip()]


async def _snapshot_defaults() -> dict[str, str]:
    """Snapshot XDG default applications (browser, file manager, etc.)."""

    defaults: dict[str, str] = {}

    # Key XDG settings the LLM needs for natural language commands
    xdg_queries = [
        ("default-web-browser", "browser"),
        ("default-url-scheme-handler https", "browser_https"),
    ]

    for query, key in xdg_queries:
        try:
            process = await asyncio.create_subprocess_shell(
                f"xdg-settings get {query} 2>/dev/null || true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
            value = stdout.decode("utf-8", errors="replace").strip()
            if value:
                # Strip .desktop suffix for cleaner output
                defaults[key] = value.replace(".desktop", "")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # Also get xdg-mime defaults for common types
    mime_queries = [
        ("inode/directory", "file_manager"),
        ("application/pdf", "pdf_viewer"),
        ("image/png", "image_viewer"),
        ("video/mp4", "video_player"),
        ("audio/mpeg", "audio_player"),
        ("text/plain", "text_editor"),
    ]

    for mime_type, key in mime_queries:
        try:
            process = await asyncio.create_subprocess_shell(
                f"xdg-mime query default {mime_type} 2>/dev/null || true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
            value = stdout.decode("utf-8", errors="replace").strip()
            if value:
                defaults[key] = value.replace(".desktop", "")
        except (asyncio.TimeoutError, Exception):
            pass

    return defaults


async def _detect_system_info() -> dict[str, object]:
    """Detect static system information (OS, desktop, display server, kernel)."""

    info: dict[str, object] = {}

    # OS info from /etc/os-release
    try:
        process = await asyncio.create_subprocess_shell(
            "cat /etc/os-release 2>/dev/null || true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
        output = stdout.decode("utf-8", errors="replace")

        for line in output.splitlines():
            if line.startswith("NAME="):
                info["os"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("ID_LIKE="):
                info["os_base"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("ID=") and "os_base" not in info:
                # Fallback if ID_LIKE not present
                info["os_base"] = line.split("=", 1)[1].strip().strip('"')
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

    # Kernel version
    try:
        process = await asyncio.create_subprocess_shell(
            "uname -r 2>/dev/null || true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
        value = stdout.decode("utf-8", errors="replace").strip()
        if value:
            info["kernel"] = value
    except (asyncio.TimeoutError, Exception):
        pass

    # Desktop environment from XDG_CURRENT_DESKTOP
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").strip()
    if desktop:
        info["desktop"] = desktop

    # Session type (wayland/x11)
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip()
    if session_type:
        info["display_server"] = session_type

    # Detect package manager and AUR helper
    pkg_managers = [
        ("pacman", "pacman"),
        ("apt", "apt"),
        ("dnf", "dnf"),
        ("zypper", "zypper"),
        ("emerge", "portage"),
    ]
    for cmd, name in pkg_managers:
        try:
            process = await asyncio.create_subprocess_shell(
                f"command -v {cmd} >/dev/null 2>&1 && echo found || true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
            if b"found" in stdout:
                info["package_manager"] = name
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # Detect AUR helper (Arch-based only)
    if info.get("package_manager") == "pacman":
        aur_helpers = ["yay", "paru", "pikaur", "trizen", "aurman"]
        for helper in aur_helpers:
            try:
                process = await asyncio.create_subprocess_shell(
                    f"command -v {helper} >/dev/null 2>&1 && echo found || true",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
                if b"found" in stdout:
                    info["aur_helper"] = helper
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    return info


async def _auto_snapshot_software(triggers: set[str]) -> dict[str, object]:
    """Run targeted snapshots based on what triggered, update inventory.json.

    Writes to inventory.json (NOT profile.json) to keep the LLM context lean.

    Triggers:
        - "packages": Snapshot installed apps in tracked categories
        - "services": Snapshot enabled systemd services
        - "defaults": Snapshot XDG default applications
    """

    if not os.environ.get(HOST_PROFILE_ENV) or not os.environ.get(HOST_ROOT_ENV):
        return {}

    try:
        _get_host_dir()
    except (RuntimeError, ValueError):
        return {}

    snapshot: dict[str, object] = {}

    # Package changes -> snapshot tracked packages + defaults (install may change defaults)
    if "packages" in triggers:
        snapshot["packages"] = await _snapshot_tracked_packages()
        snapshot["defaults"] = await _snapshot_defaults()  # May have changed

    # Service changes -> snapshot enabled services
    if "services" in triggers:
        snapshot["enabled_services"] = await _snapshot_enabled_services()

    # Default app changes -> just snapshot defaults
    if "defaults" in triggers and "packages" not in triggers:
        snapshot["defaults"] = await _snapshot_defaults()

    if not snapshot:
        return {}

    # Update inventory.json (not profile.json) with snapshot
    current = _load_inventory()
    snapshot["snapshot_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    merged = _deep_merge(current, snapshot)
    _save_inventory(merged)
    _append_delta("inventory_snapshot", snapshot, "Auto-snapshot after command")

    return snapshot


def _detect_snapshot_triggers(command: str) -> set[str]:
    """Check if a command should trigger a state snapshot.

    Returns a set of trigger categories: "packages", "services", "defaults"
    """
    triggers: set[str] = set()
    for pattern, trigger in _SNAPSHOT_TRIGGERS:
        if re.search(pattern, command, re.IGNORECASE):
            triggers.add(trigger)
    return triggers


def _detect_desktop_environment() -> str:
    """Detect the current desktop environment type."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    session = os.environ.get("DESKTOP_SESSION", "").lower()

    if "gnome" in desktop or "gnome" in session or "unity" in desktop:
        return "gnome"
    elif "kde" in desktop or "plasma" in desktop or "kde" in session:
        return "kde"
    else:
        return "generic"


def _has_gui_session() -> bool:
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return True

    uid = os.getuid()
    runtime_dir = Path(f"/run/user/{uid}")
    if runtime_dir.exists():
        if any((runtime_dir / name).exists() for name in ("wayland-0", "wayland-1", "wayland-2")):
            return True

    return Path("/tmp/.X11-unix").exists()


def _settings_panels_enabled() -> bool:
    value = os.environ.get(SETTINGS_PANELS_ENV, "").strip().lower()
    if value in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    if value in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    return _has_gui_session()


async def _open_settings_panel(command: str) -> str | None:
    """Check if command triggers a settings panel and open it.

    Returns the panel command that was launched, or None.
    """
    if not _settings_panels_enabled():
        return None

    desktop = _detect_desktop_environment()

    for pattern, panels in _SETTINGS_PANELS.items():
        if re.search(pattern, command, re.IGNORECASE):
            panel_cmd = panels.get(desktop) or panels.get("generic")
            if not panel_cmd:
                return None

            # Launch the settings panel in background (fire and forget)
            shell_env = _build_shell_env()
            try:
                await asyncio.create_subprocess_shell(
                    f"nohup {panel_cmd} >/dev/null 2>&1 &",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=shell_env,
                )
                return panel_cmd
            except asyncio.CancelledError:
                raise
            except Exception:
                return None

    return None


def _smart_truncate(text: str, *, success: bool) -> tuple[str, bool]:
    """Truncate output smartly based on command result.

    Success: Return only tail (last N bytes) - LLM just needs confirmation.
    Failure: Return head + tail - context at start, error at end.
    """
    encoded = text.encode("utf-8", errors="replace")
    total_len = len(encoded)

    if success:
        # Success: just the tail is enough
        if total_len <= OUTPUT_TAIL_BYTES:
            return text, False
        tail = encoded[-OUTPUT_TAIL_BYTES:]
        return tail.decode("utf-8", errors="replace"), True
    else:
        # Failure: head + tail for context
        max_bytes = OUTPUT_HEAD_BYTES + OUTPUT_FAIL_TAIL_BYTES
        if total_len <= max_bytes:
            return text, False
        head = encoded[:OUTPUT_HEAD_BYTES]
        tail = encoded[-OUTPUT_FAIL_TAIL_BYTES:]
        separator = b"\n...truncated...\n"
        combined = head + separator + tail
        return combined.decode("utf-8", errors="replace"), True


def _build_shell_env() -> dict[str, str]:
    """Build environment for shell commands with complete system PATH."""

    env = os.environ.copy()

    # Start with comprehensive system paths - no limitations
    system_paths = [
        # User paths
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/bin"),
        # Standard system paths
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
        # Package manager paths
        "/snap/bin",
        "/var/lib/snapd/snap/bin",
        "/var/lib/flatpak/exports/bin",
        os.path.expanduser("~/.local/share/flatpak/exports/bin"),
        # Optional/third-party paths
        "/opt/bin",
        "/opt/local/bin",
        # Common application-specific paths
        "/opt/brave-bin",
        "/opt/google/chrome",
        "/opt/microsoft/msedge",
        "/opt/vivaldi",
        # Games/Steam
        os.path.expanduser("~/.steam/debian-installation/ubuntu12_32"),
        # Language-specific (often have CLI tools)
        os.path.expanduser("~/.cargo/bin"),
        os.path.expanduser("~/.go/bin"),
        os.path.expanduser("~/go/bin"),
        os.path.expanduser("~/.npm-global/bin"),
        os.path.expanduser("~/.yarn/bin"),
        os.path.expanduser("~/.deno/bin"),
        os.path.expanduser("~/.bun/bin"),
        # Ruby
        os.path.expanduser("~/.gem/ruby/*/bin"),
        os.path.expanduser("~/.rbenv/bin"),
        os.path.expanduser("~/.rvm/bin"),
    ]

    current_path = env.get("PATH", "")
    path_parts = current_path.split(":") if current_path else []

    # Add all paths, prioritizing system paths at the end (user paths come first in current)
    for sys_path in system_paths:
        # Handle glob patterns
        if "*" in sys_path:
            matched = glob_module.glob(sys_path)
            for match in matched:
                if match not in path_parts and os.path.isdir(match):
                    path_parts.append(match)
        elif sys_path not in path_parts and os.path.isdir(sys_path):
            path_parts.append(sys_path)

    env["PATH"] = ":".join(path_parts)

    # Ensure display variables are set for GUI apps
    # For Wayland sessions, WAYLAND_DISPLAY is required
    if "WAYLAND_DISPLAY" not in env:
        # Check common Wayland display socket names
        uid = os.getuid()
        xdg_runtime = f"/run/user/{uid}"
        for wayland_name in ["wayland-1", "wayland-0"]:
            if os.path.exists(f"{xdg_runtime}/{wayland_name}"):
                env["WAYLAND_DISPLAY"] = wayland_name
                break

    # Ensure DISPLAY is set for X11/XWayland apps
    if "DISPLAY" not in env:
        # Probe for the actual display - :1 is common for Wayland sessions
        for display_num in [":1", ":0", ":2"]:
            # Just pick :1 as default for Wayland (most common)
            env["DISPLAY"] = ":1"
            break

    # Ensure XDG runtime dir for Wayland/desktop integration
    if "XDG_RUNTIME_DIR" not in env:
        uid = os.getuid()
        xdg_runtime = f"/run/user/{uid}"
        if os.path.isdir(xdg_runtime):
            env["XDG_RUNTIME_DIR"] = xdg_runtime

    # Ensure D-Bus session bus for desktop settings (gsettings, qdbus, etc.)
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        uid = os.getuid()
        dbus_socket = f"/run/user/{uid}/bus"
        if os.path.exists(dbus_socket):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={dbus_socket}"

    return env


async def _run_command(
    command: str,
    *,
    working_directory: str | None,
    timeout_seconds: int,
) -> tuple[str, str, int, float]:
    """Execute a shell command and capture results."""
    shell_env = _build_shell_env()

    start = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory or None,
            env=shell_env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=float(timeout_seconds),
            )
            exit_code = process.returncode if process.returncode is not None else -1
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            exit_code = -1
            if not stderr_bytes:
                stderr_bytes = (
                    f"Process timed out after {timeout_seconds} seconds".encode()
                )

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.perf_counter() - start) * 1000
        message = f"Error executing command: {exc}"
        return "", message, -1, duration_ms

    duration_ms = (time.perf_counter() - start) * 1000

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    return stdout, stderr, exit_code, duration_ms


async def _execute_and_log(
    command: str,
    *,
    working_directory: str | None,
    timeout_seconds: int,
) -> dict[str, object]:
    """Execute a shell command and persist the full output to a log file."""

    stdout, stderr, exit_code, duration_ms = await _run_command(
        command,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )

    success = exit_code == 0
    truncated_stdout, truncated_stdout_flag = _smart_truncate(stdout, success=success)
    truncated_stderr, truncated_stderr_flag = _smart_truncate(stderr, success=success)
    truncated = truncated_stdout_flag or truncated_stderr_flag

    # Clean up old logs before writing new one
    _cleanup_old_logs()

    # Persist the full (pre-truncated) output for retrieval
    log_id = uuid.uuid4().hex
    log_payload = {
        "log_id": log_id,
        "command": command,
        "working_directory": working_directory,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "timestamp": time.time(),
        "truncated": truncated,
    }

    log_path = _get_log_dir() / f"{log_id}.json"
    log_path.write_text(
        json.dumps(log_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result: dict[str, object] = {
        "stdout": truncated_stdout,
        "stderr": truncated_stderr,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "log_id": log_id,
        "command": command,
        "working_directory": working_directory,
    }

    # Auto-snapshot if command succeeded and triggers snapshot
    if exit_code == 0:
        triggers = _detect_snapshot_triggers(command)
        if triggers:
            snapshot = await _auto_snapshot_software(triggers)
            if snapshot:
                result["profile_updated"] = True
                result["software_snapshot"] = snapshot

        # Auto-open settings panel for configuration commands
        panel_opened = await _open_settings_panel(command)
        if panel_opened:
            result["settings_panel_opened"] = panel_opened

    return result


async def _find_path(name: str) -> str | None:
    """Search for a file/directory by name under home. Returns first match or None."""
    home = os.path.expanduser("~")
    # Escape name to prevent shell injection
    safe_name = shlex.quote(f"*{name}*")
    try:
        proc = await asyncio.create_subprocess_shell(
            (
                f"find {shlex.quote(home)} -maxdepth 4 "
                f"\\( -type d -o -type f \\) -iname {safe_name} 2>/dev/null | head -1"
            ),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        result = stdout.decode().strip()
        return result if result else None
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _launch_gui_app(
    command: str,
    working_directory: str | None = None,
) -> dict[str, object]:
    """Launch a GUI application in background. Searches for path if not found."""
    shell_env = _build_shell_env()
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    base_cmd = parts[0] if parts else command

    # For xdg-open: if path doesn't exist, search for it
    if base_cmd == "xdg-open" and len(parts) >= 2:
        target = parts[1]
        if not target.startswith(("http://", "https://", "file://")):
            expanded = os.path.expanduser(target)
            if not os.path.exists(expanded):
                # Extract the name to search for (last component)
                search_name = os.path.basename(expanded.rstrip("/"))
                found = await _find_path(search_name)
                if found:
                    command = f"xdg-open {shlex.quote(found)}"
                else:
                    return {
                        "status": "error",
                        "command": command,
                        "error": f"Path not found: {expanded}",
                    }

    # Use setsid to create new session, nohup to ignore hangup,
    # redirect all I/O to /dev/null to fully detach
    detached_command = f"setsid nohup {command} >/dev/null 2>&1 &"

    start = time.perf_counter()
    try:
        await asyncio.create_subprocess_shell(
            detached_command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=working_directory or None,
            env=shell_env,
            start_new_session=True,
        )
        # Don't wait for the process - it's fully detached
        # Just give it a moment to spawn
        await asyncio.sleep(0.1)
        duration_ms = (time.perf_counter() - start) * 1000

        return {
            "status": "launched",
            "command": command,
            "app": base_cmd,
            "background": True,
            "duration_ms": duration_ms,
            "message": f"GUI app '{base_cmd}' launched in background",
        }
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "status": "error",
            "command": command,
            "error": str(exc),
            "duration_ms": duration_ms,
        }


def _validate_timeout(timeout_seconds: int) -> int:
    """Validate and clamp timeout to safe bounds."""
    return max(TIMEOUT_MIN_SECONDS, min(timeout_seconds, TIMEOUT_MAX_SECONDS))


def _validate_working_directory(working_directory: str | None) -> str | None:
    """Validate working directory to prevent path traversal.

    Returns the validated path or None. Raises ValueError for invalid paths.
    """
    if working_directory is None:
        return None

    # Expand user home (~)
    expanded = os.path.expanduser(working_directory)

    # Resolve to absolute path to catch traversal attempts
    try:
        resolved = Path(expanded).resolve()
    except (OSError, ValueError) as e:
        raise ValueError(f"Invalid working directory path: {e}") from e

    # Must exist and be a directory
    if not resolved.exists():
        raise ValueError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")

    return str(resolved)


@mcp.tool("shell_session")  # type: ignore
async def shell_session(
    command: str,
    session_id: str | None = None,
    timeout_seconds: int = 30,
) -> str:
    """Run a command in a persistent shell session (headless/non-interactive).

    IMPORTANT: This is a headless automation environment - NO human is available
    to provide input. Commands are automatically modified for non-interactive use:
    - Package managers get --noconfirm/-y flags auto-added
    - Interactive editors (vim, nano) will fail - use sed/cat instead
    - Commands requiring password prompts need sudo with NOPASSWD configured

    PREFER THIS over shell_execute for multi-step tasks. The session persists:
    - cd changes carry over to next command
    - Environment variables persist (export FOO=bar)
    - Background jobs continue running
    - Command history within session

    Workflow example (bluetooth connect):
    1. shell_session(command="bluetoothctl devices | grep -i pixel")
       → Returns session_id, use it for subsequent commands
    2. shell_session(command="bluetoothctl connect XX:XX:XX", session_id="abc123")
       → Same session, can reference previous context

    Args:
        command: The command to run (will be auto-modified for non-interactive use)
        session_id: Reuse an existing session. Omit to create new session.
        timeout_seconds: Max time to wait for command (default 30s, max 600s)

    Returns: JSON with output, exit_code, cwd, session_id.
    Always returns session_id - use it for follow-up commands.

    Sessions auto-expire after 5 min idle or 1 hour total.
    """
    # Validate inputs
    timeout_seconds = _validate_timeout(timeout_seconds)
    start = time.perf_counter()
    sess: ShellSession | None = None

    # Preprocess command for headless automation
    original_command = command
    modified_command, automation_warnings = _make_command_noninteractive(command)
    command_was_modified = modified_command != original_command

    try:
        sess = await _get_or_create_session(session_id)
        output, exit_code, cwd = await _run_in_session(sess, modified_command, timeout_seconds)
        duration_ms = (time.perf_counter() - start) * 1000

        result: dict = {
            "status": "ok",
            "output": output,
            "exit_code": exit_code,
            "cwd": cwd,
            "session_id": sess.session_id,
            "command_count": sess.command_count,
            "duration_ms": duration_ms,
        }

        # Include automation info if command was modified or has warnings
        if command_was_modified:
            result["command_modified"] = modified_command
            result["original_command"] = original_command
        if automation_warnings:
            result["automation_notes"] = automation_warnings

        return json.dumps(result)

    except TimeoutError as e:
        duration_ms = (time.perf_counter() - start) * 1000
        if sess:
            await _close_session(sess.session_id)

        # Provide helpful context for timeouts
        timeout_hints = []
        if automation_warnings:
            timeout_hints.extend(automation_warnings)
        timeout_hints.append(
            f"💡 Command ran for {timeout_seconds}s without completing. "
            "Possible causes: waiting for input (interactive command), "
            "network delay, or slow operation."
        )

        return json.dumps(
            {
                "status": "timeout",
                "error": str(e),
                "session_id": sess.session_id if sess else session_id,
                "duration_ms": duration_ms,
                "command_executed": modified_command if command_was_modified else command,
                "hints": timeout_hints,
            }
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        if sess:
            await _close_session(sess.session_id)
        return json.dumps(
            {
                "status": "error",
                "error": str(e),
                "session_id": sess.session_id if sess else session_id,
                "duration_ms": duration_ms,
                "automation_notes": automation_warnings if automation_warnings else None,
            }
        )


@mcp.tool("shell_session_close")  # type: ignore
async def shell_session_close(session_id: str) -> str:
    """Close a shell session explicitly.

    Use when done with a multi-step task to free resources.
    Sessions also auto-expire after 5 min idle.
    """
    closed = await _close_session(session_id)
    return json.dumps(
        {
            "status": "ok" if closed else "not_found",
            "session_id": session_id,
            "message": "Session closed" if closed else "Session not found",
        }
    )


@mcp.tool("shell_session_list")  # type: ignore
async def shell_session_list() -> str:
    """List all active shell sessions.

    Shows session IDs, age, last command time, and current directory.
    """
    await _cleanup_expired_sessions()

    sessions_info = []
    for sid, sess in _sessions.items():
        sessions_info.append(
            {
                "session_id": sid,
                "cwd": sess.cwd,
                "command_count": sess.command_count,
                "age_seconds": round(time.time() - sess.created_at, 1),
                "idle_seconds": round(time.time() - sess.last_used, 1),
                "alive": sess.is_alive(),
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "sessions": sessions_info,
            "count": len(sessions_info),
        }
    )


@mcp.tool("shell_reset_all_sessions")  # type: ignore
async def shell_reset_all_sessions() -> str:
    """Close ALL active shell sessions.

    Use this when starting fresh (e.g., clearing chat history) to free resources.
    Individual sessions can be closed with shell_session_close instead.

    Returns: JSON with count of sessions closed.
    """
    async with _sessions_lock:
        count = len(_sessions)
        for sid, sess in list(_sessions.items()):
            if sess.is_alive():
                sess.process.terminate()
                try:
                    await asyncio.wait_for(sess.process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    sess.process.kill()
        _sessions.clear()

    return json.dumps(
        {
            "status": "ok",
            "sessions_closed": count,
            "message": f"Closed {count} shell session(s)",
        }
    )


@mcp.tool("shell_execute")  # type: ignore
async def shell_execute(
    command: str,
    working_directory: str | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
) -> str:
    """Execute a shell command (one-shot, headless/non-interactive).

    IMPORTANT: This is a headless automation environment - NO human is available
    to provide input. Commands are automatically modified for non-interactive use:
    - Package managers get --noconfirm/-y flags auto-added
    - Interactive editors (vim, nano) will fail - use sed/cat instead

    For multi-step tasks, PREFER shell_session instead - it maintains
    state between commands (cd, env vars, etc.).

    ⚠️ For file editing, use file_edit() instead of sed/echo/cat.
       file_edit handles quoting, atomic writes, and backups automatically.

    Use shell_execute for:
    - Simple one-off commands
    - GUI app launches (with background=true)
    - System queries (ls, stat, df, etc.)

    Args:
        background: Set True for GUI apps (wizards, dialogs, editors).
                    Returns immediately without waiting for app to close.
        timeout_seconds: Max time to wait (default 30s, max 600s)

    Returns: stdout, stderr, exit_code, duration_ms, log_id.
    If truncated=true, use shell_get_full_output(log_id).
    """
    # Validate inputs
    timeout_seconds = _validate_timeout(timeout_seconds)
    try:
        working_directory = _validate_working_directory(working_directory)
    except ValueError as e:
        return json.dumps(
            {
                "status": "error",
                "error": str(e),
                "command": command,
            }
        )

    # Preprocess command for headless automation (skip for background/GUI apps)
    original_command = command
    automation_warnings: list[str] = []
    if not background:
        command, automation_warnings = _make_command_noninteractive(command)
    command_was_modified = command != original_command

    if background:
        result = await _launch_gui_app(command, working_directory)
        return json.dumps(result)

    result = await _execute_and_log(
        command,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )

    # Add automation info to result
    if command_was_modified:
        result["command_modified"] = command
        result["original_command"] = original_command
    if automation_warnings:
        result["automation_notes"] = automation_warnings

    return json.dumps(result)


@mcp.tool("shell_get_full_output")  # type: ignore
async def shell_get_full_output(
    log_id: str,
    offset: int = 0,
    limit: int = 100000,
) -> str:
    """Retrieve full command output when shell_execute returned truncated=true.

    Args:
        log_id: The log_id returned by shell_execute
        offset: Byte offset to start reading from (for chunked retrieval)
        limit: Maximum bytes to return (default 100KB)

    Logs are retained for 48 hours.
    """

    log_path = _get_log_dir() / f"{log_id}.json"
    if not log_path.exists():
        return json.dumps(
            {
                "status": "error",
                "message": f"Log not found for id {log_id}",
                "log_id": log_id,
            }
        )

    try:
        payload = json.loads(log_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.dumps(
            {
                "status": "error",
                "message": "Stored log is corrupted",
                "log_id": log_id,
            }
        )

    start = max(offset, 0)
    end = start + limit
    stdout = payload.get("stdout", "")
    stderr = payload.get("stderr", "")

    response = {
        **payload,
        "stdout": stdout[start:end] if isinstance(stdout, str) else "",
        "stderr": stderr[start:end] if isinstance(stderr, str) else "",
        "offset": start,
        "limit": limit,
    }
    return json.dumps(response)


# =============================================================================
# Host Profile Tools
# =============================================================================


@mcp.tool("host_get_profile")  # type: ignore
async def host_get_profile() -> str:
    """Get lean host profile for shell control context.

    Contains only what the LLM needs to operate the system:
    - host_id, os, desktop, display, sudo
    - tools: screenshot, clipboard, windows, open_url, file_manager
    - quirks: edge cases and workarounds

    Package lists, services, and defaults are in inventory.json (not returned).
    """

    try:
        profile = _load_profile()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return json.dumps(
            {
                "status": "error",
                "host_id": _get_host_id_safe(),
                "message": str(exc),
            }
        )

    return json.dumps(
        {"status": "ok", "host_id": _get_host_id_safe(), "profile": profile}
    )


async def _refresh_system_inventory() -> dict[str, object]:
    """Internal: Refresh system inventory after updates.

    Called by system_update after successful package updates.
    Updates inventory.json with current packages, services, defaults.
    """
    # Detect static system info
    system_info = await _detect_system_info()

    # Snapshot dynamic software state
    packages = await _snapshot_tracked_packages()
    services = await _snapshot_enabled_services()
    defaults = await _snapshot_defaults()

    # Build full inventory snapshot
    inventory_update: dict[str, object] = {
        "system": system_info,
        "snapshot_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if packages:
        inventory_update["packages"] = packages
    if services:
        inventory_update["enabled_services"] = services
    if defaults:
        inventory_update["defaults"] = defaults

    # Save to inventory.json
    current_inventory = _load_inventory()
    merged_inventory = _deep_merge(current_inventory, inventory_update)
    _save_inventory(merged_inventory)
    _append_delta("system_detect", inventory_update, "Auto-detected system info")

    # Update lean profile with only essential context
    lean_update = {}
    if "os" in system_info:
        lean_update["os"] = system_info["os"]
    if "desktop" in system_info:
        lean_update["desktop"] = system_info["desktop"]
    if "display_server" in system_info:
        lean_update["display"] = system_info["display_server"]

    if lean_update:
        try:
            current_profile = _load_profile()
        except (FileNotFoundError, ValueError, RuntimeError):
            current_profile = {}
        merged_profile = _deep_merge(current_profile, lean_update)
        merged_profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_profile(merged_profile)

    return inventory_update


# =============================================================================
# System Maintenance Tools
# =============================================================================

# Paths to maintenance scripts (user's ~/.config/scripts/)
_MAINTENANCE_SCRIPTS = {
    "update_system": "~/.config/scripts/update-system.sh",
    "backup_system": "~/.config/scripts/backup-system.sh",
}


async def _run_maintenance_script(script_key: str, timeout_seconds: int = 300) -> dict:
    """Run a maintenance script and return structured results."""
    script_path = os.path.expanduser(_MAINTENANCE_SCRIPTS.get(script_key, ""))

    if not script_path or not os.path.isfile(script_path):
        return {
            "status": "error",
            "script": script_key,
            "message": f"Script not found: {script_path}",
        }

    if not os.access(script_path, os.X_OK):
        return {
            "status": "error",
            "script": script_key,
            "message": f"Script not executable: {script_path}",
        }

    shell_env = _build_shell_env()
    start = time.perf_counter()

    try:
        process = await asyncio.create_subprocess_shell(
            f"bash {shlex.quote(script_path)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=shell_env,
        )

        stdout_bytes, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=float(timeout_seconds),
        )

        exit_code = process.returncode if process.returncode is not None else -1
        duration_ms = (time.perf_counter() - start) * 1000
        output = stdout_bytes.decode("utf-8", errors="replace")

        # Smart truncate for LLM consumption
        truncated_output, was_truncated = _smart_truncate(output, success=(exit_code == 0))

        return {
            "status": "ok" if exit_code == 0 else "error",
            "script": script_key,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "output": truncated_output,
            "truncated": was_truncated,
        }

    except asyncio.TimeoutError:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "status": "timeout",
            "script": script_key,
            "message": f"Script timed out after {timeout_seconds} seconds",
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "status": "error",
            "script": script_key,
            "message": str(exc),
            "duration_ms": duration_ms,
        }


# =============================================================================
# File Editing Tool
# =============================================================================


@mcp.tool("file_edit")  # type: ignore
async def file_edit(
    path: str,
    operation: str,  # "read" | "write" | "patch" | "append"
    content: str | None = None,
    find: str | None = None,
    replace: str | None = None,
    backup: bool = True,
) -> str:
    """Structured file editing. Use instead of shell sed/echo for file changes.

    Operations:
        read   - Load file content (returns content in response)
        write  - Replace entire file with content
        patch  - Find and replace text (requires find and replace params)
        append - Add content to end of file

    Args:
        path: Absolute or ~ path to file
        operation: One of read, write, patch, append
        content: File content for write/append operations
        find: Text to find (patch operation only)
        replace: Replacement text (patch operation only)
        backup: Create .bak file before modifying (default True)

    Returns:
        JSON with status, path, and operation-specific data.
        For read: includes content
        For patch: includes occurrences_replaced count

    Examples:
        file_edit("/tmp/test.py", "read")
        file_edit("/tmp/test.py", "write", content="print('hello')")
        file_edit("/tmp/test.py", "patch", find="hello", replace="world")
        file_edit("~/.bashrc", "append", content="\\nalias ll='ls -la'")
    """
    # Normalize path
    try:
        file_path = Path(path).expanduser().resolve()
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Invalid path: {e}"})

    valid_ops = ("read", "write", "patch", "append")
    if operation not in valid_ops:
        return json.dumps({
            "status": "error",
            "error": f"Invalid operation: {operation}. Must be one of: {valid_ops}",
        })

    # === READ ===
    if operation == "read":
        if not file_path.exists():
            return json.dumps({
                "status": "error",
                "error": f"File not found: {file_path}",
            })
        if not file_path.is_file():
            return json.dumps({
                "status": "error",
                "error": f"Path is not a file: {file_path}",
            })
        try:
            content_read = file_path.read_text(encoding="utf-8")
            return json.dumps({
                "status": "ok",
                "operation": "read",
                "path": str(file_path),
                "size_bytes": len(content_read.encode("utf-8")),
                "content": content_read,
            })
        except UnicodeDecodeError:
            return json.dumps({
                "status": "error",
                "error": "File is not valid UTF-8 text",
            })
        except PermissionError:
            return json.dumps({
                "status": "error",
                "error": f"Permission denied: {file_path}",
            })

    # === WRITE ===
    if operation == "write":
        if content is None:
            return json.dumps({
                "status": "error",
                "error": "content is required for write operation",
            })

        # Create parent dirs if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Backup existing file
        if backup and file_path.exists():
            bak_path = file_path.with_suffix(file_path.suffix + ".bak")
            try:
                bak_path.write_bytes(file_path.read_bytes())
            except Exception as e:
                return json.dumps({
                    "status": "error",
                    "error": f"Failed to create backup: {e}",
                })

        # Atomic write via temp file
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(file_path)
            return json.dumps({
                "status": "ok",
                "operation": "write",
                "path": str(file_path),
                "size_bytes": len(content.encode("utf-8")),
                "backup_created": backup and file_path.with_suffix(
                    file_path.suffix + ".bak"
                ).exists(),
            })
        except PermissionError:
            return json.dumps({
                "status": "error",
                "error": f"Permission denied: {file_path}",
            })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Write failed: {e}",
            })

    # === PATCH ===
    if operation == "patch":
        if find is None or replace is None:
            return json.dumps({
                "status": "error",
                "error": "find and replace are required for patch operation",
            })
        if not file_path.exists():
            return json.dumps({
                "status": "error",
                "error": f"File not found: {file_path}",
            })

        try:
            original = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return json.dumps({
                "status": "error",
                "error": "File is not valid UTF-8 text",
            })
        except PermissionError:
            return json.dumps({
                "status": "error",
                "error": f"Permission denied: {file_path}",
            })

        occurrences = original.count(find)
        if occurrences == 0:
            return json.dumps({
                "status": "error",
                "error": f"Pattern not found in file: {find[:100]}...",
            })

        patched = original.replace(find, replace)

        # Backup
        if backup:
            bak_path = file_path.with_suffix(file_path.suffix + ".bak")
            try:
                bak_path.write_text(original, encoding="utf-8")
            except Exception as e:
                return json.dumps({
                    "status": "error",
                    "error": f"Failed to create backup: {e}",
                })

        # Atomic write
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        try:
            tmp_path.write_text(patched, encoding="utf-8")
            tmp_path.replace(file_path)
            return json.dumps({
                "status": "ok",
                "operation": "patch",
                "path": str(file_path),
                "occurrences_replaced": occurrences,
                "backup_created": backup,
            })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Write failed: {e}",
            })

    # === APPEND ===
    if operation == "append":
        if content is None:
            return json.dumps({
                "status": "error",
                "error": "content is required for append operation",
            })

        # Create parent dirs if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Backup existing file
        if backup and file_path.exists():
            try:
                original = file_path.read_text(encoding="utf-8")
                bak_path = file_path.with_suffix(file_path.suffix + ".bak")
                bak_path.write_text(original, encoding="utf-8")
            except Exception as e:
                return json.dumps({
                    "status": "error",
                    "error": f"Failed to create backup: {e}",
                })
        else:
            original = ""

        # Read existing content and append
        if file_path.exists():
            try:
                original = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return json.dumps({
                    "status": "error",
                    "error": "File is not valid UTF-8 text",
                })

        new_content = original + content

        # Atomic write
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        try:
            tmp_path.write_text(new_content, encoding="utf-8")
            tmp_path.replace(file_path)
            return json.dumps({
                "status": "ok",
                "operation": "append",
                "path": str(file_path),
                "appended_bytes": len(content.encode("utf-8")),
                "total_size_bytes": len(new_content.encode("utf-8")),
                "backup_created": backup,
            })
        except PermissionError:
            return json.dumps({
                "status": "error",
                "error": f"Permission denied: {file_path}",
            })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Append failed: {e}",
            })

    # Should never reach here
    return json.dumps({"status": "error", "error": "Unknown operation"})


# =============================================================================
# System Maintenance Tools
# =============================================================================


@mcp.tool("system_update")  # type: ignore
async def system_update() -> str:
    """Install and update packages, extensions, and apps.

    Includes:
    - System packages (pacman + AUR via yay)
    - VS Code Insiders extensions
    - Tarball apps (Antigravity, etc. from ~/Downloads)
    - Package cache cleanup + orphan removal

    Does NOT back up configs. For backups, use system_backup.
    Fully automatic with no prompts. Runs ~/.config/scripts/update-system.sh.
    Typical runtime: 1-5 minutes.

    Returns:
        JSON with status, exit_code, and output summary.
    """
    result = await _run_maintenance_script("update_system", timeout_seconds=600)

    # Trigger a software snapshot after successful update
    if result.get("status") == "ok":
        try:
            snapshot = await _auto_snapshot_software({"packages", "services"})
            if snapshot:
                result["profile_updated"] = True
        except Exception:
            pass  # Non-critical, don't fail the update

    return json.dumps(result)


@mcp.tool("system_backup")  # type: ignore
async def system_backup() -> str:
    """Back up everything: Timeshift snapshot + dotfiles git sync.

    Includes:
    - Timeshift snapshot (system-level, excludes /home)
    - Dotfiles sync, commit, and push to git remote

    Run this BEFORE making major system changes or after config tweaks.
    Fully automatic with no prompts. Runs ~/.config/scripts/backup-system.sh.
    Typical runtime: 1-3 minutes.

    Returns:
        JSON with status, exit_code, and backup details.
    """
    result = await _run_maintenance_script("backup_system", timeout_seconds=300)
    return json.dumps(result)


def _shutdown_sessions() -> None:
    """Synchronously terminate all active shell sessions on shutdown.

    Called via atexit to prevent orphaned bash processes.
    """
    for sid, sess in list(_sessions.items()):
        if sess.is_alive():
            try:
                sess.process.terminate()
            except Exception:
                pass
            # Best-effort cleanup without relying on an event loop
            time.sleep(0.05)
            try:
                sess.process.kill()
            except Exception:
                pass  # Nothing more we can do
    _sessions.clear()


# Register shutdown handler to cleanup sessions on exit
atexit.register(_shutdown_sessions)


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:  # pragma: no cover - CLI helper
    import argparse
    parser = argparse.ArgumentParser(description="Shell Control MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "shell_session",
    "shell_session_close",
    "shell_session_list",
    "shell_reset_all_sessions",
    "shell_execute",
    "shell_get_full_output",
    "host_get_profile",
    "file_edit",
    "system_update",
    "system_backup",
    "run",
]
