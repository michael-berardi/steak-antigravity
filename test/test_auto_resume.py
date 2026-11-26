import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
import auto_resume  # noqa: E402
import auto_resume_waiter  # noqa: E402
import ut_common  # noqa: E402

TERM_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_UUID = "11111111-2222-4333-8444-555555555555"


def _fake_identity(term_id: str = TERM_UUID, inside: bool = True, slot: int = 2) -> dict:
    return {
        "insideUltraTerm": inside,
        "slot": slot,
        "terminalId": term_id,
        "harness": "antigravity",
        "catalogHarness": "antigravity",
    }


class TestQuotaParsingAndBuffer(unittest.TestCase):
    def test_parses_durations_with_various_formats_and_ansi(self) -> None:
        self.assertEqual(auto_resume.parse_duration_seconds("Resets in 4h32m15s"), 4 * 3600 + 32 * 60 + 15)
        self.assertEqual(auto_resume.parse_duration_seconds("\x1b[31mQuota will reset after 1h 20m\x1b[0m"), 80 * 60)
        self.assertEqual(auto_resume.parse_duration_seconds("Try again in 45 minutes"), 45 * 60)
        self.assertEqual(auto_resume.parse_duration_seconds("Retry after 90s"), 90)
        self.assertEqual(auto_resume.parse_duration_seconds("Limit resets in 2 hours, 15 minutes"), 2 * 3600 + 15 * 60)
        self.assertEqual(auto_resume.parse_duration_seconds("Resets in 1d 2h"), 26 * 3600)
        self.assertEqual(auto_resume.parse_duration_seconds("Quota exhausted (3h10m)"), 3 * 3600 + 10 * 60)
        self.assertIsNone(auto_resume.parse_duration_seconds("Resets in 0s"))
        self.assertIsNone(auto_resume.parse_duration_seconds("Connection reset by peer"))

    def test_parses_iso_timestamps_and_rejects_stale_or_naive(self) -> None:
        now = 1_795_650_000.0  # 2026-11-25T23:40:00Z
        ts_z = auto_resume.parse_iso_reset_timestamp("quota_resets: 2026-11-26T04:00:00Z", now=now)
        self.assertIsNotNone(ts_z)
        self.assertGreater(ts_z, int(now))

        ts_nano = auto_resume.parse_iso_reset_timestamp(
            "RESOURCE_EXHAUSTED reset_time=2026-11-26T04:00:00.123456789Z", now=now
        )
        self.assertEqual(ts_nano, ts_z)

        ts_off = auto_resume.parse_iso_reset_timestamp("Resets at 2026-11-26T01:00:00-03:00", now=now)
        self.assertEqual(ts_off, ts_z)

        # Epoch zero, naive timestamps, and stale past timestamps must be rejected.
        self.assertIsNone(auto_resume.parse_iso_reset_timestamp("quota_resets: 1970-01-01T00:00:00Z", now=now))
        self.assertIsNone(auto_resume.parse_iso_reset_timestamp("quota_resets: 2026-11-26T04:00:00", now=now))
        self.assertIsNone(auto_resume.parse_iso_reset_timestamp("quota_resets: 2026-11-25T20:00:00Z", now=now))

    def test_safety_buffer_enforces_hard_60s_minimum_and_post_reset_invariant(self) -> None:
        now = 1_795_650_000.0
        reset_at = int(now) + 3600

        # Trying to lower buffer below 60s via env or config is clamped to >= 60s.
        cfg = auto_resume.load_config(
            env={"STEAK_ANTIGRAVITY_AUTO_RESUME_BUFFER_SEC": "0", "STEAK_ANTIGRAVITY_AUTO_RESUME_JITTER_SEC": "10"}
        )
        self.assertEqual(cfg.buffer_seconds, auto_resume.MIN_BUFFER_SECONDS)
        wake_at, buf, jit = auto_resume.compute_wake_time(reset_at, now=now, config=cfg, jitter_fn=lambda m: 7)
        self.assertEqual(buf, 60)
        self.assertEqual(jit, 7)
        self.assertEqual(wake_at, reset_at + 67)
        self.assertGreaterEqual(wake_at, reset_at + 60)

        # Even if reset_at is slightly in the past (clock skew), wake_at stays >= ceil(now) + 60.
        wake_skew, buf2, _ = auto_resume.compute_wake_time(int(now) - 10, now=now, config=cfg, jitter_fn=lambda m: 0)
        self.assertEqual(buf2, 60)
        self.assertGreaterEqual(wake_skew, int(now) + 60)

    def test_non_quota_errors_and_non_error_stops_are_ignored(self) -> None:
        now = 1_795_650_000.0
        for reason in ("NO_TOOL_CALL", "USER_CANCELED", "MAX_INVOCATIONS", "TERMINAL_STEP_TYPE", ""):
            sig, code = auto_resume.extract_quota_reset(
                {"terminationReason": reason, "error": "Quota exhausted. Resets in 2h"},
                now=now,
            )
            self.assertIsNone(sig)
            self.assertEqual(code, "not_error_stop")

        non_quota_errors = [
            "Connection reset by peer (ECONNRESET)",
            "Authentication failed: signed out, please sign in again",
            "Unknown model requested; not available on your plan",
            "Context window exceeded maximum token budget",
            "Command failed with exit status 1",
            "Permission denied writing file",
            "Cancelled by user",
        ]
        with tempfile.TemporaryDirectory() as td:
            # Even with an exhausted statusLine snapshot present, non-quota errors must ignore it.
            usage = Path(td) / "usage.json"
            usage.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "source": "antigravity-statusline",
                        "capturedAt": int(now),
                        "available": True,
                        "fiveHour": {"usedPercent": 100.0, "resetsAt": int(now) + 1800},
                        "sevenDay": {"usedPercent": 20.0, "resetsAt": int(now) + 86400},
                    }
                )
            )
            for err in non_quota_errors:
                sig, code = auto_resume.extract_quota_reset(
                    {"terminationReason": "ERROR", "error": err},
                    now=now,
                    usage_path=usage,
                )
                self.assertIsNone(sig, f"should ignore non-quota error: {err}")
                self.assertEqual(code, "non_quota_error")

    def test_statusline_fallback_and_weekly_cap_rejection(self) -> None:
        now = 1_795_650_000.0
        with tempfile.TemporaryDirectory() as td:
            usage = Path(td) / "usage.json"
            # Fresh 5h exhausted snapshot supplies reset timestamp when quota error omits inline time.
            usage.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "source": "antigravity-statusline",
                        "capturedAt": int(now) - 30,
                        "available": True,
                        "fiveHour": {"usedPercent": 99.9, "resetsAt": int(now) + 7200},
                        "sevenDay": {"usedPercent": 40.0, "resetsAt": int(now) + 5 * 86400},
                    }
                )
            )
            sig, code = auto_resume.extract_quota_reset(
                {"terminationReason": "error", "error": "RESOURCE_EXHAUSTED: quota exhausted"},
                now=now,
                usage_path=usage,
            )
            self.assertEqual(code, "ok")
            self.assertIsNotNone(sig)
            self.assertEqual(sig.source, "statusline-5h")
            self.assertEqual(sig.reset_at, int(now) + 7200)

            # Stale snapshot (> 600s) is rejected cleanly.
            usage.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "source": "antigravity-statusline",
                        "capturedAt": int(now) - 900,
                        "available": True,
                        "fiveHour": {"usedPercent": 100.0, "resetsAt": int(now) + 7200},
                        "sevenDay": {"usedPercent": 40.0, "resetsAt": int(now) + 5 * 86400},
                    }
                )
            )
            sig_stale, code_stale = auto_resume.extract_quota_reset(
                {"terminationReason": "ERROR", "error": "RESOURCE_EXHAUSTED: quota exhausted"},
                now=now,
                usage_path=usage,
            )
            self.assertIsNone(sig_stale)
            self.assertEqual(code_stale, "no_reset_timestamp")

            # Weekly lockout (> maxWaitSeconds 6h) is rejected instead of retrying in 5h.
            usage.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "source": "antigravity-statusline",
                        "capturedAt": int(now),
                        "available": True,
                        "fiveHour": {"usedPercent": 100.0, "resetsAt": int(now) + 3600},
                        "sevenDay": {"usedPercent": 100.0, "resetsAt": int(now) + 3 * 86400},
                    }
                )
            )
            sig_wk, code_wk = auto_resume.extract_quota_reset(
                {"terminationReason": "ERROR", "error": "RESOURCE_EXHAUSTED: quota exhausted"},
                now=now,
                usage_path=usage,
            )
            self.assertIsNone(sig_wk)
            self.assertEqual(code_wk, "exceeds_max_wait")


class TestAutoResumeLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="steak-agy-autoresume-"))
        self.home = self.tmp / "home"
        self.home.mkdir(mode=0o700)
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(mode=0o700)
        self.transcript = self.tmp / "transcript.jsonl"
        self.transcript.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Build it</USER_REQUEST>"}) + "\n"
        )
        self.now = 1_795_650_000.0

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _quota_payload(self, conv_id: str = "conv-1", error: str = "Quota exhausted. Resets in 2h", **extra) -> dict:
        return {
            "conversationId": conv_id,
            "cwd": str(self.tmp),
            "transcriptPath": str(self.transcript),
            "lastUserInput": "Build it",
            "terminationReason": "ERROR",
            "error": error,
            **extra,
        }

    def test_opt_out_env_and_config_file(self) -> None:
        # Default is enabled.
        self.assertTrue(auto_resume.load_config(env={}, home=self.home).enabled)

        # Env opt-out values.
        for val in ("0", "false", "off", "no"):
            res = auto_resume.schedule_from_stop(
                self._quota_payload(),
                data_dir=self.data_dir,
                env={"STEAK_ANTIGRAVITY_AUTO_RESUME": val},
                home=self.home,
                now=self.now,
                spawn_waiter=False,
                identity_fn=_fake_identity,
            )
            self.assertEqual(res, {"action": "skipped", "reason": "disabled"})

        # Config file opt-out.
        cfg_file = self.home / ".steak-antigravity" / "config.json"
        cfg_file.parent.mkdir(parents=True, mode=0o700)
        cfg_file.write_text(json.dumps({"autoResume": {"enabled": False}}))
        self.assertFalse(auto_resume.load_config(env={}, home=self.home).enabled)
        # Env opt-in overrides config file.
        self.assertTrue(auto_resume.load_config(env={"STEAK_ANTIGRAVITY_AUTO_RESUME": "1"}, home=self.home).enabled)

        # Symlinked config file is ignored safely.
        real = self.tmp / "real-cfg.json"
        real.write_text(json.dumps({"autoResume": {"enabled": False}}))
        cfg_file.unlink()
        cfg_file.symlink_to(real)
        self.assertTrue(auto_resume.load_config(env={}, home=self.home).enabled)

    def test_hold_blocks_scheduling_and_cancels_at_delivery(self) -> None:
        # 1. Env HOLD
        r1 = auto_resume.schedule_from_stop(
            self._quota_payload(),
            data_dir=self.data_dir,
            env={"STEAK_ANTIGRAVITY_HOLD": "1"},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
        )
        self.assertEqual(r1["action"], "skipped")
        self.assertIn("hold:", r1["reason"])

        # 2. lastUserInput HOLD (with system-reminder stripped)
        r2 = auto_resume.schedule_from_stop(
            self._quota_payload(lastUserInput="<system-reminder>note</system-reminder>\nHOLD everything"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
        )
        self.assertEqual(r2["action"], "skipped")
        self.assertEqual(r2["reason"], "hold:last_user_input")

        # 3. Schedule cleanly, then create agent hold-handoff.md before delivery -> delivery cancels.
        sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-hold"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(sched["action"], "scheduled")
        hold_file = self.home / ".cache" / "agent-hold" / "conv-hold" / "hold-handoff.md"
        hold_file.parent.mkdir(parents=True, mode=0o700)
        hold_file.write_text("operator hold\n")

        sent: list[tuple[str, str]] = []
        deliv = auto_resume.deliver_resume(
            Path(sched["statePath"]),
            sched["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(sched["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: {"state": {"runState": "RUN_STATE_IDLE"}},
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity", "slot": "2"},
            utp_send_fn=lambda tid, prompt: (sent.append((tid, prompt)) or True, "ok"),
        )
        self.assertEqual(deliv["action"], "cancelled")
        self.assertEqual(deliv["reason"], "hold:agent_hold_file")
        self.assertEqual(sent, [])

    def test_deduplication_and_bounded_max_retries_and_reset_on_completion(self) -> None:
        # First schedule -> attempt 1
        s1 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-dedup", error="Quota exhausted. Resets in 2h"),
            data_dir=self.data_dir,
            env={"STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES": "2"},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 10,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(s1["action"], "scheduled")
        self.assertEqual(s1["attempt"], 1)

        # Duplicate Stop for same reset window -> deduplicated, same token, no retry increment.
        s_dup = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-dedup", error="Quota exhausted. Resets in 2h"),
            data_dir=self.data_dir,
            env={"STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES": "2"},
            home=self.home,
            now=self.now + 5,
            spawn_waiter=False,
            jitter_fn=lambda _: 10,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(s_dup["action"], "deduplicated")
        self.assertEqual(s_dup["token"], s1["token"])
        self.assertEqual(s_dup["attempt"], 1)

        # Simulate firing attempt 1 -> attempt 2 schedules -> attempt 3 blocked by maxRetries=2.
        sent: list[tuple[str, str]] = []
        d1 = auto_resume.deliver_resume(
            Path(s1["statePath"]),
            s1["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s1["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: {"state": {"runState": "RUN_STATE_IDLE"}},
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity", "slot": "2"},
            utp_send_fn=lambda tid, prompt: (sent.append((tid, prompt)) or True, "ok"),
        )
        self.assertEqual(d1["action"], "fired")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], TERM_UUID)

        # Second call with same token cannot double-fire.
        d1_again = auto_resume.deliver_resume(
            Path(s1["statePath"]),
            s1["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s1["wakeAt"]) + 2.0,
            pid_alive_fn=lambda _: True,
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity", "slot": "2"},
            utp_send_fn=lambda tid, prompt: (sent.append((tid, prompt)) or True, "ok"),
        )
        self.assertEqual(d1_again["action"], "aborted")
        self.assertEqual(len(sent), 1)

        # Next quota failure schedules attempt 2.
        s2 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-dedup", error="Quota exhausted. Resets in 2h"),
            data_dir=self.data_dir,
            env={"STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES": "2"},
            home=self.home,
            now=self.now + 7500,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(s2["action"], "scheduled")
        self.assertEqual(s2["attempt"], 2)

        # Deliver attempt 2, then a 3rd consecutive failure hits max_retries_reached.
        auto_resume.deliver_resume(
            Path(s2["statePath"]),
            s2["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s2["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: {"state": {"runState": "RUN_STATE_IDLE"}},
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity", "slot": "2"},
            utp_send_fn=lambda tid, prompt: (True, "ok"),
        )
        s3 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-dedup", error="Quota exhausted. Resets in 2h"),
            data_dir=self.data_dir,
            env={"STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES": "2"},
            home=self.home,
            now=self.now + 15000,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(s3["action"], "skipped")
        self.assertEqual(s3["reason"], "max_retries_reached")

        # Normal turn completion resets retry counter so a later window works again.
        auto_resume.on_turn_completed("conv-dedup", data_dir=self.data_dir)
        s4 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-dedup", error="Quota exhausted. Resets in 2h"),
            data_dir=self.data_dir,
            env={"STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES": "2"},
            home=self.home,
            now=self.now + 20000,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(s4["action"], "scheduled")
        self.assertEqual(s4["attempt"], 1)

    def test_delivery_guards_too_early_activity_session_ended_and_conv_state(self) -> None:
        sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-guards"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 5,
            identity_fn=_fake_identity,
            conversation_state_fn=lambda _: {"metadata": {}, "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-25T23:40:00Z"}},
            parent_pid_fn=lambda: os.getpid(),
        )
        sf = Path(sched["statePath"])
        tok = sched["token"]

        # 1. Refuses firing even 1 second before wakeAt.
        early = auto_resume.deliver_resume(
            sf, tok, env={}, home=self.home, now_fn=lambda: float(sched["wakeAt"]) - 1.0
        )
        self.assertEqual(early["action"], "too_early")

        # 2. Refuses if conversation became RUNNING via ConversationService.
        running = auto_resume.deliver_resume(
            sf,
            tok,
            env={},
            home=self.home,
            now_fn=lambda: float(sched["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: {"state": {"runState": "RUN_STATE_RUNNING", "updateTime": "2026-11-25T23:40:00Z"}},
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity"},
        )
        self.assertEqual(running["action"], "cancelled")
        self.assertEqual(running["reason"], "conversation_state:RUN_STATE_RUNNING")

        # 3. Transcript user activity cancels delivery.
        s2 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-act"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        with open(self.transcript, "a", encoding="utf-8") as h:
            h.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "I resumed manually"}) + "\n")
        act = auto_resume.deliver_resume(
            Path(s2["statePath"]),
            s2["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s2["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: None,
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity"},
        )
        self.assertEqual(act["action"], "cancelled")
        self.assertEqual(act["reason"], "transcript_activity:user_input")

        # 4. Dead parentPid (session ended) cancels delivery.
        s3 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-dead"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: 999999,
        )
        dead = auto_resume.deliver_resume(
            Path(s3["statePath"]),
            s3["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s3["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: False,
        )
        self.assertEqual(dead["action"], "cancelled")
        self.assertEqual(dead["reason"], "session_ended")

        # 5. Stale wake (> MAX_LATE_WAKE_SECONDS past wakeAt) cancels delivery.
        s4 = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-stale"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        stale = auto_resume.deliver_resume(
            Path(s4["statePath"]),
            s4["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s4["wakeAt"]) + auto_resume.MAX_LATE_WAKE_SECONDS + 10.0,
            pid_alive_fn=lambda _: True,
        )
        self.assertEqual(stale["action"], "cancelled")
        self.assertEqual(stale["reason"], "stale_wake_window")

    def test_child_conversation_nested_process_and_non_ultraterm_skipped(self) -> None:
        # Child/swarm conversation skipped.
        r_child = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-child"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
            conversation_state_fn=lambda _: {"metadata": {"parentConversationId": "root-1"}, "state": {}},
        )
        self.assertEqual(r_child, {"action": "skipped", "reason": "child_conversation"})

        # Nested agy skipped.
        r_nested = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-nested"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
            is_pane_process_fn=lambda: False,
        )
        self.assertEqual(r_nested, {"action": "skipped", "reason": "nested_process"})

        # Outside UltraTerm skipped cleanly.
        r_ext = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-ext"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=lambda: _fake_identity(inside=False),
        )
        self.assertEqual(r_ext["action"], "skipped")
        self.assertEqual(r_ext["reason"], "no_utp_transport")

    def test_hook_cancellation_on_pre_invocation_session_start_and_user_cancel(self) -> None:
        sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-hook-cancel"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(len(auto_resume.list_pending(self.data_dir)), 1)

        # PreInvocation hook cancels scheduled resume.
        env = {k: v for k, v in os.environ.items() if not k.startswith("ULTRATERM_UI_")}
        env.update({"PLUGIN_DATA": str(self.data_dir), "HOME": str(self.home)})
        res = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "scripts/ui_transcript_sync.py"), "--event", "pre-invocation"],
            input=json.dumps({"conversationId": "conv-hook-cancel", "cwd": str(self.tmp)}),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(json.loads(res.stdout)["injectSteps"], [])
        self.assertEqual(auto_resume.list_pending(self.data_dir), [])
        saved = json.loads(Path(sched["statePath"]).read_text())
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(saved["cancelReason"], "pre_invocation")

        # Terminal-level cancellation (e.g. /new SessionStart) cancels prior conv on same terminalId only.
        s_a = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-a"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=lambda: _fake_identity(TERM_UUID),
        )
        s_b = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-b"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=lambda: _fake_identity(OTHER_UUID),
        )
        cancelled = auto_resume.cancel_for_terminal(TERM_UUID, reason="new_session", exclude_conversation_id="conv-new", data_dir=self.data_dir)
        self.assertEqual(cancelled, 1)
        self.assertEqual(json.loads(Path(s_a["statePath"]).read_text())["status"], "cancelled")
        self.assertEqual(json.loads(Path(s_b["statePath"]).read_text())["status"], "scheduled")

        # USER_CANCELED Stop hook cancels pending timer.
        res_stop = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "scripts/stop_hook.py")],
            input=json.dumps({"conversationId": "conv-b", "cwd": str(self.tmp), "terminationReason": "USER_CANCELED"}),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(json.loads(res_stop.stdout), {})
        self.assertEqual(json.loads(Path(s_b["statePath"]).read_text())["status"], "cancelled")

    def test_security_permissions_path_traversal_and_waiter_exits_on_cancel(self) -> None:
        # Path traversal conversation ID is hashed safely inside state_dir.
        sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="../../etc/passwd"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
        )
        sf = Path(sched["statePath"])
        self.assertEqual(sf.parent.resolve(), (self.data_dir / "auto-resume").resolve())
        self.assertEqual(stat.S_IMODE(sf.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(sf.parent.stat().st_mode), 0o700)

        # Live waiter exits promptly when cancelled (no leaked daemon); pass isolated HOME/PLUGIN_DATA.
        live_now = time.time()
        isolated_env = {
            "HOME": str(self.home),
            "PLUGIN_DATA": str(self.data_dir),
            "PATH": os.environ.get("PATH", ""),
        }
        live_sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-live-waiter", error="Quota exhausted. Resets in 2h"),
            data_dir=self.data_dir,
            env=isolated_env,
            home=self.home,
            now=live_now,
            spawn_waiter=True,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        self.assertEqual(live_sched["action"], "scheduled")
        waiter_pid = live_sched["waiterPid"]
        self.assertIsInstance(waiter_pid, int)
        # Cancel immediately and verify waiter exits cleanly.
        self.assertTrue(auto_resume.cancel_for_conversation("conv-live-waiter", reason="test_cancel", data_dir=self.data_dir))
        deadline = time.time() + 5.0
        while time.time() < deadline and auto_resume.pid_is_alive(waiter_pid):
            time.sleep(0.05)
        self.assertFalse(auto_resume.pid_is_alive(waiter_pid), "waiter process must exit promptly on cancel")

    def test_cli_doctor_and_auto_resume_subcommands(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ULTRATERM_UI_")}
        env.update({"PLUGIN_DATA": str(self.data_dir), "HOME": str(self.home)})
        auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-cli"),
            data_dir=self.data_dir,
            env=env,
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
        )
        st = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "bin/steak-antigravity"), "auto-resume", "status"],
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        parsed = json.loads(st.stdout)
        self.assertTrue(parsed["enabled"])
        self.assertEqual(parsed["bufferSeconds"], auto_resume.DEFAULT_BUFFER_SECONDS)
        self.assertEqual(len(parsed["pending"]), 1)
        self.assertEqual(parsed["pending"][0]["conversationId"], "conv-cli")

        cx = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "bin/steak-antigravity"), "auto-resume", "cancel"],
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(json.loads(cx.stdout)["cancelled"], 1)
        self.assertEqual(auto_resume.list_pending(self.data_dir), [])

    def test_get_conversation_state_bypasses_proxy_blocks_redirects_and_respects_opt_out(self) -> None:
        proxy_hits: list[str] = []
        ls_hits: list[str] = []
        redirect_mode = {"on": False}

        class ProxyTrapHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                proxy_hits.append(self.path)
                self.send_response(500)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:
                pass

        class LocalLsHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                ls_hits.append(self.headers.get("x-antigravity-csrf-token") or "")
                if redirect_mode["on"]:
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{proxy_srv.server_port}/leak")
                    self.end_headers()
                    return
                body = json.dumps({"id": "c-ls", "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-26T00:00:00Z"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:
                pass

        proxy_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyTrapHandler)
        ls_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalLsHandler)
        t1 = threading.Thread(target=proxy_srv.serve_forever, daemon=True)
        t2 = threading.Thread(target=ls_srv.serve_forever, daemon=True)
        t1.start()
        t2.start()
        orig_proxy = os.environ.get("HTTP_PROXY")
        orig_proxy_lower = os.environ.get("http_proxy")
        try:
            proxy_url = f"http://127.0.0.1:{proxy_srv.server_port}"
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["http_proxy"] = proxy_url
            env = {
                "ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{ls_srv.server_port}",
                "ANTIGRAVITY_CSRF_TOKEN": "test-placeholder-token",
                "HTTP_PROXY": proxy_url,
                "http_proxy": proxy_url,
            }
            state = auto_resume.get_conversation_state("c-ls", env=env)
            self.assertIsNotNone(state)
            self.assertEqual(state["state"]["runState"], "RUN_STATE_IDLE")
            self.assertEqual(proxy_hits, [], "CSRF request must never traverse HTTP_PROXY")
            self.assertEqual(ls_hits, ["test-placeholder-token"])

            # 302 redirect must NOT be followed (no token leakage to redirect target).
            redirect_mode["on"] = True
            self.assertIsNone(auto_resume.get_conversation_state("c-ls", env=env))
            self.assertEqual(proxy_hits, [], "302 redirect must never be followed")

            # Opt-out disables the LS query entirely.
            redirect_mode["on"] = False
            before = len(ls_hits)
            self.assertIsNone(
                auto_resume.get_conversation_state(
                    "c-ls", env={**env, "STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS": "0"}
                )
            )
            self.assertEqual(len(ls_hits), before)
        finally:
            if orig_proxy is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = orig_proxy
            if orig_proxy_lower is None:
                os.environ.pop("http_proxy", None)
            else:
                os.environ["http_proxy"] = orig_proxy_lower
            proxy_srv.shutdown()
            proxy_srv.server_close()
            ls_srv.shutdown()
            ls_srv.server_close()

    def test_structured_payload_fields_and_transcript_error_fallback(self) -> None:
        # 1. Structured top-level quota_resets ISO field.
        sig_iso, code_iso = auto_resume.extract_quota_reset(
            {
                "terminationReason": "ERROR",
                "error": "Quota exhausted",
                "quota_resets": "2026-11-26T02:00:00Z",
            },
            now=self.now,
        )
        self.assertEqual(code_iso, "ok")
        self.assertEqual(sig_iso.source, "payload-iso")

        # 2. Structured retryAfter numeric seconds and dict error.
        sig_dict, code_dict = auto_resume.extract_quota_reset(
            {
                "terminationReason": "ERROR",
                "error": {"message": "RESOURCE_EXHAUSTED", "retryAfter": 900},
            },
            now=self.now,
        )
        self.assertEqual(code_dict, "ok")
        self.assertEqual(sig_dict.source, "payload-duration")
        self.assertEqual(sig_dict.reset_at, int(self.now) + 900)

        # 3. Transcript fallback when Stop payload omits inline reset duration.
        tp = self.tmp / "quota-tail.jsonl"
        tp.write_text(
            "\n".join(
                [
                    json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "Go"}),
                    json.dumps(
                        {
                            "step_index": 1,
                            "type": "ERROR_MESSAGE",
                            "status": "ERROR",
                            "content": "You have exhausted your quota. Resets in 1h15m",
                        }
                    ),
                ]
            )
            + "\n"
        )
        sig_tp, code_tp = auto_resume.extract_quota_reset(
            {"terminationReason": "ERROR", "error": "", "transcriptPath": str(tp)},
            now=self.now,
        )
        self.assertEqual(code_tp, "ok")
        self.assertEqual(sig_tp.source, "error-duration")
        self.assertEqual(sig_tp.reset_at, int(self.now) + 75 * 60)

    def test_tampered_state_wake_before_reset_plus_60_and_cancellation_cases(self) -> None:
        # 1. Tampered state file with wakeAt < resetAt + 60 is still blocked until resetAt + 60.
        sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-tamper"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        sf = Path(sched["statePath"])
        tampered = json.loads(sf.read_text())
        tampered["wakeAt"] = tampered["resetAt"] + 10
        sf.write_text(json.dumps(tampered))

        res_early = auto_resume.deliver_resume(
            sf,
            sched["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(tampered["resetAt"] + 30),
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: None,
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity"},
            utp_send_fn=lambda tid, prompt: (True, "ok"),
        )
        self.assertEqual(res_early["action"], "too_early")
        self.assertGreaterEqual(res_early["remainingSeconds"], 30)

        # 2. conversation_updated cancellation when updateTime changed.
        s_upd = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-upd"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            conversation_state_fn=lambda _: {"metadata": {}, "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-25T23:40:00Z"}},
            parent_pid_fn=lambda: os.getpid(),
        )
        r_upd = auto_resume.deliver_resume(
            Path(s_upd["statePath"]),
            s_upd["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s_upd["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: {"state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-26T00:01:00Z"}},
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity"},
        )
        self.assertEqual(r_upd, {"action": "cancelled", "reason": "conversation_updated"})

        # 3. terminal_gone_or_exited and terminal_harness_changed cancellations.
        s_gone = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-gone"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        r_gone = auto_resume.deliver_resume(
            Path(s_gone["statePath"]),
            s_gone["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s_gone["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: None,
            terminal_lookup_fn=lambda _: None,
        )
        self.assertEqual(r_gone, {"action": "cancelled", "reason": "terminal_gone_or_exited"})

        s_harn = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-harn"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        r_harn = auto_resume.deliver_resume(
            Path(s_harn["statePath"]),
            s_harn["token"],
            env={},
            home=self.home,
            now_fn=lambda: float(s_harn["wakeAt"]) + 1.0,
            pid_alive_fn=lambda _: True,
            conversation_state_fn=lambda _: None,
            terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "different-harness"},
        )
        self.assertEqual(r_harn, {"action": "cancelled", "reason": "terminal_harness_changed"})

        # 4. Non-quota ERROR stop cancels an existing pending timer for that conversation.
        s_nq = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-nonquota"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            identity_fn=_fake_identity,
        )
        self.assertEqual(s_nq["action"], "scheduled")
        r_nq = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-nonquota", error="Connection reset by peer"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now + 5,
            spawn_waiter=False,
            identity_fn=_fake_identity,
        )
        self.assertEqual(r_nq, {"action": "skipped", "reason": "non_quota_error"})
        saved_nq = json.loads(Path(s_nq["statePath"]).read_text())
        self.assertEqual(saved_nq["status"], "cancelled")
        self.assertEqual(saved_nq["cancelReason"], "non_quota_error")

    def test_waiter_wakes_loops_on_clock_step_and_cleans_firing_on_signal(self) -> None:
        # 1. run_waiter wakes, retries after simulated backward clock step (too_early), then delivers.
        sched = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-waiter-loop"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        sf = Path(sched["statePath"])
        tok = sched["token"]
        wake_at = sched["wakeAt"]
        clock = {"t": float(wake_at - 1)}
        deliver_calls = {"n": 0}

        def fake_sleep(sec: float) -> None:
            clock["t"] += 2.0

        def fake_deliver(path: Path, token: str) -> dict:
            deliver_calls["n"] += 1
            if deliver_calls["n"] == 1:
                return {"action": "too_early", "remainingSeconds": 1}
            return auto_resume.deliver_resume(
                path,
                token,
                env={},
                home=self.home,
                now_fn=lambda: float(wake_at + 2),
                pid_alive_fn=lambda _: True,
                conversation_state_fn=lambda _: None,
                terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity"},
                utp_send_fn=lambda tid, prompt: (True, "ok"),
            )

        rc = auto_resume_waiter.run_waiter(
            sf,
            tok,
            check_interval=0.05,
            deliver_fn=fake_deliver,
            now_fn=lambda: clock["t"],
            sleep_fn=fake_sleep,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(deliver_calls["n"], 2)
        self.assertEqual(json.loads(sf.read_text())["status"], "fired")

        # 2. Signal raised during utp_send never leaves state stuck in "firing".
        s_sig = auto_resume.schedule_from_stop(
            self._quota_payload(conv_id="conv-sig-firing"),
            data_dir=self.data_dir,
            env={},
            home=self.home,
            now=self.now,
            spawn_waiter=False,
            jitter_fn=lambda _: 0,
            identity_fn=_fake_identity,
            parent_pid_fn=lambda: os.getpid(),
        )
        sf_sig = Path(s_sig["statePath"])

        def interrupt_send(_tid: str, _prompt: str) -> tuple[bool, str]:
            raise auto_resume_waiter._StopSignal()

        rc_sig = auto_resume_waiter.run_waiter(
            sf_sig,
            s_sig["token"],
            check_interval=0.05,
            now_fn=lambda: float(s_sig["wakeAt"] + 1),
            deliver_fn=lambda path, token: auto_resume.deliver_resume(
                path,
                token,
                env={},
                home=self.home,
                now_fn=lambda: float(s_sig["wakeAt"] + 1),
                pid_alive_fn=lambda _: True,
                conversation_state_fn=lambda _: None,
                terminal_lookup_fn=lambda tid: {"terminalId": tid, "harness": "antigravity"},
                utp_send_fn=interrupt_send,
            ),
        )
        self.assertEqual(rc_sig, 0)
        saved_sig = json.loads(sf_sig.read_text())
        self.assertEqual(saved_sig["status"], "cancelled")
        self.assertEqual(saved_sig["cancelReason"], "interrupted:_StopSignal")

    def test_stop_hook_error_and_session_start_cancel_subprocesses(self) -> None:
        # Build isolated stub tmux and ~/.ultraterm/bin/utp so both stop_hook.py and session_start.py
        # run end-to-end as subprocesses with verified UltraTerm identity.
        bin_dir = self.home / ".ultraterm" / "bin"
        bin_dir.mkdir(parents=True, mode=0o700)
        utp_stub = bin_dir / "utp"
        utp_stub.write_text(
            f"#!/bin/sh\n"
            f"if [ \"$1\" = \"list\" ]; then\n"
            f"  printf '2\\tTest\\tantigravity\\tdefault\\t{TERM_UUID}\\n'\n"
            f"  exit 0\n"
            f"fi\n"
            f"exit 0\n"
        )
        os.chmod(utp_stub, 0o700)

        tmux_stub = self.tmp / "fake-tmux"
        # Return pane_pid matching our own process so publisher_is_pane_process() accepts it.
        tmux_stub.write_text(
            f"#!/bin/sh\n"
            f"case \"$*\" in\n"
            f"  *pane_pid*) echo {os.getpid()} ;;\n"
            f"  *session_name*) printf 'ultraterm-matrix-2\\t%%9\\t/dev/ttys0\\t1\\n' ;;\n"
            f"esac\n"
            f"exit 0\n"
        )
        os.chmod(tmux_stub, 0o700)

        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "PLUGIN_DATA": str(self.data_dir),
            "TMUX_BIN": str(tmux_stub),
            "TMUX_PANE": "%9",
            "STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS": "0",
        }
        try:
            res_stop = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/stop_hook.py")],
                input=json.dumps(
                    {
                        "conversationId": "conv-e2e-1",
                        "cwd": str(self.tmp),
                        "transcriptPath": str(self.transcript),
                        "lastUserInput": "Continue task",
                        "terminationReason": "ERROR",
                        "error": "RESOURCE_EXHAUSTED: Quota exhausted. Resets in 2h",
                    }
                ),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            self.assertEqual(json.loads(res_stop.stdout), {})
            pending = auto_resume.list_pending(self.data_dir)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["conversationId"], "conv-e2e-1")

            # Now run session_start.py for a new conversation in the same UltraTerm pane -> cancels conv-e2e-1.
            res_start = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/session_start.py")],
                input=json.dumps({"conversationId": "conv-e2e-2", "cwd": str(self.tmp)}),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            self.assertIn("injectSteps", json.loads(res_start.stdout))
            self.assertEqual(auto_resume.list_pending(self.data_dir), [])
            sf = auto_resume.state_path_for("conv-e2e-1", self.data_dir)
            saved = json.loads(sf.read_text())
            self.assertEqual(saved["status"], "cancelled")
            self.assertEqual(saved["cancelReason"], "new_session")
        finally:
            auto_resume.cancel_all(reason="test_cleanup", data_dir=self.data_dir)


if __name__ == "__main__":
    unittest.main()
