#!/usr/bin/env python3
"""Antigravity Stop hook for Steak AGY.

1. Flushes the final UI Mode transcript row with stopReason="stop".
2. Checks open ut-todo items for the current session (finish guard) and requests
   continuation up to 3 times if solvable items remain open, unless already
   continuing or stopped due to user cancel / error.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
import ut_common  # noqa: E402
import ui_transcript_sync  # noqa: E402

MAX_FINISH_GUARD_PUSHES = 3


def count_open_todos(conversation_id: str) -> list[str]:
    if not conversation_id:
        return []
    todo_dir = ut_common.HOME / ".steak-pi/todo"
    if not todo_dir.is_dir():
        return []
    candidate = todo_dir / f"{conversation_id}.json"
    if not candidate.is_file():
        return []
    try:
        data = json.loads(candidate.read_text())
    except (OSError, ValueError):
        return []
    items = data.get("items") or data.get("tasks") or []
    open_titles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).lower()
        if status in ("pending", "in_progress", "open"):
            open_titles.append(str(item.get("title") or item.get("text") or "untitled"))
    return open_titles


def main() -> None:
    payload = ut_common.read_hook_stdin()
    cwd = str(payload.get("cwd") or os.getcwd())
    conv_id = str(payload.get("conversationId") or "")
    tp = payload.get("transcriptPath")
    source = Path(tp) if isinstance(tp, str) and tp else None

    ui_file_env = os.environ.get("ULTRATERM_UI_TRANSCRIPT_FILE", "").strip()
    ui_session_env = os.environ.get("ULTRATERM_UI_SESSION_ID", "").strip()
    if ui_file_env and ui_session_env:
        try:
            ui_transcript_sync.sync_transcript(
                ui_file=Path(ui_file_env),
                ui_session_id=ui_session_env,
                conversation_id=conv_id,
                source_transcript=source,
                cwd=cwd,
                event_name="stop",
            )
        except OSError:
            pass

    reason = str(payload.get("terminationReason") or "")
    if (
        reason != "NO_TOOL_CALL"
        or payload.get("continuationActive")
        or os.environ.get("STEAK_AGY_FINISH_GUARD") == "0"
    ):
        print("{}")
        return

    open_todos = count_open_todos(conv_id)
    if not open_todos:
        print("{}")
        return

    exec_num = int(payload.get("executionNum") or 0)
    if exec_num >= MAX_FINISH_GUARD_PUSHES:
        print("{}")
        return

    preview = ", ".join(open_todos[:5])
    print(
        json.dumps(
            {
                "decision": "continue",
                "reason": (
                    f"[steak-agy finish-guard] {len(open_todos)} open todo item(s) remain ({preview}). "
                    "Finish them or record a real external blocker before stopping."
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
