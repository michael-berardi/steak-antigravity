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
OUTPUT_TAIL = 4_000
EDIT_TOOLS = {"write_file", "edit_file"}


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
    return {
        "command": command,
        "failLimit": limit if isinstance(limit, int) and limit > 0 else DEFAULT_FAIL_LIMIT,
        "timeoutMs": timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT_MS,
    }


def main() -> None:
    payload = ut_common.read_hook_stdin()
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name", "")).lower()
    if tool_name not in EDIT_TOOLS or payload.get("error"):
        print("{}")
        return

    cwd = Path(payload.get("cwd") or os.getcwd())
    config = load_config(cwd)
    if not config:
        print("{}")
        return

    conv_id = str(payload.get("conversationId") or "default").replace("/", "_")
    state_dir = ut_common.plugin_data_dir() / "verify"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        os.killpg(child.pid, signal.SIGTERM)
        try:
            output, _ = child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            output, _ = child.communicate()
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    ok = child.returncode == 0 and not timed_out
    state["consecutive"] = 0 if ok else state["consecutive"] + 1
    state["lastRunMs"] = int(time.time() * 1000)
    state_path.write_text(json.dumps(state))

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
