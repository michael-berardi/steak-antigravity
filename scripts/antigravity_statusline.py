#!/usr/bin/env python3
"""Official Antigravity CLI statusLine snapshot writer for UltraTerm usage dials.

Reads the JSON statusLine payload that `agy` pipes to stdin and atomically writes
a non-PII quota snapshot to `~/.ultraterm/antigravity/usage.json`:

  {
    "schemaVersion": 1,
    "source": "antigravity-statusline",
    "capturedAt": <int epoch seconds>,
    "available": true,
    "fiveHour": { "usedPercent": <0..100>, "resetsAt": <int epoch seconds|null> } | null,
    "sevenDay": { "usedPercent": <0..100>, "resetsAt": <int epoch seconds|null> } | null
  }

Design & honesty rules:
- Prints nothing to stdout (so `stack_with_default: true` keeps Antigravity's
  default status bar clean) except a one-line update notice, see docs/updates.md.
- Never touches Google auth, never writes PII (email, cwd, transcript_path,
  conversation_id are discarded). Its only network use is the detached daily
  update check in lib/update_check.py.
- When `agy` has not loaded quota yet (e.g., `agent_state: "authenticating"`),
  exits 0 without overwriting an existing snapshot.
- Refuses symlinks, non-owner directories, and unsafe permissions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import ut_common  # noqa: E402
import pane_activity  # noqa: E402
import update_check  # noqa: E402

MAX_STDIN_BYTES = 256 * 1024
SCHEMA_VERSION = 1
SNAPSHOT_SOURCE = "antigravity-statusline"
_FRAC_RE = re.compile(r"\.(\d+)")


def _parse_iso_epoch_seconds(raw: object) -> int | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        def _norm_frac(match: re.Match[str]) -> str:
            return "." + (match.group(1) + "000000")[:6]

        iso = _FRAC_RE.sub(_norm_frac, text, count=1).replace("Z", "+00:00")
        sec = int(dt.datetime.fromisoformat(iso).timestamp())
        return sec if sec > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _extract_window(bucket: object) -> dict | None:
    if not isinstance(bucket, dict):
        return None
    rem = bucket.get("remaining_fraction")
    if isinstance(rem, bool) or not isinstance(rem, (int, float)):
        return None
    rem_f = float(rem)
    if not math.isfinite(rem_f):
        return None
    rem_clamped = min(1.0, max(0.0, rem_f))
    used_percent = round((1.0 - rem_clamped) * 100.0, 4)
    resets_at = _parse_iso_epoch_seconds(bucket.get("reset_time"))
    return {
        "usedPercent": used_percent,
        "resetsAt": resets_at,
    }


def build_snapshot(payload: dict, *, now_seconds: int | None = None) -> dict | None:
    """Build a validated non-PII usage snapshot from an `agy` statusLine payload.

    Returns None when the payload has no usable Gemini quota buckets yet (for
    example during startup authentication), so callers preserve any prior snapshot.
    """
    if not isinstance(payload, dict):
        return None
    quota = payload.get("quota")
    if not isinstance(quota, dict):
        return None
    five_hour = _extract_window(quota.get("gemini-5h"))
    seven_day = _extract_window(quota.get("gemini-weekly"))
    if five_hour is None and seven_day is None:
        return None
    captured_at = int(time.time()) if now_seconds is None else int(now_seconds)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": SNAPSHOT_SOURCE,
        "capturedAt": captured_at,
        "available": True,
        "fiveHour": five_hour,
        "sevenDay": seven_day,
    }


def _ensure_private_dir(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            return False
        os.chmod(path, 0o700)
    except OSError:
        return False
    ok, _ = ut_common.verify_private_dir(path)
    return ok


def write_snapshot(target: Path, snapshot: dict) -> bool:
    """Atomically write snapshot JSON with mode 0600 inside an owner-only 0700 folder."""
    if not target.is_absolute():
        return False
    if not _ensure_private_dir(target.parent):
        return False
    try:
        st = os.lstat(target)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        if st.st_uid != os.getuid() or st.st_nlink != 1:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False

    body = (json.dumps(snapshot, separators=(",", ":")) + "\n").encode("utf-8")
    tmp = target.parent / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def default_usage_path() -> Path:
    return Path.home() / ".ultraterm" / "antigravity" / "usage.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None, help="Override snapshot output path (tests only)")
    parser.add_argument("--pane-dir", default=None, help="Override per-pane activity dir (tests only)")
    args = parser.parse_args()

    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    except OSError:
        return 0
    if not raw or len(raw) > MAX_STDIN_BYTES:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return 0

    # 1. Write the quota usage snapshot FIRST so pane activity can never delay
    #    or preempt quota updates under agy's 5s statusLine timeout.
    snapshot = build_snapshot(payload)
    if snapshot is not None:
        target = Path(args.output) if args.output else default_usage_path()
        write_snapshot(target, snapshot)
        pane_activity.sweep_stale_temp_files(target.parent)

    # 2. Publish per-pane non-PII subagent/task activity (even when quota is
    #    absent or still authenticating). Skip default $HOME writes during
    #    --output tests unless --pane-dir is explicitly supplied.
    if args.pane_dir or not args.output:
        try:
            pane_activity.record_statusline(
                payload,
                panes_dir=Path(args.pane_dir) if args.pane_dir else None,
            )
        except Exception as exc:
            sys.stderr.write(f"[steak-antigravity] pane activity publish failed: {exc}\n")

    # 3. Update notice in the footer. statusLine only runs in the interactive agy TUI
    #    (never print/headless runs), so this is also where the detached daily check
    #    starts. Off for CI, agent workers and opt-outs; skipped in tests.
    if not (args.output or args.pane_dir):
        try:
            line = update_check.footer_line()
            if line:
                sys.stdout.write(line + "\n")
        except Exception as exc:
            sys.stderr.write(f"[steak-antigravity] update notice failed: {exc}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
