# Security Policy

## Scope

`steak-agy` bridges Antigravity CLI (`agy`) with local UltraTerm and Steak Pi
tooling:

- **USAP MCP bridge** (`scripts/usap_mcp.py`, `lib/usap-bridge.mjs`): registers
  a fail-closed stand-in parent provider that never executes inference on the
  parent's behalf. Strips `ULTRATERM_UI_TRANSCRIPT_*` variables before spawning
  workers so workers cannot write to the parent's UI Mode transcript.
- **UI Mode transcript publisher** (`scripts/ui_transcript_sync.py`): follows
  the `ultraterm.transcript/1` contract strictly — exclusive create (`0600`),
  symlink refusal, parent directory ownership/permission validation, atomic
  temp-file rename on conversation rotation, and zero tmux mutation.
- **Verify-after-edit** (`scripts/verify_after_edit.py`): runs only the
  project's declared `.steak-pi/config.json` `verify.command` in an owned
  process group that is always reaped on timeout or exit.
- **Owned-paths guard** (`scripts/owned_paths_guard.py`): resolves real paths
  and blocks writes outside `STEAK_AGY_OWNED_PATHS` when configured.
- **Provider neutrality**: contains no hardcoded model names, API keys,
  provider preferences, or personal paths.

## Reporting a Vulnerability

Please report security issues privately via GitHub Security Advisories on the
repository rather than opening a public issue.
