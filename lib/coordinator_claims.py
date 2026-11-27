"""Read-only inspection of active UltraTerm ut-coordinator claims for SessionStart visibility.

Interactive Antigravity sessions previously had zero visibility into running
ut-coordinator jobs unless the operator explicitly ran `ut-coordinator job-list`,
allowing silent cross-repo collisions (e.g., an interactive session editing a repo
where a background coordinator job holds an undeclared linked worktree).

This helper reads the coordinator SQLite database strictly read-only (`mode=ro`,
short timeout, fail-open) and scans each active job's cache directory (1 level
deep, file-only `.git` worktree pointer check, zero subprocesses) so SessionStart
can surface both declared paths and underlying/undeclared git repositories.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = ("starting", "running", "blocked")
_JOB_ID_RE = re.compile(r"^J-\d+$")
_MAX_CACHE_ENTRIES = 32
_MAX_ANCESTOR_HOPS = 8


def default_coordinator_db() -> Path:
    home = Path(os.environ.get("HOME") or str(Path.home()))
    return home / ".ultraterm" / "overseer" / "overseer.sqlite3"


def default_cache_root() -> Path:
    home = Path(os.environ.get("HOME") or str(Path.home()))
    return home / ".cache" / "ut-coordinator"


def worktree_checkout(path: str | Path) -> str | None:
    """Return canonical repo root if `path` is a linked git worktree, else None."""
    try:
        marker = Path(path) / ".git"
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = Path(path) / gitdir
        common = gitdir / "commondir"
        if not common.is_file():
            return None
        base = Path(os.path.normpath(gitdir / common.read_text(encoding="utf-8", errors="replace").strip()))
        return os.path.realpath(str(base.parent)) if base.name == ".git" else None
    except OSError:
        return None


def resolve_repo_root(path: str | Path) -> str | None:
    """Resolve `path` (or an ancestor up to 8 levels) to its canonical git repo root."""
    try:
        cur = Path(os.path.realpath(os.path.expanduser(str(path))))
    except (OSError, ValueError):
        return None
    for _ in range(_MAX_ANCESTOR_HOPS):
        wt_repo = worktree_checkout(cur)
        if wt_repo:
            return wt_repo
        try:
            if (cur / ".git").is_dir():
                return os.path.realpath(str(cur))
        except OSError:
            return None
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def scan_job_cache_worktrees(job_id: str, cache_root: Path) -> list[dict[str, str]]:
    """Inspect direct subdirectories of `<cache_root>/<job_id>` for linked git worktrees."""
    if not _JOB_ID_RE.match(job_id):
        return []
    job_dir = cache_root / job_id
    try:
        if not job_dir.is_dir() or job_dir.is_symlink():
            return []
        entries = sorted(job_dir.iterdir(), key=lambda p: p.name)[:_MAX_CACHE_ENTRIES]
    except OSError:
        return []

    found: list[dict[str, str]] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        wt_str = os.path.normpath(str(entry))
        repo = worktree_checkout(wt_str)
        if repo and repo != wt_str:
            found.append({"worktree": wt_str, "repo": repo})
    return found


def _paths_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def read_active_coordinator_claims(
    cwd: str | Path | None = None,
    *,
    db_path: Path | None = None,
    cache_root: Path | None = None,
    max_jobs: int = 12,
) -> list[dict[str, Any]]:
    """Return active coordinator jobs with declared paths, canonical repos, and undeclared worktrees."""
    target_db = db_path if db_path is not None else default_coordinator_db()
    target_cache = cache_root if cache_root is not None else default_cache_root()
    if not target_db.is_file():
        return []

    cwd_repo = resolve_repo_root(cwd) if cwd else None
    cwd_norm = os.path.normpath(os.path.expanduser(str(cwd))) if cwd else None

    rows: list[tuple[Any, ...]] = []
    conn: sqlite3.Connection | None = None
    try:
        uri = f"file:{target_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        rows = conn.execute(
            f"SELECT id, status, title, cwd, meta FROM jobs WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*ACTIVE_STATUSES, max_jobs),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    claims: list[dict[str, Any]] = []
    for job_id, status, title, job_cwd, raw_meta in rows:
        if not isinstance(job_id, str):
            continue
        meta: dict[str, Any] = {}
        if isinstance(raw_meta, str) and raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    meta = parsed
            except json.JSONDecodeError:
                pass
        raw_paths = meta.get("paths")
        declared_paths = [
            os.path.normpath(os.path.expanduser(p))
            for p in (raw_paths if isinstance(raw_paths, list) else [])
            if isinstance(p, str) and p.strip()
        ]
        declared_repos: set[str] = set()
        for p in declared_paths:
            repo = resolve_repo_root(p)
            if repo:
                declared_repos.add(repo)
        if isinstance(job_cwd, str) and job_cwd.strip():
            cwd_r = resolve_repo_root(job_cwd)
            if cwd_r:
                declared_repos.add(cwd_r)

        cache_wts = scan_job_cache_worktrees(job_id, target_cache)
        undeclared_repos: list[str] = []
        all_repos = set(declared_repos)
        for item in cache_wts:
            repo = item["repo"]
            wt = item["worktree"]
            all_repos.add(repo)
            covered = repo in declared_repos or any(_paths_overlap(wt, p) for p in declared_paths)
            if not covered and repo not in undeclared_repos:
                undeclared_repos.append(repo)

        overlaps_cwd = False
        if cwd_repo and cwd_repo in all_repos:
            overlaps_cwd = True
        elif cwd_norm and any(_paths_overlap(cwd_norm, p) for p in declared_paths):
            overlaps_cwd = True

        claims.append(
            {
                "id": job_id,
                "status": str(status or ""),
                "title": str(title or ""),
                "paths": declared_paths,
                "repos": sorted(all_repos),
                "undeclaredRepos": sorted(undeclared_repos),
                "overlapsCwd": overlaps_cwd,
            }
        )

    # Sort cwd-overlapping jobs first so they are never truncated out of the banner.
    claims.sort(key=lambda item: (not item["overlapsCwd"], item["id"]))
    return claims


def format_claims_banner(claims: list[dict[str, Any]], *, max_chars: int = 1200) -> str | None:
    """Format active coordinator claims into a concise SessionStart warning message."""
    if not claims:
        return None
    header = (
        "[steak-antigravity] Active ut-coordinator jobs & repo claims "
        "(check before editing/pushing overlapping repos; use isolated worktrees):"
    )
    lines = [header]
    for item in claims:
        repo_labels = []
        undeclared = set(item.get("undeclaredRepos") or [])
        for repo in item.get("repos") or []:
            name = Path(repo).name or repo
            if repo in undeclared:
                repo_labels.append(f"{name} [undeclared worktree]")
            else:
                repo_labels.append(name)
        target_str = (
            "repos=[" + ", ".join(repo_labels) + "]"
            if repo_labels
            else ("paths=[" + ", ".join(item.get("paths") or []) + "]" if item.get("paths") else "paths=[]")
        )
        prefix = "!! OVERLAPS CWD " if item.get("overlapsCwd") else ""
        title = (item.get("title") or "").strip().replace("\n", " ")[:90]
        line = f"- {prefix}{item['id']} [{item['status']}] {target_str} | {title}"
        if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            lines.append(f"- ... ({len(claims) - (len(lines) - 1)} more active job(s); run `ut-coordinator job-list --status running`)")
            break
        lines.append(line)
    return "\n".join(lines)
