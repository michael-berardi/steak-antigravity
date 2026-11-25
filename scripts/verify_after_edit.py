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
    - STEAK_AGY_ALLOW_VERIFY=1 is set in the environment, or
    - the resolved repo path appears in a private, user-owned
      `~/.steak-agy/trusted-verify-repos.json` allowlist (array of absolute paths).
    """
    if os.environ.get("STEAK_AGY_ALLOW_VERIFY") == "1":
        return True
    allowlist = ut_common.HOME / ".steak-agy/trusted-verify-repos.json"
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
            f"[steak-agy] verify skipped (untrusted repo {cwd}; set STEAK_AGY_ALLOW_VERIFY=1 "
            "or add repo path to ~/.steak-agy/trusted-verify-repos.json)\n"
        )
        print("{}")
        return

    conv_id = str(payload.get("conversationId") or "default").replace("/", "_")
    state_dir = ut_common.plugin_data_dir() / "verify"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ok, reason = ut_common.verify_private_dir(state_dir)
    if not ok:
        sys.stderr.write(f"[steak-agy] verify state dir unsafe: {reason}\n")
        print("{}")
        return
    state_path = state_dir / f"{conv_id}.json"

    state = {"consecutive": 0, "lastRunMs": 0}
    try:
        state.update(json.loads(state_path.read_text()))
    except (OSError, ValueError):
        pass

    now = int(time.time() * 1000)
    if state["consecutive"] >= config["failLimit"] or now - state["lastRunMs"] < DEBOUNCE_MS:
        print("{}")
        return

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
    tmp = state_dir / f".{conv_id}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, state_path)
    except OSError as exc:
        sys.stderr.write(f"[steak-agy] verify state write failed: {exc}\n")
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if not ok:
        tail = output.decode("utf-8", "replace")[-OUTPUT_TAIL:].strip()
        if timed_out:
            tail = (tail + "\n" if tail else "") + f"(timed out after {config['timeoutMs']} ms)"
        sys.stderr.write(
            f"[steak-agy] verify failed ({state['consecutive']}/{config['failLimit']}): {config['command']}\n{tail}\n"
        )

    print("{}")


if __name__ == "__main__":
    main()
