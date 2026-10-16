"""The GTK application: one main window (and, from S-03, the tray icon)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID  # noqa: E402
from .model import AppModel  # noqa: E402


class MultiCredApplication(Adw.Application):
    def __init__(self, model: AppModel, background: bool = False,
                 flags: Gio.ApplicationFlags = Gio.ApplicationFlags.DEFAULT_FLAGS):
        super().__init__(application_id=APP_ID, flags=flags)
        self.model = model
        self.background = background
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.model.load()

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: MultiCredApplication):
        super().__init__(application=app, title="MultiCred", default_width=720, default_height=560)
        self.model = app.model
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        if self.model.is_test_mode:
            banner = Adw.Banner(title="MODO DE TESTE — nada no sistema real muda", revealed=True)
            view.add_top_bar(banner)
        self.page = Adw.PreferencesPage(title="Perfis")
        self.group = Adw.PreferencesGroup(title="Perfis")
        self.page.add(self.group)
        view.set_content(self.page)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.rows = []
        self.last_toast = None
        self.model.add_listener(self.refresh)
        self.refresh()

    def refresh(self):
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        config = self.model.config
        if config is None or not config.profiles:
            row = Adw.ActionRow(title="Nenhum perfil configurado")
            self.group.add(row)
            self.rows.append(row)
            return
        active = self.model.state.profile_id
        for profile in config.profiles:
            identity = config.git_identity(profile.git_identity_id)
            row = Adw.ActionRow(title=profile.name, subtitle=identity.email if identity else "git sem troca",
                                activatable=not self.model.switching)
            if profile.id == active:
                row.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            row.connect("activated", self.on_profile, profile.id)
            self.group.add(row)
            self.rows.append(row)

    def show_toast(self, text: str) -> None:
        self.last_toast = text
        self.toasts.add_toast(Adw.Toast(title=text))

    def on_profile(self, _row, profile_id):
        def done(result, error):
            if error is not None:
                self.show_toast(str(error))
            else:
                self.show_toast(f"Perfil {self.model.active_profile.name} ativo")

        self.model.switch_profile(profile_id, done)


def run(model: AppModel, background: bool = False) -> int:
    model.dispatch = GLib.idle_add
    return MultiCredApplication(model, background).run([])
