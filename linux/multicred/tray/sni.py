"""`org.kde.StatusNotifierItem`: the icon itself, registered with the panel's watcher.

No watcher on the bus means no tray area (stock GNOME without the AppIndicator
extension): `on_availability(False)` lets the app open its window instead. The
watcher name is watched for its whole life, so the item registers again whenever a
watcher (re)appears, e.g. after GNOME Shell restarts or the extension is enabled.
"""

from __future__ import annotations

import itertools
import os
from typing import Callable

from gi.repository import Gio, GLib

INTERFACE = "org.kde.StatusNotifierItem"
PATH = "/StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

XML = """<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate">
    <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
  </method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll">
    <arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/>
  </method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewAttentionIcon"/>
  <signal name="NewOverlayIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>"""

_instances = itertools.count(1)


class StatusNotifierItem:
    def __init__(self, connection: Gio.DBusConnection, *, item_id: str, title: str, menu_path: str,
                 on_activate: Callable[[], None], on_availability: Callable[[bool], None] | None = None):
        self.connection = connection
        self.item_id = item_id
        self.title = title
        self.menu_path = menu_path
        self.on_activate = on_activate
        self.on_availability = on_availability
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-{next(_instances)}"
        self.available: bool | None = None
        self.status = "Active"
        self.pixmaps: list[tuple[int, int, bytes]] = []
        self.tooltip = (title, "")
        self._registration = 0
        self._watch = 0
        self._owns_name = False

    # MARK: lifecycle

    def start(self) -> None:
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self._registration = self.connection.register_object(PATH, info, self._method, self._property, None)
        try:
            # DO_NOT_QUEUE (4): 1 = primary owner. The bus daemon answers right away.
            reply = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                              "RequestName", GLib.Variant("(su)", (self.bus_name, 4)), None,
                                              Gio.DBusCallFlags.NONE, 5000, None)
            self._owns_name = reply.unpack()[0] == 1
        except GLib.Error:
            self._owns_name = False
        if not self._owns_name:
            self._set_available(False)
            return
        self._watch = Gio.bus_watch_name_on_connection(self.connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
                                                       self._watcher_appeared, self._watcher_vanished)

    def stop(self) -> None:
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0
        if self._registration:
            self.connection.unregister_object(self._registration)
            self._registration = 0
        if self._owns_name and not self.connection.is_closed():
            self.connection.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                 "ReleaseName", GLib.Variant("(s)", (self.bus_name,)), None,
                                 Gio.DBusCallFlags.NONE, -1, None, None)
        self._owns_name = False

    def update(self, *, pixmaps: list[tuple[int, int, bytes]], status: str, tooltip: tuple[str, str]) -> None:
        """Changes what the panel shows; emits only the signals whose data changed."""
        changed = []
        if pixmaps != self.pixmaps:
            self.pixmaps = pixmaps
            changed.append(("NewIcon", None))
        if status != self.status:
            self.status = status
            changed.append(("NewStatus", GLib.Variant("(s)", (status,))))
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            changed.append(("NewToolTip", None))
        if not self._registration:
            return
        for signal, value in changed:
            self.connection.emit_signal(None, PATH, INTERFACE, signal, value)

    # MARK: watcher

    def _watcher_appeared(self, connection, name, owner):
        def registered(conn, result):
            try:
                conn.call_finish(result)
                self._set_available(True)
            except GLib.Error:
                self._set_available(False)

        self.connection.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
                             GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, 5000, None,
                             registered)

    def _watcher_vanished(self, connection, name):
        self._set_available(False)

    def _set_available(self, available: bool) -> None:
        if available == self.available:
            return
        self.available = available
        if self.on_availability:
            self.on_availability(available)

    # MARK: D-Bus

    def _method(self, connection, sender, path, interface, method, params, invocation):
        invocation.return_value(None)
        if method == "Activate":
            self.on_activate()
        # SecondaryActivate (middle click), ContextMenu (the panel draws our dbusmenu
        # itself) and Scroll have nothing to do.

    def _property(self, connection, sender, path, interface, name):
        pixmaps = GLib.Variant("a(iiay)", self.pixmaps)
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.item_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", self.status),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": pixmaps,
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": pixmaps,
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "IconThemePath": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], *self.tooltip)),
            "Menu": GLib.Variant("o", self.menu_path),
            # False: a left click calls Activate (open the window); the menu is on right click.
            "ItemIsMenu": GLib.Variant("b", False),
        }.get(name)
