#!/usr/bin/env python3
"""Bounded single-shot waiter for Steak Antigravity ToS-safe quota auto-resume.

Spawned by `scripts/stop_hook.py` only on a confirmed quota exhaustion error with
an official server-reported reset timestamp. Sleeps locally without network or API
polling until strictly past `resetAt + buffer (>=60s) + jitter`, checking local
cancellation/HOLD/parent-PID state at coarse intervals, delivers one identity-bound
`utp send` resume if still valid, and exits immediately.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import auto_resume  # noqa: E402
import ut_common  # noqa: E402

class _StopSignal(BaseException):
    pass


def _on_signal(_signum: int, _frame: object) -> None:
    raise _StopSignal()


def run_waiter(
    state_file: Path,
    token: str,
    *,
    check_interval: float = auto_resume.WAIT_CHECK_INTERVAL_SECONDS,
    deliver_fn=None,
    now_fn=None,
    sleep_fn=None,
) -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if not state_file.is_absolute():
        return 0
    ok_dir, _ = ut_common.verify_private_dir(state_file.parent)
    if not ok_dir:
        return 0

    _now = now_fn or time.time
    _sleep = sleep_fn or time.sleep
    _deliver = deliver_fn or auto_resume.deliver_resume

    try:
        while True:
            state = auto_resume._read_owned_json_file(state_file, auto_resume.MAX_CONFIG_BYTES)
            if not state or state.get("token") != token or state.get("status") != "scheduled":
                return 0

            wake_at = state.get("wakeAt")
            reset_at = state.get("resetAt")
            if not isinstance(wake_at, int) or not isinstance(reset_at, int):
                return 0

            if not auto_resume.load_config().enabled:
                auto_resume.cancel_for_conversation(
                    str(state.get("conversationId") or ""),
                    reason="disabled",
                    data_dir=state_file.parent.parent,
                    expected_token=token,
                )
                return 0

            parent_pid = state.get("parentPid")
            if parent_pid is not None and not auto_resume.pid_is_alive(parent_pid):
                auto_resume.cancel_for_conversation(
                    str(state.get("conversationId") or ""),
                    reason="session_ended",
                    data_dir=state_file.parent.parent,
                    expected_token=token,
                )
                return 0

            conv_id = str(state.get("conversationId") or "")
            tp = str(state.get("transcriptPath") or "")
            held, hold_reason = auto_resume.is_hold_active(conv_id, transcript_path=tp)
            if held:
                auto_resume.cancel_for_conversation(
                    conv_id,
                    reason=f"hold:{hold_reason}",
                    data_dir=state_file.parent.parent,
                    expected_token=token,
                )
                return 0

            baseline = state.get("transcriptBaseline")
            if tp and isinstance(baseline, dict):
                active, act_reason = auto_resume.has_transcript_activity(tp, baseline)
                if active:
                    auto_resume.cancel_for_conversation(
                        conv_id,
                        reason=act_reason,
                        data_dir=state_file.parent.parent,
                        expected_token=token,
                    )
                    return 0

            min_safe_wake = max(wake_at, reset_at + auto_resume.MIN_BUFFER_SECONDS)
            now = _now()
            remaining = min_safe_wake - now
            if remaining > 0:
                _sleep(min(remaining, max(0.05, float(check_interval))))
                continue

            result = _deliver(state_file, token)
            if isinstance(result, dict) and result.get("action") == "too_early":
                rem = float(result.get("remainingSeconds") or 1.0)
                _sleep(min(max(0.05, rem), max(0.05, float(check_interval))))
                continue
            return 0
    except _StopSignal:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    return run_waiter(Path(args.state), args.token)


if __name__ == "__main__":
    sys.exit(main())
