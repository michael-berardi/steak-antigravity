#!/usr/bin/env python3
"""PreToolUse guard enforcing STEAK_AGY_OWNED_PATHS on file-mutation tools.

Note: this guards Antigravity's structured file-edit tools (`write_file`,
`edit_file`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`).
Shell commands (`run_command`) are governed separately by Antigravity's sandbox
and permission prompts.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402

TARGET_KEYS = ("Path", "path", "file_path", "TargetFile", "AbsolutePath", "FilePath")


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
    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": "steak-agy owned-paths guard: missing workspace cwd in hook payload (fail closed).",
                }
            )
        )
        return
    cwd = Path(raw_cwd).resolve()
    tool_call = payload.get("toolCall") or {}
    args = tool_call.get("args") or {}
    target_raw = None
    if isinstance(args, dict):
        for key in TARGET_KEYS:
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                target_raw = val.strip()
                break
    if not target_raw:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": "steak-agy owned-paths guard: could not extract target file path from tool arguments (fail closed).",
                }
            )
        )
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

    if allowed_roots and any(is_within(target, root) for root in allowed_roots):
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
