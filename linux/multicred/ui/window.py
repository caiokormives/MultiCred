"""The single main window (D31, approved Linux design A): sidebar + pages with back navigation.

Adw.NavigationSplitView with the sections Perfis, Contas, Verificar and Ajustes; each
section is the root of an Adw.NavigationView, and sub-pages (profile editor, account
editors) are pushed on it. Before setup, the 3-step onboarding (S-05) replaces the
whole content in this same window.

Pages are built from `PAGE_FACTORIES` (name → callable(window) returning a widget), so
the tray's `open_page(name)` and later slices can add pages without touching the
navigation: S-08/S-09 replace the "verify"/"settings" placeholders with
`PAGE_FACTORIES["verify"] = lambda window: VerifyPage(...)`. A factory may return
an Adw.NavigationPage or any widget (it is then wrapped with a header bar).
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import widgets  # noqa: E402

NO_TRAY_TEXT = ("Sem área de ícones: no Ubuntu, ative a extensão AppIndicator; "
                "no Pop!_OS, o painel do COSMIC já mostra ícones")

# Sidebar order: (name, title, icon).
SECTIONS = (
    ("main", "Perfis", "avatar-default-symbolic"),
    ("accounts", "Contas", "dialog-password-symbolic"),
    ("verify", "Verificar", "object-select-symbolic"),
    ("settings", "Ajustes", "emblem-system-symbolic"),
)
SECTION_TITLES = {name: title for name, title, _icon in SECTIONS}


def _profiles_page(window):
    from .pages.profiles import ProfilesPage
    return ProfilesPage(window)


def _accounts_page(window):
    from .pages.accounts import AccountsPage
    return AccountsPage(window)


def _verify_page(window):
    from .pages.verify import VerifyPage
    page = VerifyPage(window.model, on_report=window.verification_reported)
    # Its refresh() runs the verifier: only when the section is opened or asked to,
    # never on every model change (the report itself notifies the model).
    page.refresh_on_notify = False
    return page


def _settings_page(window):
    from .pages.settings import SettingsPage
    return SettingsPage(window)


def placeholder_page(title: str, icon: str, description: str) -> Adw.StatusPage:
    return Adw.StatusPage(title=title, icon_name=icon, description=description)


# name → callable(window) returning the section's root widget.
PAGE_FACTORIES: dict[str, Callable] = {
    "main": _profiles_page,
    "accounts": _accounts_page,
    "verify": _verify_page,
    "settings": _settings_page,
}

# Pages opened on top of a section: name → (section, callable(window)).
PUSHED_PAGES: dict[str, tuple[str, Callable]] = {
    "new-profile": ("main", lambda window: window.show_profile_editor(None)),
    "add-claude": ("accounts", lambda window: window.accounts_page.request_login("add", None, tab="claude")),
}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MultiCred", default_width=1000, default_height=700)
        widgets.load_css()
        self.model = app.model
        self.last_toast = None
        self._toast = None
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.view = view
        # Shown only while the onboarding replaces the content (the sections have their own).
        self.header = Adw.HeaderBar(visible=False)
        view.add_top_bar(self.header)
        if self.model.is_test_mode:
            view.add_top_bar(Adw.Banner(title="MODO DE TESTE — nada no sistema real muda", revealed=True))
        self.tray_banner = Adw.Banner(title=NO_TRAY_TEXT, revealed=app.tray_available is False)
        view.add_top_bar(self.tray_banner)
        self.onboarding = None
        self._section_pages: dict[str, Adw.NavigationPage] = {}
        self.current_section: str | None = None
        self._selecting = False

        self.nav = Adw.NavigationView()
        self.content_page = Adw.NavigationPage(title="Perfis", child=self.nav)
        self.split = Adw.NavigationSplitView(sidebar=self._build_sidebar(), content=self.content_page,
                                             min_sidebar_width=200, max_sidebar_width=260)
        # The normal content of the window (the onboarding swaps it out and back). No
        # Adw.Breakpoint: a breakpoint bin needs content that shrinks below its size
        # request, and the onboarding steps do not (GTK warns on every measure).
        self.page = self.split
        view.set_content(self.page)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self._install_actions(app)
        self.model.add_listener(self.refresh)
        self.connect("close-request", self.on_close_request)
        self.show_section("main")
        if self.model.config is not None:
            self.model.refresh_claude_statuses()
            self.model.load_folder_summaries()
        if self.model.config is None or app.requested_page == "onboarding":
            self.show_onboarding()

    # MARK: sidebar

    def _build_sidebar(self) -> Adw.NavigationPage:
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar_rows: dict[str, Gtk.ListBoxRow] = {}
        self.verify_badge = widgets.label("", "mc-sidebar-badge", xalign=0.5)
        self.verify_badge.set_visible(False)
        for name, title, icon in SECTIONS:
            box = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(widgets.label(title))
            if name == "verify":
                spacer = Gtk.Box(hexpand=True)
                box.append(spacer)
                box.append(self.verify_badge)
            row = Gtk.ListBoxRow(child=box)
            row.section = name
            row.title = title
            self.sidebar.append(row)
            self.sidebar_rows[name] = row
        self.sidebar.connect("row-selected", self._sidebar_selected)

        menu = Gio.Menu()
        menu.append("Configurar de novo…", "win.onboarding")
        menu.append("Sair do MultiCred", "win.quit")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu")
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(menu_button)
        view.add_top_bar(header)
        view.set_content(Gtk.ScrolledWindow(child=self.sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER))
        return Adw.NavigationPage(title="MultiCred", child=view)

    def sidebar_titles(self) -> list[str]:
        return [row.title for row in self.sidebar_rows.values()]

    def _sidebar_selected(self, _listbox, row) -> None:
        if row is None or self._selecting:
            return
        self.show_section(row.section)
        self.split.set_show_content(True)

    # MARK: navigation

    def _section_page(self, name: str) -> Adw.NavigationPage:
        page = self._section_pages.get(name)
        if page is None:
            widget = PAGE_FACTORIES[name](self)
            if isinstance(widget, Adw.NavigationPage):
                page = widget
            else:
                page, _header = widgets.page(SECTION_TITLES.get(name, name), widget)
            page.set_tag(name)
            self._section_pages[name] = page
        return page

    def show_section(self, name: str) -> Adw.NavigationPage:
        page = self._section_page(name)
        self.nav.replace([page])
        self.current_section = name
        self.content_page.set_title(SECTION_TITLES.get(name, page.get_title()))
        row = self.sidebar_rows.get(name)
        if row is not None and self.sidebar.get_selected_row() is not row:
            self._selecting = True
            self.sidebar.select_row(row)
            self._selecting = False
        if hasattr(page, "refresh"):
            page.refresh()
        return page

    def open_page(self, name: str) -> None:
        """Navigates to a page by name (the tray menu uses these names)."""
        if name == "onboarding":
            self.show_onboarding()
            return
        if self.onboarding is not None:
            return  # setup first; the tray can bring the page back afterwards
        if name in PUSHED_PAGES:
            section, opener = PUSHED_PAGES[name]
            self.show_section(section)
            opener(self)
        else:
            name = name if name in PAGE_FACTORIES else "main"
            # Already there (e.g. the tray icon clicked while an editor is open): keep the stack.
            if name != self.current_section:
                self.show_section(name)
        self.split.set_show_content(True)

    def push(self, page: Adw.NavigationPage) -> Adw.NavigationPage:
        self.nav.push(page)
        return page

    @property
    def profiles_page(self):
        return self._section_page("main")

    @property
    def accounts_page(self):
        return self._section_page("accounts")

    @property
    def rows(self):
        """Profile rows of the Perfis page, in configuration order."""
        return self.profiles_page.rows

    def show_profile_editor(self, profile_id):
        from .pages.profile_editor import ProfileEditorPage
        if self.current_section != "main":
            self.show_section("main")
        self.nav.pop_to_tag("main")
        return self.push(ProfileEditorPage(self, profile_id))

    # MARK: hooks for later slices

    def on_login_requested(self, kind: str, account) -> None:
        """Login flows in a pseudo-terminal (S-07) plug in here. kinds: "add" (new Claude
        account), "login" (sign an existing Claude account in again), "folder" (a Claude
        folder's own login; `account` is the folder or None), "github" (gh auth login)."""
        what = "do GitHub" if kind == "github" else "do Claude"
        self.show_toast(f"Login {what} pela janela: em breve")

    def run_verification(self) -> None:
        """"Verificar de novo": a deep check (gh tokens too) on the page when it is open."""
        page = self._section_pages.get("verify")
        if page is not None and self.current_section == "verify":
            page.refresh(deep=True)
        else:
            self.model.verify(lambda _report, _error: None)

    def verification_reported(self, report) -> None:
        self.model.verification = report
        self.model.notify()

    # MARK: actions and shortcuts

    def _install_actions(self, app) -> None:
        activate = Gio.SimpleAction.new("activate-profile", GLib.VariantType.new("i"))
        activate.connect("activate", lambda _a, value: self.activate_profile_number(value.get_int32()))
        self.add_action(activate)
        verify = Gio.SimpleAction.new("verify", None)
        verify.connect("activate", lambda *_: self._verify_shortcut())
        self.add_action(verify)
        onboarding = Gio.SimpleAction.new("onboarding", None)
        onboarding.connect("activate", lambda *_: self.show_onboarding())
        self.add_action(onboarding)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: app.quit())
        self.add_action(quit_action)
        for number in range(1, 10):
            app.set_accels_for_action(f"win.activate-profile({number})", [f"<Control>{number}"])
        app.set_accels_for_action("win.verify", ["<Control>r"])

    def activate_profile_number(self, number: int) -> None:
        """Ctrl+1…9: the first nine profiles in configuration order."""
        config = self.model.config
        if self.onboarding is not None or config is None or not 1 <= number <= min(9, len(config.profiles)):
            return
        if self.model.switching:
            return
        self.on_profile(None, config.profiles[number - 1].id)

    def _verify_shortcut(self) -> None:
        if self.onboarding is None:
            self.open_page("verify")  # opening the section runs the checklist

    # MARK: state

    def on_close_request(self, _window):
        if self.get_application().tray_available:
            self.set_visible(False)  # stays in the tray; the tray menu brings it back
            return True
        self.get_application().window = None
        return False

    def refresh(self) -> None:
        report = self.model.verification
        if report is not None:
            ok, total = report.ok_count, report.total
            self.verify_badge.set_label(f"{ok}/{total}")
            if ok < total:
                self.verify_badge.add_css_class("warning")
            else:
                self.verify_badge.remove_css_class("warning")
        self.verify_badge.set_visible(report is not None)
        for page in list(self._section_pages.values()):
            if (hasattr(page, "refresh") and page.get_parent() is not None
                    and getattr(page, "refresh_on_notify", True)):
                page.refresh()
        visible = self.nav.get_visible_page()
        if visible is not None and visible not in self._section_pages.values() and hasattr(visible, "refresh"):
            visible.refresh()

    def show_onboarding(self) -> None:
        if self.onboarding is not None:
            return
        from .pages.onboarding import OnboardingPage

        self.onboarding = OnboardingPage(self.model, on_finished=self._onboarding_finished)
        self.view.set_content(self.onboarding)
        self.header.set_visible(True)
        self.header.set_title_widget(Adw.WindowTitle(title="Configurar MultiCred"))

    def _onboarding_finished(self, error) -> None:
        self.onboarding = None
        self.get_application().requested_page = None
        self.view.set_content(self.page)
        self.header.set_visible(False)
        self.header.set_title_widget(None)
        self.show_section("main")
        self.model.refresh_claude_statuses()
        self.model.load_folder_summaries()
        profile = self.model.active_profile
        if error is not None:
            self.show_toast(f"MultiCred configurado, mas não consegui ativar o perfil: {error}")
        elif profile is not None:
            self.show_toast(f"Perfil {profile.name} ativo")
        else:
            self.show_toast("MultiCred configurado")

    def show_toast(self, text: str) -> None:
        self.last_toast = text
        # The newest message wins: switching twice should not queue two toasts.
        if self._toast is not None:
            self._toast.dismiss()
        self._toast = Adw.Toast(title=widgets.escape(text))
        self.toasts.add_toast(self._toast)

    def dismiss_toast(self) -> None:
        if self._toast is not None:
            self._toast.dismiss()
            self._toast = None

    def on_profile(self, _row, profile_id):
        def done(result, error):
            if error is not None:
                self.show_toast(widgets.describe_error(error))
            else:
                self.show_toast(f"Perfil {self.model.active_profile.name} ativo")

        self.model.switch_profile(profile_id, done)

    def switch_claude_only(self, account_id) -> None:
        def done(result, error):
            if error is not None:
                self.show_toast(widgets.describe_error(error))
            else:
                applied = self.model.config.claude_account(self.model.state.applied_claude_account_id)
                self.show_toast(f"Claude agora usa {applied.nickname if applied else 'o login da pasta'}")

        self.model.switch_claude_only(account_id, done)
