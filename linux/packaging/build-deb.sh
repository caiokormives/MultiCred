#!/bin/bash
# Builds dist/multicred_<version>_all.deb for Ubuntu 24.04+ and Pop!_OS 24.04+.
# usage: packaging/build-deb.sh [output-directory]
set -euo pipefail

LINUX="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$LINUX/dist}"
VERSION="$(cd "$LINUX" && python3 -c 'import multicred; print(multicred.VERSION)')"
APP_ID="app.multicred.MultiCred"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# App code, without tests or bytecode.
mkdir -p "$STAGE/usr/lib/multicred"
cp -r "$LINUX/multicred" "$STAGE/usr/lib/multicred/"
find "$STAGE/usr/lib/multicred" -name __pycache__ -type d -prune -exec rm -rf {} +

# Command.
mkdir -p "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/multicred" <<'EOF'
#!/bin/sh
cd /usr/lib/multicred && exec /usr/bin/python3 -m multicred "$@"
EOF
chmod 755 "$STAGE/usr/bin/multicred"

# Desktop integration.
install -Dm644 "$LINUX/packaging/$APP_ID.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$LINUX/packaging/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"

# Control file.
mkdir -p "$STAGE/DEBIAN"
SIZE="$(du -sk "$STAGE/usr" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$LINUX/packaging/control.in" > "$STAGE/DEBIAN/control"

mkdir -p "$OUT"
fakeroot dpkg-deb --build --root-owner-group "$STAGE" "$OUT/multicred_${VERSION}_all.deb" >/dev/null
echo "$OUT/multicred_${VERSION}_all.deb"
