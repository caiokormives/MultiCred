#!/bin/bash
# Runs the tests that need a display and a session bus, isolated from the real
# desktop: a private Xvfb display and a bare D-Bus daemon (tests/dbus-bare.conf).
# usage: tests/run-ui-tests.sh [unittest args]   (default: discover everything)
set -u
cd "$(dirname "$0")/.." || exit 1
ADDRESS_FILE=$(mktemp)
dbus-daemon --config-file=tests/dbus-bare.conf --fork --print-address=1 --print-pid=1 > "$ADDRESS_FILE"
DBUS_SESSION_BUS_ADDRESS=$(sed -n 1p "$ADDRESS_FILE")
BUS_PID=$(sed -n 2p "$ADDRESS_FILE")
rm -f "$ADDRESS_FILE"
trap 'kill "$BUS_PID" 2>/dev/null' EXIT
export DBUS_SESSION_BUS_ADDRESS MULTICRED_PRIVATE_BUS=1
export GDK_BACKEND=x11 GDK_DEBUG=no-portals ADW_DISABLE_PORTAL=1 GTK_A11Y=none NO_AT_BRIDGE=1 \
       GSK_RENDERER=cairo GIO_USE_VFS=local GSETTINGS_BACKEND=memory
if [ $# -eq 0 ]; then set -- discover -s tests -t .; fi
timeout 300 xvfb-run -a python3 -m unittest "$@"
