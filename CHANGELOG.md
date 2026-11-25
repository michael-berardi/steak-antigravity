# Changelog

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
