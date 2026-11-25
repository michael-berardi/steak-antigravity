#!/usr/bin/env python3
"""Publish Antigravity CLI transcripts to UltraTerm UI Mode (ultraterm.transcript/1).

Implements the exact UltraTerm UI transcript contract:
- Only active when ULTRATERM_UI_TRANSCRIPT_FILE and ULTRATERM_UI_SESSION_ID are set.
- Validates parent directory ownership and permissions (no symlinks, mode 0700-safe).
- Exclusive create (O_CREAT|O_EXCL, mode 0600, no symlink follow) on first publish.
- Atomic temp-file + rename when switching Antigravity conversation IDs (/new).
- Translates Antigravity `transcript.jsonl` rows (USER_INPUT, PLANNER_RESPONSE,
  tool calls, tool outputs) into append-only Pi session JSONL rows with monotonic
  unique `id` and `parentId` links and `stopReason: "stop"` on turn end.
- Also drains unread USAP completion notices on PreInvocation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402

MAX_TEXT_CHUNK = 16_000


def parse_timestamp_ms(raw: str | int | float | None) -> int:
    if isinstance(raw, (int, float)):
        return int(raw * 1000) if raw < 1e11 else int(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            iso = raw.strip().replace("Z", "+00:00")
            return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)
        except ValueError:
            pass
    return int(time.time() * 1000)


def validate_ui_target(file_path: Path, session_id: str) -> tuple[bool, str]:
    if not file_path.is_absolute() or "\0" in str(file_path):
        return False, "ULTRATERM_UI_TRANSCRIPT_FILE must be an absolute path"
    if not ut_common.UUID_RE.fullmatch(session_id):
        return False, "ULTRATERM_UI_SESSION_ID must be a UUID"
    ok, reason = ut_common.verify_private_dir(file_path.parent)
    if not ok:
        return False, reason
    if file_path.exists() or file_path.is_symlink():
        st = os.lstat(file_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False, "transcript target is not a regular file"
        if st.st_uid != os.getuid() or st.st_nlink != 1 or (st.st_mode & 0o077):
            return False, "transcript target has invalid owner, link count, or permissions"
    return True, ""


def state_file_for(session_id: str, data_dir: Path | None = None) -> Path:
    base = (data_dir or ut_common.plugin_data_dir()) / "ui-transcript-state"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base / f"{session_id}.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def normalize_tool_args(name: str, args: object) -> dict:
    if not isinstance(args, dict):
        return {}
    out = dict(args)
    # Strip extra JSON string quoting sometimes present in compact transcripts.
    for k, v in list(out.items()):
        if isinstance(v, str) and len(v) >= 2 and v.startswith('"') and v.endswith('"'):
            try:
                out[k] = json.loads(v)
            except ValueError:
                pass
    # UltraTerm UI Mode reads `path` and `command` from toolCall arguments.
    if "path" not in out:
        for alt in ("Path", "file_path", "TargetFile", "AbsolutePath"):
            if isinstance(out.get(alt), str):
                out["path"] = out[alt]
                break
    if "command" not in out:
        for alt in ("CommandLine", "cmd"):
            if isinstance(out.get(alt), str):
                out["command"] = out[alt]
                break
    return out


def strip_user_tags(text: str) -> str:
    s = text.strip()
    if s.startswith("<USER_REQUEST>"):
        s = s[len("<USER_REQUEST>") :]
        end = s.find("</USER_REQUEST>")
        if end != -1:
            s = s[:end]
    return s.strip()


def translate_entry(entry: dict, state: dict) -> list[dict]:
    """Convert one Antigravity transcript.jsonl record into 0..N Pi UI transcript rows."""
    rows: list[dict] = []
    step_type = entry.get("type")
    step_idx = entry.get("step_index")
    ts = parse_timestamp_ms(entry.get("created_at"))
    pending_calls: list[dict] = state.setdefault("pendingCalls", [])

    def next_row(prefix: str, message: dict) -> dict:
        seq = int(state.get("seq", 0)) + 1
        state["seq"] = seq
        row_id = f"{prefix}{step_idx if step_idx is not None else seq}-{seq}"
        row = {
            "type": "message",
            "id": row_id,
            "parentId": state.get("lastId"),
            "message": message,
        }
        state["lastId"] = row_id
        return row

    if step_type == "USER_INPUT":
        raw_content = entry.get("content", "")
        if isinstance(raw_content, str):
            text = strip_user_tags(raw_content)
        else:
            text = json.dumps(raw_content)
        if text:
            rows.append(
                next_row(
                    "u",
                    {
                        "role": "user",
                        "content": text[:MAX_TEXT_CHUNK],
                        "timestamp": ts,
                    },
                )
            )
            state["turnOpen"] = True

    elif step_type == "PLANNER_RESPONSE":
        parts: list[dict] = []
        response_text = entry.get("response") or entry.get("content") or ""
        if isinstance(response_text, str) and response_text.strip():
            parts.append({"type": "text", "text": response_text.strip()[:MAX_TEXT_CHUNK]})
        tool_calls = entry.get("tool_calls")
        if isinstance(tool_calls, list):
            for idx, call in enumerate(tool_calls):
                if not isinstance(call, dict):
                    continue
                call_name = str(call.get("name") or "tool")
                call_id = str(call.get("id") or f"call-{step_idx}-{idx}")
                call_args = normalize_tool_args(call_name, call.get("args") or call.get("arguments") or {})
                parts.append(
                    {
                        "type": "toolCall",
                        "id": call_id,
                        "name": call_name,
                        "arguments": call_args,
                    }
                )
                pending_calls.append({"id": call_id, "name": call_name})
        if parts:
            msg: dict = {"role": "assistant", "content": parts, "timestamp": ts}
            if isinstance(entry.get("model"), str):
                msg["model"] = entry["model"]
            # If no tool calls follow and there is response text, this is a final assistant reply.
            if not tool_calls and response_text:
                msg["stopReason"] = "stop"
                state["turnOpen"] = False
            rows.append(next_row("a", msg))

    elif step_type in ("GENERIC", "TOOL_RESULT") and entry.get("source") == "MODEL":
        content = entry.get("content", "")
        text = content if isinstance(content, str) else json.dumps(content)
        call = pending_calls.pop(0) if pending_calls else {"id": f"call-{step_idx}", "name": "tool"}
        is_error = bool(entry.get("error")) or entry.get("status") == "ERROR"
        rows.append(
            next_row(
                "t",
                {
                    "role": "toolResult",
                    "toolCallId": call["id"],
                    "toolName": call["name"],
                    "isError": is_error,
                    "content": [{"type": "text", "text": text[:MAX_TEXT_CHUNK]}],
                    "timestamp": ts,
                },
            )
        )

    return rows


def ensure_header(target: Path, session_id: str, cwd: str, rotate: bool) -> bool:
    header = {"type": "session", "version": 3, "id": session_id, "cwd": cwd}
    line = json.dumps(header, separators=(",", ":")) + "\n"
    if rotate:
        tmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, target)
        return True
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(line)
    return True


def append_rows(target: Path, rows: list[dict]) -> None:
    if not rows:
        return
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            return
        payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode("utf-8")
        os.write(fd, payload)
    finally:
        os.close(fd)


def sync_transcript(
    *,
    ui_file: Path,
    ui_session_id: str,
    conversation_id: str,
    source_transcript: Path | None,
    cwd: str,
    event_name: str,
    data_dir: Path | None = None,
) -> bool:
    ok, _ = validate_ui_target(ui_file, ui_session_id)
    if not ok:
        return False

    sf = state_file_for(ui_session_id, data_dir)
    state = load_state(sf)
    prev_conv = state.get("conversationId")

    if not ui_file.exists():
        # First publish: refuse if file already existed before our state knew it.
        created = ensure_header(ui_file, ui_session_id, cwd, rotate=False)
        if not created:
            return False
        state = {"conversationId": conversation_id, "offset": 0, "seq": 0, "lastId": None, "pendingCalls": [], "turnOpen": False}
    elif prev_conv and conversation_id and prev_conv != conversation_id:
        # Conversation changed (/new): rotate atomically per contract.
        ensure_header(ui_file, ui_session_id, cwd, rotate=True)
        state = {"conversationId": conversation_id, "offset": 0, "seq": 0, "lastId": None, "pendingCalls": [], "turnOpen": False}
    elif not prev_conv:
        # File existed prior to our state: contract says refuse to overwrite foreign file.
        return False

    new_rows: list[dict] = []
    if source_transcript and source_transcript.is_file():
        offset = int(state.get("offset", 0))
        try:
            size = source_transcript.stat().st_size
            if size < offset:
                offset = 0
            with open(source_transcript, "rb") as handle:
                handle.seek(offset)
                raw_lines = handle.readlines()
            for raw in raw_lines:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                try:
                    entry = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    new_rows.extend(translate_entry(entry, state))
            state["offset"] = offset
        except OSError:
            pass

    if event_name == "stop" and state.get("turnOpen"):
        seq = int(state.get("seq", 0)) + 1
        state["seq"] = seq
        row_id = f"end-{seq}"
        new_rows.append(
            {
                "type": "message",
                "id": row_id,
                "parentId": state.get("lastId"),
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "stop",
                    "timestamp": int(time.time() * 1000),
                },
            }
        )
        state["lastId"] = row_id
        state["turnOpen"] = False

    if new_rows:
        append_rows(ui_file, new_rows)
    save_state(sf, state)
    return True


def drain_usap_notices(data_dir: Path | None = None) -> list[str]:
    base = (data_dir or ut_common.plugin_data_dir()) / "usap-events"
    if not base.is_dir():
        return []
    cursor_path = (data_dir or ut_common.plugin_data_dir()) / "usap-events-cursor.json"
    cursors = load_state(cursor_path)
    notices: list[str] = []
    for path in sorted(base.glob("*.jsonl")):
        key = path.name
        offset = int(cursors.get(key, 0))
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                for raw in handle.readlines():
                    if not raw.endswith(b"\n"):
                        break
                    offset += len(raw)
                    try:
                        item = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        notices.append(text.strip()[:4000])
            cursors[key] = offset
        except OSError:
            continue
    if notices:
        save_state(cursor_path, cursors)
    return notices


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="sync")
    args = parser.parse_args()

    payload = ut_common.read_hook_stdin()
    ui_file_env = os.environ.get("ULTRATERM_UI_TRANSCRIPT_FILE", "").strip()
    ui_session_env = os.environ.get("ULTRATERM_UI_SESSION_ID", "").strip()
    if ui_file_env and ui_session_env:
        cwd = str(payload.get("cwd") or os.getcwd())
        conv_id = str(payload.get("conversationId") or "")
        tp = payload.get("transcriptPath")
        source = Path(tp) if isinstance(tp, str) and tp else None
        try:
            sync_transcript(
                ui_file=Path(ui_file_env),
                ui_session_id=ui_session_env,
                conversation_id=conv_id,
                source_transcript=source,
                cwd=cwd,
                event_name=args.event,
            )
        except OSError:
            pass

    if args.event == "pre-invocation":
        notices = drain_usap_notices()
        if notices:
            joined = "\n\n---\n\n".join(notices)
            print(
                json.dumps(
                    {
                        "injectSteps": [
                            {
                                "systemMessage": {
                                    "systemMessage": (
                                        "[steak-agy] USAP background completion notice "
                                        "(verify worker output before claiming done):\n\n" + joined
                                    )
                                }
                            }
                        ],
                        "terminationBehavior": "",
                    }
                )
            )
            return

    if args.event in ("pre-invocation", "post-invocation"):
        print(json.dumps({"injectSteps": [], "terminationBehavior": ""}))
    else:
        print("{}")


if __name__ == "__main__":
    main()
