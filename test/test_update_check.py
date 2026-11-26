"""Update contract v1 tests. No network: the release API and assets are local file:// fixtures."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import update_check as uc  # noqa: E402

NOW = 1_800_000_000
QUIET_KEYS = ("CI", "CLAUDECODE", "ULTRATERM_JOB_ID", "UT_COORDINATOR_JOB_ID",
              "STEAK_ANTIGRAVITY_NO_UPDATE_CHECK", "STEAK_ANTIGRAVITY_BASE_URL", "NO_COLOR")
STUB_INSTALLER = (
    "#!/bin/sh\nset -eu\n"
    'while [ $# -gt 0 ]; do case "$1" in --dir) d="$2"; shift 2;; --version) v="$2"; shift 2;; *) shift;; esac; done\n'
    'printf \'{"name": "steak-antigravity", "version": "%s"}\\n\' "$v" > "$d/package.json"\n'
)


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class UpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="steak-antigravity-update-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.release_file = self.tmp / "latest.json"
        self.assets = self.tmp / "assets"
        self.assets.mkdir()
        env = {key: value for key, value in os.environ.items() if key not in QUIET_KEYS}
        env.update(HOME=str(self.home), XDG_CACHE_HOME=str(self.tmp / "cache"), TMPDIR=str(self.tmp / "t"),
                   STEAK_ANTIGRAVITY_UPDATE_API=self.release_file.as_uri())
        (self.tmp / "t").mkdir()
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tempfile.tempdir = None  # pick up TMPDIR
        self.addCleanup(setattr, tempfile, "tempdir", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A managed install of 0.5.0 next to its kept rollback copy (0.4.1).
        self.root = self.make_install(self.tmp / "share" / "steak-antigravity", "0.5.0")
        self.publish("0.6.0")

    def make_install(self, path: Path, version: str) -> Path:
        path.mkdir(parents=True)
        (path / "package.json").write_text(json.dumps({
            "name": "steak-antigravity", "version": version,
            "repository": {"url": "git+https://github.com/example/steak-antigravity.git"}}))
        (path / uc.MANAGED_MARK).write_text("managed-by=steak-antigravity-installer\n")
        return path

    def publish(self, version: str, **fields: object) -> None:
        record = {"tag_name": f"v{version}", "draft": False, "prerelease": False,
                  "body": "Faster checks\n\nSecond line\n"}
        record.update(fields)
        self.release_file.write_text(json.dumps(record))

    def publish_assets(self, installer: str = STUB_INSTALLER, digest: str | None = None) -> None:
        (self.assets / "install.sh").write_text(installer)
        digest = digest or hashlib.sha256(installer.encode()).hexdigest()
        (self.assets / "SHA256SUMS").write_text(f"{'0' * 64}  steak-antigravity.tar.gz\n{digest}  install.sh\n")
        os.environ["STEAK_ANTIGRAVITY_BASE_URL"] = self.assets.as_uri()

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = uc.command(list(args), self.root)
        return code, out.getvalue(), err.getvalue()

    def footer(self, now: int = NOW) -> str:
        with mock.patch.object(uc.subprocess, "Popen"):  # never start a real background check
            return uc.footer_line(self.root, now)

    def background(self) -> None:
        with mock.patch("os.nice"):
            uc.run_background(self.root)

    def leftovers(self) -> list[str]:
        return sorted(os.listdir(self.tmp / "t"))

    # -- versions -----------------------------------------------------------------------
    def test_version_comparison(self) -> None:
        self.assertTrue(uc.is_newer("v0.6.0", "0.5.0"))
        self.assertTrue(uc.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(uc.is_newer("0.5.0", "0.5.0"))
        self.assertFalse(uc.is_newer("0.4.9", "0.5.0"))
        for bad in ("garbage", "0.6", "0.6.0.1", "v0.6.0-rc.1", "0.6.0+build", "", None, 6):
            self.assertFalse(uc.is_newer(bad, "0.5.0"), bad)
        self.assertIsNone(uc.stable_release({"tag_name": "v0.6.0", "prerelease": True}))
        self.assertIsNone(uc.stable_release({"tag_name": "v0.6.0", "draft": True}))
        self.assertEqual(uc.stable_release({"tag_name": "v0.6.0"}), "0.6.0")

    def test_repo_comes_from_package_metadata(self) -> None:
        self.assertEqual(uc.repo_slug(self.root), "example/steak-antigravity")
        del os.environ["STEAK_ANTIGRAVITY_UPDATE_API"]
        self.assertTrue(uc.api_url().startswith("https://api.github.com/repos/"))
        self.assertTrue(uc.api_url().endswith("/steak-antigravity/releases/latest"))
        os.environ["STEAK_ANTIGRAVITY_UPDATE_API"] = "http://example.com/latest"
        with self.assertRaises(ValueError):
            uc.api_url()

    # -- check, cache, failure ------------------------------------------------------------
    def test_one_check_per_day_and_cache_is_the_only_file(self) -> None:
        with mock.patch.object(uc.subprocess, "Popen") as popen:
            self.assertTrue(uc.start_background_check(uc.read_state(), NOW, self.root))
            self.assertFalse(uc.start_background_check(uc.read_state(), NOW + 3600, self.root))
            self.assertTrue(uc.start_background_check(uc.read_state(), NOW + uc.DAY, self.root))
        self.assertEqual(popen.call_count, 2)
        argv, kwargs = popen.call_args
        self.assertEqual(argv[0][-2:], [str(self.root / "lib" / "update_check.py"), "--background"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        state = uc.check_now(NOW)
        self.assertEqual(state["latest"], "0.6.0")
        self.assertEqual(os.listdir(uc.cache_dir()), ["update-check.json"])
        self.assertEqual(uc.state_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(uc.read_state()), {"checkedAt", "latest", "error"})

    def test_failure_is_recorded_with_cause_and_not_retried(self) -> None:
        self.release_file.unlink()
        state = uc.check_now(NOW)
        self.assertIn("latest.json", state["error"])
        code, _, err = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("update check failed:", err)
        with mock.patch.object(uc.subprocess, "Popen") as popen:
            self.assertFalse(uc.start_background_check(uc.read_state(), int(time.time()) + 60, self.root))
        popen.assert_not_called()
        self.publish("0.6.0", prerelease=True)
        self.assertIn("not a stable", uc.check_now(NOW)["error"])

    def test_check_exit_codes(self) -> None:
        code, out, _ = self.cli("--check")
        self.assertEqual((code, out.strip()),
                         (10, "steak-antigravity 0.6.0 available (you have 0.5.0) · run: steak-antigravity update"))
        self.publish("0.5.0")
        code, out, _ = self.cli("--check")
        self.assertEqual((code, out.strip()), (0, "steak-antigravity 0.5.0 is up to date."))
        self.publish("0.4.0")
        self.assertEqual(self.cli("--check")[0], 0)
        self.assertEqual(self.cli("--bogus")[0], 2)

    # -- notices ----------------------------------------------------------------------------
    def test_footer_notice_text_window_and_frequency(self) -> None:
        uc.check_now(NOW)
        line = "steak-antigravity 0.6.0 available (you have 0.5.0) · run: steak-antigravity update"
        self.assertEqual(self.footer(NOW), line)
        self.assertEqual(self.footer(NOW + 600), line)  # same notice, kept for one hour
        self.assertEqual(self.footer(NOW + uc.FOOTER_SECONDS + 1), "")
        self.assertEqual(self.footer(NOW + uc.DAY - 1), "")
        self.assertEqual(self.footer(NOW + uc.DAY), line)  # next day: once more

    def test_cli_notice_on_tty_only_once_a_day(self) -> None:
        uc.check_now(NOW)
        with redirect_stderr(io.StringIO()) as plain:
            uc.after_command(self.root, NOW)
        self.assertEqual(plain.getvalue(), "")  # not a TTY: silent, nothing recorded
        tty = TTY()
        with redirect_stderr(tty), mock.patch.object(uc.subprocess, "Popen"):
            uc.after_command(self.root, NOW)
            uc.after_command(self.root, NOW + 60)
        self.assertEqual(tty.getvalue(), (
            "steak-antigravity 0.6.0 available (you have 0.5.0) · run: steak-antigravity update\n"
            "\033[2mturn on automatic updates: steak-antigravity update --auto on"
            " · skip: steak-antigravity update --skip\033[0m\n"))

    def test_silent_in_ci_agents_and_opt_outs(self) -> None:
        uc.check_now(NOW)
        for key, value in (("CI", "true"), ("CLAUDECODE", "1"), ("ULTRATERM_JOB_ID", "job"),
                           ("STEAK_ANTIGRAVITY_NO_UPDATE_CHECK", "1")):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}), \
                    mock.patch.object(uc.subprocess, "Popen") as popen:
                self.assertTrue(uc.disabled_reason())
                self.assertEqual(uc.footer_line(self.root, NOW + 2 * uc.DAY), "")
                tty = TTY()
                with redirect_stderr(tty):
                    uc.after_command(self.root, NOW + 2 * uc.DAY)
                self.assertEqual(tty.getvalue(), "")
                popen.assert_not_called()
        uc.save_settings(checkAutomatically=False)
        self.assertIn("config.json", uc.disabled_reason())
        self.assertEqual(uc.footer_line(self.root, NOW + 2 * uc.DAY), "")

    def test_skip_hides_this_version_until_a_newer_one(self) -> None:
        code, out, _ = self.cli("--skip")
        self.assertEqual(code, 0)
        self.assertIn("Skipped steak-antigravity 0.6.0", out)
        self.assertEqual(self.footer(), "")
        self.assertIn("(skipped)", self.cli("--check")[1])
        self.publish("0.6.1")
        uc.check_now(NOW)
        self.assertIn("0.6.1 available", self.footer())
        self.publish("0.5.0")
        uc.check_now(NOW)
        self.assertIn("Nothing to skip", self.cli("--skip")[1])

    def test_statusline_prints_one_line_and_session_start_stays_untouched(self) -> None:
        self.publish("99.0.0")
        uc.check_now(int(time.time()))  # fresh cache: the statusline starts no check
        env = dict(os.environ)
        for key in ("TMUX", "TMUX_PANE", "ULTRATERM_TMUX_SESSION", "ULTRATERM_TMUX_SOCKET"):
            env.pop(key, None)
        statusline = ROOT / "scripts/antigravity_statusline.py"
        res = subprocess.run([sys.executable, "-I", "-B", str(statusline)], input='{"agent_state": "idle"}',
                             text=True, capture_output=True, env=env, timeout=30, check=True)
        self.assertEqual(res.stdout.splitlines(), [
            f"steak-antigravity 99.0.0 available (you have {uc.current_version()}) · run: steak-antigravity update"])
        quiet = subprocess.run([sys.executable, "-I", "-B", str(statusline)], input="{}", text=True,
                               capture_output=True, env={**env, "CI": "1"}, timeout=30, check=True)
        self.assertEqual(quiet.stdout, "")
        before = uc.state_path().read_bytes()
        res = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/session_start.py")],
                             input='{"conversationId": "c1"}', text=True, capture_output=True,
                             env=env, timeout=30, check=True)
        self.assertNotIn("available", res.stdout)
        self.assertEqual(uc.state_path().read_bytes(), before)
        self.assertNotIn("update_check", (ROOT / "scripts/session_start.py").read_text())

    def test_doctor_json_has_no_notice(self) -> None:
        uc.check_now(NOW)
        res = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "bin/steak-antigravity"), "doctor"],
                             capture_output=True, text=True, env=dict(os.environ), timeout=30, check=False)
        self.assertIn("updates", json.loads(res.stdout))
        self.assertNotIn("available (you have", res.stdout + res.stderr)

    # -- auto-update -------------------------------------------------------------------------
    def test_auto_off_by_default_and_persisted(self) -> None:
        self.assertIn("Automatic updates: off", self.cli("--auto", "status")[1])
        self.assertEqual(self.cli("--auto", "on")[0], 0)
        self.assertTrue(json.loads(uc.config_path().read_text())["updates"]["autoInstall"])
        self.assertIn("Automatic updates: on", self.cli("--auto", "status")[1])
        self.assertEqual(uc.notice_lines("0.6.0", "0.5.0")[1], "skip: steak-antigravity update --skip")
        uc.config_path().write_text(json.dumps({"autoResume": {"enabled": False}, "updates": {"autoInstall": True}}))
        uc._log("auto-update test entry")
        self.assertEqual(self.cli("--auto", "off")[0], 0)
        saved = json.loads(uc.config_path().read_text())
        self.assertEqual(saved["autoResume"], {"enabled": False})
        self.assertFalse(saved["updates"]["autoInstall"])
        self.assertFalse(uc.log_path().exists())
        uc.config_path().write_text("{not json")
        self.assertEqual(self.cli("--auto", "on")[0], 1)  # never overwrite a config it cannot parse
        self.assertEqual(uc.config_path().read_text(), "{not json")

    def test_background_installs_only_when_auto_is_on_and_once_per_version(self) -> None:
        with mock.patch.object(uc, "install", return_value=(True, "ok")) as install:
            self.background()
            install.assert_not_called()
            uc.save_settings(autoInstall=True)
            self.background()
            self.background()
        install.assert_called_once_with("0.6.0", self.root, capture=True)
        self.publish("0.6.1")
        with mock.patch.object(uc, "install", return_value=(False, "disk full")):
            self.background()
        self.assertEqual(uc.read_state()["autoError"], "disk full")
        self.assertIn("0.6.1 failed: disk full", uc.log_path().read_text())
        self.assertIn("0.6.1 available", self.footer())  # falls back to the notice

    def test_update_asks_on_tty_then_offers_auto_once(self) -> None:
        self.publish_assets()
        with mock.patch.object(uc, "_interactive", return_value=True), \
                mock.patch("builtins.input", side_effect=["", "n"]) as answers:
            code, out, err = self.cli()
        self.assertEqual(code, 0, err)
        self.assertEqual([c.args[0] for c in answers.call_args_list],
                         ["Install 0.6.0 now? [Y/n] ", "Install future updates automatically? [y/N] "])
        self.assertIn("steak-antigravity 0.5.0 → 0.6.0\n  Faster checks\n  Second line\n", out)
        self.assertEqual(uc.current_version(self.root), "0.6.0")
        self.assertEqual(uc.settings(), {"autoInstall": False, "autoAsked": True})
        self.publish("0.6.1")
        with mock.patch.object(uc, "_interactive", return_value=True), \
                mock.patch("builtins.input", side_effect=["y"]) as answers:
            self.assertEqual(self.cli()[0], 0)
        self.assertEqual(answers.call_count, 1)  # never asked again after "n"
        self.assertEqual(self.leftovers(), [])

    # -- install safety ----------------------------------------------------------------------
    def test_checksum_mismatch_refuses_and_cleans_up(self) -> None:
        self.publish_assets(digest="f" * 64)
        with mock.patch.object(uc.subprocess, "run") as run:
            code, _, err = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("install.sh checksum mismatch", err)
        self.assertIn("0.5.0 is still installed", err)
        run.assert_not_called()
        self.assertEqual(uc.current_version(self.root), "0.5.0")
        self.assertEqual(self.leftovers(), [])

    def test_failed_installer_leaves_current_version_and_no_temp_dir(self) -> None:
        self.publish_assets("#!/bin/sh\necho broken >&2\nexit 3\n")
        ok, output = uc.install("0.6.0", self.root, capture=True)
        self.assertFalse(ok)
        self.assertIn("broken", output)
        self.assertEqual(uc.current_version(self.root), "0.5.0")
        self.assertEqual(self.leftovers(), [])

    def test_package_managers_and_git_checkouts_are_not_overwritten(self) -> None:
        npm = self.make_install(self.tmp / "lib" / "node_modules" / "steak-antigravity", "0.5.0")
        self.assertEqual(uc.install_kind(npm, "0.6.0"), ("npm", "npm install -g steak-antigravity@0.6.0"))
        brew = self.make_install(self.tmp / "Cellar" / "steak-antigravity" / "0.5.0", "0.5.0")
        self.assertEqual(uc.install_kind(brew)[1], "brew upgrade steak-antigravity")
        self.assertEqual(uc.install_kind(self.root)[0], "installer")
        (self.root / ".git").mkdir()
        with mock.patch.object(uc, "install") as install:
            code, out, _ = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("git checkout. Update it with: git -C", out)
        install.assert_not_called()

    # -- rollback and debris -----------------------------------------------------------------
    def test_rollback_restores_the_kept_copy(self) -> None:
        self.make_install(uc.previous_dir(self.root), "0.4.1")
        self.assertTrue(uc.status_summary(self.root)["rollbackAvailable"])
        code, out, err = self.cli("--rollback")
        self.assertEqual(code, 0, err)
        self.assertIn("Rolled back to steak-antigravity 0.4.1", out)
        self.assertEqual(uc.current_version(self.root), "0.4.1")
        self.assertEqual(sorted(os.listdir(self.root.parent)), ["steak-antigravity"])
        self.assertEqual(uc.settings()["skipVersion"], "0.5.0")
        code, _, err = self.cli("--rollback")
        self.assertEqual(code, 1)
        self.assertIn("no previous version is kept", err)

    def test_rollback_copy_is_removed_after_first_run_or_seven_days(self) -> None:
        prev = self.make_install(uc.previous_dir(self.root), "0.4.1")
        self.cli("--auto", "status")  # update commands keep a fresh copy for --rollback
        self.assertTrue(prev.exists())
        old = time.time() - uc.PREVIOUS_MAX_AGE - 60
        os.utime(prev, (old, old))
        self.cli("--auto", "status")
        self.assertFalse(prev.exists())
        self.make_install(prev, "0.4.1")
        self.footer()  # the new version ran in agy
        self.assertFalse(prev.exists())
        self.make_install(prev, "0.4.1")
        with redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {"CI": "1"}):
            uc.after_command(self.root)  # or any other CLI command ran, even non-interactive
        self.assertFalse(prev.exists())
        foreign = prev
        foreign.mkdir()
        (foreign / "keep.txt").write_text("not ours")
        self.footer()
        self.assertTrue((foreign / "keep.txt").exists())

    def test_log_stays_bounded(self) -> None:
        for _ in range(400):
            uc._log("x" * 2000)
        size = uc.log_path().stat().st_size
        self.assertLessEqual(size, uc.LOG_MAX_BYTES)
        self.assertGreater(size, uc.LOG_MAX_BYTES // 4)
        self.assertEqual(os.listdir(uc.cache_dir()), ["update.log"])
        self.assertEqual(uc.log_path().stat().st_mode & 0o777, 0o600)

    def test_uninstall_forgets_cache_log_and_settings(self) -> None:
        uc.check_now(NOW)
        uc._log("entry")
        uc.config_path().parent.mkdir(mode=0o700)
        uc.config_path().write_text(json.dumps({"autoResume": {"enabled": True}, "updates": {"skipVersion": "0.6.0"}}))
        uc.forget()
        self.assertFalse(uc.cache_dir().exists())
        self.assertEqual(json.loads(uc.config_path().read_text()), {"autoResume": {"enabled": True}})
        uc.config_path().write_text(json.dumps({"updates": {"autoInstall": True}}))
        uc.forget()
        self.assertFalse(uc.config_path().exists())


if __name__ == "__main__":
    unittest.main()
