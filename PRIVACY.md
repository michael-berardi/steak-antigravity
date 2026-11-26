# Privacy

Steak Antigravity sends no telemetry, analytics, crash reports or install IDs.

## Update check

- **What is sent:** one anonymous HTTPS `GET` to
  `https://api.github.com/repos/<owner>/<repo>/releases/latest`, where
  `<owner>/<repo>` comes from `package.json`. The User-Agent is
  `steak-antigravity-update-check`. There are no cookies, tokens, IDs, installed
  version, usage or machine data. GitHub sees the request's IP address, as it
  does for any HTTPS request.
- **When:** at most once every 24 hours. It starts from the interactive agy status
  line or after a `steak-antigravity` command on a terminal. It never runs in the
  `SessionStart` hook, in print or headless runs, in CI, or for agent workers.
- **Turning it off:** set `STEAK_ANTIGRAVITY_NO_UPDATE_CHECK=1`, or add
  `"updates": {"checkAutomatically": false}` to `~/.steak-antigravity/config.json`.
- **Updating:** `steak-antigravity update`, or an automatic update you turned on,
  downloads the release's installer, `SHA256SUMS` and archive from GitHub.

## Local files

- `update-check.json` in the platform cache folder: last check time, latest
  version, last error, and when the notice was last shown (`0600`).
- `update.log` in the same folder, only for automatic updates, kept under 256 KB.
- The `updates` section of `~/.steak-antigravity/config.json`.
- `<install dir>.previous`, one rollback copy, removed once the new version has
  run (7 days at most).

`steak-antigravity update --auto off` deletes the log. `steak-antigravity uninstall`
(also run by `install.sh --uninstall`) deletes the cache, the log and the update
settings. See [docs/updates.md](docs/updates.md) for details.

## Everything else

- Apart from the update check, the hooks, status line, pane activity and MCP
  bridge read and write only local files and talk only to local processes.
- Quota auto-resume (UltraTerm only) can make one loopback HTTP request to the
  local Antigravity CLI to check that the session is still active
  (`STEAK_ANTIGRAVITY_AUTO_RESUME=0` turns the feature off).
- USAP subagents run through Steak Pi with the model routing you configured, so
  your provider receives the prompts and files you send it.
- `install.sh` and `install.ps1` download the release archive and `SHA256SUMS`
  from GitHub over HTTPS.
