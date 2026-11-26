#!/usr/bin/env python3
"""PreToolUse guard steering delegation to USAP when active inside UltraTerm.

Native Antigravity `/swarm` tools (`swarm`, `post_message`, `read_board`,
`swarm_wait`) are never intercepted so explicit `/swarm` sessions coexist
cleanly alongside ordinary USAP delegation.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402

NATIVE_SWARM_TOOLS = frozenset({"swarm", "post_message", "read_board", "swarm_wait"})


def usap_runtime_ready() -> bool:
    """Same prerequisites the usap MCP server checks (scripts/usap_mcp.py); never deny native
    subagents while USAP itself cannot start, or the agent would have no delegation route."""
    return (
        shutil.which("node") is not None
        and ut_common.pi_sdk_entry() is not None
        and ut_common.steak_pi_root() is not None
    )


def extract_tool_name(payload: object) -> str:
    """Return the invoked tool name from a PreToolUse hook payload, or empty string if absent."""
    if not isinstance(payload, Mapping):
        return ""
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, Mapping):
        name = tool_call.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    for key in ("tool_name", "toolName", "name"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def evaluate(
    payload: object,
    env: Mapping[str, str],
    runtime_ready: bool,
) -> dict[str, str]:
    if env.get("STEAK_ANTIGRAVITY_ALLOW_NATIVE_SUBAGENTS") == "1":
        return {"decision": "allow"}

    safe_payload = payload if isinstance(payload, Mapping) else {}
    tool_name = extract_tool_name(safe_payload)
    if tool_name in NATIVE_SWARM_TOOLS:
        return {"decision": "allow"}
    if tool_name and tool_name != "invoke_subagent":
        return {"decision": "allow"}

    enforce = env.get("STEAK_ANTIGRAVITY_USAP_ONLY") == "1"
    in_ultraterm = bool(env.get("ULTRATERM_TMUX_SESSION"))

    if (enforce or in_ultraterm) and runtime_ready:
        return {
            "decision": "deny",
            "reason": (
                "In UltraTerm, USAP is the shared delegation route. Use the `usap` MCP tools "
                "`ultraterm_subagents` and `ultraterm_hub` so waves share routing, caps, telemetry, "
                "and receipts across agents. Set STEAK_ANTIGRAVITY_ALLOW_NATIVE_SUBAGENTS=1 to override."
            ),
        }

    return {"decision": "allow"}


def main() -> None:
    payload = ut_common.read_hook_stdin()
    print(json.dumps(evaluate(payload, os.environ, usap_runtime_ready())))


if __name__ == "__main__":
    main()
