"""Shared helpers for Steak Antigravity (Antigravity CLI plugin for UltraTerm & Steak Pi).

Provider-neutral: carries no hardcoded model names, provider preferences, or
operator-specific paths. All configuration resolves from standard UltraTerm,
Pi, Steak Pi, and Antigravity locations or explicit environment overrides.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
UTP = HOME / ".ultraterm/bin/utp"
RETEX = HOME / ".ultraterm/bin/retex"
PI_AGENT_DIR = Path(os.environ.get("PI_CODING_AGENT_DIR") or (HOME / ".pi/agent")).expanduser()
SHARED_SKILLS = HOME / ".config/agents/skills"
SESSION_NAME = re.compile(r"^ultraterm-matrix-([1-9][0-9]{0,7})$")
AGENT_SESSION_NAME = re.compile(r"^ultraterm-agent-[0-9a-f]{12}$")
PANE_ID = re.compile(r"^%[0-9]+$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_pinned_socket: str | None = None


def plugin_data_dir() -> Path:
    configured = os.environ.get("PLUGIN_DATA", "").strip()
    if configured:
        return Path(configured).expanduser()
    return HOME / ".steak-antigravity/data"


def run(args: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def tmux_binary() -> str:
    return os.environ.get("TMUX_BIN") or "tmux"


def tmux_command(*args: str) -> list[str]:
    pin = ["-S", _pinned_socket, "-f", "/dev/null"] if _pinned_socket else []
    return [tmux_binary(), *pin, *args]


def is_ancestor(pid: int, depth: int = 16) -> bool:
    current = os.getpid()
    for _ in range(depth):
        result = run(["ps", "-o", "ppid=", "-p", str(current)])
        if not result or result.returncode != 0 or not result.stdout.strip().isdigit():
            return False
        current = int(result.stdout.strip())
        if current == pid:
            return True
        if current <= 1:
            return False
    return False


def pane_from_session() -> tuple[str | None, str]:
    session = os.environ.get("ULTRATERM_TMUX_SESSION", "")
    socket_path = os.environ.get("ULTRATERM_TMUX_SOCKET", "")
    if not (SESSION_NAME.fullmatch(session) or AGENT_SESSION_NAME.fullmatch(session)):
        return None, f"ULTRATERM_TMUX_SESSION {session!r} is not an UltraTerm session name"
    if not socket_path.startswith("/") or "\0" in socket_path:
        return None, "ULTRATERM_TMUX_SOCKET is not an absolute path"
    try:
        info = os.lstat(socket_path)
    except OSError as error:
        return None, f"ULTRATERM_TMUX_SOCKET unreadable ({error.strerror or error})"
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        return None, "ULTRATERM_TMUX_SOCKET is not a unix socket owned by this user"
    result = run(
        [
            tmux_binary(),
            "-S",
            socket_path,
            "-f",
            "/dev/null",
            "list-panes",
            "-s",
            "-t",
            f"={session}:",
            "-F",
            "#{session_name}|#{pane_id}|#{pane_pid}|#{pane_dead}",
        ],
        timeout=1.5,
    )
    if not result or result.returncode != 0:
        return None, f"tmux could not list panes of session {session}"
    rows = [line.split("|") for line in result.stdout.splitlines() if line]
    if len(rows) != 1:
        return None, f"session {session} has {len(rows)} panes"
    fields = rows[0]
    if len(fields) != 4 or fields[0] != session or not PANE_ID.fullmatch(fields[1]) or not fields[2].isdigit():
        return None, "unexpected tmux pane record"
    if fields[3] != "0":
        return None, f"pane of session {session} is dead"
    if not is_ancestor(int(fields[2])):
        return None, f"pane {fields[1]} is not an ancestor of this process"
    return fields[1], ""


def resolve_pane() -> str | None:
    global _pinned_socket
    pane = os.environ.get("TMUX_PANE", "")
    if pane:
        return pane if PANE_ID.fullmatch(pane) else None
    if not (os.environ.get("ULTRATERM_TMUX_SOCKET") and os.environ.get("ULTRATERM_TMUX_SESSION")):
        return None
    pane, _ = pane_from_session()
    if pane is not None:
        _pinned_socket = os.environ["ULTRATERM_TMUX_SOCKET"]
    return pane


def pane_session() -> dict | None:
    pane = resolve_pane()
    if not pane:
        return None
    result = run(
        tmux_command(
            "display-message",
            "-p",
            "-t",
            pane,
            "#{session_name}\t#{pane_id}\t#{pane_tty}\t#{session_attached}",
        )
    )
    if not result or result.returncode != 0:
        return None
    parts = result.stdout.rstrip("\n").split("\t")
    if len(parts) != 4 or parts[1] != pane:
        return None
    match = SESSION_NAME.match(parts[0])
    return {
        "tmuxSession": parts[0],
        "pane": pane,
        "tty": parts[2],
        "attachedClients": int(parts[3]) if parts[3].isdigit() else 0,
        "slot": int(match.group(1)) if match else None,
    }


def utp_terminals() -> list[dict] | None:
    if not os.access(UTP, os.X_OK):
        return None
    result = run([str(UTP), "list"], timeout=3.0)
    if not result or result.returncode != 0:
        return None
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0].isdigit() and UUID_RE.match(parts[4]):
            rows.append(
                {
                    "slot": int(parts[0]),
                    "title": parts[1],
                    "harness": parts[2],
                    "profile": parts[3],
                    "terminalId": parts[4],
                }
            )
    return rows


def terminal_identity() -> dict:
    pane = pane_session()
    identity: dict = {
        "insideUltraTerm": pane is not None and pane["slot"] is not None,
        "launchSlotEnv": os.environ.get("ULTRATERM_SLOT"),
        "harness": os.environ.get("ULTRATERM_HARNESS_ID"),
        "launchProfile": os.environ.get("ULTRATERM_HARNESS_PROFILE"),
    }
    if not pane:
        return identity
    identity.update(pane)
    rows = utp_terminals()
    if rows is None:
        identity["appCatalog"] = "unavailable"
        return identity
    row = next((r for r in rows if r["slot"] == pane["slot"]), None)
    if row is None:
        identity["appCatalog"] = "not listed"
    else:
        identity["appCatalog"] = "listed"
        identity["terminalId"] = row["terminalId"]
        identity["catalogHarness"] = row["harness"]
        identity["catalogProfile"] = row["profile"]
    return identity


def pi_sdk_entry(home: Path | None = None) -> Path | None:
    root = (home or HOME) / ".ultraterm/runtime"
    try:
        active = json.loads((root / "active-pi.json").read_text())
        runtime = active.get("runtime")
        if isinstance(runtime, str) and "/" not in runtime and runtime.startswith("pi-"):
            entry = root / runtime / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
            if entry.is_file():
                return entry
    except (OSError, ValueError):
        pass
    return None


def steak_pi_root(home: Path | None = None) -> Path | None:
    base_home = home or HOME
    pi_dir = base_home / ".pi/agent" if home else PI_AGENT_DIR
    candidates: list[Path] = []
    override = os.environ.get("ULTRATERM_STEAK_PI_ROOT", "").strip()
    if override:
        candidates.append(Path(override).expanduser().resolve())
    try:
        settings = json.loads((pi_dir / "settings.json").read_text())
        for entry in settings.get("packages", []) or []:
            if isinstance(entry, str) and not re.match(r"^[a-z+]+:", entry):
                candidates.append((pi_dir / entry).expanduser().resolve())
    except (OSError, ValueError):
        pass
    runtime = base_home / ".ultraterm/runtime"
    try:
        owned = sorted(
            runtime.glob("steak-pi-*/node_modules/steak-pi"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(owned)
    except OSError:
        pass
    for root in candidates:
        try:
            if json.loads((root / "package.json").read_text()).get("name") == "steak-pi":
                return root
        except (OSError, ValueError):
            continue
    return None


def retex_binary(home: Path | None = None) -> str | None:
    configured = os.environ.get("RETEX_BIN", "").strip()
    if configured and os.access(configured, os.X_OK):
        return configured
    candidate = (home or HOME) / ".ultraterm/bin/retex"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def verify_private_dir(path: Path) -> tuple[bool, str]:
    """Validate that path is a real directory owned by this user with no group/other write bits."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return False, f"{path} does not exist or is unreadable: {exc}"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False, f"{path} is not a real directory"
    if st.st_uid != os.getuid():
        return False, f"{path} is not owned by current user"
    if st.st_mode & 0o022:
        return False, f"{path} is group- or world-writable"
    return True, ""


def read_hook_stdin() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
