#!/usr/bin/env python3
"""PostToolUse hook: Steak Pi verify-after-edit contract for Antigravity CLI.

Reads `.steak-pi/config.json` -> {"verify": {"command", "failLimit", "timeoutMs"}}
in the workspace cwd. After successful write_file/edit_file, runs the verify command
with debouncing, failLimit tracking, and owned process-group reaping.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402

DEBOUNCE_MS = 500
DEFAULT_FAIL_LIMIT = 2
DEFAULT_TIMEOUT_MS = 90_000
MAX_TIMEOUT_MS = 110_000
OUTPUT_TAIL = 4_000
# After failLimit consecutive failures verify pauses (as in Steak Pi), but only for this window,
# so one broken stretch never disables verification for the rest of the conversation.
FAIL_PAUSE_MS = 5 * 60_000
MAX_PENDING_NOTES = 3
EDIT_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}


def is_verify_trusted(cwd: Path) -> bool:
    """Trust gate: never run repo-controlled verify.command without explicit operator trust.

    Trusted when either:
    - STEAK_ANTIGRAVITY_ALLOW_VERIFY=1 is set in the environment, or
    - the resolved repo path appears in a private, user-owned
      `~/.steak-antigravity/trusted-verify-repos.json` allowlist (array of absolute paths).
    """
    if os.environ.get("STEAK_ANTIGRAVITY_ALLOW_VERIFY") == "1":
        return True
    allowlist = ut_common.HOME / ".steak-antigravity/trusted-verify-repos.json"
    ok, _ = ut_common.verify_private_dir(allowlist.parent)
    if not ok or not allowlist.is_file() or allowlist.is_symlink():
        return False
    try:
        st = os.lstat(allowlist)
        if st.st_uid != os.getuid() or (st.st_mode & 0o022):
            return False
        entries = json.loads(allowlist.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(entries, list):
        return False
    resolved_cwd = cwd.resolve()
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            root = Path(entry).expanduser().resolve()
            try:
                resolved_cwd.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def load_config(cwd: Path) -> dict | None:
    try:
        verify = json.loads((cwd / ".steak-pi/config.json").read_text()).get("verify") or {}
    except (OSError, ValueError, AttributeError):
        return None
    command = verify.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    limit = verify.get("failLimit")
    timeout = verify.get("timeoutMs")
    resolved_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT_MS
    return {
        "command": command,
        "failLimit": limit if isinstance(limit, int) and limit > 0 else DEFAULT_FAIL_LIMIT,
        "timeoutMs": min(int(resolved_timeout), MAX_TIMEOUT_MS),
    }


def queue_failure_note(state_dir: Path, conv_id: str, message: str) -> None:
    """PostToolUse output never reaches the model; queue the failure for the next PreInvocation hook."""
    import fcntl

    pending = state_dir / f"{conv_id}.pending.json"
    lock = pending.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            notes: list = []
            try:
                saved = json.loads(pending.read_text())
                if isinstance(saved, dict) and isinstance(saved.get("failures"), list):
                    notes = [n for n in saved["failures"] if isinstance(n, str)]
            except (OSError, ValueError):
                pass
            notes = (notes + [message])[-MAX_PENDING_NOTES:]
            tmp = state_dir / f".{conv_id}.pending.{os.getpid()}.{time.time_ns()}.tmp"
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(tfd, "w", encoding="utf-8") as handle:
                json.dump({"failures": notes}, handle)
            os.replace(tmp, pending)
        finally:
            os.close(fd)
    except OSError as exc:
        sys.stderr.write(f"[steak-antigravity] could not queue verify failure for the agent: {exc}\n")


def main() -> None:
    payload = ut_common.read_hook_stdin()
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name", "")).lower()
    if tool_name not in EDIT_TOOLS or payload.get("error"):
        print("{}")
        return

    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        print("{}")
        return
    cwd = Path(raw_cwd)
    config = load_config(cwd)
    if not config:
        print("{}")
        return
    if not is_verify_trusted(cwd):
        sys.stderr.write(
            f"[steak-antigravity] verify skipped (untrusted repo {cwd}; set STEAK_ANTIGRAVITY_ALLOW_VERIFY=1 "
            "or add repo path to ~/.steak-antigravity/trusted-verify-repos.json)\n"
        )
        print("{}")
        return

    conv_id = str(payload.get("conversationId") or "default").replace("/", "_")
    state_dir = ut_common.plugin_data_dir() / "verify"
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        sys.stderr.write(f"[steak-antigravity] verify state dir unavailable: {exc}\n")
        print("{}")
        return
    ok, reason = ut_common.verify_private_dir(state_dir)
    if not ok:
        sys.stderr.write(f"[steak-antigravity] verify state dir unsafe: {reason}\n")
        print("{}")
        return
    state_path = state_dir / f"{conv_id}.json"

    state = {"consecutive": 0, "lastRunMs": 0}
    try:
        saved = json.loads(state_path.read_text())
        if isinstance(saved, dict):
            for key in state:
                if isinstance(saved.get(key), int) and not isinstance(saved.get(key), bool):
                    state[key] = saved[key]
    except (OSError, ValueError):
        pass

    now = int(time.time() * 1000)
    if now - state["lastRunMs"] < DEBOUNCE_MS:
        print("{}")
        return
    if state["consecutive"] >= config["failLimit"]:
        if now - state["lastRunMs"] < FAIL_PAUSE_MS:
            print("{}")
            return
        state["consecutive"] = 0

    child = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-o", "pipefail", "-c", config["command"]],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = child.communicate(timeout=config["timeoutMs"] / 1000)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            output, _ = child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            output, _ = child.communicate()
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    ok = child.returncode == 0 and not timed_out
    state["consecutive"] = 0 if ok else state["consecutive"] + 1
    state["lastRunMs"] = int(time.time() * 1000)
    tmp = state_dir / f".{conv_id}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, state_path)
    except OSError as exc:
        sys.stderr.write(f"[steak-antigravity] verify state write failed: {exc}\n")
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if not ok:
        tail = output.decode("utf-8", "replace")[-OUTPUT_TAIL:].strip()
        if timed_out:
            tail = (tail + "\n" if tail else "") + f"(timed out after {config['timeoutMs']} ms)"
        message = f"[steak-antigravity] verify failed ({state['consecutive']}/{config['failLimit']}): {config['command']}\n{tail}"
        if state["consecutive"] >= config["failLimit"]:
            message += f"\n(verify paused for {FAIL_PAUSE_MS // 60_000} minutes after {config['failLimit']} consecutive failures)"
        sys.stderr.write(message + "\n")
        queue_failure_note(state_dir, conv_id, message)

    print("{}")


if __name__ == "__main__":
    main()
