import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import coordinator_claims  # noqa: E402


class TestCoordinatorClaims(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="steak-agy-claims-"))
        self.db_path = self.tmp / "overseer.sqlite3"
        self.cache_root = self.tmp / "ut-coordinator"
        self.cache_root.mkdir(mode=0o700)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, title TEXT, cwd TEXT, meta TEXT)"
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init_repo(self, name: str) -> Path:
        repo = self.tmp / "dev" / name
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=self.env, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
            check=True,
            env=self.env,
            capture_output=True,
        )
        return repo

    def _add_worktree(self, repo: Path, dest: Path, branch: str) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(dest)], check=True, env=self.env, capture_output=True)
        return dest

    def test_surfaces_declared_and_undeclared_cross_repo_worktrees_and_cwd_overlap(self) -> None:
        repo_kit = self._init_repo("shared-kit")
        repo_alpha = self._init_repo("site-alpha")
        repo_beta = self._init_repo("site-beta")
        self._add_worktree(repo_beta, self.cache_root / "J-4839" / "beta-wt", "release-fix")

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
            (
                "J-4839",
                "running",
                "Continuation: shared kit and site-alpha updates",
                None,
                json.dumps({"paths": [str(repo_kit), str(repo_alpha)]}),
            ),
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
            ("J-4800", "done", "Finished job", None, json.dumps({"paths": [str(repo_beta)]})),
        )
        conn.commit()
        conn.close()

        claims = coordinator_claims.read_active_coordinator_claims(
            cwd=repo_beta / "apps" / "web",
            db_path=self.db_path,
            cache_root=self.cache_root,
        )
        self.assertEqual(len(claims), 1)
        item = claims[0]
        self.assertEqual(item["id"], "J-4839")
        self.assertTrue(item["overlapsCwd"])
        self.assertEqual(
            item["repos"],
            sorted([os.path.realpath(str(repo_kit)), os.path.realpath(str(repo_alpha)), os.path.realpath(str(repo_beta))]),
        )
        self.assertEqual(item["undeclaredRepos"], [os.path.realpath(str(repo_beta))])

        banner = coordinator_claims.format_claims_banner(claims)
        self.assertIsNotNone(banner)
        assert banner is not None
        self.assertIn("!! OVERLAPS CWD J-4839 [running]", banner)
        self.assertIn("site-beta [undeclared worktree]", banner)
        self.assertIn("site-alpha", banner)
        self.assertNotIn("J-4800", banner)

    def test_fails_open_when_db_missing_or_corrupt(self) -> None:
        self.assertEqual(
            coordinator_claims.read_active_coordinator_claims(
                db_path=self.tmp / "missing.sqlite3",
                cache_root=self.cache_root,
            ),
            [],
        )
        bad_db = self.tmp / "corrupt.sqlite3"
        bad_db.write_text("not sqlite")
        self.assertEqual(
            coordinator_claims.read_active_coordinator_claims(
                db_path=bad_db,
                cache_root=self.cache_root,
            ),
            [],
        )
        self.assertIsNone(coordinator_claims.format_claims_banner([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
