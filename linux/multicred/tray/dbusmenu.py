"""`com.canonical.dbusmenu` server: the panel asks for the layout and sends clicks back.

Every rebuild bumps the revision and emits LayoutUpdated; panels then call
GetLayout again. Item ids stay the same across rebuilds for the same item (same
parent, label and action), so a click that races a rebuild still hits the item the
user saw, or no item at all.

AboutToShow on the root is how the app learns the menu is opening (the Mac's
menuNeedsUpdate): `on_about_to_show(id)` refreshes in the background, and the new
layout follows with LayoutUpdated, so the answer is always "no update needed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gi.repository import Gio, GLib

from .menu_model import MenuItem

INTERFACE = "com.canonical.dbusmenu"
PATH = "/MenuBar"
UNKNOWN_ID = "com.canonical.dbusmenu.Error.UnknownId"
# The app's own symbolic icons (multicred-branch/code/sparkle-symbolic), as an icon-theme
# search path for `icon-name` (the window loads the same directory, ui/widgets.py).
ICON_THEME_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "icons")

XML = """<node><interface name="com.canonical.dbusmenu">
  <method name="GetLayout">
    <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
    <arg type="as" name="propertyNames" direction="in"/>
    <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
    <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
    <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
    <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg type="ai" name="ids" direction="in"/>
    <arg type="ai" name="updatesNeeded" direction="out"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
    <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/>
  </signal>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
</interface></node>"""

_TYPES = {"label": "s", "enabled": "b", "visible": "b", "type": "s", "toggle-type": "s", "toggle-state": "i",
          "children-display": "s", "icon-data": "ay", "icon-name": "s", "shortcut": "aas"}


@dataclass
class _Node:
    item: MenuItem | None
    children: list[int] = field(default_factory=list)


def properties(item: MenuItem | None) -> dict:
    """dbusmenu properties of an item; defaults (enabled, visible, standard type) are omitted."""
    if item is None:  # root
        return {"children-display": "submenu"}
    if item.separator:
        return {"type": "separator"}
    # A single underscore marks a mnemonic in dbusmenu labels; names and e-mails keep theirs.
    props = {"label": item.label.replace("_", "__")}
    if not item.enabled:
        props["enabled"] = False
    if item.toggle:
        props["toggle-type"] = item.toggle
        props["toggle-state"] = 1 if item.toggled else 0
    if item.icon_name:
        props["icon-name"] = item.icon_name
    if item.icon_png:
        props["icon-data"] = item.icon_png
    if item.shortcut:
        props["shortcut"] = [list(item.shortcut)]
    if item.children:
        props["children-display"] = "submenu"
    return props


class DBusMenuServer:
    def __init__(self, connection: Gio.DBusConnection, on_click: Callable[[tuple], None], path: str = PATH,
                 on_about_to_show: Callable[[int], None] | None = None):
        self.connection = connection
        self.on_click = on_click
        self.on_about_to_show = on_about_to_show
        self.path = path
        self.revision = 0
        self._nodes: dict[int, _Node] = {0: _Node(None)}
        self._ids: dict[tuple, int] = {}
        self._next_id = 1
        self._registration = 0

    def register(self) -> None:
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self._registration = self.connection.register_object(self.path, info, self._method, self._property, None)

    def unregister(self) -> None:
        if self._registration:
            self.connection.unregister_object(self._registration)
            self._registration = 0

    def set_menu(self, items: list[MenuItem]) -> None:
        nodes = {0: _Node(None)}
        self._add(nodes, 0, (), items)
        self._nodes = nodes
        self.revision += 1
        if self._registration:
            self.connection.emit_signal(None, self.path, INTERFACE, "LayoutUpdated",
                                        GLib.Variant("(ui)", (self.revision, 0)))

    def item(self, item_id: int) -> MenuItem | None:
        node = self._nodes.get(item_id)
        return node.item if node else None

    # MARK: building

    def _add(self, nodes, parent_id, parent_key, items):
        seen: dict[tuple, int] = {}
        for item in items:
            base = parent_key + ((item.label, item.action, item.separator),)
            occurrence = seen.get(base, 0)
            seen[base] = occurrence + 1
            key = base + (occurrence,)
            item_id = self._ids.get(key)
            if item_id is None:
                item_id = self._ids[key] = self._next_id
                self._next_id += 1
            nodes[item_id] = _Node(item)
            nodes[parent_id].children.append(item_id)
            if item.children:
                self._add(nodes, item_id, key, item.children)

    def _props(self, item_id, names):
        props = properties(self._nodes[item_id].item)
        return {name: GLib.Variant(_TYPES[name], value) for name, value in props.items() if not names or name in names}

    def _layout(self, item_id, depth, names) -> tuple:
        """(id, properties, children) with each child boxed as a `(ia{sv}av)` variant."""
        children = [] if depth == 0 else [GLib.Variant("(ia{sv}av)", self._layout(child, depth - 1, names))
                                          for child in self._nodes[item_id].children]
        return item_id, self._props(item_id, names), children

    # MARK: D-Bus

    def _method(self, connection, sender, path, interface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, names = args
            if parent not in self._nodes:
                return invocation.return_dbus_error(UNKNOWN_ID, f"unknown id {parent}")
            layout = self._layout(parent, depth, set(names))
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, layout)))
        elif method == "GetGroupProperties":
            ids, names = args
            ids = ids or list(self._nodes)
            found = [(i, self._props(i, set(names))) for i in ids if i in self._nodes]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (found,)))
        elif method == "GetProperty":
            item_id, name = args
            props = self._props(item_id, {name}) if item_id in self._nodes else {}
            if name not in props:
                return invocation.return_dbus_error(UNKNOWN_ID, f"no property {name} on {item_id}")
            invocation.return_value(GLib.Variant("(v)", (props[name],)))
        elif method == "Event":
            item_id, event, _data, _timestamp = args
            if item_id not in self._nodes:
                return invocation.return_dbus_error(UNKNOWN_ID, f"unknown id {item_id}")
            invocation.return_value(None)
            self._event(item_id, event)
        elif method == "EventGroup":
            events = args[0]
            errors = [item_id for item_id, *_ in events if item_id not in self._nodes]
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
            for item_id, event, _data, _timestamp in events:
                if item_id in self._nodes:
                    self._event(item_id, event)
        elif method == "AboutToShow":
            item_id = args[0]
            if item_id not in self._nodes:
                return invocation.return_dbus_error(UNKNOWN_ID, f"unknown id {item_id}")
            invocation.return_value(GLib.Variant("(b)", (False,)))
            self._about_to_show([item_id])
        elif method == "AboutToShowGroup":
            ids = args[0]
            errors = [i for i in ids if i not in self._nodes]
            invocation.return_value(GLib.Variant("(aiai)", ([], errors)))
            self._about_to_show([i for i in ids if i in self._nodes])
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _event(self, item_id, event):
        item = self._nodes[item_id].item
        if event == "clicked" and item is not None and item.enabled and item.action is not None:
            self.on_click(item.action)

    def _about_to_show(self, ids):
        if self.on_about_to_show is not None:
            for item_id in ids:
                self.on_about_to_show(item_id)

    def _property(self, connection, sender, path, interface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [ICON_THEME_PATH]),
        }.get(name)
