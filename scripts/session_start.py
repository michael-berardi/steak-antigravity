#!/usr/bin/env python3
"""Antigravity SessionStart hook for Steak Antigravity.

- Initializes UltraTerm UI Mode transcript header when ULTRATERM_UI_TRANSCRIPT_FILE is set.
- Injects operator's shared ~/.pi/agent/AGENTS.md (when present) so Antigravity
  follows the exact same rules as Pi, Steak Pi, and Claude Code.
- Injects live UltraTerm pane/terminal identity summary.
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
import auto_resume  # noqa: E402
import coordinator_claims  # noqa: E402
import pane_activity  # noqa: E402
import ut_common  # noqa: E402
import ui_transcript_sync  # noqa: E402

MAX_RULES_BYTES = 48_000


def main() -> None:
    payload = ut_common.read_hook_stdin()
    try:
        pane_activity.record_hook_event("session-start", payload)
    except Exception as exc:
        sys.stderr.write(f"[steak-antigravity] pane activity update failed at session start: {exc}\n")
    cwd = str(payload.get("cwd") or os.getcwd())
    conv_id = str(payload.get("conversationId") or "")
    tp = payload.get("transcriptPath")
    source = Path(tp) if isinstance(tp, str) and tp else None

    ui_file_env = os.environ.get("ULTRATERM_UI_TRANSCRIPT_FILE", "").strip()
    ui_session_env = os.environ.get("ULTRATERM_UI_SESSION_ID", "").strip()
    if ui_file_env and ui_session_env and ui_transcript_sync.publisher_is_pane_process():
        try:
            ui_transcript_sync.sync_transcript(
                ui_file=Path(ui_file_env),
                ui_session_id=ui_session_env,
                conversation_id=conv_id,
                source_transcript=source,
                cwd=cwd,
                event_name="session-start",
            )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            sys.stderr.write(f"[steak-antigravity] UI transcript sync failed at session start: {exc}\n")

    inject_steps: list[dict] = []

    agents_md = ut_common.PI_AGENT_DIR / "AGENTS.md"
    if agents_md.is_file():
        try:
            text = agents_md.read_text(encoding="utf-8", errors="replace")[:MAX_RULES_BYTES]
            inject_steps.append(
                {
                    "systemMessage": {
                        "systemMessage": (
                            f"[steak-antigravity] Shared UltraTerm operator rules loaded from {agents_md}:\n\n{text}"
                        )
                    }
                }
            )
        except OSError:
            pass

    identity = ut_common.terminal_identity()
    if identity.get("terminalId"):
        try:
            auto_resume.cancel_for_terminal(
                str(identity["terminalId"]),
                reason="new_session",
                exclude_conversation_id=conv_id,
            )
        except Exception as exc:
            sys.stderr.write(f"[steak-antigravity] auto-resume cancel failed at session start: {exc}\n")
    if identity.get("insideUltraTerm"):
        summary_parts = [
            f"slot={identity.get('slot')}",
            f"pane={identity.get('pane')}",
            f"tmux={identity.get('tmuxSession')}",
        ]
        if identity.get("terminalId"):
            summary_parts.append(f"terminalId={identity['terminalId']}")
        if identity.get("harness"):
            summary_parts.append(f"harness={identity['harness']}")
        if identity.get("launchProfile"):
            summary_parts.append(f"profile={identity['launchProfile']}")
        inject_steps.append(
            {
                "systemMessage": {
                    "systemMessage": "[steak-antigravity] UltraTerm pane identity: " + ", ".join(summary_parts)
                }
            }
        )

    try:
        active_claims = coordinator_claims.read_active_coordinator_claims(cwd=cwd)
        banner = coordinator_claims.format_claims_banner(active_claims)
        if banner:
            inject_steps.append({"systemMessage": {"systemMessage": banner}})
    except Exception as exc:
        sys.stderr.write(f"[steak-antigravity] coordinator claims check failed at session start: {exc}\n")

    print(json.dumps({"injectSteps": inject_steps}))


if __name__ == "__main__":
    main()
