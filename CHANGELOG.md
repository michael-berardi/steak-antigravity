# Changelog

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
