# Changelog

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
