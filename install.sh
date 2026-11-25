#!/bin/sh
# Idempotent installer for Steak AGY (Antigravity CLI plugin for UltraTerm & Steak Pi).
set -eu
ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
chmod +x "$ROOT/bin/steak-agy" "$ROOT"/scripts/*.py
exec python3 -I -B "$ROOT/bin/steak-agy" install
