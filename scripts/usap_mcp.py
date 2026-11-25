#!/usr/bin/env python3
"""Launch the Steak AGY USAP stdio MCP bridge with the active Pi SDK and Steak Pi."""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402


def resolve_session_id(data_dir: Path) -> str:
    env_id = os.environ.get("ULTRATERM_UI_SESSION_ID", "").strip()
    if ut_common.UUID_RE.fullmatch(env_id):
        return env_id.lower()
    # Stable per-pane session UUID when running inside UltraTerm tmux.
    tmux_session = os.environ.get("ULTRATERM_TMUX_SESSION", "").strip()
    if tmux_session:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"steak-agy:{tmux_session}"))
    state_file = data_dir / "usap-default-session-id"
    try:
        saved = state_file.read_text().strip()
        if ut_common.UUID_RE.fullmatch(saved):
            return saved.lower()
    except OSError:
        pass
    fresh = str(uuid.uuid4())
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_file.write_text(fresh + "\n")
    return fresh


def main() -> None:
    data_dir = ut_common.plugin_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    node = shutil.which("node")
    sdk = ut_common.pi_sdk_entry()
    steak = ut_common.steak_pi_root()
    unavailable = ROOT / "lib/mcp-unavailable.mjs"

    reason = (
        "node is not on PATH"
        if not node
        else "no active Pi runtime (~/.ultraterm/runtime/active-pi.json)"
        if not sdk
        else "Steak Pi package not found"
        if not steak
        else None
    )
    if reason:
        os.execv(node or "/usr/bin/false", [node or "false", str(unavailable), reason])

    bridge = ROOT / "lib/usap-bridge.mjs"
    session_id = resolve_session_id(data_dir)
    parent_model = os.environ.get("ULTRATERM_HARNESS_PROFILE") or "antigravity"
    os.environ["STEAK_AGY_PARENT_PID"] = str(os.getppid())
    os.execv(
        node,
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            str(bridge),
            str(sdk),
            str(steak),
            str(data_dir),
            session_id,
            parent_model,
        ],
    )


if __name__ == "__main__":
    main()
