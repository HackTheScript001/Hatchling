#!/usr/bin/env bash
# Hatchling for Linux and Raspberry Pi OS (64-bit).
#
# One line install (downloads the latest release):
#   curl -fsSL https://raw.githubusercontent.com/HackTheScript001/Hatchling/main/install-linux.sh | bash
# Or from an unpacked release folder:
#   ./install-linux.sh                 install / update
#   ./install-linux.sh --autostart     also start Hatchling full screen when you log in (handy on a Pi with a screen)
#   ./install-linux.sh --uninstall     remove Hatchling (your backups and history in ~/Documents/Hatchling stay)
#
# Installs for your user only: ~/.local/share/hatchling (app), ~/.local/bin/hatchling (command) and a menu entry.
# Hatchling then updates itself from inside the app.
set -euo pipefail

REPO="HackTheScript001/Hatchling"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/hatchling"
BIN="$HOME/.local/bin"
MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ---- uninstall ---------------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$APP" "$BIN/hatchling" "$MENU/hatchling.desktop" "$AUTOSTART/hatchling.desktop"
  say "Hatchling removed. Your backups, history and features are still in ~/Documents/Hatchling."
  exit 0
fi

# ---- run from a release folder (download one if this script was piped from the web) --
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/core/version.py" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -z "$SRC" ]; then
  command -v curl >/dev/null || die "curl is needed: sudo apt install curl"
  TMP="$(mktemp -d)"
  say "Downloading the latest Hatchling..."
  curl -fsSL -o "$TMP/hatchling.tar.gz" "https://github.com/$REPO/releases/latest/download/Hatchling-linux.tar.gz" \
    || die "Couldn't download Hatchling. Check the internet connection."
  tar -xzf "$TMP/hatchling.tar.gz" -C "$TMP"
  exec bash "$(ls -d "$TMP"/hatchling-*/ | head -n1)install-linux.sh" "$@"
fi
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/core/version.py")"
[ -n "$VERSION" ] || die "Couldn't read the Hatchling version - is this a complete release folder?"

# ---- checks ------------------------------------------------------------------------
case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ;;
  armv6l|armv7l) die "This is a 32-bit system. Hatchling needs a 64-bit OS - on a Raspberry Pi 3, 4 or 5, install
'Raspberry Pi OS (64-bit)' with Raspberry Pi Imager. (Qt 6, which Hatchling uses, isn't available for 32-bit.)" ;;
  *) say "Unusual processor ($(uname -m)) - trying anyway." ;;
esac
command -v python3 >/dev/null || die "Python 3 is needed: sudo apt install python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is needed (this system has $(python3 -V))."

say "Installing Hatchling $VERSION"

# ---- system packages Qt needs (Debian, Ubuntu, Raspberry Pi OS, Mint...) -------------
if command -v apt-get >/dev/null; then
  PKGS="python3-venv python3-pip libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libfontconfig1 libdbus-1-3 fonts-dejavu-core"
  MISSING=""
  for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
  if [ -n "$MISSING" ]; then
    say "Installing system packages Hatchling needs:$MISSING (asks for your password)"
    sudo apt-get update -qq && sudo apt-get install -y -qq $MISSING || say "Some packages didn't install - carrying on."
  fi
else
  say "Not a Debian-style system: make sure Python's venv module and Qt's usual libraries (xcb-cursor, xkbcommon-x11,
EGL, OpenGL, fontconfig) are installed."
fi

# ---- Python environment ----------------------------------------------------------------
mkdir -p "$APP/versions" "$BIN" "$MENU"
if [ ! -x "$APP/venv/bin/python" ]; then
  python3 -m venv "$APP/venv" || die "Couldn't create a Python environment (sudo apt install python3-venv)"
fi
"$APP/venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
say "Installing Qt and the serial library (a few minutes the first time)..."
if ! "$APP/venv/bin/python" -m pip install -q -r "$SRC/requirements.txt"; then
  # No PySide6 wheel for this system: use the distribution's own Qt for Python packages instead
  if command -v apt-get >/dev/null; then
    say "Using the system's Qt for Python packages instead..."
    sudo apt-get install -y -qq python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-serial \
      || die "Couldn't install Qt for Python. Please report this with: Hatchling > Report a problem, or on GitHub."
    rm -rf "$APP/venv"
    python3 -m venv --system-site-packages "$APP/venv"
  else
    die "Couldn't install PySide6 (Qt for Python) for this system."
  fi
fi

# ---- the app itself: versions/<version>, 'current' points at it -----------------------------
DEST="$APP/versions/$VERSION"
rm -rf "$DEST.new"
mkdir -p "$DEST.new"
( cd "$SRC" && tar --exclude=.git --exclude=.github --exclude=tests --exclude='__pycache__' -cf - . ) | tar -xf - -C "$DEST.new"
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
ln -sfn "$DEST" "$APP/current.new" && mv -T "$APP/current.new" "$APP/current"

cat > "$BIN/hatchling" <<EOF
#!/bin/sh
exec "$APP/venv/bin/python" "$APP/current/main.py" "\$@"
EOF
chmod +x "$BIN/hatchling"

DESKTOP="[Desktop Entry]
Type=Application
Name=Hatchling
GenericName=Car diagnostics
Comment=Ford-family diagnostics, features and any-car OBD-II
Exec=$BIN/hatchling
Icon=$APP/current/assets/logo.png
Terminal=false
Categories=Utility;Development;
StartupWMClass=hatchling"
printf '%s\n' "$DESKTOP" > "$MENU/hatchling.desktop"
if [ "${1:-}" = "--autostart" ]; then
  mkdir -p "$AUTOSTART"
  printf '%s\n' "$DESKTOP" > "$AUTOSTART/hatchling.desktop"
  say "Hatchling will start by itself when you log in."
fi

# ---- permission to use the USB / Bluetooth adapter ----------------------------------------
NOTE=""
if ! id -nG "$USER" | grep -qw dialout; then
  say "Giving your user permission to use the adapter (asks for your password)..."
  if sudo usermod -aG dialout "$USER"; then
    NOTE="Log out and back in (or restart) once so the adapter permission takes effect."
  fi
fi

# keep the two newest versions, remove older ones
ls -1dt "$APP"/versions/*/ 2>/dev/null | tail -n +3 | xargs -r rm -rf

say "Hatchling $VERSION is installed."
echo "Start it from the applications menu, or type: hatchling"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "(Open a new terminal first so the 'hatchling' command is found.)";; esac
[ -n "$NOTE" ] && echo "$NOTE"
echo "Bluetooth adapters: pair the adapter in Bluetooth settings, then run once:  sudo rfcomm bind 0 <adapter address>"
exit 0
