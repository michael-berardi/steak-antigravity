#!/bin/sh
# Steak Antigravity installer for macOS and Linux:
#   curl -fsSL https://github.com/michael-berardi/steak-antigravity/releases/latest/download/install.sh | sh
# Downloads steak-antigravity.tar.gz and SHA256SUMS over HTTPS, verifies SHA-256 fail-closed before touching disk,
# stages and swaps atomically into ${STEAK_ANTIGRAVITY_HOME:-$HOME/.local/share/steak-antigravity}
# (keeping one <dir>.previous copy for rollback),
# installs the CLI symlink in ${STEAK_ANTIGRAVITY_BIN_DIR:-$HOME/.local/bin}, and wires Antigravity CLI.
# Never uses sudo or edits shell profiles. Re-running upgrades atomically.
set -eu

# Never let a tool we invoke (curl/wget, python helpers, Node hints) send its own usage analytics.
DO_NOT_TRACK=1; export DO_NOT_TRACK
HOMEBREW_NO_ANALYTICS=1; export HOMEBREW_NO_ANALYTICS
NEXT_TELEMETRY_DISABLED=1; export NEXT_TELEMETRY_DISABLED
HF_HUB_DISABLE_TELEMETRY=1; export HF_HUB_DISABLE_TELEMETRY

REPO="michael-berardi/steak-antigravity"
NAME="steak-antigravity"
ARCHIVE="$NAME.tar.gz"
MANAGED_MARK=".steak-antigravity-managed"
NODE_MIN_MAJOR=22

say() { printf '%s\n' "$*"; }
warn() { printf 'steak-antigravity install: warning: %s\n' "$*" >&2; }
die() { printf 'steak-antigravity install: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --version <vX.Y.Z>   Install a specific release tag (or env STEAK_ANTIGRAVITY_VERSION)
  --dir <path>         Managed install dir (default: ~/.local/share/steak-antigravity, env STEAK_ANTIGRAVITY_HOME)
  --bin-dir <path>     CLI symlink dir (default: ~/.local/bin, env STEAK_ANTIGRAVITY_BIN_DIR)
  --dry-run            Show planned actions without downloading or changing disk
  --no-wire            Install files and CLI symlink without running `steak-antigravity install`
  --uninstall          Unwire Antigravity and remove the managed install dir and CLI symlink
  -h, --help           Show this help

Environment:
  STEAK_ANTIGRAVITY_VERSION   Pin release version (e.g. 0.4.0 or v0.4.0)
  STEAK_ANTIGRAVITY_HOME      Override managed install directory
  STEAK_ANTIGRAVITY_BIN_DIR   Override CLI symlink directory
  STEAK_ANTIGRAVITY_BASE_URL  Override download base URL (https:// or file:// for offline tests)
EOF
}

resolve_abs() {
  raw="$1"
  [ -n "$raw" ] || die "path must not be empty"
  case "$raw" in
    *"
"*) die "path must not contain newlines" ;;
  esac
  case "$raw" in
    /*) abs="$raw" ;;
    *) abs="$(pwd -P)/$raw" ;;
  esac
  while [ "$abs" != "/" ] && [ "${abs%/}" != "$abs" ]; do
    abs="${abs%/}"
  done
  printf '%s' "$abs"
}

validate_target_dir() {
  dir="$1"
  home_abs=$(resolve_abs "$HOME")
  case "$dir" in
    ""|/|"."|".."|"$home_abs")
      die "refusing unsafe install directory: $dir"
      ;;
  esac
  case "/$dir/" in
    */../*|*/./*)
      die "refusing unnormalized install directory: $dir"
      ;;
  esac
}

validate_version() {
  ver="$1"
  [ -n "$ver" ] || return 0
  case "$ver" in
    *..*|*/*|*\\*|*[!0-9A-Za-z._+-]*)
      die "invalid version '$ver' (expected e.g. 0.4.0 or v0.4.0)"
      ;;
    [0-9A-Za-z]*) ;;
    *) die "invalid version '$ver' (expected e.g. 0.4.0 or v0.4.0)" ;;
  esac
}

read_pkg_field() {
  sed -n "s/^[[:space:]]*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -n 1
}

read_version_of() {
  if [ -f "$1/VERSION" ]; then
    tr -d ' \t\r\n' < "$1/VERSION"
  elif [ -f "$1/package.json" ]; then
    read_pkg_field "$1/package.json" version
  fi
}

is_own_existing_install() {
  dir="$1"
  if [ -f "$dir/$MANAGED_MARK" ]; then
    return 0
  fi
  # Allow upgrading a marker-less older steak-antigravity install only when both manifests identify it as ours.
  if [ -f "$dir/plugin.json" ] && [ -f "$dir/package.json" ] && [ -f "$dir/bin/$NAME" ]; then
    p_name=$(read_pkg_field "$dir/plugin.json" name)
    k_name=$(read_pkg_field "$dir/package.json" name)
    if [ "$p_name" = "$NAME" ] && [ "$k_name" = "$NAME" ]; then
      return 0
    fi
  fi
  return 1
}

check_node_hint() {
  if ! command -v node >/dev/null 2>&1; then
    say "Note: Node ${NODE_MIN_MAJOR}+ is not on PATH (optional for hooks, required for the USAP MCP bridge)."
    return 0
  fi
  nver=$(node -p 'process.versions.node' 2>/dev/null || true)
  nmajor=${nver%%.*}
  case "$nmajor" in
    ''|*[!0-9]*) ;;
    *)
      if [ "$nmajor" -lt "$NODE_MIN_MAJOR" ]; then
        say "Note: Node $nver found; Node ${NODE_MIN_MAJOR}+ is required for the USAP MCP bridge."
      fi
      ;;
  esac
}

uninstall_managed() {
  target="$1"
  bin_dir="$2"
  dry_run="$3"
  no_wire="$4"
  link="$bin_dir/$NAME"

  if [ "$dry_run" = "1" ]; then
    say "[dry-run] Would uninstall $NAME:"
    if [ "$no_wire" = "0" ]; then
      say "  unwire:  $target/bin/$NAME uninstall (updates ~/.gemini/config/plugins.json and statusLine)"
    else
      say "  unwire:  skipped (--no-wire)"
    fi
    say "  symlink: remove $link (only if pointing to $target/bin/$NAME)"
    say "  dir:     remove $target and $target.previous (only if managed by this installer)"
    return 0
  fi

  target_real=""
  if [ -d "$target" ] && [ ! -L "$target" ]; then
    target_real=$(cd "$target" 2>/dev/null && pwd -P || true)
  fi

  if [ -L "$target" ]; then
    die "$target is a symbolic link; refusing to remove it"
  fi
  if [ -e "$target" ]; then
    [ -d "$target" ] || die "$target is not a directory; refusing to remove it"
    [ ! -e "$target/.git" ] || die "$target is a git checkout; refusing to remove it"
    [ -f "$target/$MANAGED_MARK" ] || die "$target was not created by this installer (missing $MANAGED_MARK); refusing to remove it"
    [ -f "$target/plugin.json" ] && [ -f "$target/bin/$NAME" ] || die "$target does not look like a $NAME installation; refusing to remove it"
    unsafe=$(find "$target" \( -name .git -o -type l \) -print 2>/dev/null | head -n 1)
    [ -z "$unsafe" ] || die "$target contains a symlink or .git entry ($unsafe); refusing to remove it"

    if [ "$no_wire" = "0" ]; then
      if command -v python3 >/dev/null 2>&1; then
        STEAK_ANTIGRAVITY_BIN_DIR="$bin_dir" python3 -I -B "$target/bin/$NAME" uninstall \
          || die "unwire failed; $target was left untouched"
      else
        warn "python3 not found on PATH; skipped Antigravity config unwiring"
      fi
    fi
  fi

  if [ -L "$link" ]; then
    ltarget=$(readlink "$link" 2>/dev/null || true)
    if [ "$ltarget" = "$target/bin/$NAME" ] || { [ -n "$target_real" ] && [ "$ltarget" = "$target_real/bin/$NAME" ]; }; then
      rm -f "$link"
    else
      warn "$link points to ${ltarget:-unknown} (not $target/bin/$NAME); leaving it alone"
    fi
  elif [ -e "$link" ]; then
    warn "$link is not a symlink; leaving it alone"
  fi

  if [ -d "$target" ]; then
    rm -rf "$target"
    say "Removed $NAME from $target."
  else
    say "$NAME is not installed at $target."
  fi
  if [ -d "$target.previous" ] && [ ! -L "$target.previous" ] && [ -f "$target.previous/$MANAGED_MARK" ]; then
    rm -rf "$target.previous"
  fi
}

main() {
  VERSION_PIN="${STEAK_ANTIGRAVITY_VERSION:-}"
  TARGET_RAW="${STEAK_ANTIGRAVITY_HOME:-}"
  BIN_RAW="${STEAK_ANTIGRAVITY_BIN_DIR:-}"
  BASE_OVERRIDE="${STEAK_ANTIGRAVITY_BASE_URL:-}"
  DRY_RUN=0
  NO_WIRE=0
  UNINSTALL=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --version)
        [ $# -ge 2 ] || die "--version requires a value"
        VERSION_PIN="$2"
        shift 2
        ;;
      --version=*)
        VERSION_PIN="${1#--version=}"
        [ -n "$VERSION_PIN" ] || die "--version requires a value"
        shift
        ;;
      --dir)
        [ $# -ge 2 ] || die "--dir requires a path"
        TARGET_RAW="$2"
        shift 2
        ;;
      --dir=*)
        TARGET_RAW="${1#--dir=}"
        [ -n "$TARGET_RAW" ] || die "--dir requires a path"
        shift
        ;;
      --bin-dir)
        [ $# -ge 2 ] || die "--bin-dir requires a path"
        BIN_RAW="$2"
        shift 2
        ;;
      --bin-dir=*)
        BIN_RAW="${1#--bin-dir=}"
        [ -n "$BIN_RAW" ] || die "--bin-dir requires a path"
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --no-wire)
        NO_WIRE=1
        shift
        ;;
      --uninstall)
        UNINSTALL=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1 (run with --help for usage)"
        ;;
    esac
  done

  [ -n "${HOME:-}" ] || die "HOME is not set; set HOME to your home folder and run again"

  VERSION_PIN="${VERSION_PIN#v}"
  validate_version "$VERSION_PIN"

  TARGET=$(resolve_abs "${TARGET_RAW:-$HOME/.local/share/$NAME}")
  BIN_DIR=$(resolve_abs "${BIN_RAW:-$HOME/.local/bin}")
  validate_target_dir "$TARGET"

  if [ "$UNINSTALL" = "1" ]; then
    uninstall_managed "$TARGET" "$BIN_DIR" "$DRY_RUN" "$NO_WIRE"
    return 0
  fi

  if [ -n "$BASE_OVERRIDE" ]; then
    BASE="${BASE_OVERRIDE%/}"
  elif [ -n "$VERSION_PIN" ]; then
    BASE="https://github.com/$REPO/releases/download/v$VERSION_PIN"
  else
    BASE="https://github.com/$REPO/releases/latest/download"
  fi

  case "$BASE" in
    https://*|file://*) ;;
    *) die "refusing non-HTTPS base URL: $BASE (only https:// or file:// is allowed)" ;;
  esac

  if [ "$DRY_RUN" = "1" ]; then
    say "[dry-run] Would install $NAME:"
    say "  archive:  $BASE/$ARCHIVE"
    say "  sums:     $BASE/SHA256SUMS"
    say "  dir:      $TARGET"
    say "  symlink:  $BIN_DIR/$NAME -> $TARGET/bin/$NAME"
    if [ "$NO_WIRE" = "1" ]; then
      say "  wire:     skipped (--no-wire)"
    else
      say "  wire:     python3 -I -B $TARGET/bin/$NAME install"
    fi
    return 0
  fi

  if command -v curl >/dev/null 2>&1; then
    fetch() {
      case "$1" in
        https://*) curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1" || die "download failed: $1" ;;
        file://*)  curl -fsSL --proto '=file' -o "$2" "$1" || die "download failed: $1" ;;
        *) die "refusing non-HTTPS URL: $1" ;;
      esac
    }
  elif command -v wget >/dev/null 2>&1; then
    fetch() {
      case "$1" in
        https://*) wget --https-only -q -O "$2" "$1" || die "download failed: $1" ;;
        file://*)  cp -- "${1#file://}" "$2" || die "download failed: $1" ;;
        *) die "refusing non-HTTPS URL: $1" ;;
      esac
    }
  else
    die "needs curl or wget; install curl with your package manager and run again"
  fi
  command -v tar >/dev/null 2>&1 || die "needs tar; install tar with your package manager and run again"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256_file() { sha256sum "$1" | awk '{ print tolower($1) }'; }
  elif command -v shasum >/dev/null 2>&1; then
    sha256_file() { shasum -a 256 "$1" | awk '{ print tolower($1) }'; }
  else
    die "needs sha256sum or shasum to verify the download; install coreutils and run again"
  fi

  TMP=$(mktemp -d "${TMPDIR:-/tmp}/steak-antigravity-install.XXXXXX") || die "cannot create a temporary folder"
  STAGE=""
  BACKUP_HOLDER=""
  BACKUP=""
  SWAP_PENDING=0
  LINK_TMP_DIR=""
  cleanup() {
    if [ "$SWAP_PENDING" = "1" ] && [ -n "$BACKUP" ] && [ -d "$BACKUP" ] && [ ! -e "$TARGET" ]; then
      if mv "$BACKUP" "$TARGET" 2>/dev/null; then
        SWAP_PENDING=0
      else
        warn "interrupted during swap; previous copy remains at $BACKUP"
      fi
    fi
    if [ "$SWAP_PENDING" = "0" ] && [ -n "$BACKUP_HOLDER" ]; then
      rm -rf "$BACKUP_HOLDER" 2>/dev/null || true
    fi
    if [ -n "$STAGE" ]; then
      rm -rf "$STAGE" 2>/dev/null || true
    fi
    if [ -n "$LINK_TMP_DIR" ]; then
      rm -rf "$LINK_TMP_DIR" 2>/dev/null || true
    fi
    rm -rf "$TMP" 2>/dev/null || true
  }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  verify_sha256() {
    file="$1"
    name="$2"
    sums="$3"
    matches=$(awk -v n="$name" '
      {
        sub(/\r$/, "")
        if (NF < 2) next
        f = $2
        sub(/^\*/, "", f)
        if (f == n) print tolower($1)
      }
    ' "$sums")
    count=$(printf '%s\n' "$matches" | awk 'NF { c++ } END { print c + 0 }')
    [ "$count" -eq 1 ] || die "expected 1 checksum entry for $name in SHA256SUMS (found $count); nothing was installed"
    want=$(printf '%s\n' "$matches" | head -n 1)
    case "$want" in
      *[!0-9a-f]*) die "malformed SHA-256 checksum for $name; nothing was installed" ;;
    esac
    [ "${#want}" -eq 64 ] || die "malformed SHA-256 checksum length for $name; nothing was installed"
    got=$(sha256_file "$file")
    [ "$got" = "$want" ] || die "checksum mismatch for $name (expected $want, got $got); nothing was installed"
  }

  say "Downloading $NAME..."
  fetch "$BASE/$ARCHIVE" "$TMP/$ARCHIVE"
  fetch "$BASE/SHA256SUMS" "$TMP/SHA256SUMS"
  verify_sha256 "$TMP/$ARCHIVE" "$ARCHIVE" "$TMP/SHA256SUMS"

  # Fail closed on unsafe archive paths before extracting anything.
  tar -tzf "$TMP/$ARCHIVE" > "$TMP/listing" || die "cannot read archive listing; nothing was installed"
  [ -s "$TMP/listing" ] || die "archive is empty; nothing was installed"
  if awk '
    {
      sub(/\r$/, "")
      if ($0 ~ /^\//) { print $0; exit 1 }
      n = split($0, parts, "/")
      for (i = 1; i <= n; i++) {
        if (parts[i] == "..") { print $0; exit 1 }
      }
    }
  ' "$TMP/listing" >/dev/null; then
    :
  else
    die "archive contains unsafe path traversal; nothing was installed"
  fi

  mkdir "$TMP/src"
  tar -xzf "$TMP/$ARCHIVE" -C "$TMP/src" || die "cannot unpack archive; nothing was installed"

  unsafe_new=$(find "$TMP/src" \( -type l -o -name .git \) -print 2>/dev/null | head -n 1)
  [ -z "$unsafe_new" ] || die "archive contains forbidden symlink or .git entry ($unsafe_new); nothing was installed"

  for req in package.json plugin.json "bin/$NAME" VERSION; do
    [ -f "$TMP/src/$req" ] || die "archive is missing required file $req; nothing was installed"
  done
  got_name=$(read_pkg_field "$TMP/src/package.json" name)
  [ "$got_name" = "$NAME" ] || die "archive package name is '${got_name:-empty}', expected $NAME; nothing was installed"
  VERSION=$(tr -d ' \t\r\n' < "$TMP/src/VERSION")
  PKG_VERSION=$(read_pkg_field "$TMP/src/package.json" version)
  [ -n "$VERSION" ] && [ "$VERSION" = "$PKG_VERSION" ] || die "archive VERSION ('$VERSION') does not match package.json ('$PKG_VERSION'); nothing was installed"

  chmod 755 "$TMP/src/bin/$NAME"
  for py in "$TMP/src/scripts"/*.py; do
    [ -f "$py" ] && chmod 755 "$py"
  done
  printf 'managed-by=%s-installer\nversion=%s\n' "$NAME" "$VERSION" > "$TMP/src/$MANAGED_MARK"

  # Inspect existing target before touching disk.
  if [ -L "$TARGET" ]; then
    link_dest=$(readlink "$TARGET" 2>/dev/null || true)
    die "$TARGET is a symbolic link to ${link_dest:-another folder}; refusing to overwrite"
  fi
  if [ -e "$TARGET" ] && [ ! -d "$TARGET" ]; then
    die "$TARGET exists and is not a directory; refusing to overwrite"
  fi
  if [ -e "$TARGET/.git" ]; then
    die "$TARGET is a git checkout; refusing to overwrite (update it with git instead)"
  fi
  if [ -d "$TARGET" ]; then
    unsafe_old=$(find "$TARGET" \( -name .git -o -type l \) -print 2>/dev/null | head -n 1)
    [ -z "$unsafe_old" ] || die "$TARGET contains a symlink or .git entry ($unsafe_old); refusing to overwrite"
    if ! is_own_existing_install "$TARGET"; then
      die "$TARGET already exists and is not a $NAME installation; choose another --dir"
    fi
  fi

  PREVIOUS=""
  if [ -d "$TARGET" ]; then
    PREVIOUS=$(read_version_of "$TARGET" || true)
  fi

  TARGET_PARENT=$(dirname "$TARGET")
  mkdir -p "$TARGET_PARENT" || die "cannot create $TARGET_PARENT"
  STAGE=$(mktemp -d "$TARGET_PARENT/.$NAME.new.XXXXXX") || die "cannot create staging folder in $TARGET_PARENT"
  cp -pR "$TMP/src/." "$STAGE/" || die "cannot stage release into $TARGET_PARENT; nothing was changed"
  chmod 755 "$STAGE"

  if [ -d "$TARGET" ]; then
    BACKUP_HOLDER=$(mktemp -d "$TARGET_PARENT/.$NAME.old.XXXXXX") || die "cannot create backup folder in $TARGET_PARENT"
    BACKUP="$BACKUP_HOLDER/prev"
    SWAP_PENDING=1
    mv "$TARGET" "$BACKUP" || { SWAP_PENDING=0; die "cannot move previous install aside at $TARGET; nothing was changed"; }
    if ! mv "$STAGE" "$TARGET"; then
      if mv "$BACKUP" "$TARGET"; then
        SWAP_PENDING=0
        die "cannot move staged release into $TARGET; previous copy was restored"
      else
        die "swap failed and rollback to $TARGET failed; previous copy is preserved at $BACKUP"
      fi
    fi
    STAGE=""
    SWAP_PENDING=0
    # Keep exactly one rollback copy for `steak-antigravity update --rollback`. The new version
    # removes it after its first successful run (or after 7 days).
    PREV_DIR="$TARGET.previous"
    if [ -L "$PREV_DIR" ] || { [ -e "$PREV_DIR" ] && [ ! -f "$PREV_DIR/$MANAGED_MARK" ]; }; then
      warn "$PREV_DIR is not a $NAME rollback copy; leaving it alone (no rollback copy kept)"
    else
      [ -f "$BACKUP/$MANAGED_MARK" ] || printf 'managed-by=%s-installer\nversion=%s\n' "$NAME" "$PREVIOUS" > "$BACKUP/$MANAGED_MARK"
      if rm -rf "$PREV_DIR" && mv "$BACKUP" "$PREV_DIR"; then
        touch "$PREV_DIR"
      else
        warn "could not keep a rollback copy at $PREV_DIR"
      fi
    fi
    if ! rm -rf "$BACKUP_HOLDER"; then
      warn "could not remove temporary backup folder $BACKUP_HOLDER"
    fi
    BACKUP_HOLDER=""
    BACKUP=""
  else
    mv "$STAGE" "$TARGET" || die "cannot move staged release into $TARGET; nothing was changed"
    STAGE=""
  fi

  # Install or atomically update CLI symlink in BIN_DIR without clobbering user files.
  mkdir -p "$BIN_DIR" || die "cannot create $BIN_DIR"
  LINK="$BIN_DIR/$NAME"
  LINK_WRITTEN=0
  if [ -L "$LINK" ] || [ ! -e "$LINK" ]; then
    LINK_TMP_DIR=$(mktemp -d "$BIN_DIR/.$NAME.link.XXXXXX") || die "cannot create temp dir in $BIN_DIR"
    ln -s "$TARGET/bin/$NAME" "$LINK_TMP_DIR/$NAME" || die "cannot create symlink in $BIN_DIR"
    mv -f "$LINK_TMP_DIR/$NAME" "$LINK" || die "cannot update symlink $LINK"
    rmdir "$LINK_TMP_DIR" 2>/dev/null || rm -rf "$LINK_TMP_DIR"
    LINK_TMP_DIR=""
    LINK_WRITTEN=1
  else
    warn "$LINK exists and is not a symlink; leaving it untouched"
  fi

  # Wire Antigravity CLI unless --no-wire.
  WIRED=0
  if [ "$NO_WIRE" = "1" ]; then
    say "Skipped Antigravity wiring (--no-wire)."
  elif command -v python3 >/dev/null 2>&1; then
    STEAK_ANTIGRAVITY_BIN_DIR="$BIN_DIR" STEAK_ANTIGRAVITY_SKIP_BIN_LINK=1 \
      python3 -I -B "$TARGET/bin/$NAME" install \
      || die "$NAME install failed; files are installed at $TARGET"
    WIRED=1
  else
    warn "python3 not found on PATH; skipped Antigravity wiring. Install Python 3, then run: $TARGET/bin/$NAME install"
  fi

  check_node_hint

  if [ -n "$PREVIOUS" ]; then
    say "Upgraded $NAME from $PREVIOUS to $VERSION."
  else
    say "Installed $NAME $VERSION."
  fi
  say "Install dir: $TARGET"
  if [ "$LINK_WRITTEN" = "1" ]; then
    say "CLI symlink: $LINK"
  fi

  case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *) say "Add to PATH: export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac

  if [ "$NO_WIRE" = "1" ]; then
    say "Next: $NAME install && $NAME doctor"
  elif ! command -v agy >/dev/null 2>&1; then
    say "Next: install Antigravity CLI (agy), then run: $NAME doctor"
  elif [ "$WIRED" = "1" ]; then
    say "Next: $NAME doctor"
  fi
}

main "$@"
