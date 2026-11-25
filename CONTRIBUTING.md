# Contributing

Small, focused pull requests are welcome.

## Local checks

```sh
git clone https://github.com/michael-berardi/steak-antigravity.git
cd steak-antigravity
npm test
```

Checks run locally; this repository does not use GitHub Actions workflows.

## Ground rules

- Keep zero runtime npm dependencies and rely only on Node built-ins and the Python 3 standard library.
- Keep the plugin provider-neutral: no built-in model IDs, provider preferences, fallback chains, API keys, or operator/client names (`test/steak-antigravity.test.mjs` enforces this).
- Never pass `ULTRATERM_UI_TRANSCRIPT_*` to child worker processes, and preserve fail-closed behavior on hooks and MCP bridges.
- Update `README.md` and `CHANGELOG.md` in the same change whenever user-facing behavior changes.
- Do not commit credentials, session transcripts, or local state files.
