"""The GTK application: one main window and the tray icon.

The app lives in the tray: closing the window keeps it running (Gio.Application.hold)
while a tray area exists. Without one (stock GNOME without the AppIndicator
extension) the window is the only way in, so it opens with a banner explaining why
and closing it quits."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID  # noqa: E402
from .model import AppModel  # noqa: E402

NO_TRAY_TEXT = ("Sem área de ícones: no Ubuntu, ative a extensão AppIndicator; "
                "no Pop!_OS, o painel do COSMIC já mostra ícones")


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
        # Page asked for from the tray menu ("verify", "accounts", ...); navigation comes in S-06.
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
        if page == "onboarding":
            self.window.show_onboarding()

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


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: MultiCredApplication):
        super().__init__(application=app, title="MultiCred", default_width=720, default_height=560)
        self.model = app.model
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.view = view
        self.header = Adw.HeaderBar()
        view.add_top_bar(self.header)
        # The 3-step onboarding (S-05) replaces the content while it runs, in this same window (D31).
        self.onboarding = None
        if self.model.is_test_mode:
            banner = Adw.Banner(title="MODO DE TESTE — nada no sistema real muda", revealed=True)
            view.add_top_bar(banner)
        self.tray_banner = Adw.Banner(title=NO_TRAY_TEXT, revealed=app.tray_available is False)
        view.add_top_bar(self.tray_banner)
        self.page = Adw.PreferencesPage(title="Perfis")
        self.group = Adw.PreferencesGroup(title="Perfis")
        self.page.add(self.group)
        view.set_content(self.page)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.rows = []
        self.last_toast = None
        self.model.add_listener(self.refresh)
        self.connect("close-request", self.on_close_request)
        self.refresh()
        if self.model.config is None or app.requested_page == "onboarding":
            self.show_onboarding()

    def on_close_request(self, _window):
        if self.get_application().tray_available:
            self.set_visible(False)  # stays in the tray; the tray menu brings it back
            return True
        self.get_application().window = None
        return False

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

    def show_onboarding(self) -> None:
        if self.onboarding is not None:
            return
        from .pages.onboarding import OnboardingPage

        self.onboarding = OnboardingPage(self.model, on_finished=self._onboarding_finished)
        self.view.set_content(self.onboarding)
        self.header.set_title_widget(Adw.WindowTitle(title="Configurar MultiCred"))

    def _onboarding_finished(self, error) -> None:
        self.onboarding = None
        self.get_application().requested_page = None
        self.view.set_content(self.page)
        self.header.set_title_widget(None)
        self.refresh()
        profile = self.model.active_profile
        if error is not None:
            self.show_toast(f"MultiCred configurado, mas não consegui ativar o perfil: {error}")
        elif profile is not None:
            self.show_toast(f"Perfil {profile.name} ativo")
        else:
            self.show_toast("MultiCred configurado")

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
