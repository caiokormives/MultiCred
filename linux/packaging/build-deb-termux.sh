#!/bin/bash
# Builds multicred_<version>_all.deb for Termux (Android), into dist/termux/ by default.
# usage: packaging/build-deb-termux.sh [output-directory]
# Needs dpkg-deb and fakeroot. Builds only: it never installs anything.
#
# Termux installs packages under its prefix (/data/data/com.termux/files/usr), so every
# file of the package lives there: the app in $PREFIX/lib/multicred, the command in
# $PREFIX/bin/multicred. Only the terminal interface ships: no window, tray, desktop
# entry, icons or autostart (Android has no GTK), no tests, no bytecode. The members are
# xz-compressed, which Termux's dpkg reads. On the phone: apt install ./multicred_<v>_all.deb
set -euo pipefail

LINUX="$(cd "$(dirname "$0")/.." && pwd)"
PACKAGING="$LINUX/packaging"
OUT="${1:-$LINUX/dist/termux}"
VERSION="$(cd "$LINUX" && python3 -c 'import multicred; print(multicred.VERSION)')"
TERMUX_PREFIX="/data/data/com.termux/files/usr"

umask 022
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 755 "$STAGE"
PREFIX_DIR="$STAGE$TERMUX_PREFIX"

# App code, without tests, bytecode or what only the desktop window and tray use.
mkdir -p "$PREFIX_DIR/lib/multicred"
cp -r "$LINUX/multicred" "$PREFIX_DIR/lib/multicred/"
APP="$PREFIX_DIR/lib/multicred/multicred"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$APP" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
rm -rf "$APP/tray" "$APP/ui/pages" "$APP/ui/icons"
rm -f "$APP/ui/app.py" "$APP/ui/window.py" "$APP/ui/widgets.py" "$APP/ui/model.py"

# Command.
mkdir -p "$PREFIX_DIR/bin"
sed -e "s|@PREFIX@|$TERMUX_PREFIX|g" "$PACKAGING/termux/multicred.in" > "$PREFIX_DIR/bin/multicred"

# Documentation: the license and the Termux README.
install -Dm644 "$PACKAGING/copyright" "$PREFIX_DIR/share/doc/multicred/copyright"
install -Dm644 "$LINUX/README-termux.md" "$PREFIX_DIR/share/doc/multicred/README.md"

# Permissions: 0755 directories and the command, 0644 everything else.
find "$STAGE" -type d -exec chmod 755 {} +
find "$PREFIX_DIR" -type f -exec chmod 644 {} +
chmod 755 "$PREFIX_DIR/bin/multicred"

# Control files: no maintainer scripts (`multicred configurar` does the per-user setup).
mkdir -p "$STAGE/DEBIAN"
SIZE="$(du -sk --apparent-size "$STAGE/data" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$PACKAGING/termux/control.in" > "$STAGE/DEBIAN/control"
(cd "$STAGE" && find data -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum) > "$STAGE/DEBIAN/md5sums"
chmod 644 "$STAGE/DEBIAN/control" "$STAGE/DEBIAN/md5sums"

mkdir -p "$OUT"
fakeroot dpkg-deb -Zxz --build --root-owner-group "$STAGE" "$OUT/multicred_${VERSION}_all.deb" >/dev/null
echo "$OUT/multicred_${VERSION}_all.deb"
