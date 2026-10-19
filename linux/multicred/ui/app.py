"""The GTK application: one main window and the tray icon.

The app lives in the tray: closing the window keeps it running (Gio.Application.hold)
while a tray area exists. Without one (stock GNOME without the AppIndicator
extension) the window is the only way in, so it opens with a banner explaining why
and closing it quits."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from .. import APP_ID  # noqa: E402
from .model import AppModel  # noqa: E402
from .window import NO_TRAY_TEXT, MainWindow  # noqa: E402,F401


class MultiCredApplication(Adw.Application):
    def __init__(self, model: AppModel, background: bool = False,
                 flags: Gio.ApplicationFlags = Gio.ApplicationFlags.DEFAULT_FLAGS):
        super().__init__(application_id=APP_ID, flags=flags)
        self.model = model
        self.background = background
        self.window = None
        self.tray = None
        # None until the panel's watcher answered; False = no tray area.
        self.tray_available: bool | None = None
        # Page asked for from the tray menu ("verify", "accounts", ...), see window.PAGE_FACTORIES.
        self.requested_page: str | None = None
        self._held = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.model.load()
        self._start_tray()

    def do_activate(self):
        if self.background:
            # Only the launch at session start is silent, and only when the tray can be reached.
            self.background = False
            if self.tray_available is not False:
                return
        self.present_window()

    def do_shutdown(self):
        if self.tray is not None:
            self.tray.stop()
        Adw.Application.do_shutdown(self)

    def present_window(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()

    def open_page(self, page: str) -> None:
        self.requested_page = page
        self.present_window()
        self.window.open_page(page)

    def show_error(self, title: str, message: str) -> None:
        self.present_window()
        self.window.show_toast(f"{title}: {message}")

    def _start_tray(self) -> None:
        from ..tray.controller import TrayController

        # Held until the watcher answers, so a --background launch does not exit at once.
        self._hold()
        self.tray = TrayController(self.model, on_open=self.open_page, on_quit=self.quit, on_error=self.show_error,
                                   on_availability=self._tray_availability_changed)
        try:
            self.tray.start()
        except GLib.Error:  # no session bus at all
            self.tray = None
            self._tray_availability_changed(False)

    def _tray_availability_changed(self, available: bool) -> None:
        first_answer = self.tray_available is None
        self.tray_available = available
        if available:
            self._hold()
        else:
            if first_answer:
                self.present_window()
            self._release()
        if self.window is not None:
            self.window.tray_banner.set_revealed(not available)

    def _hold(self) -> None:
        if not self._held:
            self.hold()
            self._held = True

    def _release(self) -> None:
        if self._held:
            self.release()
            self._held = False


def run(model: AppModel, background: bool = False) -> int:
    model.dispatch = GLib.idle_add
    return MultiCredApplication(model, background).run([])
