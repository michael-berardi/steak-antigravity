#!/usr/bin/env python3
"""PreToolUse guard steering delegation to USAP when active inside UltraTerm."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402


def usap_runtime_ready() -> bool:
    return ut_common.pi_sdk_entry() is not None and ut_common.steak_pi_root() is not None


def main() -> None:
    if os.environ.get("STEAK_ANTIGRAVITY_ALLOW_NATIVE_SUBAGENTS") == "1":
        print(json.dumps({"decision": "allow"}))
        return

    enforce = os.environ.get("STEAK_ANTIGRAVITY_USAP_ONLY") == "1"
    in_ultraterm = bool(os.environ.get("ULTRATERM_TMUX_SESSION"))

    if (enforce or in_ultraterm) and usap_runtime_ready():
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        "In UltraTerm, USAP is the shared delegation route. Use the `usap` MCP tools "
                        "`ultraterm_subagents` and `ultraterm_hub` so waves share routing, caps, telemetry, "
                        "and receipts across agents. Set STEAK_ANTIGRAVITY_ALLOW_NATIVE_SUBAGENTS=1 to override."
                    ),
                }
            )
        )
        return

    print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
