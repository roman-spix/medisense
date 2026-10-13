#!/usr/bin/env bash
# MediSense installer for macOS (tested target: macOS 11 Big Sur on an Intel Mac).
#
#   bash install.sh
#
# What it does, all inside your user account (no admin password, no Homebrew,
# no Xcode, nothing system-wide):
#   1. downloads uv (a small, checksum-verified tool) into ~/.medisense/bin
#   2. uses it to install a private Python 3.12 into ~/.medisense/python
#   3. installs MediSense and its libraries into this folder (.venv)
#   4. puts "MediSense" and "MediSense Demo" launchers on your Desktop
#
# Safe to run again at any time (for example after downloading an update).
# Your recordings live in ~/MediSense and are never touched by this script.
set -euo pipefail

UV_VERSION="0.12.18"
UV_SHA256_X86_64="2e4108f5395397c8bc5d43bf83d3bdbb2d0e92b90d0efa607756be704905fa33"
UV_SHA256_ARM64="cf40e0c6a202190ccd9e0406dcfdd5b2d6668a9a5c779b17948963df32aafe5b"
PY_VERSION="3.12"
PBS_MIRROR="https://github.com/astral-sh/python-build-standalone/releases/download"

TOOLS="$HOME/.medisense"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
die() { printf '\n\033[31mInstallation stopped:\033[0m %s\n' "$*" >&2; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
[ -f pyproject.toml ] && grep -q 'name = "medisense"' pyproject.toml || die "run this script from inside the MediSense folder."

say "Installing MediSense"
OS="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS" = "Darwin" ]; then
  ok "macOS $(sw_vers -productVersion) on $ARCH"
  MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
  if [ "$MAJOR" -lt 11 ] && [ "$(sw_vers -productVersion | cut -d. -f1-2)" != "10.15" ]; then
    die "MediSense needs macOS 10.15 or newer."
  fi
else
  ok "$OS on $ARCH (development use; the supported platform is macOS)"
fi

# A copy that lives in Downloads gets deleted or duplicated easily. Move a
# downloaded (non-git) copy to ~/Applications/MediSense and install there.
case "$APP_DIR" in
  "$HOME/Downloads"/*|"$HOME/Desktop"/*)
    if [ "$OS" = "Darwin" ] && [ ! -d "$APP_DIR/.git" ]; then
      DEST="$HOME/Applications/MediSense"
      say "Moving MediSense to $DEST (so it doesn't live in Downloads)"
      mkdir -p "$DEST"
      # copy everything except a previous environment
      (cd "$APP_DIR" && tar cf - --exclude ./.venv .) | (cd "$DEST" && tar xf -)
      ok "copied; you can delete the downloaded folder afterwards"
      exec bash "$DEST/install.sh"
    fi
    ;;
esac

# ------------------------------------------------------------------ uv
mkdir -p "$TOOLS/bin"
UV="$TOOLS/bin/uv"
if [ ! -x "$UV" ] || [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  if [ "$OS" = "Darwin" ]; then
    case "$ARCH" in
      x86_64) TRIPLE="x86_64-apple-darwin"; SUM="$UV_SHA256_X86_64" ;;
      arm64)  TRIPLE="aarch64-apple-darwin"; SUM="$UV_SHA256_ARM64" ;;
      *) die "unsupported processor: $ARCH" ;;
    esac
    say "Downloading uv $UV_VERSION"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL --retry 3 -o "$TMP/uv.tar.gz" \
      "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$TRIPLE.tar.gz" \
      || die "couldn't download uv. Check the internet connection and try again."
    GOT="$(shasum -a 256 "$TMP/uv.tar.gz" | awk '{print $1}')"
    [ "$GOT" = "$SUM" ] || die "the uv download is damaged (checksum mismatch). Try again."
    tar xzf "$TMP/uv.tar.gz" -C "$TMP"
    mv "$TMP/uv-$TRIPLE/uv" "$UV"
    chmod +x "$UV"
  elif command -v uv >/dev/null 2>&1; then
    ln -sf "$(command -v uv)" "$UV"
  else
    die "install uv first (https://docs.astral.sh/uv/) on this system."
  fi
fi
ok "uv $("$UV" --version | awk '{print $2}')"

# --------------------------------------------------------------- Python
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_CACHE_DIR="$TOOLS/cache"
export UV_NO_CONFIG=1
export UV_PYTHON_PREFERENCE=only-managed  # never pick up the Mac's old system Python
say "Getting Python $PY_VERSION (private to MediSense)"
if ! "$UV" python install "$PY_VERSION" 2>"$TOOLS/python-install.log"; then
  # second try straight from GitHub, in case the default mirror is blocked
  UV_PYTHON_INSTALL_MIRROR="$PBS_MIRROR" "$UV" python install "$PY_VERSION" \
    || die "couldn't install Python. Details: $TOOLS/python-install.log"
fi

# ------------------------------------------------------------ libraries
say "Installing MediSense and its libraries (a minute or two)"
rm -rf .venv
"$UV" venv --quiet --python "$PY_VERSION" .venv
ok "$(.venv/bin/python -V)"
if [ "$OS" = "Darwin" ]; then
  "$UV" pip install --quiet --python .venv/bin/python --require-hashes \
    --only-binary numpy --only-binary cryptography --only-binary cffi --only-binary aiohttp \
    --only-binary pyyaml --only-binary frozenlist --only-binary multidict --only-binary yarl \
    --only-binary propcache --only-binary tibs \
    -r requirements-macos.txt || die "installing libraries failed (see the messages above)."
  "$UV" pip install --quiet --python .venv/bin/python --no-deps -e . || die "installing MediSense failed."
else
  "$UV" pip install --quiet --python .venv/bin/python -e ".[dev]" || die "installing failed."
fi
ok "installed into $APP_DIR/.venv"

# ------------------------------------------------------------ launchers
make_launcher() {  # name, command
  local f="$HOME/Desktop/$1.command"
  cat > "$f" <<LAUNCH
#!/bin/bash
# Opens MediSense. Keep the Terminal window open while you use it.
cd "$APP_DIR" || exit 1
exec "$APP_DIR/.venv/bin/medisense" $2
LAUNCH
  chmod +x "$f"
}
if [ -d "$HOME/Desktop" ]; then
  make_launcher "MediSense" "start"
  make_launcher "MediSense Demo" "demo"
  ok "Desktop launchers: MediSense, MediSense Demo"
fi

# --------------------------------------------------------- final checks
say "Downloading the webcam posture model (optional, ~25 MB)"
.venv/bin/medisense fetch-assets || true

say "Checking the installation"
.venv/bin/medisense doctor || true

cat <<DONE

$(printf '\033[1m')MediSense is installed.$(printf '\033[0m')

  • Double-click "MediSense" on your Desktop to start (it opens in your browser).
  • No board yet? Double-click "MediSense Demo" to try it with a simulated meditator.
  • From Terminal: $APP_DIR/.venv/bin/medisense start
  • Your recordings are kept in: $HOME/MediSense

DONE
