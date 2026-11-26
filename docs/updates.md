# Updates

<!-- update-contract: v1 -->

Steak Antigravity tells you when a newer stable release is out and installs it with
one command. Automatic updates are opt-in and off by default.

## The check

- At most once every 24 hours, one anonymous `GET` to this plugin's public GitHub
  "latest release" record. The owner and repository come from `package.json`.
  The User-Agent is `steak-antigravity-update-check`. Nothing else is sent: no ID,
  version, usage or machine data.
- It starts from the agy status line (the interactive TUI only) or after a
  `steak-antigravity` command on a terminal. It runs as a detached, low-priority
  process with a 4-second timeout. It never runs in the `SessionStart` hook.
- It never runs in print or headless mode, in CI (`CI`), or for agent workers
  (`CLAUDECODE`, `ULTRATERM_JOB_ID`, `UT_COORDINATOR_JOB_ID`).
- To turn it off, set `STEAK_ANTIGRAVITY_NO_UPDATE_CHECK=1`, or add
  `"updates": {"checkAutomatically": false}` to `~/.steak-antigravity/config.json`.
- Only stable `x.y.z` releases that are strictly newer count. Drafts, prereleases
  and tags that do not parse are ignored.
- A failed check is recorded with its cause, shown by `update --check`, and not
  retried until the next day.

## The notice

In the agy status line (one line, shown for an hour, at most once a day per version):

```text
steak-antigravity 0.6.0 available (you have 0.5.0) · run: steak-antigravity update
```

After a `steak-antigravity` command on a terminal (stderr, at most once a day per version):

```text
steak-antigravity 0.6.0 available (you have 0.5.0) · run: steak-antigravity update
turn on automatic updates: steak-antigravity update --auto on · skip: steak-antigravity update --skip
```

If you use your own agy status line command, `steak-antigravity install` leaves it
alone. You then see the notice after `steak-antigravity` commands, or with `update --check`.

## Commands

| Command | What it does |
| --- | --- |
| `steak-antigravity update` | Shows current → new and the first lines of the release notes. On a terminal it asks `Install <new> now? [Y/n]`, and after the first install asks once whether to install future updates automatically. Without a terminal it installs without asking. |
| `update --check` | Report only. Exit `0` up to date, `10` update available, `1` check failed (cause printed). |
| `update --skip` | No notice for this version. A newer release notifies again. |
| `update --auto on\|off\|status` | Turn automatic updates on or off, or show the setting. |
| `update --rollback` | Put back the previous version (macOS and Linux). |

`steak-antigravity doctor` includes the update settings and the last error.

## Automatic updates

With `update --auto on`, the daily check installs a new release in the background.
New agy sessions use it. A failed automatic install keeps the current version,
records the cause (`doctor`, `update.log`), and leaves the notice in place.
Each version is tried once.

## Install safety

`update` downloads the release's `install.sh` (or `install.ps1` on Windows) and
`SHA256SUMS`, and verifies the installer's SHA-256 before running it. The installer
then verifies the archive against the same `SHA256SUMS`, swaps it in atomically,
and keeps one rollback copy, `<install dir>.previous`.

Git checkouts are not overwritten: `update` prints the `git pull` command instead.
npm (`npm install -g`) and Homebrew (`brew upgrade`) installs are updated with their
own package manager.

## Files

- `update-check.json` in the cache folder (`~/Library/Caches/steak-antigravity` on
  macOS, `${XDG_CACHE_HOME:-~/.cache}/steak-antigravity` on Linux,
  `%LOCALAPPDATA%\steak-antigravity` on Windows): time of the last check, the latest
  version, the last error, and when the notice was last shown. Mode `0600`.
- `update.log` in the same folder, written only by automatic updates. It is kept
  under 256 KB.
- The `updates` section of `~/.steak-antigravity/config.json`: skipped version,
  automatic updates on or off, and the check switch.
- `<install dir>.previous`: removed once the new version has run, and after 7 days
  at the latest. There is never more than one.

`update --auto off` deletes the log. `steak-antigravity uninstall` (also run by
`install.sh --uninstall`) deletes the cache, the log and the `updates` settings.
Downloads go to a temporary folder that is deleted whether the update succeeds or fails.

## Testing against a local release

`STEAK_ANTIGRAVITY_UPDATE_API` (an `https://`, `file://` or loopback `http://` URL for
the release record) and `STEAK_ANTIGRAVITY_BASE_URL` (an `https://` or `file://`
folder with the release assets) point the check and the download at a local fixture.
