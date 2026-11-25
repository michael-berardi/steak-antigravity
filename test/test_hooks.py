import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import stop_hook  # noqa: E402
import ui_transcript_sync  # noqa: E402


class TestUiTranscriptSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="steak-antigravity-ui-"))
        self.ui_dir = self.tmp / "ui-transcripts"
        self.ui_dir.mkdir(mode=0o700)
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(mode=0o700)
        self.session_id = "11111111-2222-3333-4444-555555555555"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publishes_valid_pi_rows_and_rotates_on_new_conversation(self) -> None:
        src = self.tmp / "transcript.jsonl"
        src.write_text(
            "\n".join(
                [
                    json.dumps({
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-25T01:00:00Z",
                        "content": "<USER_REQUEST>\nHello world\n</USER_REQUEST>",
                    }),
                    json.dumps({
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-25T01:00:01Z",
                        "model": "test-model",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/a.txt"}}],
                    }),
                    json.dumps({
                        "step_index": 2,
                        "type": "GENERIC",
                        "source": "MODEL",
                        "created_at": "2026-11-25T01:00:02Z",
                        "content": "file contents",
                    }),
                ]
            )
            + "\n"
        )
        ui_file = self.ui_dir / f"{self.session_id}.jsonl"
        ok = ui_transcript_sync.sync_transcript(
            ui_file=ui_file,
            ui_session_id=self.session_id,
            conversation_id="conv-1",
            source_transcript=src,
            cwd=str(self.tmp),
            event_name="stop",
            data_dir=self.data_dir,
        )
        self.assertTrue(ok)
        self.assertEqual(stat.S_IMODE(ui_file.stat().st_mode), 0o600)

        rows = [json.loads(line) for line in ui_file.read_text().splitlines()]
        self.assertEqual(rows[0]["type"], "session")
        self.assertEqual(rows[0]["version"], 3)
        self.assertEqual(rows[0]["id"], self.session_id)
        self.assertEqual(rows[1]["message"]["role"], "user")
        self.assertEqual(rows[1]["message"]["content"], "Hello world")
        self.assertIsNone(rows[1]["parentId"])
        # Tool call normalized Path -> path
        tc = rows[2]["message"]["content"][0]
        self.assertEqual(tc["type"], "toolCall")
        self.assertEqual(tc["arguments"]["path"], "/tmp/a.txt")
        self.assertEqual(rows[2]["parentId"], rows[1]["id"])
        # Tool result matched toolCallId
        self.assertEqual(rows[3]["message"]["role"], "toolResult")
        self.assertEqual(rows[3]["message"]["toolCallId"], tc["id"])
        # Stop emits stopReason: stop
        self.assertEqual(rows[4]["message"]["stopReason"], "stop")

        old_inode = ui_file.stat().st_ino
        # Conversation switch rotates atomically (new inode, fresh header)
        ok2 = ui_transcript_sync.sync_transcript(
            ui_file=ui_file,
            ui_session_id=self.session_id,
            conversation_id="conv-2",
            source_transcript=None,
            cwd=str(self.tmp),
            event_name="session-start",
            data_dir=self.data_dir,
        )
        self.assertTrue(ok2)
        self.assertNotEqual(ui_file.stat().st_ino, old_inode)
        rotated = [json.loads(line) for line in ui_file.read_text().splitlines()]
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0]["id"], self.session_id)

    def test_refuses_preexisting_foreign_file_and_symlinks(self) -> None:
        foreign = self.ui_dir / f"{self.session_id}.jsonl"
        foreign.write_text("existing\n")
        os.chmod(foreign, 0o600)
        ok = ui_transcript_sync.sync_transcript(
            ui_file=foreign,
            ui_session_id=self.session_id,
            conversation_id="conv-1",
            source_transcript=None,
            cwd=str(self.tmp),
            event_name="session-start",
            data_dir=self.data_dir,
        )
        self.assertFalse(ok)
        self.assertEqual(foreign.read_text(), "existing\n")

        link = self.ui_dir / "22222222-2222-3333-4444-555555555555.jsonl"
        link.symlink_to(foreign)
        ok_sym = ui_transcript_sync.sync_transcript(
            ui_file=link,
            ui_session_id="22222222-2222-3333-4444-555555555555",
            conversation_id="conv-1",
            source_transcript=None,
            cwd=str(self.tmp),
            event_name="session-start",
            data_dir=self.data_dir,
        )
        self.assertFalse(ok_sym)

    def test_matches_tool_results_by_tool_call_id_out_of_order(self) -> None:
        state: dict = {}
        ui_transcript_sync.translate_entry(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"id": "call-a", "name": "read_file", "args": {"Path": "/tmp/a"}},
                    {"id": "call-b", "name": "run_command", "args": {"CommandLine": "ls"}},
                ],
            },
            state,
        )
        rows_b = ui_transcript_sync.translate_entry(
            {
                "step_index": 2,
                "type": "TOOL_RESULT",
                "source": "MODEL",
                "tool_call_id": "call-b",
                "content": "b-out",
            },
            state,
        )
        rows_a = ui_transcript_sync.translate_entry(
            {
                "step_index": 3,
                "type": "TOOL_RESULT",
                "source": "MODEL",
                "tool_call_id": "call-a",
                "content": "a-out",
            },
            state,
        )
        self.assertEqual(rows_b[0]["message"]["toolCallId"], "call-b")
        self.assertEqual(rows_b[0]["message"]["toolName"], "run_command")
        self.assertEqual(rows_a[0]["message"]["toolCallId"], "call-a")
        self.assertEqual(rows_a[0]["message"]["toolName"], "read_file")

    def test_safe_int_rejects_negative_and_infinite_values(self) -> None:
        self.assertEqual(ui_transcript_sync.safe_int(-5, 0), 0)
        self.assertEqual(ui_transcript_sync.safe_int("-10", 7), 7)
        self.assertEqual(ui_transcript_sync.safe_int(float("inf"), 3), 3)
        self.assertEqual(ui_transcript_sync.safe_int(True, 9), 9)
        self.assertEqual(ui_transcript_sync.safe_int(42, 0), 42)

    def test_drain_usap_notices_scoped_by_pid_and_resets_on_truncation(self) -> None:
        events = self.data_dir / "usap-events"
        events.mkdir(mode=0o700)
        f1 = events / "4242.jsonl"
        other = events / "9999.jsonl"
        f1.write_text(json.dumps({"runIds": ["run-a"], "at": 100, "text": "x" * 200}) + "\n")
        other.write_text(json.dumps({"runIds": ["run-other"], "at": 100}) + "\n")
        first = ui_transcript_sync.drain_usap_notices(self.data_dir, parent_pid=4242)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["runIds"], ["run-a"])
        # Sibling session file must never be drained by this session.
        self.assertEqual(ui_transcript_sync.drain_usap_notices(self.data_dir, parent_pid=4242), [])
        # Simulate PID reuse / truncation with a shorter file
        f1.write_text(json.dumps({"runIds": ["run-b"], "at": 200}) + "\n")
        second = ui_transcript_sync.drain_usap_notices(self.data_dir, parent_pid=4242)
        self.assertEqual(second, [{"runIds": ["run-b"], "at": 200}])

    def test_empty_initial_conversation_id_adopts_on_later_event(self) -> None:
        ui_file = self.ui_dir / f"{self.session_id}.jsonl"
        # SessionStart with empty conversationId creates header and records dev/ino ownership.
        ok1 = ui_transcript_sync.sync_transcript(
            ui_file=ui_file,
            ui_session_id=self.session_id,
            conversation_id="",
            source_transcript=None,
            cwd=str(self.tmp),
            event_name="session-start",
            data_dir=self.data_dir,
        )
        self.assertTrue(ok1)
        src = self.tmp / "golden.jsonl"
        src.write_text(
            "\n".join(
                [
                    json.dumps({
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-25T01:18:19.123456789Z",
                        "content": "<USER_REQUEST>\nAudit live sites\n</USER_REQUEST>",
                    }),
                    json.dumps({
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-25T01:18:20.987654321Z",
                        "input_tokens": 11449,
                        "cache_read_tokens": 17167,
                        "output_tokens": 847,
                        "response": "Checking live sites.",
                        "tool_calls": [{"id": "tc-1", "name": "run_command", "args": {"CommandLine": "ls -la"}}],
                    }),
                    json.dumps({
                        "step_index": 2,
                        "type": "GENERIC",
                        "source": "MODEL",
                        "created_at": "2026-11-25T01:18:21Z",
                        "content": "total 0",
                    }),
                ]
            )
            + "\n"
        )
        ok2 = ui_transcript_sync.sync_transcript(
            ui_file=ui_file,
            ui_session_id=self.session_id,
            conversation_id="conv-real-1",
            source_transcript=src,
            cwd=str(self.tmp),
            event_name="stop",
            data_dir=self.data_dir,
        )
        self.assertTrue(ok2)
        rows = [json.loads(l) for l in ui_file.read_text().splitlines()]
        self.assertEqual(rows[1]["message"]["role"], "user")
        self.assertEqual(rows[2]["message"]["content"][1]["arguments"]["command"], "ls -la")
        self.assertEqual(rows[3]["message"]["role"], "toolResult")
        self.assertEqual(rows[4]["message"]["stopReason"], "stop")

    def test_oversized_single_line_skips_with_placeholder_and_continues(self) -> None:
        orig_bytes = ui_transcript_sync.MAX_SYNC_BYTES
        try:
            ui_transcript_sync.MAX_SYNC_BYTES = 256
            src = self.tmp / "oversized.jsonl"
            big = json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "A" * 600})
            tail = json.dumps({"step_index": 1, "type": "USER_INPUT", "created_at": "2026-11-25T01:00:00.5Z", "content": "after big"})
            src.write_text(big + "\n" + tail + "\n")
            ui_file = self.ui_dir / f"{self.session_id}.jsonl"
            ok = ui_transcript_sync.sync_transcript(
                ui_file=ui_file,
                ui_session_id=self.session_id,
                conversation_id="conv-big",
                source_transcript=src,
                cwd=str(self.tmp),
                event_name="post-tool",
                data_dir=self.data_dir,
            )
            self.assertTrue(ok)
            rows = [json.loads(l) for l in ui_file.read_text().splitlines()]
            self.assertTrue(any("omitted" in str(r) for r in rows))
            self.assertTrue(any(r.get("message", {}).get("content") == "after big" for r in rows))
        finally:
            ui_transcript_sync.MAX_SYNC_BYTES = orig_bytes

    def test_ancestor_walk_resolves_usap_notices_across_shell_wrapper(self) -> None:
        events = self.data_dir / "usap-events"
        events.mkdir(mode=0o700)
        # Key events on our current process PID; a child sh -> exec python subprocess
        # discovers it via ppid, and _resolve_notice_pid also walks higher ancestors.
        (events / f"{os.getpid()}.jsonl").write_text(
            json.dumps({"runIds": ["run-anc-1", "bad\nid"], "at": 123}) + "\n"
        )
        res = subprocess.run(
            [
                "/bin/sh",
                "-c",
                f'exec "{sys.executable}" -I -B -c "import json, sys; from pathlib import Path; sys.path.insert(0, {str(ROOT / "scripts")!r}); import ui_transcript_sync as m; print(json.dumps(m.drain_usap_notices(Path({str(self.data_dir)!r}))))"',
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        drained = json.loads(res.stdout)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["runIds"], ["run-anc-1"])

        # Also verify multi-hop ancestor resolution directly (e.g. non-exec sh fork).
        by_pid = self.data_dir / "usap" / "by-pid"
        by_pid.mkdir(parents=True, mode=0o700)
        (by_pid / "77777.json").write_text("{}")
        orig_anc = ui_transcript_sync._ancestor_pids
        try:
            ui_transcript_sync._ancestor_pids = lambda max_depth=16: [99999, 88888, 77777]
            self.assertEqual(ui_transcript_sync._resolve_notice_pid(self.data_dir, None), 77777)
        finally:
            ui_transcript_sync._ancestor_pids = orig_anc

    def test_stop_does_not_emit_premature_stop_when_line_cap_hit(self) -> None:
        src = self.tmp / "many.jsonl"
        orig_cap = ui_transcript_sync.MAX_SYNC_LINES
        try:
            ui_transcript_sync.MAX_SYNC_LINES = 2
            lines = [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "hi"}),
                json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "tool_calls": [{"id": "c1", "name": "read_file"}]}),
                json.dumps({"step_index": 2, "type": "TOOL_RESULT", "source": "MODEL", "tool_call_id": "c1", "content": "ok"}),
            ]
            src.write_text("\n".join(lines) + "\n")
            ui_file = self.ui_dir / f"{self.session_id}.jsonl"
            ui_transcript_sync.sync_transcript(
                ui_file=ui_file,
                ui_session_id=self.session_id,
                conversation_id="conv-cap",
                source_transcript=src,
                cwd=str(self.tmp),
                event_name="stop",
                data_dir=self.data_dir,
            )
            rows = [json.loads(l) for l in ui_file.read_text().splitlines()]
            self.assertFalse(any(r.get("message", {}).get("stopReason") == "stop" for r in rows))
        finally:
            ui_transcript_sync.MAX_SYNC_LINES = orig_cap

    def test_oversized_single_line_is_skipped_without_stalling_sync(self) -> None:
        src = self.tmp / "giant.jsonl"
        orig_bytes = ui_transcript_sync.MAX_SYNC_BYTES
        try:
            ui_transcript_sync.MAX_SYNC_BYTES = 128
            giant = json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "x" * 300})
            follow = json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "after giant"})
            src.write_text(giant + "\n" + follow + "\n")
            ui_file = self.ui_dir / f"{self.session_id}.jsonl"
            ui_transcript_sync.sync_transcript(
                ui_file=ui_file,
                ui_session_id=self.session_id,
                conversation_id="conv-giant",
                source_transcript=src,
                cwd=str(self.tmp),
                event_name="sync",
                data_dir=self.data_dir,
            )
            # Second sync must advance past the oversized line and read the following line
            ui_transcript_sync.sync_transcript(
                ui_file=ui_file,
                ui_session_id=self.session_id,
                conversation_id="conv-giant",
                source_transcript=src,
                cwd=str(self.tmp),
                event_name="stop",
                data_dir=self.data_dir,
            )
            rows = [json.loads(l) for l in ui_file.read_text().splitlines()]
            self.assertTrue(any(r.get("message", {}).get("content") == "after giant" for r in rows))
            self.assertTrue(any(r.get("message", {}).get("stopReason") == "stop" for r in rows))
        finally:
            ui_transcript_sync.MAX_SYNC_BYTES = orig_bytes

    def test_numeric_tool_call_id_and_pending_calls_cap(self) -> None:
        state: dict = {}
        ui_transcript_sync.translate_entry(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"id": i, "name": f"tool-{i}"} for i in range(300)],
            },
            state,
        )
        self.assertEqual(len(state["pendingCalls"]), ui_transcript_sync.MAX_PENDING_CALLS)
        rows = ui_transcript_sync.translate_entry(
            {"step_index": 2, "type": "TOOL_RESULT", "source": "MODEL", "tool_call_id": 299, "content": "ok"},
            state,
        )
        self.assertEqual(rows[0]["message"]["toolCallId"], "299")
        self.assertEqual(rows[0]["message"]["toolName"], "tool-299")


class TestStopHookTodos(unittest.TestCase):
    def test_reads_canonical_workspace_steak_pi_todo_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            todo_dir = Path(td) / ".steak-pi" / "todo" / "abcd1234"
            todo_dir.mkdir(parents=True)
            (todo_dir / "todo.json").write_text(
                json.dumps(
                    {
                        "ownerSessionId": "conv-xyz",
                        "items": [
                            {"title": "Done item", "status": "done"},
                            {"title": "Open item", "status": "in_progress"},
                        ],
                    }
                )
            )
            self.assertEqual(stop_hook.count_open_todos("conv-xyz", td), ["Open item"])
            self.assertEqual(stop_hook.count_open_todos("other-conv", td), [])
            # Empty conversation_id must never trust unowned workspace todos
            self.assertEqual(stop_hook.count_open_todos("", td), [])
            # Symlinked namespace directory must be ignored
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "todo.json").write_text(
                json.dumps({"ownerSessionId": "conv-link", "items": [{"title": "Evil", "status": "open"}]})
            )
            (Path(td) / ".steak-pi" / "todo" / "symns").symlink_to(outside)
            self.assertEqual(stop_hook.count_open_todos("conv-link", td), [])
            # Symlinked todo.json inside a real namespace dir must also be ignored
            real_ns = Path(td) / ".steak-pi" / "todo" / "realns"
            real_ns.mkdir()
            (real_ns / "todo.json").symlink_to(outside / "todo.json")
            self.assertEqual(stop_hook.count_open_todos("conv-link", td), [])


class TestUsapMcpWorkspace(unittest.TestCase):
    def test_resolve_workspace_cwd_fails_closed_from_plugin_root(self) -> None:
        import usap_mcp

        orig_cwd = os.getcwd()
        orig_env = dict(os.environ)
        try:
            for k in ("STEAK_ANTIGRAVITY_WORKSPACE_CWD", "ULTRATERM_WORKSPACE_CWD", "TMUX_PANE", "ULTRATERM_TMUX_SESSION", "ULTRATERM_TMUX_SOCKET"):
                os.environ.pop(k, None)
            os.chdir(ROOT)
            self.assertIsNone(usap_mcp.resolve_workspace_cwd())
            with tempfile.TemporaryDirectory() as td:
                os.environ["STEAK_ANTIGRAVITY_WORKSPACE_CWD"] = td
                self.assertEqual(usap_mcp.resolve_workspace_cwd(), os.path.realpath(td))
        finally:
            os.chdir(orig_cwd)
            os.environ.clear()
            os.environ.update(orig_env)


class TestOwnedPathsGuard(unittest.TestCase):
    def test_allows_in_scope_and_denies_out_of_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "STEAK_ANTIGRAVITY_OWNED_PATHS": f"{td}/src"}
            allow_payload = json.dumps({"cwd": td, "toolCall": {"name": "write_file", "args": {"Path": f"{td}/src/a.ts"}}})
            res1 = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/owned_paths_guard.py")],
                input=allow_payload, text=True, capture_output=True, env=env, check=True,
            )
            self.assertEqual(json.loads(res1.stdout)["decision"], "allow")

            deny_payload = json.dumps({"cwd": td, "toolCall": {"name": "write_file", "args": {"Path": f"{td}/other/b.ts"}}})
            res2 = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/owned_paths_guard.py")],
                input=deny_payload, text=True, capture_output=True, env=env, check=True,
            )
            self.assertEqual(json.loads(res2.stdout)["decision"], "deny")

            # Missing target argument must fail CLOSED when STEAK_ANTIGRAVITY_OWNED_PATHS is set.
            missing_payload = json.dumps({"cwd": td, "toolCall": {"name": "edit_file", "args": {}}})
            res3 = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/owned_paths_guard.py")],
                input=missing_payload, text=True, capture_output=True, env=env, check=True,
            )
            self.assertEqual(json.loads(res3.stdout)["decision"], "deny")

            # TargetFile / AbsolutePath keys are also enforced.
            target_file_payload = json.dumps({"cwd": td, "toolCall": {"name": "replace_file_content", "args": {"TargetFile": f"{td}/outside.ts"}}})
            res4 = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/owned_paths_guard.py")],
                input=target_file_payload, text=True, capture_output=True, env=env, check=True,
            )
            self.assertEqual(json.loads(res4.stdout)["decision"], "deny")

    def test_hooks_resolve_via_plugin_root_and_fail_closed_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as hostile:
            (Path(hostile) / "scripts").mkdir()
            stub_marker = Path(hostile) / "pwned.txt"
            (Path(hostile) / "scripts" / "owned_paths_guard.py").write_text(
                f"from pathlib import Path; Path({str(stub_marker)!r}).write_text('pwned'); raise SystemExit(99)\n"
            )
            hooks = json.loads((ROOT / "hooks.json").read_text())
            cmd = hooks["steak-antigravity-guards"]["PreToolUse"][0]["hooks"][0]["command"]
            env = {**os.environ, "PLUGIN_ROOT": str(ROOT), "STEAK_ANTIGRAVITY_OWNED_PATHS": f"{hostile}/src"}
            res = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=hostile,
                input=json.dumps({"cwd": hostile, "toolCall": {"name": "write_file", "args": {"Path": f"{hostile}/evil.py"}}}),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            self.assertEqual(json.loads(res.stdout)["decision"], "deny")

            # With PLUGIN_ROOT unset in a hostile cwd, hook MUST fail closed and NEVER execute the repo stub.
            env_unset = {k: v for k, v in env.items() if k != "PLUGIN_ROOT"}
            res_fail = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=hostile,
                input="{}",
                text=True,
                capture_output=True,
                env=env_unset,
                check=False,
            )
            self.assertNotEqual(res_fail.returncode, 0)
            self.assertFalse(stub_marker.exists())


class TestVerifyTrustGate(unittest.TestCase):
    def test_verify_skips_untrusted_repo_and_runs_when_explicitly_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "ran.txt"
            cfg_dir = Path(td) / ".steak-pi"
            cfg_dir.mkdir()
            (cfg_dir / "config.json").write_text(json.dumps({"verify": {"command": f"echo ok > {marker}", "timeoutMs": 999999}}))
            payload = json.dumps({"cwd": td, "conversationId": "c1", "toolCall": {"name": "write_file", "args": {"Path": f"{td}/a.ts"}}})

            # Untrusted (with isolated HOME so real user allowlist is never read): must NOT execute repo command.
            env_untrusted = {k: v for k, v in os.environ.items() if k != "STEAK_ANTIGRAVITY_ALLOW_VERIFY"}
            env_untrusted["HOME"] = str(Path(td) / "home")
            env_untrusted["PLUGIN_DATA"] = str(Path(td) / "pdata")
            res1 = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/verify_after_edit.py")],
                input=payload, text=True, capture_output=True, env=env_untrusted, check=True,
            )
            self.assertFalse(marker.exists())
            self.assertIn("untrusted repo", res1.stderr)

            # Explicitly trusted: runs command.
            env_trusted = {**env_untrusted, "STEAK_ANTIGRAVITY_ALLOW_VERIFY": "1"}
            subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/verify_after_edit.py")],
                input=payload, text=True, capture_output=True, env=env_trusted, check=True,
            )
            self.assertTrue(marker.exists())

    def test_usap_mcp_drops_tmux_env_and_prefers_ultraterm_bin(self) -> None:
        source = (ROOT / "scripts/usap_mcp.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.pop("TMUX", None)', source)
        self.assertIn('os.environ.pop("TMUX_PANE", None)', source)
        self.assertIn('".ultraterm" / "bin"', source)


class TestAntigravityStatuslineSnapshot(unittest.TestCase):
    def test_writes_non_pii_snapshot_and_prints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "antigravity" / "usage.json"
            payload = {
                "email": "private-user@example.com",
                "cwd": "/private/project",
                "agent_state": "idle",
                "quota": {
                    "gemini-5h": {
                        "remaining_fraction": 0.85,
                        "reset_time": "2026-11-25T14:07:38Z",
                    },
                    "gemini-weekly": {
                        "remaining_fraction": 0.625,
                        "reset_time": "2026-12-02T02:38:11Z",
                    },
                    "3p-5h": {
                        "remaining_fraction": 1,
                        "reset_time": "1970-01-01T00:00:00Z",
                    },
                },
            }
            res = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/antigravity_statusline.py"), "--output", str(out)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(res.stdout, "")
            self.assertTrue(out.is_file())
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(out.parent).st_mode & 0o777, 0o700)
            raw = out.read_text(encoding="utf-8")
            self.assertNotIn("private-user@example.com", raw)
            self.assertNotIn("/private/project", raw)
            snap = json.loads(raw)
            self.assertEqual(snap["schemaVersion"], 1)
            self.assertEqual(snap["source"], "antigravity-statusline")
            self.assertTrue(snap["available"])
            self.assertAlmostEqual(snap["fiveHour"]["usedPercent"], 15.0, places=3)
            self.assertGreater(snap["fiveHour"]["resetsAt"], 1_700_000_000)
            self.assertAlmostEqual(snap["sevenDay"]["usedPercent"], 37.5, places=3)
            self.assertGreater(snap["sevenDay"]["resetsAt"], snap["fiveHour"]["resetsAt"])

            # Authenticating payload without quota must preserve the existing snapshot.
            auth_payload = {"agent_state": "authenticating", "model": None}
            subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/antigravity_statusline.py"), "--output", str(out)],
                input=json.dumps(auth_payload),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), snap)

    def test_untouched_bucket_sets_resets_at_null_and_symlink_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "antigravity" / "usage.json"
            payload = {
                "quota": {
                    "gemini-5h": {"remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                    "gemini-weekly": {"remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                }
            }
            subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/antigravity_statusline.py"), "--output", str(out)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )
            snap = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(snap["fiveHour"], {"usedPercent": 0.0, "resetsAt": None})
            self.assertEqual(snap["sevenDay"], {"usedPercent": 0.0, "resetsAt": None})

            # Symlink target must be refused without modifying the link target.
            victim = Path(td) / "victim.json"
            victim.write_text("KEEP")
            link = Path(td) / "antigravity" / "link.json"
            link.symlink_to(victim)
            subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "scripts/antigravity_statusline.py"), "--output", str(link)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(victim.read_text(), "KEEP")

    def test_configure_and_remove_statusline_preserve_third_party_commands(self) -> None:
        import importlib.machinery
        import importlib.util

        loader = importlib.machinery.SourceFileLoader("steak_agy_cli", str(ROOT / "bin/steak-antigravity"))
        spec = importlib.util.spec_from_loader("steak_agy_cli", loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)

        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            settings.write_text(json.dumps({"permissions": {"allow": ["read"]}}))
            self.assertTrue(cli.configure_statusline(settings))
            saved = json.loads(settings.read_text())
            self.assertEqual(saved["permissions"], {"allow": ["read"]})
            self.assertTrue(saved["statusLine"]["stack_with_default"])
            self.assertIn("antigravity_statusline.py", saved["statusLine"]["command"])
            self.assertTrue(cli.remove_statusline(settings))
            self.assertNotIn("statusLine", json.loads(settings.read_text()))

            # Third-party statusLine command must never be overwritten or removed.
            settings.write_text(json.dumps({"statusLine": {"command": "echo custom"}}))
            self.assertFalse(cli.configure_statusline(settings))
            self.assertFalse(cli.remove_statusline(settings))
            self.assertEqual(json.loads(settings.read_text())["statusLine"]["command"], "echo custom")


if __name__ == "__main__":
    unittest.main()
