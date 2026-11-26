import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import pane_activity  # noqa: E402


class TestPaneActivity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="steak-antigravity-pane-"))
        self.panes_dir = self.tmp / "ultraterm" / "antigravity" / "panes"
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(mode=0o700)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_public(self, session: str = "ultraterm-matrix-1") -> dict:
        return json.loads((self.panes_dir / f"{session}.json").read_text(encoding="utf-8"))

    def test_statusline_publishes_non_pii_subagent_counts_and_permissions(self) -> None:
        payload = {
            "email": "private-user@example.com",
            "cwd": "/private/secret/project",
            "conversation_id": "9081fd43-39a1-4356-839f-58a20906c2a3",
            "conversation_title": "Secret client title",
            "transcript_path": "/private/transcript.jsonl",
            "agent_state": "working",
            "task_count": 2,
            "subagents": [
                {"name": "secret-worker-1", "role": "worker", "status": "running"},
                {"name": "secret-worker-2", "role": "worker", "status": "running"},
                {"name": "secret-worker-3", "role": "verifier", "status": "completed"},
            ],
        }
        ok = pane_activity.record_statusline(
            payload,
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=1_795_665_000,
        )
        self.assertTrue(ok)
        out = self.panes_dir / "ultraterm-matrix-1.json"
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.panes_dir.stat().st_mode), 0o700)
        raw = out.read_text(encoding="utf-8")
        for forbidden in (
            "private-user@example.com",
            "/private/secret/project",
            "9081fd43-39a1-4356-839f-58a20906c2a3",
            "Secret client title",
            "secret-worker-1",
        ):
            self.assertNotIn(forbidden, raw)
        snap = json.loads(raw)
        self.assertEqual(snap["schemaVersion"], 1)
        self.assertEqual(snap["source"], "antigravity-statusline")
        self.assertEqual(snap["pid"], 47084)
        self.assertEqual(snap["capturedAt"], 1_795_665_000)
        self.assertEqual(snap["activeSubagents"], 2)
        self.assertEqual(snap["inactiveSubagents"], 1)
        self.assertEqual(snap["taskCount"], 2)
        self.assertEqual(snap["agentState"], "working")

    def test_pane_ownership_gate_refuses_foreign_nested_processes(self) -> None:
        # When first non-shell ancestor != tmux pane_pid, resolve_owned_pane_pid refuses
        # and neither overwrites nor resets the live pane's state.
        coord = "coord-1"
        t0 = 1_795_665_000
        self.assertTrue(
            pane_activity.record_statusline(
                {"conversation_id": coord, "subagents": [{"status": "running"}]},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0,
            )
        )
        with (
            mock.patch.object(pane_activity, "_pane_pid_for_current_pane", return_value=47084),
            mock.patch.object(pane_activity.os, "getppid", return_value=99999),
            mock.patch.object(pane_activity, "_process_row", return_value=(47084, "node")),
        ):
            self.assertIsNone(pane_activity.resolve_owned_pane_pid())
            self.assertFalse(
                pane_activity.record_hook_event(
                    "pre-invocation",
                    {"conversationId": "nested-test"},
                    tmux_session="ultraterm-matrix-1",
                    panes_dir=self.panes_dir,
                    data_dir=self.data_dir,
                    now_seconds=t0 + 5,
                )
            )
        # Live pane snapshot remains untouched with real agy PID.
        snap = self._read_public()
        self.assertEqual(snap["pid"], 47084)
        self.assertEqual(snap["activeSubagents"], 1)

        # Direct child of shell -> agy pane_pid succeeds.
        with (
            mock.patch.object(pane_activity, "_pane_pid_for_current_pane", return_value=47084),
            mock.patch.object(pane_activity.os, "getppid", return_value=55000),
            mock.patch.object(pane_activity, "_process_row", return_value=(47084, "sh")),
        ):
            self.assertEqual(pane_activity.resolve_owned_pane_pid(), 47084)

    def test_swarm_child_hooks_track_per_child_active_and_inactive_across_turns(self) -> None:
        coord = "9081fd43-39a1-4356-839f-58a20906c2a3"
        w1 = "63ff5a20-03b5-4dcc-9ee9-134b625ef7dc"
        w2 = "d4b97781-2102-4c34-8997-cb181fd39ce2"
        t0 = 1_795_665_000

        pane_activity.record_statusline(
            {"conversation_id": coord, "agent_state": "idle"},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0,
        )
        # 1 worker × 5 turns (pre-inv -> post-tool swarm_wait -> post-inv -> stop)
        # must report inactive=1 (per distinct child), NEVER 10, and post-invocation
        # after swarm_wait/stop must NEVER resurrect a phantom active child.
        for turn in range(5):
            base = t0 + turn * 20
            pane_activity.record_hook_event(
                "pre-invocation",
                {"conversationId": w1},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=base + 1,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 1)
            self.assertEqual(self._read_public()["inactiveSubagents"], 0)
            pane_activity.record_hook_event(
                "post-tool",
                {"conversationId": w1, "toolCall": {"name": "swarm_wait"}},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=base + 2,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 0)
            self.assertEqual(self._read_public()["inactiveSubagents"], 1)
            # post-invocation arriving after swarm_wait is refresh-only (no resurrection).
            pane_activity.record_hook_event(
                "post-invocation",
                {"conversationId": w1},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=base + 3,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 0)
            pane_activity.record_hook_event(
                "stop",
                {"conversationId": w1},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=base + 4,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 0)
            self.assertEqual(self._read_public()["inactiveSubagents"], 1)

        # Second distinct child finishes -> inactive becomes 2.
        pane_activity.record_hook_event(
            "pre-invocation",
            {"conversationId": w2},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 200,
        )
        self.assertEqual(self._read_public()["activeSubagents"], 1)
        self.assertEqual(self._read_public()["inactiveSubagents"], 1)
        pane_activity.record_hook_event(
            "stop",
            {"conversationId": w2},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 210,
        )
        self.assertEqual(self._read_public()["activeSubagents"], 0)
        self.assertEqual(self._read_public()["inactiveSubagents"], 2)

        # Private state must not contain raw conversation IDs either.
        priv_raw = (self.data_dir / "pane-activity" / "ultraterm-matrix-1.json").read_text(encoding="utf-8")
        for raw_id in (coord, w1, w2):
            self.assertNotIn(raw_id, priv_raw)

    def test_unpinned_coordinator_fails_closed_and_ui_transcript_repins_on_new(self) -> None:
        t0 = 1_795_665_000
        # Mid-session upgrade before statusLine/session-start: unknown pre-invocation
        # must NOT be counted as a child.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ULTRATERM_UI_SESSION_ID", None)
            pane_activity.record_hook_event(
                "pre-invocation",
                {"conversationId": "coord-unpinned"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 0)

        # statusLine with only session_id (no conversation_id) does NOT pin a mismatched id.
        pane_activity.record_statusline(
            {"session_id": "non-conv-session", "agent_state": "idle"},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 1,
        )
        priv = json.loads((self.data_dir / "pane-activity" / "ultraterm-matrix-1.json").read_text(encoding="utf-8"))
        self.assertEqual(priv["coordinatorHash"], "")

        # UI transcript state pins coordinator AND re-pins on /new (clearing old leases).
        ui_id = "11111111-2222-3333-4444-555555555555"
        ui_dir = self.data_dir / "ui-transcript-state"
        ui_dir.mkdir(mode=0o700, exist_ok=True)
        ui_state_path = ui_dir / f"{ui_id}.json"
        ui_state_path.write_text(json.dumps({"conversationId": "coord-v1"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"ULTRATERM_UI_SESSION_ID": ui_id}):
            pane_activity.record_hook_event(
                "pre-invocation",
                {"conversationId": "worker-old"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 2,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 1)
            # /new switches ui-transcript-state to coord-v2 on session-start -> re-pins & clears old leases.
            ui_state_path.write_text(json.dumps({"conversationId": "coord-v2"}), encoding="utf-8")
            pane_activity.record_hook_event(
                "session-start",
                {"conversationId": "coord-v2"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 3,
            )
            pane_activity.record_hook_event(
                "pre-invocation",
                {"conversationId": "coord-v2"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 4,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 0)
            self.assertEqual(self._read_public()["agentState"], "working")

    def test_statusline_pin_ignores_rotating_ui_transcript_state(self) -> None:
        # Regression test for R2-1: when statusLine pins coordinator, UI transcript
        # state rotating to a child conversation must NOT re-pin or wipe active workers.
        t0 = 1_795_665_000
        ui_id = "22222222-3333-4444-5555-666666666666"
        ui_dir = self.data_dir / "ui-transcript-state"
        ui_dir.mkdir(mode=0o700, exist_ok=True)
        ui_state_path = ui_dir / f"{ui_id}.json"
        ui_state_path.write_text(json.dumps({"conversationId": "coord"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"ULTRATERM_UI_SESSION_ID": ui_id}):
            pane_activity.record_statusline(
                {"conversation_id": "coord", "agent_state": "idle"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0,
            )
            # Worker 1 starts, and ui_transcript_sync rotates ui-transcript-state to w1.
            pane_activity.record_hook_event(
                "pre-invocation",
                {"conversationId": "w1"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 1,
            )
            ui_state_path.write_text(json.dumps({"conversationId": "w1"}), encoding="utf-8")
            # Worker 2 starts: must see 2 active (w1 + w2), NOT wipe w1 or re-pin to w1.
            pane_activity.record_hook_event(
                "pre-invocation",
                {"conversationId": "w2"},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 2,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 2)
            # Coordinator tool call keeps active=2 (never counts coord as child).
            pane_activity.record_hook_event(
                "post-tool",
                {"conversationId": "coord", "toolCall": {"name": "read_file"}},
                tmux_session="ultraterm-matrix-1",
                agy_pid=47084,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 3,
            )
            self.assertEqual(self._read_public()["activeSubagents"], 2)

    def test_preserves_preexisting_ancestor_mode_and_sweeps_dead_pane_files(self) -> None:
        # R2-2: pre-existing ancestor dir keeps its mode (e.g. 0755) while leaf is 0700.
        ancestor = self.tmp / "preexisting"
        ancestor.mkdir(mode=0o755)
        os.chmod(ancestor, 0o755)
        leaf = ancestor / "panes"
        self.assertTrue(pane_activity._ensure_private_dir(leaf, trusted_root=self.tmp))
        self.assertEqual(stat.S_IMODE(ancestor.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o700)

        # R2-3: dead-pid pane snapshots are pruned on session-start while live ones remain.
        dead = leaf / "ultraterm-agent-dead00000001.json"
        live = leaf / "ultraterm-matrix-1.json"
        dead.write_text(json.dumps({"pid": 2_147_483_647, "activeSubagents": 1}), encoding="utf-8")
        live.write_text(json.dumps({"pid": os.getpid(), "activeSubagents": 1}), encoding="utf-8")
        removed = pane_activity.sweep_dead_pane_files(leaf)
        self.assertEqual(removed, 1)
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())

    def test_child_stop_decrements_statusline_by_one_not_to_zero(self) -> None:
        coord = "coord-1111"
        w1 = "worker-1"
        t0 = 1_795_665_000
        # statusLine reports 3 running subagents; 1 hooked child starts and stops -> drops to 2, not 0.
        pane_activity.record_statusline(
            {
                "conversation_id": coord,
                "subagents": [
                    {"status": "running"},
                    {"status": "running"},
                    {"status": "running"},
                ],
            },
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0,
        )
        pane_activity.record_hook_event(
            "pre-invocation",
            {"conversationId": w1},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 1,
        )
        self.assertEqual(self._read_public()["activeSubagents"], 3)
        pane_activity.record_hook_event(
            "stop",
            {"conversationId": w1},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 5,
        )
        self.assertEqual(self._read_public()["activeSubagents"], 2)

        # Duplicate stop for an already-inactive child does not decrement again.
        pane_activity.record_hook_event(
            "stop",
            {"conversationId": w1},
            tmux_session="ultraterm-matrix-1",
            agy_pid=47084,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 6,
        )
        self.assertEqual(self._read_public()["activeSubagents"], 2)

    def test_stale_leases_lock_timeout_and_symlink_ancestor_refusals(self) -> None:
        t0 = 1_795_665_000
        # Crashed child lease expires after CHILD_LEASE_TTL_SECS.
        pane_activity.record_statusline(
            {"conversation_id": "c1"},
            tmux_session="ultraterm-matrix-2",
            agy_pid=100,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0,
        )
        pane_activity.record_hook_event(
            "pre-invocation",
            {"conversationId": "crashed"},
            tmux_session="ultraterm-matrix-2",
            agy_pid=100,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 1,
        )
        self.assertEqual(self._read_public("ultraterm-matrix-2")["activeSubagents"], 1)
        pane_activity.record_hook_event(
            "pre-invocation",
            {"conversationId": "c1"},
            tmux_session="ultraterm-matrix-2",
            agy_pid=100,
            panes_dir=self.panes_dir,
            data_dir=self.data_dir,
            now_seconds=t0 + 1 + pane_activity.CHILD_LEASE_TTL_SECS + 1,
        )
        self.assertEqual(self._read_public("ultraterm-matrix-2")["activeSubagents"], 0)

        # Non-blocking lock times out cleanly instead of hanging.
        lock_file = self.data_dir / "pane-activity" / "ultraterm-matrix-2.lock"
        with open(lock_file, "wb") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with pane_activity._state_lock(lock_file, trusted_root=self.data_dir, timeout=0.05) as acquired:
                    self.assertFalse(acquired)
            finally:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)

        # Symlink target AND symlink ancestor dir are both refused.
        victim = self.tmp / "victim.json"
        victim.write_text("KEEP", encoding="utf-8")
        link = self.panes_dir / "ultraterm-matrix-3.json"
        link.symlink_to(victim)
        self.assertFalse(
            pane_activity.record_statusline(
                {"conversation_id": "c1", "subagents": [{"status": "running"}]},
                tmux_session="ultraterm-matrix-3",
                agy_pid=100,
                panes_dir=self.panes_dir,
                data_dir=self.data_dir,
                now_seconds=t0 + 10,
            )
        )
        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP")

        sym_parent = self.tmp / "sym-parent"
        real_dir = self.tmp / "real-dir"
        real_dir.mkdir(mode=0o700)
        sym_parent.symlink_to(real_dir)
        self.assertFalse(
            pane_activity._ensure_private_dir(sym_parent / "panes", trusted_root=self.tmp)
        )

        # Stale own .tmp files are swept while fresh ones remain.
        stale_tmp = self.panes_dir / ".usage.json.1.tmp"
        fresh_tmp = self.panes_dir / ".usage.json.2.tmp"
        stale_tmp.write_text("old", encoding="utf-8")
        fresh_tmp.write_text("new", encoding="utf-8")
        old_time = t0 - pane_activity.STALE_TMP_AGE_SECS - 10
        os.utime(stale_tmp, (old_time, old_time))
        pane_activity.sweep_stale_temp_files(self.panes_dir, now=float(t0))
        self.assertFalse(stale_tmp.exists())
        self.assertTrue(fresh_tmp.exists())

    def test_statusline_cli_writes_usage_first_and_pane_activity_isolated(self) -> None:
        usage_out = self.tmp / "usage.json"
        # Strip tmux/ULTRATERM env so pane ownership gate refuses cleanly outside real pane,
        # while usage.json STILL succeeds first.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("ULTRATERM_") and k not in ("TMUX", "TMUX_PANE")
        }
        env["HOME"] = str(self.tmp)
        env["PLUGIN_DATA"] = str(self.data_dir)
        payload = {
            "conversation_id": "conv-7",
            "agent_state": "working",
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.9, "reset_time": "2026-11-26T12:00:00Z"},
            },
        }
        res = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(ROOT / "scripts/antigravity_statusline.py"),
                "--output",
                str(usage_out),
                "--pane-dir",
                str(self.panes_dir),
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(res.stdout, "")
        self.assertTrue(usage_out.is_file())
        self.assertFalse((self.panes_dir / "ultraterm-matrix-7.json").exists())


if __name__ == "__main__":
    unittest.main()
