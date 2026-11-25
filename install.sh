#!/bin/sh
# Idempotent installer for Steak Antigravity (Antigravity CLI plugin for UltraTerm & Steak Pi).
set -eu
ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
chmod +x "$ROOT/bin/steak-antigravity" "$ROOT"/scripts/*.py
exec python3 -I -B "$ROOT/bin/steak-antigravity" install
