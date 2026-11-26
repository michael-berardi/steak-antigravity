"""Per-pane non-PII Antigravity CLI (`agy`) subagent activity publisher for UltraTerm.

Combines two strictly official, local-only Antigravity CLI signals:
1. Official `statusLine` JSON (`scripts/antigravity_statusline.py`): pins the
   pane's foreground coordinator conversation hash and reads `subagents`,
   `task_count`, and `agent_state`.
2. Official lifecycle hooks (`SessionStart`, `PreInvocation`, `PostToolUse`,
   `PostInvocation`, `Stop`): tracks live active-turn leases for child
   conversations (native `/swarm` workers and `invoke_subagent` subagents) so
   UltraTerm updates immediately even while the coordinator is idle and
   `statusLine` does not re-fire.

Design, privacy, security, and ToS-safety rules:
- Zero binary modification, zero network/HTTP calls, zero extra LLM calls.
- Never persists PII (`email`, `cwd`, `transcript_path`, `conversation_id`,
  `conversation_title`, worker names, or prompts). Internal coordination state
  keeps only 16-char SHA-256 hashes inside private `0600` plugin data.
- Strict pane-ownership gate: resolves the tmux pane's live `#{pane_pid}` and
  verifies that this process's first non-shell ancestor IS `pane_pid`. Nested
  subprocesses (such as test runners or shell tools inheriting
  `ULTRATERM_TMUX_SESSION`) are refused and never overwrite or reset pane state.
- Public UltraTerm snapshot at `~/.ultraterm/antigravity/panes/<tmux-session>.json`
  contains strictly bounded numeric counts, owning `agy` PID, epoch timestamp,
  and short allowlisted agent state; written atomically (`0600` in `0700` dir,
  `O_NOFOLLOW`, owner-verified, non-blocking lock with 0.5s deadline).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import ut_common

SCHEMA_VERSION = 1
STATUSLINE_SOURCE = "antigravity-statusline"
HOOK_SOURCE = "steak-antigravity"
MAX_COUNT = 999
MAX_TRACKED_CHILDREN = 256
CHILD_LEASE_TTL_SECS = 15 * 60
COMPLETED_RETENTION_SECS = 6 * 60 * 60
LOCK_TIMEOUT_SECS = 0.5
LOCK_RETRY_SECS = 0.02
STALE_TMP_AGE_SECS = 60 * 60
SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "fish", "env", "timeout"})
ALLOWED_AGENT_STATES = frozenset(
    {"idle", "working", "tool_use", "reviewing", "authenticating", "initializing", "error"}
)


def default_panes_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".ultraterm" / "antigravity" / "panes"


def valid_tmux_session(name: str) -> bool:
    return bool(ut_common.SESSION_NAME.fullmatch(name) or ut_common.AGENT_SESSION_NAME.fullmatch(name))


def _hash_conv_id(conv_id: str) -> str:
    clean = conv_id.strip()
    if not clean:
        return ""
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]


def _clamp_count(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        parsed = int(value)
    except (ValueError, OverflowError):
        return default
    if parsed < 0:
        return default
    return min(parsed, MAX_COUNT)


def _process_row(pid: int) -> tuple[int, str] | None:
    res = ut_common.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], timeout=0.5)
    if not res or res.returncode != 0:
        return None
    parts = res.stdout.strip().split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), os.path.basename(parts[1].strip())


def _pane_pid_for_current_pane() -> int | None:
    pane = ut_common.resolve_pane()
    if not pane:
        return None
    res = ut_common.run(
        ut_common.tmux_command("display-message", "-p", "-t", pane, "#{pane_pid}"),
        timeout=0.5,
    )
    if not res or res.returncode != 0 or not res.stdout.strip().isdigit():
        return None
    pid = int(res.stdout.strip())
    return pid if pid > 1 else None


def resolve_owned_pane_pid() -> int | None:
    """Return the tmux pane PID ONLY if this process's first non-shell ancestor IS that pane PID.

    Prevents nested processes (e.g., `npm test`, subshells, or nested CLIs inheriting
    `ULTRATERM_TMUX_SESSION`) from overwriting or resetting the live pane's telemetry.
    """
    pane_pid = _pane_pid_for_current_pane()
    if pane_pid is None:
        return None
    current = os.getppid()
    for _ in range(8):
        if current == pane_pid:
            return pane_pid
        if current <= 1:
            return None
        row = _process_row(current)
        if row is None:
            return None
        ppid, comm = row
        if comm not in SHELL_NAMES:
            return None
        current = ppid
    return None


def _open_dir_nofollow(path: Path) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError:
        return None


def _ensure_private_dir(path: Path, *, trusted_root: Path | None = None) -> bool:
    """Create/validate every directory component from trusted_root (or $HOME) down to path.

    Refuses symlinks at every checked component, creates missing segments with mode 0700,
    and hardens permissions via fchmod on an O_DIRECTORY|O_NOFOLLOW descriptor (no TOCTOU).
    """
    if not path.is_absolute():
        return False
    root = trusted_root
    if root is None:
        home = Path.home()
        try:
            path.relative_to(home)
            root = home
        except ValueError:
            # Outside $HOME (e.g. test tempdirs): start at the deepest existing ancestor
            # so every created segment and any symlink below it is checked with O_NOFOLLOW.
            probe = path
            while probe.parent != probe:
                try:
                    os.lstat(probe)
                    break
                except FileNotFoundError:
                    probe = probe.parent
                except OSError:
                    return False
            root = probe.parent if probe.is_symlink() else probe
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False

    # Validate the starting root itself when it is not `/` (which on macOS has /var -> /private/var).
    if root != Path("/"):
        root_fd = _open_dir_nofollow(root)
        if root_fd is None:
            return False
        try:
            st = os.fstat(root_fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o022):
                return False
        finally:
            os.close(root_fd)

    cur = root
    for part in rel.parts:
        cur = cur / part
        created = False
        try:
            os.lstat(cur)
        except FileNotFoundError:
            try:
                os.mkdir(cur, 0o700)
                created = True
            except FileExistsError:
                pass
            except OSError:
                return False
        except OSError:
            return False

        fd = _open_dir_nofollow(cur)
        if fd is None:
            return False
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                return False
            # Only chmod directories we created or the leaf directory; never mutate
            # pre-existing user ancestor directories (e.g. ~/.gemini).
            if (created or cur == path) and (st.st_mode & 0o077):
                try:
                    os.fchmod(fd, 0o700)
                except OSError:
                    return False
                st = os.fstat(fd)
            if st.st_mode & 0o022:
                return False
        finally:
            os.close(fd)
    return True


def _pid_is_dead(pid: int) -> bool:
    if pid <= 1:
        return True
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False


def sweep_dead_pane_files(panes_dir: Path, *, max_entries: int = 256) -> int:
    """Remove own pane snapshot files whose owning agy PID no longer exists."""
    removed = 0
    try:
        with os.scandir(panes_dir) as entries:
            count = 0
            for entry in entries:
                if count >= max_entries:
                    break
                name = entry.name
                if not name.endswith(".json") or name.startswith("."):
                    continue
                count += 1
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 4096:
                    continue
                data = _read_private_json(Path(entry.path))
                raw_pid = data.get("pid")
                if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and _pid_is_dead(raw_pid):
                    try:
                        os.unlink(entry.path)
                        removed += 1
                    except OSError:
                        pass
    except OSError:
        pass
    return removed


def sweep_stale_temp_files(directory: Path, *, now: float | None = None) -> None:
    """Best-effort cleanup of own stale `.*.tmp` files older than STALE_TMP_AGE_SECS."""
    cutoff = (time.time() if now is None else now) - STALE_TMP_AGE_SECS
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not (name.startswith(".") and name.endswith(".tmp")):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_mtime < cutoff:
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
    except OSError:
        pass


def _write_json_atomic(target: Path, data: dict, *, trusted_root: Path | None = None) -> bool:
    if not target.is_absolute():
        return False
    if not _ensure_private_dir(target.parent, trusted_root=trusted_root):
        return False
    try:
        st = os.lstat(target)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        if st.st_uid != os.getuid() or st.st_nlink != 1:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False

    body = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
    tmp = target.parent / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(sys.exc_info()[1], OSError):
            return False
        raise
    dir_fd = _open_dir_nofollow(target.parent)
    if dir_fd is not None:
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    sweep_stale_temp_files(target.parent)
    return True


def _read_private_json(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return {}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 64 * 1024:
            return {}
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            parsed = json.loads(handle.read(64 * 1024 + 1).decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        return {}
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


@contextlib.contextmanager
def _state_lock(lock_path: Path, *, trusted_root: Path | None = None, timeout: float = LOCK_TIMEOUT_SECS):
    if not _ensure_private_dir(lock_path.parent, trusted_root=trusted_root):
        yield False
        return
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
        yield False
        return
    acquired = False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            yield False
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_RETRY_SECS)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _fresh_state(pid: int) -> dict:
    return {
        "pid": pid,
        "coordinatorHash": "",
        "coordinatorPinnedByStatusline": False,
        "lastUiCoord": "",
        "activeChildren": {},
        "completedChildren": {},
        "statuslineActive": 0,
        "statuslineCompleted": 0,
        "statuslineAt": 0,
        "taskCount": 0,
        "agentState": "idle",
    }


def _prune_hash_map(raw: object, now: int, ttl_secs: int) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, ts in raw.items():
            if not isinstance(key, str) or len(key) != 16:
                continue
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            ts_int = int(ts)
            if ts_int > now + 300:
                continue
            if now - ts_int <= ttl_secs:
                cleaned[key] = ts_int
    if len(cleaned) > MAX_TRACKED_CHILDREN:
        newest = sorted(cleaned.items(), key=lambda item: item[1], reverse=True)[:MAX_TRACKED_CHILDREN]
        cleaned = dict(newest)
    return cleaned


def _prune_state_maps(state: dict, now: int) -> tuple[dict[str, int], dict[str, int]]:
    active = _prune_hash_map(state.get("activeChildren"), now, CHILD_LEASE_TTL_SECS)
    completed = _prune_hash_map(state.get("completedChildren"), now, COMPLETED_RETENTION_SECS)
    for key in active:
        completed.pop(key, None)
    state["activeChildren"] = active
    state["completedChildren"] = completed
    return active, completed


def _ui_transcript_coordinator_hash(data_dir: Path) -> str:
    """When UI Mode is active, ui-transcript-state/<uuid>.json pins the pane's main conversation."""
    ui_session = os.environ.get("ULTRATERM_UI_SESSION_ID", "").strip()
    if not ut_common.UUID_RE.fullmatch(ui_session):
        return ""
    ui_state = _read_private_json(data_dir / "ui-transcript-state" / f"{ui_session}.json")
    conv_id = ui_state.get("conversationId")
    return _hash_conv_id(conv_id) if isinstance(conv_id, str) else ""


def _build_public_snapshot(state: dict, *, source: str, now: int) -> dict:
    active_map = state.get("activeChildren") or {}
    completed_map = state.get("completedChildren") or {}
    active_children = len(active_map)
    completed_only = len(set(completed_map.keys()) - set(active_map.keys()))
    sl_active = _clamp_count(state.get("statuslineActive"), 0)
    sl_completed = _clamp_count(state.get("statuslineCompleted"), 0)
    agent_state = state.get("agentState")
    if not isinstance(agent_state, str) or agent_state not in ALLOWED_AGENT_STATES:
        agent_state = "idle"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "pid": int(state["pid"]),
        "capturedAt": int(now),
        "activeSubagents": min(MAX_COUNT, max(sl_active, active_children)),
        "inactiveSubagents": min(MAX_COUNT, max(sl_completed, completed_only)),
        "taskCount": _clamp_count(state.get("taskCount"), 0),
        "agentState": agent_state,
    }


def _extract_statusline_subagent_counts(payload: dict) -> tuple[int, int]:
    subagents = payload.get("subagents")
    if not isinstance(subagents, list):
        return 0, 0
    running = 0
    completed = 0
    for item in subagents:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status == "running":
            running += 1
        elif status in ("completed", "done", "finished", "failed", "cancelled", "canceled"):
            completed += 1
    return min(running, MAX_COUNT), min(completed, MAX_COUNT)


def record_statusline(
    payload: dict,
    *,
    tmux_session: str | None = None,
    agy_pid: int | None = None,
    panes_dir: Path | None = None,
    data_dir: Path | None = None,
    now_seconds: int | None = None,
) -> bool:
    """Update per-pane Antigravity activity from an official `agy` statusLine payload."""
    if not isinstance(payload, dict):
        return False
    session = (tmux_session if tmux_session is not None else os.environ.get("ULTRATERM_TMUX_SESSION", "")).strip()
    if not valid_tmux_session(session):
        return False
    pid = agy_pid if agy_pid is not None else resolve_owned_pane_pid()
    if pid is None or pid <= 1:
        return False

    now = int(time.time()) if now_seconds is None else int(now_seconds)
    root_data = data_dir or ut_common.plugin_data_dir()
    state_dir = root_data / "pane-activity"
    state_path = state_dir / f"{session}.json"
    out_dir = panes_dir or default_panes_dir()
    out_path = out_dir / f"{session}.json"
    trusted_data = root_data if data_dir is not None else None
    trusted_out = None if panes_dir is not None else Path.home()

    # Only accept canonical `conversation_id` (never fall back to mismatched `session_id`).
    raw_conv = payload.get("conversation_id")
    coord_hash = _hash_conv_id(raw_conv) if isinstance(raw_conv, str) else ""
    sl_active, sl_completed = _extract_statusline_subagent_counts(payload)
    task_count = _clamp_count(payload.get("task_count"), 0)
    raw_state = str(payload.get("agent_state") or "").strip().lower()
    agent_state = raw_state if raw_state in ALLOWED_AGENT_STATES else "idle"

    with _state_lock(state_dir / f"{session}.lock", trusted_root=trusted_data) as locked:
        if not locked:
            return False
        state = _read_private_json(state_path)
        if state.get("pid") != pid:
            state = _fresh_state(pid)
        active_children, completed_children = _prune_state_maps(state, now)

        if coord_hash:
            prev_coord = str(state.get("coordinatorHash") or "")
            if prev_coord and prev_coord != coord_hash:
                # Coordinator switched conversations (/new or /resume): clear prior child sets.
                active_children = {}
                completed_children = {}
                state["activeChildren"] = active_children
                state["completedChildren"] = completed_children
            state["coordinatorHash"] = coord_hash
            state["coordinatorPinnedByStatusline"] = True
            active_children.pop(coord_hash, None)
            completed_children.pop(coord_hash, None)

        state["statuslineActive"] = sl_active
        state["statuslineCompleted"] = sl_completed
        state["statuslineAt"] = now
        state["taskCount"] = task_count
        state["agentState"] = agent_state

        if not _write_json_atomic(state_path, state, trusted_root=trusted_data):
            return False
        public = _build_public_snapshot(state, source=STATUSLINE_SOURCE, now=now)
        return _write_json_atomic(out_path, public, trusted_root=trusted_out)


def record_hook_event(
    event: str,
    payload: dict,
    *,
    tmux_session: str | None = None,
    agy_pid: int | None = None,
    panes_dir: Path | None = None,
    data_dir: Path | None = None,
    now_seconds: int | None = None,
) -> bool:
    """Update per-pane Antigravity child-conversation leases from official `hooks.json` events."""
    if not isinstance(payload, dict):
        return False
    session = (tmux_session if tmux_session is not None else os.environ.get("ULTRATERM_TMUX_SESSION", "")).strip()
    if not valid_tmux_session(session):
        return False
    conv_id = payload.get("conversationId")
    if not isinstance(conv_id, str) or not conv_id.strip():
        return False
    conv_hash = _hash_conv_id(conv_id)
    if not conv_hash:
        return False
    pid = agy_pid if agy_pid is not None else resolve_owned_pane_pid()
    if pid is None or pid <= 1:
        return False

    now = int(time.time()) if now_seconds is None else int(now_seconds)
    root_data = data_dir or ut_common.plugin_data_dir()
    state_dir = root_data / "pane-activity"
    state_path = state_dir / f"{session}.json"
    out_dir = panes_dir or default_panes_dir()
    out_path = out_dir / f"{session}.json"
    trusted_data = root_data if data_dir is not None else None
    trusted_out = None if panes_dir is not None else Path.home()

    tool_call = payload.get("toolCall")
    tool_name = str(tool_call.get("name") or "") if isinstance(tool_call, dict) else ""

    with _state_lock(state_dir / f"{session}.lock", trusted_root=trusted_data) as locked:
        if not locked:
            return False
        state = _read_private_json(state_path)
        if state.get("pid") != pid:
            state = _fresh_state(pid)
        active_children, completed_children = _prune_state_maps(state, now)

        if event == "session-start":
            sweep_dead_pane_files(out_dir)

        coord_hash = str(state.get("coordinatorHash") or "")
        pinned_by_statusline = bool(state.get("coordinatorPinnedByStatusline"))

        # When statusLine has pinned the foreground coordinator, it is authoritative —
        # ignore ui-transcript-state (which can rotate when child hooks run in the same
        # pane process). Otherwise only react when ui-transcript-state changes on
        # session-start (or to fill an initially empty coordinatorHash).
        if not pinned_by_statusline:
            ui_coord = _ui_transcript_coordinator_hash(root_data)
            last_ui = str(state.get("lastUiCoord") or "")
            if ui_coord and (not coord_hash or (event == "session-start" and ui_coord != last_ui)):
                if coord_hash and ui_coord != coord_hash:
                    active_children = {}
                    completed_children = {}
                    state["activeChildren"] = active_children
                    state["completedChildren"] = completed_children
                coord_hash = ui_coord
                state["coordinatorHash"] = coord_hash
                state["lastUiCoord"] = ui_coord
                active_children.pop(coord_hash, None)
                completed_children.pop(coord_hash, None)
            elif event == "session-start" and not active_children and (
                not coord_hash or state.get("agentState") == "idle"
            ):
                # First session-start for this PID, or /new while coordinator is idle
                # with no active children when neither statusLine nor UI Mode is active.
                if coord_hash and conv_hash != coord_hash:
                    completed_children = {}
                    state["completedChildren"] = completed_children
                coord_hash = conv_hash
                state["coordinatorHash"] = coord_hash
                active_children.pop(coord_hash, None)
                completed_children.pop(coord_hash, None)

        # Fail closed when no coordinator has been pinned yet: never count an unpinned
        # conversation (which may be the coordinator after a mid-session upgrade) as a child.
        if not coord_hash:
            if not _write_json_atomic(state_path, state, trusted_root=trusted_data):
                return False
            public = _build_public_snapshot(state, source=HOOK_SOURCE, now=now)
            return _write_json_atomic(out_path, public, trusted_root=trusted_out)

        if conv_hash == coord_hash:
            if event in ("pre-invocation", "post-tool"):
                state["agentState"] = "working"
            elif event == "stop":
                state["agentState"] = "idle"
            if not _write_json_atomic(state_path, state, trusted_root=trusted_data):
                return False
            public = _build_public_snapshot(state, source=HOOK_SOURCE, now=now)
            return _write_json_atomic(out_path, public, trusted_root=trusted_out)

        # Child conversation (native /swarm worker or invoke_subagent child).
        if event == "stop" or (event == "post-tool" and tool_name == "swarm_wait"):
            was_active = active_children.pop(conv_hash, None) is not None
            if was_active:
                completed_children[conv_hash] = now
                sl_active = _clamp_count(state.get("statuslineActive"), 0)
                if sl_active > 0:
                    state["statuslineActive"] = max(len(active_children), sl_active - 1)
                _prune_state_maps(state, now)
        elif event == "post-invocation":
            # Refresh-only: never resurrect a child lease that already ended via swarm_wait or Stop.
            if conv_hash in active_children:
                active_children[conv_hash] = now
        elif event in ("session-start", "pre-invocation", "post-tool"):
            active_children[conv_hash] = now
            completed_children.pop(conv_hash, None)
            if len(active_children) > MAX_TRACKED_CHILDREN:
                _prune_state_maps(state, now)

        if not _write_json_atomic(state_path, state, trusted_root=trusted_data):
            return False
        public = _build_public_snapshot(state, source=HOOK_SOURCE, now=now)
        return _write_json_atomic(out_path, public, trusted_root=trusted_out)
