# Steak Antigravity

**Steak Pi & UltraTerm parity for Google Antigravity CLI (`agy`).**

`steak-antigravity` is a provider-neutral native Antigravity plugin that connects `agy`
to the shared UltraTerm and Steak Pi ecosystem using Antigravity's supported
plugin mechanisms (`plugin.json`, `mcp_config.json`, `hooks.json`, `rules/`,
and `skills/`).

MIT. Zero runtime npm dependencies. By [Pareto Cybernetics](https://paretocybernetics.com).

## What it adds

| Capability | Mechanism | What it does |
| --- | --- | --- |
| **USAP subagents** | `mcp_config.json` + `lib/usap-bridge.mjs` | Hosts Steak Pi's `ultraterm-subagents` extension over stdio MCP (`ultraterm_subagents`, `ultraterm_hub`), honoring `~/.config/ultraterm/routing.json`. Fail-closed stand-in parent; renders an enriched deterministic Overview (DAG dependencies, route-fallback hops, isolated worktree branch/path, and stall indicators) and never executes parent inference. |
| **UltraTerm UI Mode** | `scripts/ui_transcript_sync.py` | Publishes Antigravity `transcript.jsonl` incrementally to `ULTRATERM_UI_TRANSCRIPT_FILE` in `ultraterm.transcript/1` format (mode `0600`, exclusive create, atomic `/new` rotation, `stopReason: "stop"`). |
| **Shared operator rules & skills** | `scripts/session_start.py` + `skills.json` | Injects `~/.pi/agent/AGENTS.md` on `SessionStart` and wires `~/.config/agents/skills` into Antigravity discovery. |
| **UTP peer awareness & USAP events** | `lib/ut_common.py` + `scripts/session_start.py` + `scripts/ui_transcript_sync.py` | Reports live tmux pane & UltraTerm terminal UUID (`utp list`) and surfaces metadata-only USAP `complete`, `stall`, and `route_hop` pointers on `PreInvocation` (from completion subscriptions, progress updates, and `tools/call` run snapshots) without injecting worker text. |
| **Verify-after-edit** | `scripts/verify_after_edit.py` | Runs `.steak-pi/config.json` `verify.command` after `write_file`/`edit_file` with debouncing, `failLimit` (then a 5-minute pause), and process-group reaping. Failures are shown to the agent on its next model call. Runs only in repos you trust (`STEAK_ANTIGRAVITY_ALLOW_VERIFY=1` or `~/.steak-antigravity/trusted-verify-repos.json`). |
| **Owned-paths guard** | `scripts/owned_paths_guard.py` | Enforces `STEAK_ANTIGRAVITY_OWNED_PATHS` on file writes for scoped worker runs. |
| **Finish guard** | `scripts/stop_hook.py` | Checks open Steak Pi todo items and Antigravity's own `update_steps` plan before stopping (bounded to 3 nudges; respects `continuationActive` and `STEAK_ANTIGRAVITY_FINISH_GUARD=0`). |
| **Quota auto-resume** | `lib/auto_resume.py` + `scripts/auto_resume_waiter.py` | Enabled by default inside UltraTerm: on a confirmed quota-exhaustion `Stop` (`ERROR`), waits strictly past the server-reported reset time plus a conservative safety buffer (`>= 60s`, default `65s` + `0..25s` jitter) and issues one identity-bound `utp send` resume. No remote API polling or persistent daemons (one bounded single-shot waiter checking local files/PID every 30s); cancels on user interaction, `HOLD`, session exit, or opt-out (`STEAK_ANTIGRAVITY_AUTO_RESUME=0`). |
| **USAP delegation guard** | `scripts/delegation_guard.py` | Steers `invoke_subagent` to USAP when active inside UltraTerm (`STEAK_ANTIGRAVITY_ALLOW_NATIVE_SUBAGENTS=1` overrides) while leaving explicit Antigravity `/swarm` tools (`swarm`, `post_message`, `read_board`, `swarm_wait`) unblocked. |

## USAP Overview & Native `/swarm` Coexistence

- **Zero-conflict `/swarm` coexistence**: The `PreToolUse` delegation guard matches only `invoke_subagent` and explicitly allows Antigravity's native `/swarm` board tools (`swarm`, `post_message`, `read_board`, `swarm_wait`). Ordinary UltraTerm turns automatically delegate via USAP (`ultraterm_subagents` / `ultraterm_hub`), while explicit user `/swarm` sessions work without interference.
- **Deterministic live Overview & metadata-only event pointers**: `lib/usap-bridge.mjs` passes Steak Pi's USAP tool schemas through directly, formats optional run/task metadata in deterministic Overview rows when present (DAG awaiting state, route hops, worktree branch/path, and active-task stall indicators), and writes strictly metadata-only `{ kind, runIds, taskCount, doneCount, failedCount, at }` pointers to `usap-events/<pid>.jsonl` from completion subscriptions, progress updates, and `tools/call` run snapshots for `PreInvocation` delivery. Worker output is never elevated into hook system messages.

## Quota Auto-Resume

When an interactive `agy` session inside UltraTerm stops on a quota/capacity limit (`terminationReason: ERROR`), `steak-antigravity` reads the official server-reported reset time (from the error payload's ISO timestamp or `Resets in <duration>`, or from a fresh official `statusLine` snapshot in `~/.ultraterm/antigravity/usage.json` when the error confirms quota exhaustion without an inline time) and spawns one bounded, single-shot detached waiter (`scripts/auto_resume_waiter.py`).

- **Strict post-reset wait**: Never fires before `resetAt + buffer` (`buffer >= 60s`, default `65s`) plus random jitter (default `0..25s`). Never polls remote APIs, bypasses limits, rotates accounts, or installs LaunchAgents/persistent daemons (the single-shot waiter sleeps locally up to `maxWaitSeconds` checking local state/PID every 30s, then exits immediately after firing or cancelling).
- **Sanctioned delivery & local checks**: Delivers via identity-bound `utp send --id <terminalId> --expected-id <terminalId>` only for top-level interactive UltraTerm sessions (nested `agy` processes, child/swarm conversations, and non-UltraTerm runs skip cleanly). When `ANTIGRAVITY_LS_ADDRESS` (loopback only) and `ANTIGRAVITY_CSRF_TOKEN` are present in memory, also checks Antigravity CLI's built-in `conversations` skill local API (`product.v1.ConversationService/GetConversation`, with HTTP proxies and redirects disabled; optional via `STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS=0`). State transitions from `scheduled` to `firing` under a file lock before `utp send` runs, so a concurrent `PreInvocation` in that brief sub-second send window cannot cancel an already-dispatched send.
- **Automatic cancellation**: Cancels immediately on manual user activity (`PreInvocation`, transcript growth, or `ConversationService/GetConversation` activity), `/new` (`SessionStart` in the same pane), `USER_CANCELED`, session exit, non-quota `ERROR` stops, waits exceeding `maxWaitSeconds` (default `21600s` / 6 hours), retry cap (`maxRetries`, default `3`), or any active operator `HOLD`:
  - `STEAK_ANTIGRAVITY_HOLD=1` or `ULTRATERM_HOLD=1`
  - Latest user/transcript input starting with `HOLD`, `STOP`, or `PAUSE`
  - Hold handoff marker file at `~/.cache/agent-hold/<conversationId>/hold-handoff.md` or (when `ULTRATERM_JOB_ID` or `UT_COORDINATOR_JOB_ID` is set) `~/.cache/ut-coordinator/<jobId>/hold-handoff.md`
- **Opt-out & configuration**:
  - Disable via `STEAK_ANTIGRAVITY_AUTO_RESUME=0` or `~/.steak-antigravity/config.json`:
    ```json
    {
      "autoResume": {
        "enabled": false,
        "bufferSeconds": 65,
        "jitterMaxSeconds": 25,
        "maxWaitSeconds": 21600,
        "maxRetries": 3
      }
    }
    ```
  - Optional environment overrides: `STEAK_ANTIGRAVITY_AUTO_RESUME_BUFFER_SEC` (min `60`), `STEAK_ANTIGRAVITY_AUTO_RESUME_JITTER_SEC`, `STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_WAIT_SEC`, `STEAK_ANTIGRAVITY_AUTO_RESUME_MAX_RETRIES`, `STEAK_ANTIGRAVITY_AUTO_RESUME_PROMPT`, `STEAK_ANTIGRAVITY_AUTO_RESUME_CHECK_LS=0`.
  - Inspect or cancel pending timers: `steak-antigravity doctor` or `steak-antigravity auto-resume [status|cancel]`.

## Install

```sh
git clone https://github.com/michael-berardi/steak-antigravity.git
cd steak-antigravity
./install.sh
steak-antigravity doctor
```

Or install directly via Antigravity CLI:

```sh
agy plugin install /path/to/steak-antigravity
```

## Provider Neutrality

`steak-antigravity` carries no built-in model IDs, provider preferences, fallback chains,
paid-route grants, or operator/client names. All model routing lives exclusively
in the operator's own `~/.config/ultraterm/routing.json`. A dedicated unit test
(`test/steak-antigravity.test.mjs`) enforces this invariant.

## Testing

```sh
npm test
```
