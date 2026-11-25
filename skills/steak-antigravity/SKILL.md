---
name: steak-antigravity
description: >-
  How Antigravity CLI (agy) cooperates with UltraTerm, Steak Pi, Steak DSH,
  and Claude Code — pane identity, shared operator rules, shared skills, Retex
  recall, ut-todo progress, UTP inbox messages and receipts, USAP delegation,
  verify-after-edit, owned paths, finish guard, and UI Mode transcript sync.
---

# Steak Antigravity workflow

`steak-antigravity` gives Antigravity CLI (`agy`) the same UltraTerm operating surface
that Pi, Steak Pi, Steak DSH, and Claude Code share, using Antigravity's native
plugin mechanisms (`plugin.json`, `mcp_config.json`, `hooks.json`, `rules/`,
and `skills/`).

## Capabilities

- **USAP delegation (`mcp_config.json`)**: Hosts Steak Pi's `ultraterm-subagents`
  extension via stdio MCP (`ultraterm_subagents`, `ultraterm_hub`), honoring
  `~/.config/ultraterm/routing.json` and active Pi/Steak Pi runtimes.
- **UTP & pane identity (`ut-identity`, `utp`)**: Resolves this pane's live
  tmux session and terminal UUID (`utp list`), checks inbox receipts, and sends
  verified peer messages (`utp message --to N --expected-id UUID "..."`).
- **Todo & finish guard (`ut-todo`, `Stop` hook)**: Tracks multi-step phases in
  Steak Pi's todo store and blocks premature stops while solvable items remain
  open.
- **Verify-after-edit (`PostToolUse` hook)**: Runs `.steak-pi/config.json`
  `verify.command` after `write_file` / `edit_file`, debounced with bounded
  output and clean process-group reaping.
- **Owned-paths guard (`PreToolUse` hook)**: When `STEAK_ANTIGRAVITY_OWNED_PATHS` is
  set (colon-separated paths), blocks edits outside the declared scope.
- **UI Mode transcript publisher (`ultraterm.transcript/1`)**: Incrementally
  translates Antigravity's `transcript.jsonl` into `ULTRATERM_UI_TRANSCRIPT_FILE`
  so UltraTerm UI Mode renders live conversation, tool calls, and turn endings.
- **Shared rules & skills**: Injects `~/.pi/agent/AGENTS.md` on `SessionStart`
  and pairs with `~/.gemini/config/skills.json` pointing to `~/.config/agents/skills`.

## Commands

```sh
steak-antigravity doctor       # Verify plugin, USAP runtime, UTP, Retex, skills, UI Mode
steak-antigravity install      # Register steak-antigravity and shared skills in ~/.gemini/config
steak-antigravity status       # Show current pane identity, active runtimes, and wiring
ut-identity            # Print live UltraTerm tmux/terminal identity
ut-todo view           # Inspect active todo plan
utp list               # List UltraTerm terminals and their UUIDs
```
