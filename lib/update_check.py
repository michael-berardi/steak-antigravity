"""Update checks and verified updates for Steak Antigravity (update contract v1).

- One anonymous GET to this plugin's public GitHub "latest release" record, at most once
  per 24 h, from a detached low-priority process. Never on the SessionStart path.
- Notices: one line in the agy statusLine footer (the interactive TUI only) and two lines
  on stderr after a CLI command on a TTY. Off in CI, agent/worker runs, or when opted out.
- `update` downloads the release's installer, verifies it against SHA256SUMS and runs it.
  The installer verifies the archive, swaps atomically and keeps one `.previous` copy,
  which this module removes once the new version has run (or after 7 days).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOL = "steak-antigravity"
USER_AGENT = f"{TOOL}-update-check"
ROOT = Path(__file__).resolve().parent.parent
MANAGED_MARK = ".steak-antigravity-managed"
DAY = 24 * 60 * 60
FOOTER_SECONDS = 60 * 60  # the footer shows a notice for one hour, at most once a day per version
PREVIOUS_MAX_AGE = 7 * DAY
CHECK_TIMEOUT = 4
DOWNLOAD_TIMEOUT = 60
MAX_JSON_BYTES = 1 << 20
MAX_ASSET_BYTES = 8 << 20
LOG_MAX_BYTES = 256 * 1024
VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
QUIET_ENV = ("CI", "CLAUDECODE", "ULTRATERM_JOB_ID", "UT_COORDINATOR_JOB_ID")
USAGE = f"Usage: {TOOL} update [--check | --skip | --auto on|off|status | --rollback]"


# -- files ---------------------------------------------------------------------------

def cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg) / TOOL
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / TOOL
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / TOOL
    return Path.home() / ".cache" / TOOL


def state_path() -> Path:
    return cache_dir() / "update-check.json"


def log_path() -> Path:
    return cache_dir() / "update.log"


def config_path() -> Path:
    return Path.home() / ".steak-antigravity" / "config.json"


def _read_json(path: Path) -> dict | None:
    """Small JSON object: {} when missing, None when unreadable, a symlink, or not an object."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
        parsed = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_JSON_BYTES else None
    except (OSError, ValueError, UnicodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_private(path: Path, body: bytes) -> bool:
    """Atomically replace a 0600 file in a 0700 folder."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            return False
        tmp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return True
    except OSError:
        return False


def _write_json(path: Path, data: dict) -> bool:
    return _write_private(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_state() -> dict:
    """The one cache file: checkedAt, latest, error, noticeVersion/noticeAt, autoTried/autoError."""
    return _read_json(state_path()) or {}


def write_state(state: dict) -> bool:
    return _write_json(state_path(), state)


def settings() -> dict:
    """The `updates` section of ~/.steak-antigravity/config.json (survives updates)."""
    section = (_read_json(config_path()) or {}).get("updates")
    return section if isinstance(section, dict) else {}


def save_settings(**changes: object) -> bool:
    """Update the `updates` section; refuses to touch a config file it cannot parse."""
    raw = _read_json(config_path())
    if raw is None:
        return False
    section = raw.get("updates") if isinstance(raw.get("updates"), dict) else {}
    for key, value in changes.items():
        if value is None:
            section.pop(key, None)
        else:
            section[key] = value
    raw["updates"] = section
    return _write_json(config_path(), raw)


def _log(message: str) -> None:
    """Append to the one auto-update log, keeping it under LOG_MAX_BYTES."""
    path = log_path()
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message.strip()}\n".encode("utf-8", "replace")
    line = line[-(LOG_MAX_BYTES // 4):]
    try:
        old = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    except OSError:
        old = b""
    if len(old) + len(line) > LOG_MAX_BYTES:
        old = old[-(LOG_MAX_BYTES // 2):]
        old = old[old.find(b"\n") + 1:]
    _write_private(path, old + line)


def forget() -> None:
    """Remove the update cache, log and settings (called by `uninstall`)."""
    for path in (state_path(), log_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        cache_dir().rmdir()
    except OSError:
        pass
    raw = _read_json(config_path())
    if raw and "updates" in raw:
        raw.pop("updates")
        if raw:
            _write_json(config_path(), raw)
        else:
            config_path().unlink(missing_ok=True)


# -- versions and the release record -------------------------------------------------

def _package(root: Path = ROOT) -> dict:
    return _read_json(root / "package.json") or {}


def current_version(root: Path = ROOT) -> str:
    return str(_package(root).get("version") or "").strip()


def repo_slug(root: Path = ROOT) -> str:
    """owner/name from package.json `repository`, never hard-coded."""
    repo = _package(root).get("repository")
    url = str(repo.get("url", "") if isinstance(repo, dict) else repo or "")
    match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def parse_version(text: object) -> tuple[int, int, int] | None:
    """Only stable x.y.z (optional leading v); anything else is unparseable."""
    if not isinstance(text, str):
        return None
    match = VERSION_RE.fullmatch(text.strip().removeprefix("v"))
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def is_newer(candidate: object, current: object) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return bool(new and cur and new > cur)


def stable_release(record: object) -> str | None:
    if not isinstance(record, dict) or record.get("draft") or record.get("prerelease"):
        return None
    parsed = parse_version(record.get("tag_name"))
    return ".".join(map(str, parsed)) if parsed else None


def note_lines(body: object, limit: int = 6) -> list[str]:
    if not isinstance(body, str):
        return []
    lines = []
    for raw in body.splitlines():
        text = "".join(ch for ch in raw if ch == "\t" or ord(ch) >= 32).strip()
        if text:
            lines.append(text[:200])
        if len(lines) >= limit:
            break
    return lines


def api_url() -> str:
    override = os.environ.get("STEAK_ANTIGRAVITY_UPDATE_API", "").strip()
    if override:
        if not re.match(r"(https://|file://|http://(127\.0\.0\.1|localhost)[:/])", override):
            raise ValueError("STEAK_ANTIGRAVITY_UPDATE_API must be https://, file:// or loopback http://")
        return override
    slug = repo_slug()
    if not slug:
        raise ValueError("package.json has no GitHub repository")
    return f"https://api.github.com/repos/{slug}/releases/latest"


def download_base(version: str) -> str:
    override = os.environ.get("STEAK_ANTIGRAVITY_BASE_URL", "").strip().rstrip("/")
    if override:
        if not override.startswith(("https://", "file://")):
            raise ValueError("STEAK_ANTIGRAVITY_BASE_URL must be https:// or file://")
        return override
    return f"https://github.com/{repo_slug()}/releases/download/v{version}"


def _get(url: str, limit: int, timeout: float) -> bytes:
    import urllib.request  # imported lazily: keeps the statusLine and CLI start-up light

    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"{url} is larger than expected")
    return body


def _cause(exc: BaseException) -> str:
    text = " ".join(str(exc).split())[:300]
    return text or type(exc).__name__


def check_now(now: int | None = None) -> dict:
    """One anonymous GET. Records the result, or the failure cause, in the cache."""
    state = read_state()
    state["checkedAt"] = int(time.time() if now is None else now)
    notes: list[str] = []
    try:
        record = json.loads(_get(api_url(), MAX_JSON_BYTES, CHECK_TIMEOUT).decode("utf-8"))
        latest = stable_release(record)
        if latest:
            state.update(latest=latest, error=None)
            notes = note_lines(record.get("body"))
        else:
            state["error"] = "the latest release is not a stable x.y.z release"
    except (OSError, ValueError, UnicodeError) as exc:
        state["error"] = _cause(exc)
    write_state(state)
    return {**state, "notes": notes}


# -- when to check and notify --------------------------------------------------------

def disabled_reason() -> str:
    """Why automatic checks and notices are off, or '' when they are on."""
    if os.environ.get("STEAK_ANTIGRAVITY_NO_UPDATE_CHECK", "").strip() not in ("", "0"):
        return "STEAK_ANTIGRAVITY_NO_UPDATE_CHECK is set"
    for key in QUIET_ENV:
        if os.environ.get(key, "").strip().lower() not in ("", "0", "false"):
            return f"{key} is set"
    if settings().get("checkAutomatically") is False:
        return "turned off in ~/.steak-antigravity/config.json"
    return ""


def _due(state: dict, now: int) -> bool:
    checked = state.get("checkedAt")
    return not isinstance(checked, int) or not 0 <= now - checked < DAY


def start_background_check(state: dict, now: int, root: Path = ROOT) -> bool:
    """Claim today's check in the cache, then run it in a detached low-priority process."""
    if not _due(state, now):
        return False
    state.update(checkedAt=now, error="the check started but did not finish")
    if not write_state(state):
        return False
    if os.name == "nt":
        detach: dict = {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        detach = {"start_new_session": True}
    try:
        subprocess.Popen(
            [sys.executable, "-I", "-B", str(root / "lib" / "update_check.py"), "--background"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, **detach)
    except OSError as exc:
        state["error"] = _cause(exc)
        write_state(state)
        return False
    return True


def pending(state: dict, current: str) -> str | None:
    latest = state.get("latest")
    if is_newer(latest, current) and settings().get("skipVersion") != latest:
        return str(latest)
    return None


def _notice_due(state: dict, latest: str, now: int, keep: int) -> bool:
    """Open a notice at most once per 24 h per version; keep it visible for `keep` seconds."""
    shown = state.get("noticeAt") if state.get("noticeVersion") == latest else None
    if isinstance(shown, int) and 0 <= now - shown < DAY:
        return now - shown < keep
    state.update(noticeVersion=latest, noticeAt=now)
    write_state(state)
    return True


def notice_lines(latest: str, current: str) -> list[str]:
    second = f"skip: {TOOL} update --skip"
    if settings().get("autoInstall") is not True:
        second = f"turn on automatic updates: {TOOL} update --auto on · " + second
    return [f"{TOOL} {latest} available (you have {current}) · run: {TOOL} update", second]


def footer_line(root: Path = ROOT, now: int | None = None) -> str:
    """statusLine footer (interactive agy TUI only): start the daily check, return the notice."""
    prune_previous(root)  # this version has run successfully, so the rollback copy can go
    if disabled_reason():
        return ""
    stamp = int(time.time() if now is None else now)
    state = read_state()
    start_background_check(state, stamp, root)
    current = current_version(root)
    latest = pending(state, current)
    if latest and _notice_due(state, latest, stamp, FOOTER_SECONDS):
        return notice_lines(latest, current)[0]
    return ""


def after_command(root: Path = ROOT, now: int | None = None) -> None:
    """Two stderr lines after a CLI command, only on a TTY, at most once per 24 h per version."""
    prune_previous(root)  # this version has run successfully, so the rollback copy can go
    if not sys.stderr.isatty() or disabled_reason():
        return
    stamp = int(time.time() if now is None else now)
    state = read_state()
    start_background_check(state, stamp, root)
    current = current_version(root)
    latest = pending(state, current)
    if latest and _notice_due(state, latest, stamp, 0):
        first, second = notice_lines(latest, current)
        dim, reset = ("", "") if os.environ.get("NO_COLOR") else ("\033[2m", "\033[0m")
        print(f"{first}\n{dim}{second}{reset}", file=sys.stderr)


# -- install, rollback and package managers -------------------------------------------

def install_kind(root: Path = ROOT, version: str = "") -> tuple[str, str]:
    """('git' | 'npm' | 'brew' | 'installer', the command that updates this copy)."""
    if (root / ".git").exists():
        return "git", f"git -C {shlex.quote(str(root))} pull --ff-only"
    parts = root.resolve().parts
    if "node_modules" in parts:
        return "npm", f"npm install -g {TOOL}@{version or 'latest'}"
    if "Cellar" in parts:
        return "brew", f"brew upgrade {TOOL}"
    return "installer", ""


def expected_sha256(sums: str, name: str) -> str:
    found = []
    for line in sums.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            found.append(parts[0].lower())
    if len(found) != 1 or not SHA256_RE.fullmatch(found[0]):
        raise ValueError(f"SHA256SUMS has no single valid entry for {name}")
    return found[0]


def _run(cmd: list[str], capture: bool, env: dict | None = None) -> tuple[bool, str]:
    try:
        done = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=capture,
                              text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, _cause(exc)
    output = ((done.stdout or "") + (done.stderr or "")).strip()[-2000:] if capture else ""
    if done.returncode != 0 and not output:
        output = f"{Path(cmd[0]).name} exited with status {done.returncode}"
    return done.returncode == 0, output


def install(version: str, root: Path = ROOT, capture: bool = False) -> tuple[bool, str]:
    """Install `version` with the package manager, or the release installer verified first."""
    kind, command = install_kind(root, version)
    if kind == "git":
        return False, f"this copy is a git checkout; update it with: {command}"
    if kind != "installer":
        return _run(shlex.split(command), capture)
    name = "install.ps1" if os.name == "nt" else "install.sh"
    work = Path(tempfile.mkdtemp(prefix=f"{TOOL}-update-"))
    try:
        try:
            base = download_base(version)
            script = _get(f"{base}/{name}", MAX_ASSET_BYTES, DOWNLOAD_TIMEOUT)
            expected = expected_sha256(_get(f"{base}/SHA256SUMS", MAX_ASSET_BYTES, DOWNLOAD_TIMEOUT)
                                       .decode("utf-8", "replace"), name)
        except (OSError, ValueError) as exc:
            return False, f"download failed: {_cause(exc)}; nothing was installed"
        actual = hashlib.sha256(script).hexdigest()
        if actual != expected:
            return False, f"{name} checksum mismatch (expected {expected}, got {actual}); nothing was installed"
        path = work / name
        path.write_bytes(script)
        env = {**os.environ, "STEAK_ANTIGRAVITY_BASE_URL": base}
        if name == "install.ps1":
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path),
                   "-Version", version, "-Dir", str(root)]
        else:
            cmd = ["sh", str(path), "--version", version, "--dir", str(root)]
        return _run(cmd, capture, env)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def previous_dir(root: Path = ROOT) -> Path:
    return root.with_name(root.name + ".previous")


def _rollback_copy(root: Path) -> Path | None:
    prev = previous_dir(root)
    if prev.is_dir() and not prev.is_symlink() and (prev / MANAGED_MARK).is_file():
        return prev
    return None


def prune_previous(root: Path = ROOT, max_age: int = 0) -> bool:
    """Remove the one rollback copy (always, or only once it is older than `max_age`)."""
    prev = _rollback_copy(root)
    try:
        if prev is None or (max_age and time.time() - prev.stat().st_mtime < max_age):
            return False
    except OSError:
        return False
    shutil.rmtree(prev, ignore_errors=True)
    return not prev.exists()


def rollback(root: Path = ROOT) -> tuple[bool, str]:
    """Swap the kept `.previous` copy back in place of the current install."""
    if os.name == "nt":
        return False, "rollback is not available on Windows; reinstall with install.ps1 -Version <x.y.z>"
    prev = _rollback_copy(root)
    if prev is None:
        return False, "no previous version is kept (it is removed once the new version has run)"
    old = current_version(prev) or "the previous version"
    holder = Path(tempfile.mkdtemp(prefix=f".{TOOL}.rollback.", dir=root.parent))
    try:
        os.rename(root, holder / "current")
        try:
            os.rename(prev, root)
        except OSError:
            os.rename(holder / "current", root)
            raise
    except OSError as exc:
        if root.exists():
            shutil.rmtree(holder, ignore_errors=True)
        return False, f"rollback failed: {_cause(exc)}; nothing was changed"
    shutil.rmtree(holder, ignore_errors=True)
    return True, old


# -- background ----------------------------------------------------------------------

def run_background(root: Path = ROOT) -> int:
    """The detached daily check; installs in place only when automatic updates are on."""
    try:
        if hasattr(os, "nice"):
            os.nice(10)
        if disabled_reason():
            return 0
        state = check_now()
        latest, current, cfg = state.get("latest"), current_version(root), settings()
        if (cfg.get("autoInstall") is not True or not is_newer(latest, current)
                or cfg.get("skipVersion") == latest or state.get("autoTried") == latest):
            return 0
        state = read_state()
        state["autoTried"] = latest
        write_state(state)
        ok, output = install(str(latest), root, capture=True)
        _log(f"auto-update {current} -> {latest} {'installed' if ok else 'failed'}: {output}")
        state = read_state()
        state["autoError"] = None if ok else (output[-300:] or "install failed")
        write_state(state)
    except Exception as exc:  # a detached helper has nowhere else to report
        _log(f"update check failed: {_cause(exc)}")
    return 0


# -- the `update` command ------------------------------------------------------------

def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(question: str, default: bool) -> bool:
    try:
        answer = input(question).strip().lower()
    except (EOFError, OSError):
        return default
    return default if not answer else answer.startswith("y")


def command(args: list[str], root: Path = ROOT) -> int:
    prune_previous(root, max_age=PREVIOUS_MAX_AGE)
    current = current_version(root)
    if args == ["--check"]:
        return _check(current)
    if args == ["--skip"]:
        return _skip(current)
    if len(args) == 2 and args[0] == "--auto" and args[1] in ("on", "off", "status"):
        return _auto(args[1])
    if args == ["--rollback"]:
        ok, message = rollback(root)
        if not ok:
            print(f"{TOOL} update --rollback: {message}", file=sys.stderr)
            return 1
        if is_newer(current, message):
            save_settings(skipVersion=current)
        print(f"Rolled back to {TOOL} {message}. {current} is skipped until a newer release.")
        return 0
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    return _update(current, root)


def _check(current: str) -> int:
    state = check_now()
    if state.get("error"):
        print(f"{TOOL} update check failed: {state['error']}", file=sys.stderr)
        return 1
    latest = state.get("latest")
    if not is_newer(latest, current):
        print(f"{TOOL} {current} is up to date.")
        return 0
    skipped = " (skipped)" if settings().get("skipVersion") == latest else ""
    print(f"{TOOL} {latest} available (you have {current}){skipped} · run: {TOOL} update")
    return 10


def _skip(current: str) -> int:
    state = read_state()
    if not is_newer(state.get("latest"), current):
        state = check_now()
    latest = state.get("latest")
    if not is_newer(latest, current):
        print(f"Nothing to skip: {state.get('error') or f'{TOOL} {current} is up to date'}.")
        return 0
    if not save_settings(skipVersion=latest):
        print(f"{TOOL} update --skip: cannot write {config_path()}", file=sys.stderr)
        return 1
    print(f"Skipped {TOOL} {latest}. A newer release will notify again.")
    return 0


def _auto(mode: str) -> int:
    if mode == "status":
        reason = disabled_reason()
        print(f"Automatic updates: {'on' if settings().get('autoInstall') is True else 'off'}")
        print(f"Update checks: {'off (' + reason + ')' if reason else 'on'}")
        return 0
    if not save_settings(autoInstall=mode == "on", autoAsked=True):
        print(f"{TOOL} update --auto: cannot write {config_path()}", file=sys.stderr)
        return 1
    if mode == "off":  # drop the auto-update log and its bookkeeping
        log_path().unlink(missing_ok=True)
        state = read_state()
        if "autoTried" in state or "autoError" in state:
            state.pop("autoTried", None)
            state.pop("autoError", None)
            write_state(state)
    print(f"Automatic updates: {mode}")
    return 0


def _update(current: str, root: Path) -> int:
    state = check_now()
    if state.get("error"):
        print(f"{TOOL} update check failed: {state['error']}", file=sys.stderr)
        return 1
    latest = str(state.get("latest"))
    if not is_newer(latest, current):
        print(f"{TOOL} {current} is up to date.")
        return 0
    print(f"{TOOL} {current} → {latest}")
    for line in state["notes"]:
        print(f"  {line}")
    kind, cmd = install_kind(root, latest)
    if kind == "git":
        print(f"This copy is a git checkout. Update it with: {cmd}")
        return 1
    interactive = _interactive()
    if interactive and not _ask(f"Install {latest} now? [Y/n] ", True):
        print("Not installed.")
        return 0
    ok, output = install(latest, root)
    if not ok:
        print(f"{TOOL} update failed: {output}\n{current} is still installed.", file=sys.stderr)
        return 1
    print(f"Installed {TOOL} {latest}. New agy sessions use it.")
    cfg = settings()
    if interactive and cfg.get("autoInstall") is not True and cfg.get("autoAsked") is not True:
        save_settings(autoInstall=_ask("Install future updates automatically? [y/N] ", False), autoAsked=True)
    return 0


def status_summary(root: Path = ROOT) -> dict:
    state, cfg, reason = read_state(), settings(), disabled_reason()
    return {
        "checks": f"off ({reason})" if reason else "on",
        "autoInstall": cfg.get("autoInstall") is True,
        "skipVersion": cfg.get("skipVersion"),
        "latest": state.get("latest"),
        "lastCheckError": state.get("error"),
        "lastAutoInstallError": state.get("autoError"),
        "rollbackAvailable": _rollback_copy(root) is not None,
    }


if __name__ == "__main__":
    sys.exit(run_background() if sys.argv[1:] == ["--background"] else 2)
