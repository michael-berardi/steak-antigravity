"""ToS-safe quota auto-resume for Steak Antigravity.

Design invariants (strictly enforced):
- Never bypasses, accelerates, or evades any rate limit or quota; never rotates
  accounts, spoofs identity, or polls remote APIs.
- Strictly waits until AFTER the official server-reported reset time (from the
  Stop hook quota error payload or, as secondary fallback on confirmed quota
  errors, the official Antigravity statusLine snapshot) PLUS a mandatory safety
  buffer (>= 60s, default 65s) and small jitter (default 0..25s) before issuing
  one resume.
- Uses only documented, sanctioned interfaces: Antigravity lifecycle hooks,
  Antigravity's built-in `conversations` skill local API
  (`product.v1.ConversationService/GetConversation` over loopback with proxies
  and redirects disabled; in-memory env only, never persisted, optional via
  `STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS=0`), and identity-bound UltraTerm
  `utp send --id <uuid> --expected-id <uuid>`.
- Never installs LaunchAgents, cron jobs, or persistent background daemons:
  spawns at most one bounded, single-shot, cancellable waiter per quota event
  (checking local state/PID files every 30s; zero remote API polling).
- Cancels automatically on opt-out, operator HOLD (env `STEAK_ANTIGRAVITY_HOLD=1`
  or `ULTRATERM_HOLD=1`, `HOLD`/`STOP`/`PAUSE` user/transcript input, or
  `~/.cache/agent-hold/<conv>/hold-handoff.md` / `~/.cache/ut-coordinator/<job>/hold-handoff.md`),
  user interaction / manual resume, new session in the same pane, session exit,
  non-quota errors, USER_CANCELED, or retry cap exhaustion.
- 100% provider-neutral: no hardcoded model IDs, provider preferences, or
  operator/client names.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import ut_common

SCHEMA_VERSION = 1

DEFAULT_ENABLED = True
MIN_BUFFER_SECONDS = 60
DEFAULT_BUFFER_SECONDS = 65
MAX_BUFFER_SECONDS = 3600
DEFAULT_JITTER_MAX_SECONDS = 25
MAX_JITTER_SECONDS = 300
MIN_MAX_WAIT_SECONDS = 300
DEFAULT_MAX_WAIT_SECONDS = 6 * 3600  # 6 hours (covers 5-hour quota windows, rejects weekly lockouts)
MAX_MAX_WAIT_SECONDS = 12 * 3600
DEFAULT_MAX_RETRIES = 3
MAX_RETRIES_CAP = 10
MAX_PROMPT_CHARS = 500
DEFAULT_PROMPT = "[steak-antigravity auto-resume] Quota has reset. Please continue where you left off."

STATUSLINE_MAX_AGE_SECONDS = 600
DEDUP_WINDOW_SECONDS = 120
WAIT_CHECK_INTERVAL_SECONDS = 30.0
MAX_LATE_WAKE_SECONDS = 3600
STALE_STATE_MAX_AGE_SECONDS = 7 * 24 * 3600
MAX_CONFIG_BYTES = 64 * 1024
MAX_USAGE_BYTES = 64 * 1024
MAX_TRANSCRIPT_TAIL_BYTES = 64 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FRAC_RE = re.compile(r"\.(\d+)")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_FALSE_VALUES = frozenset({"0", "false", "off", "no", "disabled"})
_TRUE_VALUES = frozenset({"1", "true", "on", "yes", "enabled"})

# Non-quota errors that must NEVER trigger auto-resume, even if they mention "reset" or "limit".
_NON_QUOTA_EXCLUDE_RE = re.compile(
    r"(?:"
    r"connection\s+reset\s+by\s+peer|econnreset|etimedout|enotfound|"
    r"unauthenticated|authentication\s+(?:failed|error|required)|"
    r"not\s+signed\s+in|signed\s+out|log\s*in\s+required|invalid\s+api\s+key|"
    r"invalid\s+model|unknown\s+model|model\s+not\s+found|"
    r"permission\s+denied|access\s+denied|"
    r"context\s+(?:length|window)\s+exceeded|max(?:imum)?\s+token\s+budget|"
    r"user[_\s-]cancell?ed|cancell?ed\s+by\s+user"
    r")",
    re.IGNORECASE,
)

# Positive signals that an ERROR stop is specifically a quota/capacity/rate-limit exhaustion.
_QUOTA_SIGNAL_RE = re.compile(
    r"(?:"
    r"resource[_\s-]?exhausted|"
    r"quota[_\s-]?(?:exceeded|exhausted|limit|resets?)|"
    r"exhausted\s+(?:your\s+)?(?:quota|capacity|usage)|"
    r"capacity\s+(?:limit|exhausted|reached)|"
    r"rate[\s-]?limit(?:ed|\s+exceeded|\s+reached)?|"
    r"too\s+many\s+requests|"
    r"\b(?:http\s*)?429\b|"
    r"usage\s+limit\s+(?:reached|exceeded)|"
    r"5[\s-]?hour\s+(?:quota|limit|cap)|"
    r"quota\s+will\s+reset|"
    r"resets?\s+(?:in|after)\s+\d+\s*(?:d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)"
    r")",
    re.IGNORECASE,
)

# Explicit ISO-8601 / RFC-3339 reset timestamp tied to a quota/reset keyword.
_ISO_STAMP = r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2}))"
_ISO_RESET_RE = re.compile(
    r"(?:quota[_\s-]?resets?|reset[_\s-]?time|resets?\s+at|reset[_\s-]?at|available\s+at|retry[_\s-]?after)"
    r"[\s:=,\"'(){}\[\]]{1,8}" + _ISO_STAMP,
    re.IGNORECASE,
)

# Relative duration anchored to a reset/retry phrase (supports compound forms like "4h32m15s", "1h 20m", "45 minutes").
_ANCHORED_DUR_RE = re.compile(
    r"(?:resets?\s+(?:in|after)|try\s+again\s+in|retry\s+(?:in|after)|available\s+(?:in|after)|"
    r"limit\s+resets?\s+(?:in|after)|quota\s+will\s+reset\s+(?:in|after))\s*(?:about\s+|approximately\s+)?"
    r"((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*){1,4})",
    re.IGNORECASE,
)
_DUR_PART_RE = re.compile(
    r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
# Compact compound token such as "4h32m15s", "2h15m", "45m10s" inside an already-verified quota message.
_COMPACT_DUR_RE = re.compile(
    r"\b(?:(\d+)d)?(?:(\d+)h)(?:(\d+)m)?(?:(\d+)s)?\b|\b(\d+)m(\d+)s\b",
    re.IGNORECASE,
)

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>", re.IGNORECASE)
_USER_REQUEST_RE = re.compile(r"</?USER_REQUEST>", re.IGNORECASE)
_HOLD_TEXT_RE = re.compile(
    r"(?:^\s*(?:hold|stop|pause)\b|\boperator\s+hold\b|\bdo\s+not\s+(?:continue|resume)\b)",
    re.IGNORECASE,
)

SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "fish", "env", "python3", "python", "timeout"})


@dataclasses.dataclass(frozen=True)
class AutoResumeConfig:
    enabled: bool = DEFAULT_ENABLED
    buffer_seconds: int = DEFAULT_BUFFER_SECONDS
    jitter_max_seconds: int = DEFAULT_JITTER_MAX_SECONDS
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    prompt: str = DEFAULT_PROMPT


@dataclasses.dataclass(frozen=True)
class QuotaResetSignal:
    reset_at: int
    source: str
    error_excerpt: str


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _sanitize_prompt(raw: object) -> str:
    if not isinstance(raw, str):
        return DEFAULT_PROMPT
    cleaned = _CTRL_RE.sub(" ", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return DEFAULT_PROMPT
    return cleaned[:MAX_PROMPT_CHARS]


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _read_owned_json_file(path: Path, max_bytes: int) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        parsed = json.loads(raw.decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def config_path(home: Path | None = None) -> Path:
    return (home or ut_common.HOME) / ".steak-antigravity" / "config.json"


def load_config(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> AutoResumeConfig:
    environ = os.environ if env is None else env
    file_cfg = _read_owned_json_file(config_path(home), MAX_CONFIG_BYTES) or {}
    section = file_cfg.get("autoResume")
    if not isinstance(section, dict):
        section = {}

    enabled = DEFAULT_ENABLED
    if isinstance(section.get("enabled"), bool):
        enabled = section["enabled"]

    raw_env = str(environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME", "")).strip().lower()
    if raw_env in _FALSE_VALUES:
        enabled = False
    elif raw_env in _TRUE_VALUES:
        enabled = True

    buffer_seconds = _clamp_int(
        section.get("bufferSeconds"),
        DEFAULT_BUFFER_SECONDS,
        MIN_BUFFER_SECONDS,
        MAX_BUFFER_SECONDS,
    )
    if environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_BUFFER_SEC", "").strip():
        buffer_seconds = _clamp_int(
            environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_BUFFER_SEC"),
            buffer_seconds,
            MIN_BUFFER_SECONDS,
            MAX_BUFFER_SECONDS,
        )

    jitter_max_seconds = _clamp_int(
        section.get("jitterMaxSeconds"),
        DEFAULT_JITTER_MAX_SECONDS,
        0,
        MAX_JITTER_SECONDS,
    )
    if environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_JITTER_SEC", "").strip():
        jitter_max_seconds = _clamp_int(
            environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_JITTER_SEC"),
            jitter_max_seconds,
            0,
            MAX_JITTER_SECONDS,
        )

    max_wait_seconds = _clamp_int(
        section.get("maxWaitSeconds"),
        DEFAULT_MAX_WAIT_SECONDS,
        MIN_MAX_WAIT_SECONDS,
        MAX_MAX_WAIT_SECONDS,
    )
    if environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_WAIT_SEC", "").strip():
        max_wait_seconds = _clamp_int(
            environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_WAIT_SEC"),
            max_wait_seconds,
            MIN_MAX_WAIT_SECONDS,
            MAX_MAX_WAIT_SECONDS,
        )

    max_retries = _clamp_int(
        section.get("maxRetries"),
        DEFAULT_MAX_RETRIES,
        1,
        MAX_RETRIES_CAP,
    )
    if environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES", "").strip():
        max_retries = _clamp_int(
            environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES"),
            max_retries,
            1,
            MAX_RETRIES_CAP,
        )

    prompt = _sanitize_prompt(section.get("prompt"))
    if environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_PROMPT", "").strip():
        prompt = _sanitize_prompt(environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_PROMPT"))

    return AutoResumeConfig(
        enabled=enabled,
        buffer_seconds=buffer_seconds,
        jitter_max_seconds=jitter_max_seconds,
        max_wait_seconds=max_wait_seconds,
        max_retries=max_retries,
        prompt=prompt,
    )


def _parse_iso_epoch_seconds(raw: object) -> int | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        if not math.isfinite(val) or val <= 0:
            return None
        # Distinguish epoch milliseconds from seconds.
        sec = int(val / 1000) if val > 1e11 else int(val)
        return sec if sec > 86_400 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        def _norm_frac(match: re.Match[str]) -> str:
            return "." + (match.group(1) + "000000")[:6]

        iso = _FRAC_RE.sub(_norm_frac, text, count=1).replace("Z", "+00:00").replace("z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            return None
        sec = int(parsed.timestamp())
        return sec if sec > 86_400 else None
    except (ValueError, OverflowError, OSError):
        return None


def is_quota_exhaustion_error(text: str) -> bool:
    cleaned = strip_ansi(text or "").strip()
    if not cleaned:
        return False
    if _NON_QUOTA_EXCLUDE_RE.search(cleaned):
        return False
    return bool(_QUOTA_SIGNAL_RE.search(cleaned))


def parse_duration_seconds(text: str) -> int | None:
    """Parse an official server-reported reset duration (e.g. 'Resets in 4h32m15s')."""
    cleaned = strip_ansi(text or "")
    if not cleaned:
        return None

    m = _ANCHORED_DUR_RE.search(cleaned)
    if m:
        chunk = m.group(1)
        total = 0
        matched_any = False
        for num_str, unit in _DUR_PART_RE.findall(chunk):
            val = int(num_str)
            u = unit.lower()
            if u.startswith("d"):
                total += val * 86400
            elif u.startswith("h"):
                total += val * 3600
            elif u.startswith("m"):
                total += val * 60
            elif u.startswith("s"):
                total += val
            matched_any = True
        if matched_any and total > 0:
            return total

    # Fallback: compact compound duration ("4h32m15s", "2h15m", "45m10s") when quota context is present.
    if is_quota_exhaustion_error(cleaned):
        cm = _COMPACT_DUR_RE.search(cleaned)
        if cm:
            if cm.group(5) is not None:
                total = int(cm.group(5)) * 60 + int(cm.group(6))
            else:
                d = int(cm.group(1) or 0)
                h = int(cm.group(2) or 0)
                mins = int(cm.group(3) or 0)
                s = int(cm.group(4) or 0)
                total = d * 86400 + h * 3600 + mins * 60 + s
            if total > 0:
                return total
    return None


def parse_iso_reset_timestamp(text: str, *, now: float | None = None) -> int | None:
    """Parse an explicit ISO-8601/RFC-3339 quota reset timestamp from error text."""
    cleaned = strip_ansi(text or "")
    if not cleaned:
        return None
    m = _ISO_RESET_RE.search(cleaned)
    if not m:
        return None
    ts = _parse_iso_epoch_seconds(m.group(1))
    if ts is None:
        return None
    ref_now = time.time() if now is None else float(now)
    # Reject stale historical timestamps (> 60s in the past).
    if ts < ref_now - 60:
        return None
    return ts


def default_usage_path(home: Path | None = None) -> Path:
    return (home or ut_common.HOME) / ".ultraterm" / "antigravity" / "usage.json"


def read_statusline_reset(
    usage_path: Path | None = None,
    *,
    now: float | None = None,
    home: Path | None = None,
) -> tuple[int | None, str | None]:
    """Read a fresh 5-hour reset timestamp from the official statusLine usage snapshot."""
    path = usage_path if usage_path is not None else default_usage_path(home)
    snap = _read_owned_json_file(path, MAX_USAGE_BYTES)
    if not snap or snap.get("source") != "antigravity-statusline" or not snap.get("available"):
        return None, None
    ref_now = time.time() if now is None else float(now)
    captured_at = snap.get("capturedAt")
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
        return None, None
    age = ref_now - float(captured_at)
    if age < -60 or age > STATUSLINE_MAX_AGE_SECONDS:
        return None, None

    five = snap.get("fiveHour") if isinstance(snap.get("fiveHour"), dict) else None
    seven = snap.get("sevenDay") if isinstance(snap.get("sevenDay"), dict) else None

    five_used = five.get("usedPercent") if five else None
    five_reset = five.get("resetsAt") if five else None
    seven_used = seven.get("usedPercent") if seven else None
    seven_reset = seven.get("resetsAt") if seven else None

    # If the weekly bucket itself is exhausted and resets later than the 5-hour bucket,
    # surface the weekly reset so maxWaitSeconds rejects futile 5-hour retries.
    if (
        isinstance(seven_used, (int, float))
        and not isinstance(seven_used, bool)
        and float(seven_used) >= 99.9
        and isinstance(seven_reset, int)
        and not isinstance(seven_reset, bool)
        and seven_reset > ref_now - 60
    ):
        if not isinstance(five_reset, int) or seven_reset > five_reset:
            return seven_reset, "statusline-7d"

    if (
        isinstance(five_used, (int, float))
        and not isinstance(five_used, bool)
        and float(five_used) >= 99.0
        and isinstance(five_reset, int)
        and not isinstance(five_reset, bool)
        and five_reset > ref_now - 60
    ):
        return five_reset, "statusline-5h"
    return None, None


def _read_transcript_tail_lines(transcript_path: str, max_bytes: int = MAX_TRANSCRIPT_TAIL_BYTES) -> list[dict]:
    if not transcript_path:
        return []
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(transcript_path, flags)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return []
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            if st.st_size > max_bytes:
                handle.seek(st.st_size - max_bytes)
                handle.readline()  # discard partial first line
            raw = handle.read(max_bytes + 1)
    except OSError:
        return []
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    rows: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _extract_structured_reset(obj: object, ref_now: float) -> tuple[int | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    for key in ("quota_resets", "quotaResets", "reset_time", "resetTime", "resetsAt", "resets_at"):
        if key in obj and obj[key] is not None:
            ts = _parse_iso_epoch_seconds(obj[key])
            if ts is not None and ts >= ref_now - 60:
                return ts, "payload-iso"
    for key in ("retryAfter", "retry_after", "resetInSeconds", "reset_in_seconds"):
        val = obj.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and val > 0:
            return int(math.ceil(ref_now + float(val))), "payload-duration"
        if isinstance(val, str):
            dur = parse_duration_seconds(f"retry after {val}")
            if dur is not None:
                return int(math.ceil(ref_now + dur)), "payload-duration"
    return None, None


def _collect_error_text(payload: dict) -> str:
    parts: list[str] = []
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        parts.append(err.strip())
    elif isinstance(err, dict):
        for k in ("message", "error", "detail", "details", "reason"):
            v = err.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
    msg = payload.get("errorMessage")
    if isinstance(msg, str) and msg.strip():
        parts.append(msg.strip())

    # If the Stop hook error string is empty or lacks reset detail, inspect only the
    # trailing error/system rows of the conversation transcript.
    joined = "\n".join(parts)
    if not joined or (is_quota_exhaustion_error(joined) and parse_duration_seconds(joined) is None and parse_iso_reset_timestamp(joined) is None):
        tp = payload.get("transcriptPath")
        if isinstance(tp, str) and tp:
            for row in reversed(_read_transcript_tail_lines(tp)[-8:]):
                rtype = str(row.get("type") or "").upper()
                rstatus = str(row.get("status") or "").upper()
                if rtype in ("USER_INPUT", "PLANNER_RESPONSE"):
                    break
                if rtype in ("ERROR", "ERROR_MESSAGE", "SYSTEM_MESSAGE", "QUOTA_EXHAUSTED") or rstatus == "ERROR" or row.get("error"):
                    for k in ("error", "content", "message"):
                        v = row.get(k)
                        if isinstance(v, str) and v.strip():
                            parts.append(v.strip())
                    break
    return "\n".join(parts)


def extract_quota_reset(
    payload: dict,
    *,
    now: float | None = None,
    usage_path: Path | None = None,
    config: AutoResumeConfig | None = None,
    home: Path | None = None,
) -> tuple[QuotaResetSignal | None, str]:
    """Extract an official quota reset signal from a Stop hook payload.

    Returns (QuotaResetSignal, "ok") or (None, reason_code).
    """
    if not isinstance(payload, dict):
        return None, "invalid_payload"
    reason = str(payload.get("terminationReason") or "").strip().upper()
    if reason != "ERROR":
        return None, "not_error_stop"

    ref_now = time.time() if now is None else float(now)
    cfg = config or load_config(home=home)

    structured_ts, structured_src = _extract_structured_reset(payload, ref_now)
    if structured_ts is None and isinstance(payload.get("error"), dict):
        structured_ts, structured_src = _extract_structured_reset(payload["error"], ref_now)

    error_text = _collect_error_text(payload)
    cleaned_error = strip_ansi(error_text).strip()
    excerpt = _CTRL_RE.sub(" ", cleaned_error)[:240]

    # Must be a genuine quota/capacity error (or carry an explicit structured quota reset field).
    if structured_ts is None and not is_quota_exhaustion_error(cleaned_error):
        return None, "non_quota_error"
    if cleaned_error and _NON_QUOTA_EXCLUDE_RE.search(cleaned_error):
        return None, "non_quota_error"

    reset_at: int | None = None
    source: str | None = None

    if structured_ts is not None and structured_src is not None:
        reset_at, source = structured_ts, structured_src
    else:
        iso_ts = parse_iso_reset_timestamp(cleaned_error, now=ref_now)
        if iso_ts is not None:
            reset_at, source = iso_ts, "error-iso"
        else:
            dur = parse_duration_seconds(cleaned_error)
            if dur is not None:
                reset_at, source = int(math.ceil(ref_now + dur)), "error-duration"
            else:
                snap_ts, snap_src = read_statusline_reset(usage_path, now=ref_now, home=home)
                if snap_ts is not None and snap_src is not None:
                    reset_at, source = snap_ts, snap_src

    if reset_at is None or source is None:
        return None, "no_reset_timestamp"

    wait_until_reset = max(0.0, float(reset_at) - ref_now)
    if wait_until_reset > cfg.max_wait_seconds:
        return None, "exceeds_max_wait"

    return QuotaResetSignal(reset_at=reset_at, source=source, error_excerpt=excerpt), "ok"


def compute_wake_time(
    reset_at: int,
    *,
    now: float,
    config: AutoResumeConfig,
    jitter_fn: Callable[[int], int] | None = None,
) -> tuple[int, int, int]:
    """Compute a strictly post-reset wake timestamp: max(reset_at, ceil(now)) + buffer (>=60s) + jitter."""
    buffer_sec = max(MIN_BUFFER_SECONDS, int(config.buffer_seconds))
    jitter_max = max(0, min(MAX_JITTER_SECONDS, int(config.jitter_max_seconds)))
    if jitter_max == 0:
        jitter_sec = 0
    elif jitter_fn is not None:
        jitter_sec = max(0, min(jitter_max, int(jitter_fn(jitter_max))))
    else:
        jitter_sec = secrets.randbelow(jitter_max + 1)
    effective_reset = max(int(reset_at), int(math.ceil(now)))
    wake_at = effective_reset + buffer_sec + jitter_sec
    return wake_at, buffer_sec, jitter_sec


def _safe_id(raw: str) -> str:
    if _SAFE_ID_RE.fullmatch(raw) and raw not in (".", ".."):
        return raw
    return "conv-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _normalize_operator_text(text: str) -> str:
    stripped = _SYSTEM_REMINDER_RE.sub("", text or "")
    stripped = _USER_REQUEST_RE.sub("", stripped)
    return stripped.strip()


def _latest_transcript_user_text(transcript_path: str) -> str:
    for row in reversed(_read_transcript_tail_lines(transcript_path)):
        if row.get("type") == "USER_INPUT":
            content = row.get("content")
            if isinstance(content, str):
                return _normalize_operator_text(content)
    return ""


def is_hold_active(
    conversation_id: str,
    *,
    last_user_input: str = "",
    transcript_path: str = "",
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[bool, str]:
    """Check whether an operator HOLD or stop instruction is active."""
    environ = os.environ if env is None else env
    base_home = home or ut_common.HOME

    if str(environ.get("STEAK_ANTIGRAVITY_HOLD", "")).strip().lower() in _TRUE_VALUES:
        return True, "env:STEAK_ANTIGRAVITY_HOLD"
    if str(environ.get("ULTRATERM_HOLD", "")).strip().lower() in _TRUE_VALUES:
        return True, "env:ULTRATERM_HOLD"

    user_text = _normalize_operator_text(last_user_input)
    if user_text and _HOLD_TEXT_RE.search(user_text):
        return True, "last_user_input"

    if transcript_path:
        latest_user = _latest_transcript_user_text(transcript_path)
        if latest_user and _HOLD_TEXT_RE.search(latest_user):
            return True, "transcript_hold"

    if conversation_id and _SAFE_ID_RE.fullmatch(conversation_id) and conversation_id not in (".", ".."):
        hold_file = base_home / ".cache" / "agent-hold" / conversation_id / "hold-handoff.md"
        try:
            st = os.lstat(hold_file)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                return True, "agent_hold_file"
        except OSError:
            pass

    for job_var in ("ULTRATERM_JOB_ID", "UT_COORDINATOR_JOB_ID"):
        job_id = str(environ.get(job_var, "")).strip()
        if job_id and _SAFE_ID_RE.fullmatch(job_id) and job_id not in (".", ".."):
            job_hold = base_home / ".cache" / "ut-coordinator" / job_id / "hold-handoff.md"
            try:
                st = os.lstat(job_hold)
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                    return True, f"coordinator_hold_file:{job_var}"
            except OSError:
                pass

    return False, ""


def transcript_snapshot(transcript_path: str) -> dict:
    """Capture lightweight baseline stats for detecting subsequent user/model activity."""
    if not transcript_path:
        return {}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(transcript_path, flags)
    except OSError:
        return {}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return {}
        return {
            "path": transcript_path,
            "size": st.st_size,
            "mtimeNs": st.st_mtime_ns,
            "dev": st.st_dev,
            "ino": st.st_ino,
        }
    finally:
        os.close(fd)


def has_transcript_activity(transcript_path: str, baseline: Mapping[str, Any]) -> tuple[bool, str]:
    """Detect whether the user or model produced new conversation steps after scheduling."""
    if not transcript_path or not baseline:
        return False, ""
    base_size = baseline.get("size")
    if not isinstance(base_size, int) or base_size < 0:
        return False, ""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(transcript_path, flags)
    except OSError:
        return False, ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return False, ""
        if baseline.get("ino") is not None and st.st_ino != baseline.get("ino"):
            return True, "transcript_rotated"
        if st.st_size < base_size:
            return True, "transcript_truncated"
        if st.st_size == base_size:
            return False, ""
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            handle.seek(base_size)
            delta = handle.read(MAX_TRANSCRIPT_TAIL_BYTES)
        for line in delta.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            etype = str(entry.get("type") or "").upper()
            if etype in ("USER_INPUT", "PLANNER_RESPONSE", "TOOL_RESULT"):
                return True, f"transcript_activity:{etype.lower()}"
        return False, ""
    except OSError:
        return False, ""
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _is_loopback_address(address: str) -> bool:
    host, sep, port = address.rpartition(":")
    if not sep or not port.isdigit():
        return False
    port_num = int(port)
    if port_num < 1 or port_num > 65535:
        return False
    norm = host.strip().lower()
    return norm in ("localhost", "127.0.0.1", "[::1]")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow HTTP 3xx redirects so local CSRF headers can never leave loopback."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_conversation_state(
    conversation_id: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 3.0,
) -> dict | None:
    """Query Antigravity's local ConversationService/GetConversation API.

    Documented in Antigravity CLI's built-in `conversations` skill
    (`product.v1.ConversationService/GetConversation` using `ANTIGRAVITY_LS_ADDRESS`
    and `ANTIGRAVITY_CSRF_TOKEN`). Only runs when both env vars are present in
    memory, the address is strictly loopback, and `STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS`
    is not disabled (`0`/`false`/`off`). Uses an explicit no-proxy, no-redirect
    opener so credentials never touch proxies or leave loopback, and never
    persists or logs the CSRF token.
    """
    if not conversation_id:
        return None
    environ = os.environ if env is None else env
    if str(environ.get("STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS", "")).strip().lower() in _FALSE_VALUES:
        return None
    address = str(environ.get("ANTIGRAVITY_LS_ADDRESS", "")).strip()
    token = str(environ.get("ANTIGRAVITY_CSRF_TOKEN", "")).strip()
    if not address or not token or not _is_loopback_address(address):
        return None
    url = f"http://{address}/product.v1.ConversationService/GetConversation"
    body = json.dumps({"conversationId": conversation_id}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": token,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_CONFIG_BYTES)
        parsed = json.loads(raw.decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else None
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except OSError:
            pass
        return None
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _process_row(pid: int) -> tuple[int, str] | None:
    res = ut_common.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], timeout=1.0)
    if not res or res.returncode != 0:
        return None
    parts = res.stdout.strip().split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), os.path.basename(parts[1].strip())


def resolve_agy_parent_pid() -> int | None:
    """Find the nearest non-shell ancestor PID (the live `agy` process running this hook)."""
    current = os.getppid()
    for _ in range(12):
        if current <= 1:
            return None
        row = _process_row(current)
        if row is None:
            return None
        ppid, comm = row
        if comm not in SHELL_NAMES:
            return current
        current = ppid
    return None


_SPAWNED_WAITERS: dict[int, subprocess.Popen[bytes]] = {}


def pid_is_alive(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    proc = _SPAWNED_WAITERS.get(pid)
    if proc is not None:
        if proc.poll() is not None:
            _SPAWNED_WAITERS.pop(pid, None)
            return False
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _is_our_waiter_pid(pid: object, expected_token: str) -> bool:
    if not expected_token or not pid_is_alive(pid):
        return False
    res = ut_common.run(["ps", "-o", "args=", "-p", str(pid)], timeout=1.0)
    if not res or res.returncode != 0:
        return False
    cmdline = res.stdout.strip()
    return "auto_resume_waiter.py" in cmdline and expected_token in cmdline


def _stop_waiter(pid: object, expected_token: str) -> None:
    if isinstance(pid, int) and not isinstance(pid, bool) and pid == os.getpid():
        return
    if not _is_our_waiter_pid(pid, expected_token):
        return
    target_pid = int(pid)  # type: ignore[arg-type]
    try:
        os.kill(target_pid, signal.SIGTERM)
    except OSError:
        pass
    proc = _SPAWNED_WAITERS.get(target_pid)
    if proc is not None:
        try:
            proc.wait(timeout=2.0)
            _SPAWNED_WAITERS.pop(target_pid, None)
        except subprocess.TimeoutExpired:
            pass


def find_utp_terminal(terminal_id: str) -> dict | None:
    """Verify via `utp list` that terminal_id is still live (not replaced or exited)."""
    if not terminal_id or not ut_common.UUID_RE.fullmatch(terminal_id):
        return None
    if not os.access(ut_common.UTP, os.X_OK):
        return None
    res = ut_common.run([str(ut_common.UTP), "list"], timeout=3.0)
    if not res or res.returncode != 0:
        return None
    target = terminal_id.lower()
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        if parts[4].strip().lower() != target:
            continue
        # Reject panes whose process exited (utp list appends `exited: ...`).
        tail = "\t".join(parts[5:]).lower()
        if "exited:" in tail or "exited:" in parts[1].lower():
            return None
        return {
            "slot": parts[0].strip(),
            "title": parts[1],
            "harness": parts[2].strip(),
            "profile": parts[3].strip(),
            "terminalId": parts[4].strip(),
        }
    return None


def state_dir(data_dir: Path | None = None) -> Path:
    base = (data_dir or ut_common.plugin_data_dir()) / "auto-resume"
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _ensure_private_state_dir(data_dir: Path | None = None) -> Path | None:
    target = state_dir(data_dir)
    ok, _ = ut_common.verify_private_dir(target)
    return target if ok else None


def state_path_for(conversation_id: str, data_dir: Path | None = None) -> Path:
    return state_dir(data_dir) / f"{_safe_id(conversation_id)}.json"


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_state_atomic(path: Path, state: dict) -> bool:
    ok, _ = ut_common.verify_private_dir(path.parent)
    if not ok:
        return False
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False

    tmp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _sweep_stale_states(base: Path, now: float) -> None:
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            st = os.lstat(entry)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                continue
            if now - st.st_mtime < STALE_STATE_MAX_AGE_SECONDS:
                continue
            data = _read_owned_json_file(entry, MAX_CONFIG_BYTES)
            if data and data.get("status") == "scheduled" and _is_our_waiter_pid(data.get("waiterPid"), str(data.get("token") or "")):
                continue
            os.unlink(entry)
            lock = entry.with_suffix(".lock")
            try:
                if lock.is_file() and not lock.is_symlink():
                    os.unlink(lock)
            except OSError:
                pass
        except OSError:
            continue


def _waiter_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Build child env for the waiter, stripping UI Mode vars and keeping no leaked state."""
    cleaned = {}
    for k, v in base_env.items():
        if k.startswith("ULTRATERM_UI_") or k == "ULTRATERM_MAIN_SCREEN":
            continue
        cleaned[k] = v
    return cleaned


def _spawn_waiter_process(state_file: Path, token: str, env: Mapping[str, str]) -> int | None:
    waiter_script = Path(__file__).resolve().parent.parent / "scripts" / "auto_resume_waiter.py"
    if not waiter_script.is_file():
        return None
    proc = subprocess.Popen(
        [sys.executable, "-I", "-B", str(waiter_script), "--state", str(state_file), "--token", token],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=_waiter_env(env),
    )
    _SPAWNED_WAITERS[proc.pid] = proc
    return proc.pid


def schedule_from_stop(
    payload: dict,
    *,
    data_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    usage_path: Path | None = None,
    now: float | None = None,
    spawn_waiter: bool = True,
    jitter_fn: Callable[[int], int] | None = None,
    identity_fn: Callable[[], dict] | None = None,
    is_pane_process_fn: Callable[[], bool] | None = None,
    conversation_state_fn: Callable[[str], dict | None] | None = None,
    parent_pid_fn: Callable[[], int | None] | None = None,
) -> dict:
    """Evaluate a Stop hook payload and schedule a single ToS-safe auto-resume if warranted."""
    environ = os.environ if env is None else env
    ref_now = time.time() if now is None else float(now)
    cfg = load_config(env=environ, home=home)

    if not cfg.enabled:
        return {"action": "skipped", "reason": "disabled"}

    conv_id = str(payload.get("conversationId") or "").strip()
    if not conv_id:
        return {"action": "skipped", "reason": "missing_conversation_id"}

    signal_info, reason_code = extract_quota_reset(
        payload,
        now=ref_now,
        usage_path=usage_path,
        config=cfg,
        home=home,
    )
    if signal_info is None:
        if reason_code == "non_quota_error":
            cancel_for_conversation(conv_id, reason="non_quota_error", data_dir=data_dir)
        return {"action": "skipped", "reason": reason_code}

    tp = payload.get("transcriptPath")
    transcript_path = tp if isinstance(tp, str) and tp else ""
    last_user = str(payload.get("lastUserInput") or "")

    held, hold_reason = is_hold_active(
        conv_id,
        last_user_input=last_user,
        transcript_path=transcript_path,
        env=environ,
        home=home,
    )
    if held:
        cancel_for_conversation(conv_id, reason=f"hold:{hold_reason}", data_dir=data_dir)
        return {"action": "skipped", "reason": f"hold:{hold_reason}"}

    # Never let a nested agy (e.g. spawned from a tool call) drive the parent pane.
    if is_pane_process_fn is not None:
        if not is_pane_process_fn():
            return {"action": "skipped", "reason": "nested_process"}

    # Never let a child / subagent / swarm worker drive the top-level interactive pane.
    conv_state = (
        conversation_state_fn(conv_id)
        if conversation_state_fn is not None
        else get_conversation_state(conv_id, env=environ)
    )
    if isinstance(conv_state, dict):
        meta = conv_state.get("metadata")
        if isinstance(meta, dict) and str(meta.get("parentConversationId") or "").strip():
            return {"action": "skipped", "reason": "child_conversation"}

    identity = identity_fn() if identity_fn is not None else ut_common.terminal_identity()
    term_id = str(identity.get("terminalId") or "").strip() if isinstance(identity, dict) else ""
    if not (isinstance(identity, dict) and identity.get("insideUltraTerm") and ut_common.UUID_RE.fullmatch(term_id)):
        sys.stderr.write(
            f"[steak-antigravity] auto-resume: quota reset detected (resetAt={signal_info.reset_at}), "
            "but no UltraTerm UTP terminal identity is active; skipping automatic resume\n"
        )
        return {"action": "skipped", "reason": "no_utp_transport", "resetAt": signal_info.reset_at}

    if not os.access(ut_common.UTP, os.X_OK) and identity_fn is None:
        return {"action": "skipped", "reason": "utp_unavailable", "resetAt": signal_info.reset_at}

    base = _ensure_private_state_dir(data_dir)
    if base is None:
        return {"action": "skipped", "reason": "unsafe_state_dir"}
    _sweep_stale_states(base, ref_now)

    sf = state_path_for(conv_id, data_dir)
    with _file_lock(sf.with_suffix(".lock")):
        prev = _read_owned_json_file(sf, MAX_CONFIG_BYTES) or {}
        prev_status = str(prev.get("status") or "")
        prev_token = str(prev.get("token") or "")
        prev_waiter = prev.get("waiterPid")
        prev_attempt = prev.get("attempt") if isinstance(prev.get("attempt"), int) and not isinstance(prev.get("attempt"), bool) else 0

        wake_at, buffer_sec, jitter_sec = compute_wake_time(
            signal_info.reset_at,
            now=ref_now,
            config=cfg,
            jitter_fn=jitter_fn,
        )

        # Single-timer deduplication: if a waiter is already scheduled for this same quota window, keep it.
        if (
            prev_status == "scheduled"
            and isinstance(prev.get("wakeAt"), int)
            and abs(int(prev["wakeAt"]) - wake_at) <= DEDUP_WINDOW_SECONDS
            and (not spawn_waiter or _is_our_waiter_pid(prev_waiter, prev_token))
        ):
            return {
                "action": "deduplicated",
                "conversationId": conv_id,
                "token": prev_token,
                "resetAt": prev.get("resetAt"),
                "wakeAt": prev.get("wakeAt"),
                "attempt": prev_attempt,
            }

        # Enforce bounded retry cap per conversation incident.
        next_attempt = prev_attempt + 1
        if next_attempt > cfg.max_retries:
            prev["status"] = "max_retries"
            prev["updatedAt"] = int(ref_now)
            _write_state_atomic(sf, prev)
            return {
                "action": "skipped",
                "reason": "max_retries_reached",
                "attempt": prev_attempt,
                "maxRetries": cfg.max_retries,
            }

        # Supersede any older waiter cleanly before spawning a new one.
        if prev_status == "scheduled" and prev_token:
            _stop_waiter(prev_waiter, prev_token)

        parent_pid = parent_pid_fn() if parent_pid_fn is not None else resolve_agy_parent_pid()
        token = str(uuid.uuid4())
        conv_update_time = ""
        if isinstance(conv_state, dict) and isinstance(conv_state.get("state"), dict):
            conv_update_time = str(conv_state["state"].get("updateTime") or "")

        record = {
            "schemaVersion": SCHEMA_VERSION,
            "conversationId": conv_id,
            "status": "scheduled",
            "token": token,
            "attempt": next_attempt,
            "maxRetries": cfg.max_retries,
            "resetAt": signal_info.reset_at,
            "bufferSeconds": buffer_sec,
            "jitterSeconds": jitter_sec,
            "wakeAt": wake_at,
            "scheduledAt": int(ref_now),
            "resetSource": signal_info.source,
            "waiterPid": None,
            "parentPid": parent_pid,
            "transcriptPath": transcript_path,
            "transcriptBaseline": transcript_snapshot(transcript_path),
            "conversationUpdateTime": conv_update_time,
            "prompt": cfg.prompt,
            "transport": {
                "kind": "utp",
                "terminalId": term_id,
                "slot": identity.get("slot"),
                "harness": identity.get("catalogHarness") or identity.get("harness") or "antigravity",
            },
            "cancelReason": None,
            "firedAt": None,
            "updatedAt": int(ref_now),
        }

        if not _write_state_atomic(sf, record):
            return {"action": "skipped", "reason": "state_write_failed"}

        if spawn_waiter:
            waiter_pid = _spawn_waiter_process(sf, token, environ)
            if waiter_pid is None:
                record["status"] = "failed"
                record["cancelReason"] = "waiter_spawn_failed"
                _write_state_atomic(sf, record)
                return {"action": "failed", "reason": "waiter_spawn_failed"}
            record["waiterPid"] = waiter_pid
            _write_state_atomic(sf, record)

        wait_sec = max(0, wake_at - int(ref_now))
        sys.stderr.write(
            f"[steak-antigravity] auto-resume scheduled for {conv_id}: "
            f"resetAt={signal_info.reset_at}, wakeAt={wake_at} (+{buffer_sec}s buffer +{jitter_sec}s jitter, "
            f"wait={wait_sec}s, attempt={next_attempt}/{cfg.max_retries})\n"
        )
        return {
            "action": "scheduled",
            "conversationId": conv_id,
            "token": token,
            "resetAt": signal_info.reset_at,
            "bufferSeconds": buffer_sec,
            "jitterSeconds": jitter_sec,
            "wakeAt": wake_at,
            "attempt": next_attempt,
            "waiterPid": record["waiterPid"],
            "statePath": str(sf),
        }


def cancel_for_conversation(
    conversation_id: str,
    *,
    reason: str,
    data_dir: Path | None = None,
    reset_attempts: bool = False,
    expected_token: str | None = None,
) -> bool:
    """Cancel any scheduled auto-resume timer for a conversation."""
    if not conversation_id:
        return False
    base = (data_dir or ut_common.plugin_data_dir()) / "auto-resume"
    if not base.is_dir():
        return False
    sf = base / f"{_safe_id(conversation_id)}.json"
    if not sf.exists() and not sf.is_symlink():
        return False
    ok, _ = ut_common.verify_private_dir(base)
    if not ok:
        return False
    with _file_lock(sf.with_suffix(".lock")):
        state = _read_owned_json_file(sf, MAX_CONFIG_BYTES)
        if not state:
            return False
        if expected_token is not None and state.get("token") != expected_token:
            return False
        was_scheduled = state.get("status") == "scheduled"
        if was_scheduled:
            _stop_waiter(state.get("waiterPid"), str(state.get("token") or ""))
            state["status"] = "cancelled"
            state["cancelReason"] = reason
            state["waiterPid"] = None
            state["updatedAt"] = int(time.time())
        if reset_attempts and state.get("attempt", 0) != 0:
            state["attempt"] = 0
            state["updatedAt"] = int(time.time())
            _write_state_atomic(sf, state)
            return was_scheduled
        if was_scheduled:
            _write_state_atomic(sf, state)
        return was_scheduled


def cancel_for_terminal(
    terminal_id: str,
    *,
    reason: str,
    exclude_conversation_id: str = "",
    data_dir: Path | None = None,
) -> int:
    """Cancel any scheduled auto-resume targeting terminal_id (e.g. on /new SessionStart)."""
    if not terminal_id:
        return 0
    base = (data_dir or ut_common.plugin_data_dir()) / "auto-resume"
    if not base.is_dir():
        return 0
    ok, _ = ut_common.verify_private_dir(base)
    if not ok:
        return 0
    target = terminal_id.strip().lower()
    cancelled = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.suffix != ".json":
            continue
        state = _read_owned_json_file(entry, MAX_CONFIG_BYTES)
        if not state or state.get("status") != "scheduled":
            continue
        conv_id = str(state.get("conversationId") or "")
        if exclude_conversation_id and conv_id == exclude_conversation_id:
            continue
        transport = state.get("transport") if isinstance(state.get("transport"), dict) else {}
        if str(transport.get("terminalId") or "").strip().lower() == target:
            if cancel_for_conversation(conv_id, reason=reason, data_dir=data_dir):
                cancelled += 1
    return cancelled


def cancel_all(*, reason: str = "operator_cancel", data_dir: Path | None = None) -> int:
    base = (data_dir or ut_common.plugin_data_dir()) / "auto-resume"
    if not base.is_dir():
        return 0
    ok, _ = ut_common.verify_private_dir(base)
    if not ok:
        return 0
    cancelled = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.suffix != ".json":
            continue
        state = _read_owned_json_file(entry, MAX_CONFIG_BYTES)
        if not state or state.get("status") != "scheduled":
            continue
        conv_id = str(state.get("conversationId") or "")
        if conv_id and cancel_for_conversation(conv_id, reason=reason, data_dir=data_dir, reset_attempts=True):
            cancelled += 1
    return cancelled


def on_turn_completed(conversation_id: str, *, data_dir: Path | None = None) -> None:
    """Normal turn completion clears any stale timer and resets the consecutive retry counter."""
    cancel_for_conversation(conversation_id, reason="turn_completed", data_dir=data_dir, reset_attempts=True)


def list_pending(data_dir: Path | None = None) -> list[dict]:
    base = (data_dir or ut_common.plugin_data_dir()) / "auto-resume"
    if not base.is_dir():
        return []
    ok, _ = ut_common.verify_private_dir(base)
    if not ok:
        return []
    pending: list[dict] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        state = _read_owned_json_file(entry, MAX_CONFIG_BYTES)
        if not state or state.get("status") != "scheduled":
            continue
        token = str(state.get("token") or "")
        waiter_pid = state.get("waiterPid")
        pending.append(
            {
                "conversationId": state.get("conversationId"),
                "resetAt": state.get("resetAt"),
                "wakeAt": state.get("wakeAt"),
                "bufferSeconds": state.get("bufferSeconds"),
                "jitterSeconds": state.get("jitterSeconds"),
                "attempt": state.get("attempt"),
                "maxRetries": state.get("maxRetries"),
                "resetSource": state.get("resetSource"),
                "waiterAlive": _is_our_waiter_pid(waiter_pid, token),
            }
        )
    return pending


def doctor_summary(
    data_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    identity: dict | None = None,
) -> dict:
    cfg = load_config(env=env, home=home)
    ident = identity if identity is not None else ut_common.terminal_identity()
    term_id = str(ident.get("terminalId") or "") if isinstance(ident, dict) else ""
    transport_ok = bool(
        isinstance(ident, dict)
        and ident.get("insideUltraTerm")
        and ut_common.UUID_RE.fullmatch(term_id)
        and os.access(ut_common.UTP, os.X_OK)
    )
    return {
        "enabled": cfg.enabled,
        "bufferSeconds": cfg.buffer_seconds,
        "jitterMaxSeconds": cfg.jitter_max_seconds,
        "maxWaitSeconds": cfg.max_wait_seconds,
        "maxRetries": cfg.max_retries,
        "transportAvailable": transport_ok,
        "optOutEnv": "STEAK_ANTIGRAVITY_AUTO_RESUME=0",
        "pending": list_pending(data_dir=data_dir),
    }


def _default_utp_send(terminal_id: str, prompt: str) -> tuple[bool, str]:
    res = ut_common.run(
        [str(ut_common.UTP), "send", "--id", terminal_id, "--expected-id", terminal_id, prompt],
        timeout=10.0,
    )
    if res is None:
        return False, "utp_exec_failed"
    if res.returncode != 0:
        err = _CTRL_RE.sub(" ", (res.stderr or res.stdout or "").strip())[:200]
        return False, f"utp_send_exit_{res.returncode}:{err}"
    return True, "ok"


def deliver_resume(
    state_file: Path,
    expected_token: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    now_fn: Callable[[], float] | None = None,
    pid_alive_fn: Callable[[object], bool] | None = None,
    conversation_state_fn: Callable[[str], dict | None] | None = None,
    terminal_lookup_fn: Callable[[str], dict | None] | None = None,
    utp_send_fn: Callable[[str, str], tuple[bool, str]] | None = None,
) -> dict:
    """Re-verify every ToS-safety and cancellation guard and deliver one resume if still valid.

    Locking note: all guards and the transition `scheduled -> firing` run under
    the per-conversation file lock; the lock is released before invoking
    `utp send` so concurrent hooks are never blocked on the subprocess. That
    leaves a brief sub-second window in which a concurrent `PreInvocation` sees
    status `"firing"` and does not cancel. If a signal or exception interrupts
    `utp send`, a `finally` block transitions `"firing"` to `"cancelled"` /
    `"failed"` so the record is never left stuck in `"firing"`.
    """
    environ = os.environ if env is None else env
    now = (now_fn or time.time)()
    ok_dir, _ = ut_common.verify_private_dir(state_file.parent)
    if not ok_dir:
        return {"action": "aborted", "reason": "unsafe_state_dir"}

    with _file_lock(state_file.with_suffix(".lock")):
        state = _read_owned_json_file(state_file, MAX_CONFIG_BYTES)
        if not state:
            return {"action": "aborted", "reason": "state_missing"}
        if state.get("token") != expected_token:
            return {"action": "aborted", "reason": "token_mismatch"}
        if state.get("status") != "scheduled":
            return {"action": "aborted", "reason": f"status_{state.get('status')}"}

        def _cancel(reason: str) -> dict:
            state["status"] = "cancelled"
            state["cancelReason"] = reason
            state["waiterPid"] = None
            state["updatedAt"] = int(now)
            _write_state_atomic(state_file, state)
            return {"action": "cancelled", "reason": reason}

        cfg = load_config(env=environ, home=home)
        if not cfg.enabled:
            return _cancel("disabled")

        reset_at = state.get("resetAt")
        wake_at = state.get("wakeAt")
        if not isinstance(reset_at, int) or not isinstance(wake_at, int):
            return _cancel("invalid_timestamps")

        # Hard ToS invariant: NEVER fire before reset_at + MIN_BUFFER_SECONDS or before wake_at.
        min_safe_wake = max(wake_at, reset_at + MIN_BUFFER_SECONDS)
        if now < min_safe_wake:
            return {"action": "too_early", "remainingSeconds": int(math.ceil(min_safe_wake - now))}

        if now > wake_at + MAX_LATE_WAKE_SECONDS:
            return _cancel("stale_wake_window")

        conv_id = str(state.get("conversationId") or "")
        parent_pid = state.get("parentPid")
        alive_check = pid_alive_fn or pid_is_alive
        if parent_pid is not None and not alive_check(parent_pid):
            return _cancel("session_ended")

        tp = str(state.get("transcriptPath") or "")
        held, hold_reason = is_hold_active(
            conv_id,
            transcript_path=tp,
            env=environ,
            home=home,
        )
        if held:
            return _cancel(f"hold:{hold_reason}")

        baseline = state.get("transcriptBaseline")
        if tp and isinstance(baseline, dict):
            active, act_reason = has_transcript_activity(tp, baseline)
            if active:
                return _cancel(act_reason)

        conv_state = (
            conversation_state_fn(conv_id)
            if conversation_state_fn is not None
            else get_conversation_state(conv_id, env=environ)
        )
        if isinstance(conv_state, dict):
            st_obj = conv_state.get("state")
            if isinstance(st_obj, dict):
                run_state = str(st_obj.get("runState") or "")
                if run_state and run_state != "RUN_STATE_IDLE":
                    return _cancel(f"conversation_state:{run_state}")
                recorded_ut = str(state.get("conversationUpdateTime") or "")
                current_ut = str(st_obj.get("updateTime") or "")
                if recorded_ut and current_ut and current_ut != recorded_ut:
                    return _cancel("conversation_updated")

        transport = state.get("transport") if isinstance(state.get("transport"), dict) else {}
        term_id = str(transport.get("terminalId") or "")
        expected_harness = str(transport.get("harness") or "")
        if transport.get("kind") != "utp" or not ut_common.UUID_RE.fullmatch(term_id):
            return _cancel("invalid_transport")

        lookup = terminal_lookup_fn or find_utp_terminal
        live_term = lookup(term_id)
        if live_term is None:
            return _cancel("terminal_gone_or_exited")
        if expected_harness and live_term.get("harness") and live_term.get("harness") != expected_harness:
            return _cancel("terminal_harness_changed")

        prompt = _sanitize_prompt(state.get("prompt") or cfg.prompt)
        # Transition to firing under lock so two waiters can never double-fire.
        state["status"] = "firing"
        state["updatedAt"] = int(now)
        if not _write_state_atomic(state_file, state):
            return {"action": "aborted", "reason": "state_write_failed"}

    sender = utp_send_fn or _default_utp_send
    sent_ok = False
    send_msg = "interrupted_during_send"
    interrupted = False
    try:
        sent_ok, send_msg = sender(term_id, prompt)
    except BaseException as exc:
        interrupted = True
        send_msg = f"interrupted:{type(exc).__name__}"
        raise
    finally:
        finished_at = int((now_fn or time.time)())
        with _file_lock(state_file.with_suffix(".lock")):
            latest = _read_owned_json_file(state_file, MAX_CONFIG_BYTES)
            if latest and latest.get("token") == expected_token and latest.get("status") == "firing":
                latest["waiterPid"] = None
                latest["updatedAt"] = finished_at
                if sent_ok:
                    latest["status"] = "fired"
                    latest["firedAt"] = finished_at
                elif interrupted:
                    latest["status"] = "cancelled"
                    latest["cancelReason"] = send_msg
                else:
                    latest["status"] = "failed"
                    latest["cancelReason"] = send_msg
                _write_state_atomic(state_file, latest)

    if sent_ok:
        return {"action": "fired", "conversationId": conv_id, "terminalId": term_id, "firedAt": finished_at}
    return {"action": "failed", "conversationId": conv_id, "reason": send_msg}
