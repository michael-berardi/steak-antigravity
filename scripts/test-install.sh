#!/bin/sh
# Offline installer test harness for install.sh (and install.ps1 when pwsh is available).
# Run after scripts/build-release.sh.
# Uses only temporary HOME directories and local file:// release assets (zero network).
# Usage: scripts/test-install.sh [dist-dir]   (default: dist)
set -eu

cd "$(dirname "$0")/.."
DIST_ARG="${1:-dist}"

pass() { printf 'ok - %s\n' "$1"; }
skip() { printf 'skip - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1" >&2; exit 1; }

DIST=$(cd "$DIST_ARG" 2>/dev/null && pwd -P) || fail "release dir '$DIST_ARG' not found; run scripts/build-release.sh first"
for f in steak-antigravity.tar.gz install.sh install.ps1 SHA256SUMS; do
  [ -f "$DIST/$f" ] || fail "missing release asset: $DIST/$f"
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/steak-antigravity-test.XXXXXX") || fail "cannot create temp dir"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

BASE="file://$DIST"
INSTALLER="$DIST/install.sh"
PS_INSTALLER="$DIST/install.ps1"

# 0. Verify archive structure & metadata.
mkdir "$WORK/inspect"
tar -xzf "$DIST/steak-antigravity.tar.gz" -C "$WORK/inspect" || fail "archive failed to unpack"
VERSION=$(tr -d ' \t\r\n' < "$WORK/inspect/VERSION")
COMMIT=$(tr -d ' \t\r\n' < "$WORK/inspect/COMMIT")
[ -n "$VERSION" ] && [ -n "$COMMIT" ] || fail "archive missing VERSION or COMMIT"
[ -z "$(find "$WORK/inspect" -type l)" ] || fail "archive contains symlinks"
[ -z "$(find "$WORK/inspect" -name node_modules)" ] || fail "archive contains node_modules"
if tar -tzf "$DIST/steak-antigravity.tar.gz" | grep -Eq '^(\./)?(test/|scripts/build-release\.sh|scripts/test-install\.sh)'; then
  fail "archive contains test or build scripts"
fi
pass "archive v$VERSION ($COMMIT) unpacks cleanly with 0 symlinks, 0 node_modules, VERSION + COMMIT"

# 0b. build-release.sh refuses repo subdirs (e.g. lib) and non-empty unrelated folders.
if ./scripts/build-release.sh lib >"$WORK/last.log" 2>&1; then fail "build-release accepted lib"; fi
[ -f lib/usap-bridge.mjs ] || fail "build-release damaged lib"
UNREL_OUT="$WORK/unrelated-out"; mkdir -p "$UNREL_OUT"
printf 'keep\n' > "$UNREL_OUT/keep.txt"
if ./scripts/build-release.sh "$UNREL_OUT" >"$WORK/last.log" 2>&1; then fail "build-release accepted unrelated folder"; fi
[ -f "$UNREL_OUT/keep.txt" ] || fail "build-release deleted unrelated folder"
pass "build-release.sh refuses repo subdirs and non-empty unrelated output folders"

run_inst() {
  h="$1"; shift
  env -i PATH="$PATH" TMPDIR="$WORK" HOME="$h" STEAK_ANTIGRAVITY_BASE_URL="$BASE" \
    sh "$INSTALLER" "$@" >"$WORK/last.log" 2>&1
}

no_staging_leftovers() {
  [ -z "$(find "$1" -mindepth 1 -maxdepth 1 -name '.steak-antigravity.*' 2>/dev/null)" ]
}

# 1. --dry-run (install & uninstall) touches nothing on disk.
H1="$WORK/h1"; mkdir -p "$H1"
run_inst "$H1" --dry-run || { cat "$WORK/last.log" >&2; fail "--dry-run exited non-zero"; }
grep -q '^\[dry-run\] Would install steak-antigravity:' "$WORK/last.log" || fail "--dry-run output missing plan"
[ ! -e "$H1/.local" ] && [ ! -e "$H1/.gemini" ] || fail "--dry-run modified HOME"
run_inst "$H1" --uninstall --dry-run || { cat "$WORK/last.log" >&2; fail "--uninstall --dry-run exited non-zero"; }
grep -q '^\[dry-run\] Would uninstall steak-antigravity:' "$WORK/last.log" || fail "--uninstall --dry-run output missing plan"
[ ! -e "$H1/.local" ] && [ ! -e "$H1/.gemini" ] || fail "--uninstall --dry-run modified HOME"
pass "--dry-run (install and uninstall) prints plan and touches nothing"

# 2. Full install into clean HOME with Antigravity wiring.
H2="$WORK/h2"; mkdir -p "$H2/.gemini/config" "$H2/.gemini/antigravity-cli"
printf '{"entries":[{"path":"/other/plugins"}]}\n' > "$H2/.gemini/config/plugins.json"
printf '{"theme":"dark"}\n' > "$H2/.gemini/antigravity-cli/settings.json"
run_inst "$H2" || { cat "$WORK/last.log" >&2; fail "full install exited non-zero"; }
T2="$H2/.local/share/steak-antigravity"
L2="$H2/.local/bin/steak-antigravity"
[ -f "$T2/.steak-antigravity-managed" ] || fail "missing managed marker"
[ -f "$T2/VERSION" ] && [ -f "$T2/COMMIT" ] || fail "missing VERSION/COMMIT in install dir"
[ -L "$L2" ] && [ "$(readlink "$L2")" = "$T2/bin/steak-antigravity" ] || fail "CLI symlink missing or wrong target"
got_ver=$(HOME="$H2" "$L2" --version) || fail "installed CLI failed --version"
[ "$got_ver" = "$VERSION" ] || fail "installed CLI reported '$got_ver', expected '$VERSION'"
grep -q '"include_only": \[' "$H2/.gemini/config/plugins.json" || fail "plugins.json not wired"
grep -q '"/other/plugins"' "$H2/.gemini/config/plugins.json" || fail "existing plugins.json entry lost"
grep -q 'antigravity_statusline.py' "$H2/.gemini/antigravity-cli/settings.json" || fail "statusLine not wired"
grep -q '"theme": "dark"' "$H2/.gemini/antigravity-cli/settings.json" || fail "existing settings.json key lost"
no_staging_leftovers "$H2/.local/share" || fail "install left staging folder"
pass "full install wires Antigravity, preserves user configs, creates CLI symlink, leaves no staging dirs"

# 3. Idempotent upgrade in place.
printf '0.0.1\n' > "$T2/VERSION"
run_inst "$H2" || { cat "$WORK/last.log" >&2; fail "upgrade exited non-zero"; }
grep -q "Upgraded steak-antigravity from 0.0.1 to $VERSION\." "$WORK/last.log" || { cat "$WORK/last.log" >&2; fail "upgrade message missing"; }
[ "$(tr -d ' \t\r\n' < "$T2/VERSION")" = "$VERSION" ] || fail "VERSION not updated on upgrade"
entries_count=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(1 for e in d["entries"] if e.get("include_only")==["steak-antigravity"]))' "$H2/.gemini/config/plugins.json")
[ "$entries_count" -eq 1 ] || fail "upgrade duplicated plugins.json entry (count=$entries_count)"
no_staging_leftovers "$H2/.local/share" || fail "upgrade left staging folder"
pass "idempotent upgrade 0.0.1 -> $VERSION preserves single plugins.json entry and leaves no staging dirs"

# 4. Tampered, missing, malformed, short, and duplicate SHA256SUMS fail closed before touching disk.
BAD="$WORK/bad"; mkdir -p "$BAD"
cp "$DIST/steak-antigravity.tar.gz" "$DIST/SHA256SUMS" "$BAD/"
printf 'tampered' >> "$BAD/steak-antigravity.tar.gz"
before_commit=$(cat "$T2/COMMIT")
if env -i PATH="$PATH" TMPDIR="$WORK" HOME="$H2" STEAK_ANTIGRAVITY_BASE_URL="file://$BAD" sh "$INSTALLER" >"$WORK/last.log" 2>&1; then
  fail "tampered tarball was accepted"
fi
grep -q "checksum mismatch for steak-antigravity.tar.gz" "$WORK/last.log" || { cat "$WORK/last.log" >&2; fail "checksum mismatch error missing"; }
[ "$(cat "$T2/COMMIT")" = "$before_commit" ] || fail "tampered tarball modified existing install"
no_staging_leftovers "$H2/.local/share" || fail "tampered tarball left staging folder"

H4="$WORK/h4"; mkdir -p "$H4"
for case_name in missing malformed short duplicate; do
  CDIR="$WORK/sum-$case_name"; mkdir -p "$CDIR"
  cp "$DIST/steak-antigravity.tar.gz" "$CDIR/"
  case "$case_name" in
    missing)   printf '0000000000000000000000000000000000000000000000000000000000000000  other.tar.gz\n' > "$CDIR/SHA256SUMS" ;;
    malformed) printf 'zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz  steak-antigravity.tar.gz\n' > "$CDIR/SHA256SUMS" ;;
    short)     printf 'deadbeef  steak-antigravity.tar.gz\n' > "$CDIR/SHA256SUMS" ;;
    duplicate)
      valid_line=$(grep 'steak-antigravity\.tar\.gz' "$DIST/SHA256SUMS" | head -n 1)
      printf '%s\n%s\n' "$valid_line" "$valid_line" > "$CDIR/SHA256SUMS"
      ;;
  esac
  if env -i PATH="$PATH" TMPDIR="$WORK" HOME="$H4" STEAK_ANTIGRAVITY_BASE_URL="file://$CDIR" sh "$INSTALLER" >"$WORK/last.log" 2>&1; then
    fail "SHA256SUMS case '$case_name' was accepted"
  fi
  [ ! -e "$H4/.local" ] && [ ! -e "$H4/.gemini" ] || fail "SHA256SUMS case '$case_name' touched disk"
done
pass "tampered, missing, malformed, short, and duplicate SHA256SUMS all fail closed before touching disk"

# 4b. Malicious archive payloads (../ traversal, absolute path, symlink, embedded .git) fail closed even with valid SHA256SUMS.
H_EVIL="$WORK/h-evil"; mkdir -p "$H_EVIL"
python3 - "$WORK" <<'PY'
import hashlib, io, pathlib, sys, tarfile

work = pathlib.Path(sys.argv[1])

def make_case(name, members):
    d = work / f"evil-{name}"
    d.mkdir(parents=True, exist_ok=True)
    tgz = d / "steak-antigravity.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        for kind, arcname, payload in members:
            ti = tarfile.TarInfo(arcname)
            if kind == "file":
                data = payload.encode()
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                tf.addfile(ti)
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    (d / "SHA256SUMS").write_text(f"{digest}  steak-antigravity.tar.gz\n")

make_case("traversal", [("file", "../escape.txt", "boom")])
make_case("absolute", [("file", "/tmp/escape.txt", "boom")])
make_case("symlink", [("symlink", "badlink", "/etc/passwd")])
make_case("dotgit", [("file", ".git/HEAD", "ref: refs/heads/main\n")])
PY
for evil in traversal absolute symlink dotgit; do
  if env -i PATH="$PATH" TMPDIR="$WORK" HOME="$H_EVIL" STEAK_ANTIGRAVITY_BASE_URL="file://$WORK/evil-$evil" sh "$INSTALLER" >"$WORK/last.log" 2>&1; then
    fail "malicious archive '$evil' was accepted"
  fi
  [ ! -e "$H_EVIL/.local" ] && [ ! -e "$H_EVIL/.gemini" ] || fail "malicious archive '$evil' touched disk"
done
# Non-HTTPS URL rejected fail-closed.
if env -i PATH="$PATH" TMPDIR="$WORK" HOME="$H_EVIL" STEAK_ANTIGRAVITY_BASE_URL="http://example.invalid/rel" sh "$INSTALLER" >"$WORK/last.log" 2>&1; then
  fail "non-HTTPS base URL was accepted"
fi
grep -q "refusing non-HTTPS base URL" "$WORK/last.log" || fail "non-HTTPS refusal message missing"
pass "archive traversal, absolute paths, symlinks, embedded .git, and non-HTTPS URLs are rejected fail-closed"

# 5. --no-wire with custom --dir and --bin-dir.
H5="$WORK/h5"; mkdir -p "$H5"
CUSTOM_DIR="$H5/opt/steak-agy"
CUSTOM_BIN="$H5/custom-bin"
run_inst "$H5" --no-wire --dir "$CUSTOM_DIR" --bin-dir "$CUSTOM_BIN" || { cat "$WORK/last.log" >&2; fail "--no-wire install failed"; }
[ -d "$CUSTOM_DIR" ] && [ -L "$CUSTOM_BIN/steak-antigravity" ] || fail "custom --dir / --bin-dir not created"
[ ! -e "$H5/.gemini" ] || fail "--no-wire created ~/.gemini"
[ ! -e "$H5/.local/bin" ] || fail "--bin-dir still wrote to ~/.local/bin"
[ "$(HOME="$H5" "$CUSTOM_BIN/steak-antigravity" --version)" = "$VERSION" ] || fail "custom bin CLI failed"
pass "--no-wire + custom --dir + --bin-dir installs cleanly without touching ~/.gemini or ~/.local/bin"

# 6. Safety guards: invalid version, --dir $HOME, --dir /, symlink, git checkout, foreign plugin dir, unmanaged dir, foreign bin file.
if run_inst "$H5" --version "../evil"; then fail "invalid --version accepted"; fi
grep -q "invalid version" "$WORK/last.log" || fail "invalid --version error missing"

for bad_dir in "$H5" "/"; do
  if run_inst "$H5" --dir "$bad_dir"; then fail "unsafe --dir '$bad_dir' accepted on install"; fi
  if run_inst "$H5" --dir "$bad_dir" --uninstall; then fail "unsafe --dir '$bad_dir' accepted on uninstall"; fi
done

H6="$WORK/h6"; mkdir -p "$H6/.local/share" "$WORK/precious"
printf 'keep\n' > "$WORK/precious/data.txt"
ln -s "$WORK/precious" "$H6/.local/share/steak-antigravity"
if run_inst "$H6"; then fail "symlinked install dir accepted"; fi
[ -L "$H6/.local/share/steak-antigravity" ] && [ -f "$WORK/precious/data.txt" ] || fail "symlink target harmed"
if run_inst "$H6" --uninstall; then fail "symlinked uninstall dir accepted"; fi
[ -f "$WORK/precious/data.txt" ] || fail "symlink target harmed on uninstall"

H7="$WORK/h7"; mkdir -p "$H7/git-checkout/.git"
printf 'repo\n' > "$H7/git-checkout/keep.txt"
if run_inst "$H7" --dir "$H7/git-checkout"; then fail "git checkout accepted on install"; fi
if run_inst "$H7" --dir "$H7/git-checkout" --uninstall; then fail "git checkout accepted on uninstall"; fi
[ -f "$H7/git-checkout/keep.txt" ] && [ -d "$H7/git-checkout/.git" ] || fail "git checkout harmed"

H_FOREIGN="$WORK/h-foreign"; mkdir -p "$H_FOREIGN/other-plugin"
printf '{"name":"other-plugin","version":"1.0.0"}\n' > "$H_FOREIGN/other-plugin/plugin.json"
printf 'precious-plugin-data\n' > "$H_FOREIGN/other-plugin/data.txt"
if run_inst "$H_FOREIGN" --dir "$H_FOREIGN/other-plugin"; then fail "foreign plugin dir accepted on install"; fi
[ -f "$H_FOREIGN/other-plugin/data.txt" ] && [ -f "$H_FOREIGN/other-plugin/plugin.json" ] || fail "foreign plugin dir harmed"

H8="$WORK/h8"; mkdir -p "$H8/unmanaged"
printf 'important\n' > "$H8/unmanaged/doc.txt"
if run_inst "$H8" --dir "$H8/unmanaged" --uninstall; then fail "unmanaged dir accepted on uninstall"; fi
[ -f "$H8/unmanaged/doc.txt" ] || fail "unmanaged dir harmed on uninstall"

H9="$WORK/h9"; mkdir -p "$H9/.local/bin"
printf '#!/bin/sh\necho custom\n' > "$H9/.local/bin/steak-antigravity"
run_inst "$H9" --no-wire || { cat "$WORK/last.log" >&2; fail "install with foreign bin file failed"; }
grep -q 'echo custom' "$H9/.local/bin/steak-antigravity" || fail "foreign bin file was overwritten"
run_inst "$H9" --no-wire --uninstall || { cat "$WORK/last.log" >&2; fail "uninstall with foreign bin file failed"; }
grep -q 'echo custom' "$H9/.local/bin/steak-antigravity" || fail "foreign bin file was deleted on uninstall"
pass "safety guards protect invalid versions, HOME/root dirs, symlinks, git checkouts, foreign plugins, unmanaged dirs, and foreign bin files"

# 6b. Rollback on failed swap restores previous installation intact and leaves no staging folders.
H_RB="$WORK/h-rollback"; mkdir -p "$H_RB"
run_inst "$H_RB" --no-wire || { cat "$WORK/last.log" >&2; fail "rollback setup install failed"; }
T_RB="$H_RB/.local/share/steak-antigravity"
printf '0.0.1-prev\n' > "$T_RB/VERSION"
FAKE_BIN="$WORK/fake-mv-bin"; mkdir -p "$FAKE_BIN"
REAL_MV=$(command -v mv)
cat > "$FAKE_BIN/mv" <<EOF
#!/bin/sh
case "\$1" in
  *.steak-antigravity.new.*)
    if [ "\$2" = "$T_RB" ]; then
      echo "simulated swap failure" >&2
      exit 1
    fi
    ;;
esac
exec "$REAL_MV" "\$@"
EOF
chmod 755 "$FAKE_BIN/mv"
if env -i PATH="$FAKE_BIN:$PATH" TMPDIR="$WORK" HOME="$H_RB" STEAK_ANTIGRAVITY_BASE_URL="$BASE" \
    sh "$INSTALLER" --no-wire >"$WORK/last.log" 2>&1; then
  fail "simulated swap failure exited 0"
fi
grep -q "previous copy was restored" "$WORK/last.log" || { cat "$WORK/last.log" >&2; fail "rollback message missing"; }
[ "$(tr -d ' \t\r\n' < "$T_RB/VERSION")" = "0.0.1-prev" ] || fail "previous copy was not restored on swap failure"
no_staging_leftovers "$H_RB/.local/share" || fail "rollback left staging or backup folders"
pass "swap failure rolls back previous installation cleanly with zero leftover folders"

# 7. Clean --uninstall removes managed dir, symlink, and unwires Antigravity without touching unrelated user settings.
run_inst "$H2" --uninstall || { cat "$WORK/last.log" >&2; fail "--uninstall exited non-zero"; }
[ ! -e "$T2" ] || fail "--uninstall did not remove managed install dir"
[ ! -L "$L2" ] && [ ! -e "$L2" ] || fail "--uninstall did not remove CLI symlink"
grep -q '"/other/plugins"' "$H2/.gemini/config/plugins.json" || fail "--uninstall removed unrelated plugins.json entry"
if grep -q '"steak-antigravity"' "$H2/.gemini/config/plugins.json"; then
  fail "--uninstall left steak-antigravity entry in plugins.json"
fi
grep -q '"theme": "dark"' "$H2/.gemini/antigravity-cli/settings.json" || fail "--uninstall removed unrelated settings.json key"
if grep -q 'antigravity_statusline.py' "$H2/.gemini/antigravity-cli/settings.json"; then
  fail "--uninstall left statusLine in settings.json"
fi
# Idempotent second uninstall succeeds cleanly.
run_inst "$H2" --uninstall || { cat "$WORK/last.log" >&2; fail "idempotent second --uninstall failed"; }
pass "clean --uninstall removes dir, symlink, and wiring while preserving unrelated user config (and is idempotent)"

# 8. install.ps1 checks: static safety checks always, plus full offline pwsh run when pwsh is installed.
grep -q 'Get-FileHash' "$PS_INSTALLER" || fail "install.ps1 missing Get-FileHash"
grep -q 'Test-OwnExistingInstall' "$PS_INSTALLER" || fail "install.ps1 missing foreign-plugin guard"
if grep -Eq '\$matches[[:space:]]*=' "$PS_INSTALLER"; then
  fail "install.ps1 assigns to built-in \$matches variable"
fi
if command -v pwsh >/dev/null 2>&1; then
  HPS="$WORK/hps"; mkdir -p "$HPS/foreign-plugin"
  printf '{"name":"other-plugin"}\n' > "$HPS/foreign-plugin/plugin.json"
  printf 'keep\n' > "$HPS/foreign-plugin/keep.txt"
  env -i PATH="$PATH" TMPDIR="$WORK" HOME="$HPS" STEAK_ANTIGRAVITY_BASE_URL="$BASE" \
    pwsh -NoProfile -NonInteractive -File "$PS_INSTALLER" -DryRun >"$WORK/last.log" 2>&1 \
    || { cat "$WORK/last.log" >&2; fail "install.ps1 -DryRun failed"; }
  [ ! -e "$HPS/.local" ] || fail "install.ps1 -DryRun touched disk"
  env -i PATH="$PATH" TMPDIR="$WORK" HOME="$HPS" STEAK_ANTIGRAVITY_BASE_URL="$BASE" \
    pwsh -NoProfile -NonInteractive -File "$PS_INSTALLER" -NoWire >"$WORK/last.log" 2>&1 \
    || { cat "$WORK/last.log" >&2; fail "install.ps1 fresh install failed"; }
  [ -f "$HPS/.local/share/steak-antigravity/.steak-antigravity-managed" ] || fail "install.ps1 missing managed marker"
  if env -i PATH="$PATH" TMPDIR="$WORK" HOME="$HPS" STEAK_ANTIGRAVITY_BASE_URL="file://$BAD" \
      pwsh -NoProfile -NonInteractive -File "$PS_INSTALLER" -NoWire >"$WORK/last.log" 2>&1; then
    fail "install.ps1 accepted tampered tarball"
  fi
  if env -i PATH="$PATH" TMPDIR="$WORK" HOME="$HPS" STEAK_ANTIGRAVITY_BASE_URL="$BASE" \
      pwsh -NoProfile -NonInteractive -File "$PS_INSTALLER" -Dir "$HPS/foreign-plugin" -NoWire >"$WORK/last.log" 2>&1; then
    fail "install.ps1 accepted foreign plugin dir"
  fi
  [ -f "$HPS/foreign-plugin/keep.txt" ] || fail "install.ps1 harmed foreign plugin dir"
  env -i PATH="$PATH" TMPDIR="$WORK" HOME="$HPS" STEAK_ANTIGRAVITY_BASE_URL="$BASE" \
    pwsh -NoProfile -NonInteractive -File "$PS_INSTALLER" -NoWire -Uninstall >"$WORK/last.log" 2>&1 \
    || { cat "$WORK/last.log" >&2; fail "install.ps1 -Uninstall failed"; }
  [ ! -e "$HPS/.local/share/steak-antigravity" ] || fail "install.ps1 -Uninstall left managed dir"
  pass "install.ps1 offline pwsh suite passed (dry-run, install, tampered checksum, foreign plugin, uninstall)"
else
  skip "pwsh not installed; ran static safety checks on install.ps1"
fi

printf 'all installer tests passed for v%s\n' "$VERSION"
