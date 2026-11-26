# Changelog

## 0.4.0 - 2026-11-26

- Add checksum-verified one-liner installers (`install.sh` for macOS/Linux POSIX `sh`, `install.ps1` for Windows PowerShell 5.1/7+):
  - Downloads `steak-antigravity.tar.gz` and `SHA256SUMS` over HTTPS (`--proto '=https' --tlsv1.2` / `--https-only`, plus `file://` for offline testing) and verifies SHA-256 fail-closed before touching disk.
  - Rejects path-traversal archive entries, symlinks, git checkouts, foreign plugin directories, and unmanaged directories; stages and swaps atomically with unpredictable temp directories into `${STEAK_ANTIGRAVITY_HOME:-$HOME/.local/share/steak-antigravity}` (or `%LOCALAPPDATA%\steak-antigravity` on Windows) with automatic rollback on swap failure or interruption.
  - Installs/updates the `steak-antigravity` CLI launcher in `${STEAK_ANTIGRAVITY_BIN_DIR:-$HOME/.local/bin}` without clobbering unrelated user files, wires Antigravity CLI (`steak-antigravity install`) unless `--no-wire`, checks Python/Node/`agy`/`PATH` prerequisites, and supports `--version`, `--dir`, `--bin-dir`, `--dry-run`, `--no-wire`, `--uninstall`, and `STEAK_ANTIGRAVITY_BASE_URL` for offline testing.
- Update `bin/steak-antigravity` to honor `STEAK_ANTIGRAVITY_BIN_DIR`, replace CLI symlinks atomically, and only remove symlinks pointing to its own installation on `uninstall`.
- Add deterministic release bundle builder (`scripts/build-release.sh`) producing `dist/steak-antigravity.tar.gz` (0 symlinks, 0 `node_modules`, includes `VERSION` + `COMMIT`), `install.sh`, `install.ps1`, and self-verified `SHA256SUMS` while refusing repo subdirectories or non-empty unrelated output folders.
- Add zero-network offline installer test suite (`scripts/test-install.sh`) covering dry-run, fresh install, idempotent upgrade, tampered/missing/malformed/short/duplicate checksum rejection, traversal/absolute/symlink/`.git` archive rejection, non-HTTPS refusal, foreign-plugin and unsafe-dir refusal, swap-failure rollback, `--no-wire`, custom dirs, clean `--uninstall`, and `install.ps1` checks (`pwsh` when available).

## 0.3.0 - 2026-11-25

- Zero-conflict native `/swarm` coexistence (`scripts/delegation_guard.py`, `rules/AGENTS.md`,
  `skills/steak-antigravity/SKILL.md`):
  - Reinforced the existing `hooks.json` `PreToolUse` `invoke_subagent` matcher with an explicit
    tool-name check inside `scripts/delegation_guard.py` that allows native Antigravity `/swarm`
    tools (`swarm`, `post_message`, `read_board`, `swarm_wait`) and safely treats non-object hook
    inputs as `{}` without crashing, while continuing to steer `invoke_subagent` to USAP inside
    UltraTerm.
- Enriched deterministic live USAP Overview (`lib/usap-bridge.mjs`):
  - Upgraded `formatRunView(run)` to gracefully render optional run/task metadata fields when
    present (`dependsOn` / `awaitingDependencies`, `routeFallbacks` / `reviewFallback`,
    `worktree` / `worktreeBranch` / `worktreePath`, `stalled` / `stalledTasks` / `lastProgressAt`
    on active tasks) while leaving compact output unchanged when absent.
- Structured metadata-only USAP event pointers (`lib/usap-bridge.mjs`,
  `scripts/ui_transcript_sync.py`):
  - Extended `usap-events/<pid>.jsonl` with strictly metadata-only
    `{ kind: "complete" | "stall" | "route_hop", runIds, taskCount, doneCount, failedCount, at }`
    records (never carrying untrusted worker output, errors, or free-form text), aligned run-ID
    validation (`^[A-Za-z0-9._:-]{1,64}$`) across writer and reader, emitted `complete` events on
    completion subscriptions and `stall` / `route_hop` events when observed on bridge progress
    updates or `tools/call` run snapshots, and formatted per-run pointers on `PreInvocation`.

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
