#!/usr/bin/env python3
"""Launch the Steak Antigravity USAP stdio MCP bridge with the active Pi SDK and Steak Pi."""
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


def resolve_session_id() -> str:
    env_id = os.environ.get("ULTRATERM_UI_SESSION_ID", "").strip()
    if ut_common.UUID_RE.fullmatch(env_id):
        return env_id.lower()
    # Stable per-pane session UUID when running inside UltraTerm tmux: include
    # pane ID / terminal ID so sibling panes in a matrix never collide on one
    # Pi host session file.
    tmux_session = os.environ.get("ULTRATERM_TMUX_SESSION", "").strip()
    pane = ut_common.resolve_pane() or ""
    term_id = os.environ.get("ULTRATERM_TERMINAL_ID", "").strip()
    if tmux_session and (pane or term_id):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"steak-antigravity:{tmux_session}:{pane}:{term_id}"))
    # Outside UltraTerm (or without a verified pane/terminal), isolate each
    # MCP bridge process with a fresh UUID instead of sharing one file.
    return str(uuid.uuid4())


def resolve_workspace_cwd() -> str | None:
    for var in ("STEAK_ANTIGRAVITY_WORKSPACE_CWD", "ULTRATERM_WORKSPACE_CWD"):
        val = os.environ.get(var, "").strip()
        if val and os.path.isdir(val):
            return os.path.realpath(val)
    pane = ut_common.resolve_pane()
    if pane:
        res = ut_common.run(ut_common.tmux_command("display-message", "-p", "-t", pane, "#{pane_current_path}"))
        if res and res.returncode == 0:
            candidate = res.stdout.strip()
            if candidate and os.path.isdir(candidate):
                return os.path.realpath(candidate)
    cwd = os.getcwd()
    # Antigravity launches plugin MCP servers with cwd set to the plugin root.
    # Never silently widen workspace scope to $HOME; fail closed when no workspace cwd is known.
    if os.path.realpath(cwd) == os.path.realpath(str(ROOT)):
        return None
    return os.path.realpath(cwd) if os.path.isdir(cwd) else None


def main() -> None:
    data_dir = ut_common.plugin_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ok_dir, dir_reason = ut_common.verify_private_dir(data_dir)
    if not ok_dir:
        sys.stderr.write(f"[steak-antigravity-usap] UnsafePluginDataDir: {dir_reason}\n")
        sys.exit(1)
    raw_node = shutil.which("node")
    node = os.path.realpath(raw_node) if raw_node else None
    sdk = ut_common.pi_sdk_entry()
    steak = ut_common.steak_pi_root()
    unavailable = ROOT / "lib/mcp-unavailable.mjs"
    workspace_cwd = resolve_workspace_cwd()

    reason = (
        "node is not on PATH"
        if not node
        else "no active Pi runtime (~/.ultraterm/runtime/active-pi.json)"
        if not sdk
        else "Steak Pi package not found"
        if not steak
        else "WorkspaceCwdUnknown: no workspace cwd available (set STEAK_ANTIGRAVITY_WORKSPACE_CWD or run inside an UltraTerm pane)"
        if not workspace_cwd
        else None
    )
    if reason:
        sys.stderr.write(f"[steak-antigravity-usap] unavailable: {reason}\n")
        if node:
            os.execv(node, [node, str(unavailable), reason])
        sys.exit(1)

    bridge = ROOT / "lib/usap-bridge.mjs"
    session_id = resolve_session_id()
    parent_model = os.environ.get("ULTRATERM_HARNESS_PROFILE") or "antigravity"
    os.environ["STEAK_ANTIGRAVITY_PARENT_PID"] = str(os.getppid())
    os.environ["STEAK_ANTIGRAVITY_WORKSPACE_CWD"] = workspace_cwd
    # Request no tracking from the third-party Pi SDK / Steak Pi code this bridge execs.
    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    # Workers spawned through this bridge never need the operator's tmux pane server.
    # Drop TMUX/TMUX_PANE after pane discovery and keep ~/.ultraterm/bin ahead of Homebrew on PATH.
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    ut_bin = str(Path.home() / ".ultraterm" / "bin")
    if os.path.isdir(ut_bin):
        parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != ut_bin]
        os.environ["PATH"] = os.pathsep.join([ut_bin, *parts])
    try:
        os.chdir(workspace_cwd)
    except OSError as exc:
        sys.stderr.write(f"[steak-antigravity-usap] WorkspaceChdirFailed: {exc}\n")
        sys.exit(1)
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
