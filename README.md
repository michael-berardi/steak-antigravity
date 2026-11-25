# Steak AGY

**Steak Pi & UltraTerm parity for Google Antigravity CLI (`agy`).**

`steak-agy` is a provider-neutral native Antigravity plugin that connects `agy`
to the shared UltraTerm and Steak Pi ecosystem using Antigravity's supported
plugin mechanisms (`plugin.json`, `mcp_config.json`, `hooks.json`, `rules/`,
and `skills/`).

MIT. Zero runtime npm dependencies. By [Pareto Cybernetics](https://paretocybernetics.com).

## What it adds

| Capability | Mechanism | What it does |
| --- | --- | --- |
| **USAP subagents** | `mcp_config.json` + `lib/usap-bridge.mjs` | Hosts Steak Pi's `ultraterm-subagents` extension over stdio MCP (`ultraterm_subagents`, `ultraterm_hub`), honoring `~/.config/ultraterm/routing.json`. Fail-closed stand-in parent; never executes parent inference. |
| **UltraTerm UI Mode** | `scripts/ui_transcript_sync.py` | Publishes Antigravity `transcript.jsonl` incrementally to `ULTRATERM_UI_TRANSCRIPT_FILE` in `ultraterm.transcript/1` format (mode `0600`, exclusive create, atomic `/new` rotation, `stopReason: "stop"`). |
| **Shared operator rules & skills** | `scripts/session_start.py` + `skills.json` | Injects `~/.pi/agent/AGENTS.md` on `SessionStart` and wires `~/.config/agents/skills` into Antigravity discovery. |
| **UTP peer awareness** | `lib/ut_common.py` + `scripts/session_start.py` | Reports live tmux pane & UltraTerm terminal UUID (`utp list`) and surfaces background USAP completion notices on `PreInvocation`. |
| **Verify-after-edit** | `scripts/verify_after_edit.py` | Runs `.steak-pi/config.json` `verify.command` after `write_file`/`edit_file` with debouncing, `failLimit`, and process-group reaping. |
| **Owned-paths guard** | `scripts/owned_paths_guard.py` | Enforces `STEAK_AGY_OWNED_PATHS` on file writes for scoped worker runs. |
| **Finish guard** | `scripts/stop_hook.py` | Checks open Steak Pi todo items before stopping (bounded to 3 nudges; respects `continuationActive` and `STEAK_AGY_FINISH_GUARD=0`). |
| **USAP delegation guard** | `scripts/delegation_guard.py` | Steers delegation to USAP when active inside UltraTerm (`STEAK_AGY_ALLOW_NATIVE_SUBAGENTS=1` overrides). |

## Install

```sh
git clone https://github.com/michael-berardi/steak-agy.git
cd steak-agy
./install.sh
steak-agy doctor
```

Or install directly via Antigravity CLI:

```sh
agy plugin install /path/to/steak-agy
```

## Provider Neutrality

`steak-agy` carries no built-in model IDs, provider preferences, fallback chains,
paid-route grants, or operator/client names. All model routing lives exclusively
in the operator's own `~/.config/ultraterm/routing.json`. A dedicated unit test
(`test/steak-agy.test.mjs`) enforces this invariant.

## Testing

```sh
npm test
```
