#!/usr/bin/env python3
"""Antigravity Stop hook for Steak Antigravity.

1. Flushes the final UI Mode transcript row with stopReason="stop".
2. Checks open ut-todo items for the current session (finish guard) and requests
   continuation up to 3 times if solvable items remain open, unless already
   continuing or stopped due to user cancel / error.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
import ut_common  # noqa: E402
import ui_transcript_sync  # noqa: E402

MAX_FINISH_GUARD_PUSHES = 3
MAX_TODO_BYTES = 256 * 1024


def _is_owned_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid()


def _read_owned_json(path: Path) -> tuple[object | None, float]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, 0.0
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_TODO_BYTES:
            return None, 0.0
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_TODO_BYTES + 1)
        if len(raw) > MAX_TODO_BYTES:
            return None, 0.0
        return json.loads(raw.decode("utf-8", "replace")), st.st_mtime
    except (OSError, ValueError):
        return None, 0.0
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _extract_open_titles(data: object) -> list[str]:
    if not isinstance(data, dict):
        return []
    items = data.get("items") or data.get("tasks") or []
    if not isinstance(items, list):
        return []
    open_titles: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).lower()
        if status in ("pending", "in_progress", "open"):
            open_titles.append(str(item.get("title") or item.get("text") or "untitled"))
    return open_titles


def count_open_todos(conversation_id: str, cwd: str = "") -> list[str]:
    if not conversation_id:
        return []
    matched: list[tuple[float, object]] = []
    # Canonical Steak Pi todo store: <cwd>/.steak-pi/todo/<namespace>/todo.json
    if cwd:
        steak_dir = Path(cwd) / ".steak-pi"
        ws_todo = steak_dir / "todo"
        if _is_owned_dir(steak_dir) and _is_owned_dir(ws_todo):
            try:
                with os.scandir(ws_todo) as entries:
                    for ns in entries:
                        ns_path = Path(ns.path)
                        if not _is_owned_dir(ns_path):
                            continue
                        todo_file = ns_path / "todo.json"
                        parsed, mtime = _read_owned_json(todo_file)
                        if isinstance(parsed, dict) and str(parsed.get("ownerSessionId") or "") == conversation_id:
                            matched.append((mtime, parsed))
            except OSError:
                pass
    matched.sort(key=lambda item: item[0], reverse=True)
    for _, parsed in matched:
        titles = _extract_open_titles(parsed)
        if titles:
            return titles
    # Legacy fallback: ~/.steak-pi/todo/<conversationId>.json
    legacy_root = ut_common.HOME / ".steak-pi"
    legacy_todo = legacy_root / "todo"
    if _is_owned_dir(legacy_root) and _is_owned_dir(legacy_todo):
        parsed, _ = _read_owned_json(legacy_todo / f"{conversation_id}.json")
        if parsed is not None:
            titles = _extract_open_titles(parsed)
            if titles:
                return titles
    return []


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
        or os.environ.get("STEAK_ANTIGRAVITY_FINISH_GUARD") == "0"
    ):
        print("{}")
        return

    open_todos = count_open_todos(conv_id, cwd)
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
                    f"[steak-antigravity finish-guard] {len(open_todos)} open todo item(s) remain ({preview}). "
                    "Finish them or record a real external blocker before stopping."
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
