"""Glue between AppModel and the two D-Bus objects: every model change rebuilds the
menu and the icon; clicks become model calls or window requests."""

from __future__ import annotations

from datetime import tzinfo
from typing import Callable

from gi.repository import Gio

from .. import APP_ID
from .dbusmenu import PATH as MENU_PATH
from .dbusmenu import DBusMenuServer
from .icon import appearance, profile_png, sni_pixmaps
from .menu_model import build_menu
from .sni import StatusNotifierItem


class TrayController:
    def __init__(self, model, *, on_open: Callable[[str], None], on_quit: Callable[[], None],
                 on_error: Callable[[str, str], None] | None = None,
                 on_availability: Callable[[bool], None] | None = None,
                 connection: Gio.DBusConnection | None = None, tz: tzinfo | None = None):
        self.model = model
        self.on_open = on_open
        self.on_quit = on_quit
        self.on_error = on_error
        self.on_availability = on_availability
        self.connection = connection
        self.tz = tz
        self.menu: DBusMenuServer | None = None
        self.item: StatusNotifierItem | None = None
        self._running = False

    @property
    def available(self) -> bool | None:
        """None until the watcher answered; False = no tray area on this desktop."""
        return self.item.available if self.item else None

    @property
    def bus_name(self) -> str | None:
        return self.item.bus_name if self.item else None

    def start(self) -> None:
        """Raises GLib.Error when there is no session bus at all."""
        connection = self.connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.menu = DBusMenuServer(connection, self.handle)
        self.item = StatusNotifierItem(connection, item_id=APP_ID, title="MultiCred", menu_path=MENU_PATH,
                                       on_activate=lambda: self.on_open("main"),
                                       on_availability=self._availability_changed)
        self.menu.register()
        self._running = True
        self.refresh()  # icon and menu are ready before the panel can ask
        self.model.add_listener(self.refresh)
        self.item.start()

    def stop(self) -> None:
        self._running = False
        if self.item:
            self.item.stop()
        if self.menu:
            self.menu.unregister()

    def refresh(self) -> None:
        if not self._running:
            return
        model = self.model
        self.menu.set_menu(build_menu(model.config, model.state, inconsistent=model.inconsistent,
                                      switching=model.switching, test_mode=model.is_test_mode, tz=self.tz,
                                      icon=profile_png))
        profile = model.active_profile
        color, letter = appearance(profile)
        title = f"MultiCred · {profile.name}" if profile else "MultiCred"
        self.item.update(pixmaps=sni_pixmaps(color, letter, alert=model.inconsistent),
                         status="NeedsAttention" if model.inconsistent else "Active",
                         tooltip=(title, "Inconsistente — abra Verificar" if model.inconsistent else ""))

    def handle(self, action: tuple) -> None:
        kind, value = action
        if kind == "profile":
            profile = self.model.config.profile(value) if self.model.config else None
            name = profile.name if profile else "o perfil"
            self.model.switch_profile(value, self._reporting(f"Não consegui trocar para {name}"))
        elif kind == "claude":
            self.model.switch_claude_only(value, self._reporting("Não consegui trocar a conta Claude"))
        elif kind == "open":
            self.on_open(value)
        elif kind == "quit":
            self.on_quit()

    def _reporting(self, title: str):
        def done(_result, error):
            if error is not None and self.on_error:
                from ..ui.messages import describe_switch_error
                self.on_error(title, describe_switch_error(error))
        return done

    def _availability_changed(self, available: bool) -> None:
        if self.on_availability:
            self.on_availability(available)
