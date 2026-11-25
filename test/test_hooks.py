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
import ui_transcript_sync  # noqa: E402


class TestUiTranscriptSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="steak-agy-ui-"))
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


class TestOwnedPathsGuard(unittest.TestCase):
    def test_allows_in_scope_and_denies_out_of_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "STEAK_AGY_OWNED_PATHS": f"{td}/src"}
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


if __name__ == "__main__":
    unittest.main()
