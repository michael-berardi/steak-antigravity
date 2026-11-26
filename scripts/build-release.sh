#!/bin/sh
# Builds clean, deterministic release assets for HEAD into <out> (default: dist):
#   steak-antigravity.tar.gz  runtime plugin bundle (0 symlinks, 0 node_modules, includes COMMIT + VERSION)
#   install.sh, install.ps1   checksum-verified one-liner installers
#   SHA256SUMS                SHA-256 checksums for all three files above (self-verified)
# Usage: scripts/build-release.sh [out-dir]
set -eu

cd "$(dirname "$0")/.."
ROOT=$(pwd -P)
OUT="${1-dist}"

die() { printf 'build-release: %s\n' "$*" >&2; exit 1; }

# Refuse any unsafe, symlinked, or non-empty output directory containing unrelated files.
case "$OUT" in
  ""|.|./|/|..|../) die "refusing unsafe output folder '$OUT'" ;;
esac
[ ! -L "$OUT" ] || die "refusing symlinked output folder '$OUT'"
if [ -e "$OUT" ] && [ ! -d "$OUT" ]; then
  die "refusing non-directory output path '$OUT'"
fi
if [ -d "$OUT" ]; then
  out_abs=$(cd "$OUT" && pwd -P)
  [ "$out_abs" != "/" ] || die "refusing '/' as output folder"
  case "$ROOT/" in
    "$out_abs"/*) die "refusing '$OUT' (repo root or ancestor) as output folder" ;;
  esac
  # Inside the repo, only allow dist (or dist/*) so source folders like lib/ or scripts/ can never be targeted.
  case "$out_abs" in
    "$ROOT"/*)
      case "$out_abs" in
        "$ROOT/dist"|"$ROOT/dist"/*) ;;
        *) die "refusing repo subdirectory '$OUT' (inside repo, use dist or dist/<subdir>)" ;;
      esac
      ;;
  esac
  # Refuse any existing directory containing files other than the 4 known release outputs.
  for existing in "$OUT"/* "$OUT"/.[!.]* "$OUT"/..?*; do
    [ -e "$existing" ] || [ -L "$existing" ] || continue
    base=$(basename "$existing")
    case "$base" in
      steak-antigravity.tar.gz|install.sh|install.ps1|SHA256SUMS) ;;
      *) die "refusing non-empty output folder '$OUT' containing unrelated entry: $base" ;;
    esac
  done
fi

[ -z "$(git status --porcelain --untracked-files=no)" ] || die "working tree has uncommitted tracked changes; commit first"

COMMIT=$(git rev-parse HEAD)
PKG_VERSION=$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' package.json | head -n 1)
PLUGIN_VERSION=$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' plugin.json | head -n 1)
[ -n "$PKG_VERSION" ] || die "could not read version from package.json"
[ "$PKG_VERSION" = "$PLUGIN_VERSION" ] || die "package.json ($PKG_VERSION) != plugin.json ($PLUGIN_VERSION)"

mkdir -p "$OUT"
rm -f "$OUT/steak-antigravity.tar.gz" "$OUT/install.sh" "$OUT/install.ps1" "$OUT/SHA256SUMS"

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/steak-antigravity-build.XXXXXX") || die "cannot create temp dir"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$STAGE/bundle"

# Export shipped runtime files directly from git HEAD (excludes tests, build/test scripts, installers, dotfiles).
git archive --format=tar HEAD -- \
  package.json plugin.json hooks.json mcp_config.json \
  LICENSE README.md CHANGELOG.md SECURITY.md PRIVACY.md docs \
  assets bin lib rules scripts skills \
  ':(exclude)scripts/build-release.sh' \
  ':(exclude)scripts/test-install.sh' \
  | tar -x -C "$STAGE/bundle" || die "git archive export failed"

printf '%s\n' "$PKG_VERSION" > "$STAGE/bundle/VERSION"
printf '%s\n' "$COMMIT" > "$STAGE/bundle/COMMIT"

# Enforce bundle invariants: 0 symlinks, 0 node_modules, 0 __pycache__/*.pyc.
bad_link=$(find "$STAGE/bundle" -type l -print | head -n 1)
[ -z "$bad_link" ] || die "bundle contains symlink: $bad_link"
bad_nm=$(find "$STAGE/bundle" -name node_modules -print | head -n 1)
[ -z "$bad_nm" ] || die "bundle contains node_modules: $bad_nm"
bad_pyc=$(find "$STAGE/bundle" \( -name __pycache__ -o -name '*.pyc' \) -print | head -n 1)
[ -z "$bad_pyc" ] || die "bundle contains Python bytecode: $bad_pyc"

# Normalize executable permissions on shipped entrypoints.
chmod 755 "$STAGE/bundle/bin/steak-antigravity"
for py in "$STAGE/bundle/scripts"/*.py; do
  [ -f "$py" ] && chmod 755 "$py"
done

# Deterministic tar.gz: sorted paths, normalized mtimes, numeric root owner, gzip -n (no mtime/filename).
( cd "$STAGE/bundle" && find . -mindepth 1 | LC_ALL=C sort | sed 's|^\./||' > "$STAGE/list" )
if tar --version 2>/dev/null | grep -qi gnu; then
  tar -C "$STAGE/bundle" --owner=0 --group=0 --numeric-owner --mtime=@0 --no-recursion \
    -cf "$STAGE/bundle.tar" -T "$STAGE/list" || die "tar failed"
else
  # Normalize mtimes on BSD/macOS tar so repeated builds of the same commit are byte-identical.
  find "$STAGE/bundle" -exec env TZ=UTC touch -t 197001010000 {} +
  COPYFILE_DISABLE=1 tar -C "$STAGE/bundle" --uid 0 --gid 0 --uname root --gname root \
    --no-xattrs --no-recursion -cf "$STAGE/bundle.tar" -T "$STAGE/list" || die "tar failed"
fi
gzip -n < "$STAGE/bundle.tar" > "$OUT/steak-antigravity.tar.gz" || die "gzip failed"

cp install.sh install.ps1 "$OUT/"
chmod 755 "$OUT/install.sh"

(
  cd "$OUT"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum steak-antigravity.tar.gz install.sh install.ps1 > SHA256SUMS
    sha256sum -c SHA256SUMS
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 steak-antigravity.tar.gz install.sh install.ps1 > SHA256SUMS
    shasum -a 256 -c SHA256SUMS
  else
    die "needs sha256sum or shasum"
  fi
)

printf 'built %s for v%s (%s)\n' "$OUT" "$PKG_VERSION" "$(git rev-parse --short HEAD)"
ls -l "$OUT"
