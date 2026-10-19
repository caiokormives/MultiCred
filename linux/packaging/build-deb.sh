#!/bin/bash
# Builds dist/multicred_<version>_all.deb for Ubuntu 24.04+ and Pop!_OS 24.04+.
# usage: packaging/build-deb.sh [output-directory]
# Needs dpkg-deb, fakeroot and python3 with pycairo (python3-gi-cairo); librsvg's
# GObject bindings (gir1.2-rsvg-2.0) are used for the PNG icons when present.
# Builds only: it never installs anything.
set -euo pipefail

LINUX="$(cd "$(dirname "$0")/.." && pwd)"
PACKAGING="$LINUX/packaging"
OUT="${1:-$LINUX/dist}"
VERSION="$(cd "$LINUX" && python3 -c 'import multicred; print(multicred.VERSION)')"
APP_ID="app.multicred.MultiCred"

# The changelog's top entry must be this version (lintian and the AppStream release use it).
CHANGELOG_VERSION="$(dpkg-parsechangelog -l "$PACKAGING/changelog" -S Version)"
if [ "$CHANGELOG_VERSION" != "$VERSION" ]; then
    echo "packaging/changelog starts at $CHANGELOG_VERSION, multicred.VERSION is $VERSION" >&2
    exit 1
fi
RELEASE_DATE="$(date -u -d "$(dpkg-parsechangelog -l "$PACKAGING/changelog" -S Date)" +%Y-%m-%d)"

umask 022
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 755 "$STAGE"

# App code, without tests or bytecode.
mkdir -p "$STAGE/usr/lib/multicred"
cp -r "$LINUX/multicred" "$STAGE/usr/lib/multicred/"
find "$STAGE/usr/lib/multicred" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE/usr/lib/multicred" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

# Command.
mkdir -p "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/multicred" <<'EOF'
#!/bin/sh
cd /usr/lib/multicred && exec /usr/bin/python3 -m multicred "$@"
EOF

# Desktop integration: launcher, icons (SVG plus PNGs for docks that want them), AppStream.
install -Dm644 "$PACKAGING/$APP_ID.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$PACKAGING/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
python3 "$PACKAGING/render-icons.py" "$PACKAGING/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor" >/dev/null
mkdir -p "$STAGE/usr/share/metainfo"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$RELEASE_DATE/" "$PACKAGING/$APP_ID.metainfo.xml.in" \
    > "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"

# Debian documentation.
install -Dm644 "$PACKAGING/copyright" "$STAGE/usr/share/doc/multicred/copyright"
gzip -9nc "$PACKAGING/changelog" > "$STAGE/usr/share/doc/multicred/changelog.gz"

# Permissions: 0755 directories and the command, 0644 everything else.
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/multicred"

# Control files: no maintainer scripts (the app's onboarding does the per-user setup).
mkdir -p "$STAGE/DEBIAN"
SIZE="$(du -sk --apparent-size "$STAGE/usr" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$PACKAGING/control.in" > "$STAGE/DEBIAN/control"
(cd "$STAGE" && find usr -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum) > "$STAGE/DEBIAN/md5sums"
chmod 644 "$STAGE/DEBIAN/control" "$STAGE/DEBIAN/md5sums"

mkdir -p "$OUT"
fakeroot dpkg-deb --build --root-owner-group "$STAGE" "$OUT/multicred_${VERSION}_all.deb" >/dev/null
echo "$OUT/multicred_${VERSION}_all.deb"
