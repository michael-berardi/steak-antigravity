# Changelog

## 0.2.0 - 2026-11-25

- Add ToS-safe default quota auto-resume (`lib/auto_resume.py`, `scripts/auto_resume_waiter.py`):
  - Detects official quota-exhaustion reset signals on `Stop` (`terminationReason: ERROR`) by
    parsing server-reported reset timestamps (`quota_resets` / ISO-8601) and durations
    (`Resets in XhYmZs`), falling back to a fresh official `statusLine` snapshot
    (`~/.ultraterm/antigravity/usage.json`) only when the error confirms quota exhaustion without
    an inline timestamp.
  - Strictly waits past the official reset time plus a mandatory safety buffer (`>= 60s`, default
    `65s`) and small jitter (default `0..25s`) before issuing one identity-bound UltraTerm
    `utp send --id <uuid> --expected-id <uuid>` resume — zero remote API polling, zero limit
    evasion, no persistent daemons or LaunchAgents (one bounded single-shot waiter checking local
    state/PID files every 30s).
  - Cancels automatically on user interaction (`PreInvocation`, transcript growth, or Antigravity
    CLI's built-in `conversations` skill local `ConversationService/GetConversation` API with
    proxies and redirects disabled, optional via `STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS=0`),
    `SessionStart` (`/new` in the same pane), `USER_CANCELED`, session exit, non-quota `ERROR`
    stops, operator `HOLD`, weekly/excessive wait caps (default `6h`), or retry cap exhaustion
    (default `3` consecutive attempts).
  - Enabled by default with explicit opt-out via `STEAK_ANTIGRAVITY_AUTO_RESUME=0` or
    `~/.steak-antigravity/config.json` (`{"autoResume": {"enabled": false}}`).
- Surface `autoResume` config, transport readiness, and pending timers in `steak-antigravity doctor`
  and add `steak-antigravity auto-resume [status|cancel [conversationId]]`.

## 0.1.5 - 2026-11-25

- Finish guard now reads Antigravity's own plan (the latest `update_steps` call in the
  conversation transcript) as well as the Steak Pi todo store, so `agy` no longer stops
  with open native steps. Checked live: the Stop hook made `agy` finish an open step.
- Verify-after-edit failures now reach the agent: `PostToolUse` output is discarded by
  `agy`, so the failure is queued and injected on the next `PreInvocation`. After
  `failLimit` consecutive failures verify pauses for 5 minutes instead of for the rest
  of the conversation.
- UI Mode: a nested `agy` started from a tool call no longer writes into the pane's
  transcript (`NestedPublisherRefused`); tool results drop Antigravity's
  `Created At:`/`Completed At:` bookkeeping lines.
- `install` writes absolute skill paths (Antigravity rejects `~`), migrates the old `~`
  entry, removes the stale `steak-agy_usap` MCP cache, refuses to overwrite a
  `plugins.json`/`skills.json` it cannot parse, and writes them atomically; `uninstall`
  removes only this checkout's entry; `doctor` reports both problems.
- USAP bridge: non-object JSON-RPC lines get `-32600` instead of crashing the server;
  a cancel that arrives before startup finishes is honoured and a cancelled request
  gets no result; no progress after the final reply; shutdown never hangs on a stuck
  start; unique temp names and sweep of leftover temp files.
- Delegation guard only steers to USAP when `node` is available too, matching the
  server's own startup checks.
- Hooks no longer crash on corrupt state files; the `UsapNoticeAncestorUnmatched`
  log line is shown only with `STEAK_ANTIGRAVITY_DEBUG=1`.

## 0.1.4 - 2026-11-25

- Add `scripts/antigravity_statusline.py`, an official Antigravity CLI `statusLine`
  snapshot writer that records non-PII Gemini 5-hour and weekly quota percentages
  and reset timestamps to `~/.ultraterm/antigravity/usage.json` (`0600`) without
  printing extra lines or touching Google credentials.
- Wire `statusLine` (`stack_with_default: true`) safely in `steak-antigravity install`
  (preserving existing permissions and third-party status lines), clean up on
  `uninstall`, and report status in `doctor`.

## 0.1.3 - 2026-11-25

- Drop inherited operator `TMUX` and `TMUX_PANE` environment variables and prepend
  `~/.ultraterm/bin` to `PATH` before launching the USAP MCP bridge in
  `scripts/usap_mcp.py`, isolating spawned subagent workers from the live terminal
  multiplexer server.
- Add regression test coverage in `test/test_hooks.py`.

## 0.1.2 - 2026-11-25

- Read MCP `serverInfo.version` dynamically from `package.json` in `lib/usap-bridge.mjs`
  and `lib/mcp-unavailable.mjs` instead of hardcoding `0.1.0`.
- Make `bin/steak-antigravity` fail closed with a named error if `package.json` version
  cannot be read, instead of silently falling back to `0.1.0`.
- Add a unit test enforcing version alignment across `package.json`, `plugin.json`,
  `CHANGELOG.md`, and both MCP server modules.

## 0.1.1 - 2026-11-25

- Align `plugin.json` and `package.json` versions (`0.1.1`).

## 0.1.0 - 2026-11-24

- Initial release of `steak-antigravity`, native Antigravity CLI (`agy`) plugin for
  UltraTerm and Steak Pi parity.
- Added provider-neutral USAP stdio MCP bridge (`ultraterm_subagents`,
  `ultraterm_hub`) hosting Steak Pi's USAP extension via headless Pi SDK.
- Added `ultraterm.transcript/1` UI Mode transcript publisher translating
  Antigravity `transcript.jsonl` into append-only Pi session rows with atomic
  rotation on conversation switch.
- Added `SessionStart` shared `~/.pi/agent/AGENTS.md` and pane identity hook,
  `PostToolUse` `.steak-pi/config.json` verify-after-edit hook, `PreToolUse`
  owned-paths and USAP delegation guards, and `Stop` finish guard.
- Added `steak-antigravity` CLI (`doctor`, `install`, `uninstall`, `status`, `identity`)
  and full Node + Python test suites including provider-neutrality enforcement.
