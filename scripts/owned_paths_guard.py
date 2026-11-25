#!/usr/bin/env python3
"""PreToolUse guard enforcing STEAK_AGY_OWNED_PATHS on file mutations."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402


def is_within(target: Path, allowed: Path) -> bool:
    try:
        target.relative_to(allowed)
        return True
    except ValueError:
        return False


def main() -> None:
    raw_owned = os.environ.get("STEAK_AGY_OWNED_PATHS", "").strip()
    if not raw_owned:
        print(json.dumps({"decision": "allow"}))
        return

    payload = ut_common.read_hook_stdin()
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    tool_call = payload.get("toolCall") or {}
    args = tool_call.get("args") or {}
    target_raw = args.get("Path") or args.get("path") or args.get("file_path")
    if not isinstance(target_raw, str) or not target_raw.strip():
        print(json.dumps({"decision": "allow"}))
        return

    target = Path(target_raw).expanduser()
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve()

    allowed_roots = []
    for entry in raw_owned.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        p = Path(entry).expanduser()
        if not p.is_absolute():
            p = cwd / p
        allowed_roots.append(p.resolve())

    if any(is_within(target, root) for root in allowed_roots):
        print(json.dumps({"decision": "allow"}))
    else:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": f"steak-agy owned-paths guard: {target} is outside STEAK_AGY_OWNED_PATHS.",
                }
            )
        )


if __name__ == "__main__":
    main()
