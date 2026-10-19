"""Glue between AppModel and the two D-Bus objects: every model change rebuilds the
menu and the icon; clicks become model calls or window requests; opening the menu
refreshes the Claude statuses and re-verifies a stale report (AppDelegate.swift:134-139)."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Callable

from gi.repository import Gio

from .. import APP_ID
from .dbusmenu import PATH as MENU_PATH
from .dbusmenu import DBusMenuServer
from .icon import appearance, png_bytes, profile_png, sni_pixmaps
from .menu_model import build_menu
from .sni import StatusNotifierItem

# AppDelegate.swift:136: opening the menu re-verifies a report older than this.
STALE_VERIFICATION_SECONDS = 120


def account_png(account) -> bytes:
    """The Claude account's colored square with its initial (StatusIcon.image(letter:), 16 px)."""
    return png_bytes(account.color.hex, (account.nickname.strip() or "?")[:1].upper())


class TrayController:
    def __init__(self, model, *, on_open: Callable[[str], None], on_quit: Callable[[], None],
                 on_error: Callable[[str, str], None] | None = None,
                 on_availability: Callable[[bool], None] | None = None,
                 on_login: Callable[[str, object], None] | None = None,
                 connection: Gio.DBusConnection | None = None, tz: tzinfo | None = None,
                 clock: Callable[[], datetime] | None = None):
        """`on_login(kind, target)` opens a login page like MainWindow.on_login_requested:
        ("folder", config_dir) or ("login", account); without it the Claude page opens."""
        self.model = model
        self.on_open = on_open
        self.on_quit = on_quit
        self.on_error = on_error
        self.on_availability = on_availability
        self.on_login = on_login
        self.connection = connection
        self.tz = tz
        self.clock = clock or _model_clock(model)
        self.menu: DBusMenuServer | None = None
        self.item: StatusNotifierItem | None = None
        self._running = False
        # One status refresh and one verification at a time, however often the menu opens.
        self._refreshing = False
        self.verifying = False

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
        self.menu = DBusMenuServer(connection, self.handle, on_about_to_show=self.about_to_show)
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
                                      icon=profile_png, verification=model.verification,
                                      verifying=self.verifying, folder_status=model.folder_status,
                                      has_token=model.has_token, folder_name=model.folder_name,
                                      account_icon=account_png))
        profile = model.active_profile
        color, letter = appearance(profile)
        title = f"MultiCred · {profile.name}" if profile else "MultiCred"
        # AppDelegate.swift:59: the dot follows the report's problems (plus the Linux
        # half-switched state).
        alert = model.inconsistent or bool(model.verification is not None and model.verification.problems)
        self.item.update(pixmaps=sni_pixmaps(color, letter, alert=alert),
                         status="NeedsAttention" if alert else "Active",
                         tooltip=(title, "Inconsistente — abra Verificar" if alert else ""))

    # MARK: opening the menu (AppDelegate.swift:134-139)

    def about_to_show(self, item_id: int) -> None:
        """The root menu is opening: refresh the folders' login status, then verify
        (without checking tokens) when the last report is missing or older than 120 s."""
        if item_id != 0 or self.model.config is None or self._refreshing:
            return
        self._refreshing = True
        self.model.refresh_claude_statuses(on_done=self._statuses_refreshed)

    def _statuses_refreshed(self) -> None:
        self._refreshing = False
        if self._running and not self.verifying and self._verification_is_stale():
            self.verifying = True
            self.refresh()  # "Verificando"
            self.model.verify(on_done=self._verified)

    def _verification_is_stale(self) -> bool:
        report = self.model.verification
        if report is None:
            return True
        return (self.clock() - report.checked_at).total_seconds() > STALE_VERIFICATION_SECONDS

    def _verified(self, _report, _error) -> None:
        self.verifying = False
        self.refresh()

    # MARK: clicks

    def handle(self, action: tuple) -> None:
        kind, value = action
        if kind == "profile":
            profile = self.model.config.profile(value) if self.model.config else None
            name = profile.name if profile else "o perfil"
            report = self._reporting(f"Não consegui trocar para {name}")

            def done(result, error):
                report(result, error)
                if error is None and profile is not None:
                    self._login_if_needed(profile)
            self.model.switch_profile(value, done)
        elif kind == "folder-login":
            self._login("folder", value)
        elif kind == "claude":
            self.model.switch_claude_only(value, self._reporting("Não consegui trocar a conta Claude"))
        elif kind == "open":
            self.on_open(value)
        elif kind == "quit":
            self.on_quit()

    def _login_if_needed(self, profile) -> None:
        """AppDelegate.swift:203-208: a switched-to profile without its Claude credential
        opens its login (the account's login again, or the folder's own login)."""
        config = self.model.config
        if config is None or not self.model.needs_claude_login(profile):
            return
        account = config.claude_account(profile.claude_account_id)
        if account is not None:
            self._login("login", account)
        else:
            self._login("folder", profile.claude_config_dir)

    def _login(self, kind: str, target) -> None:
        if self.on_login is not None:
            self.on_login(kind, target)
        else:
            self.on_open("claude")

    def _reporting(self, title: str):
        def done(_result, error):
            if error is not None and self.on_error:
                from ..ui.messages import describe_switch_error
                self.on_error(title, describe_switch_error(error))
        return done

    def _availability_changed(self, available: bool) -> None:
        if self.on_availability:
            self.on_availability(available)


def _model_clock(model) -> Callable[[], datetime]:
    """The injected environment's clock (the report's checked_at comes from it)."""
    environment = getattr(model, "environment", None)
    clock = getattr(environment, "clock", None)
    return clock if callable(clock) else (lambda: datetime.now(timezone.utc))
