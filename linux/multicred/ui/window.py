"""The single main window (D31): sidebar + pages with back navigation, structured like the Mac.

The sidebar follows `MainWindowView.swift:35-100`: section "Perfis" (one row per profile
with its 20 px badge and a gray ✓ on the active one, then "Novo perfil"), "Contas" (Git,
GitHub, Claude, Chaves de API with a gray count and an orange ⚠ by the Mac rule) and
"Sistema" (Verificar with a green/orange dot once something was verified, Ajustes).
Linux extra (user decision): "Visão geral", the dashboard of `pages/profiles.py`, heads
the Perfis section and is the start page; without profiles the start page is Claude, as
on the Mac. Clicking a profile opens its editor as the page of that row (no back button)
and never switches (the editor's and the dashboard's buttons do). Before setup, the
onboarding replaces the whole content.

Pages are built from `PAGE_FACTORIES` (name → callable(window) returning a widget), so
the tray's `open_page(name)` and later slices can replace a page without touching the
navigation. A factory may return an Adw.NavigationPage or any widget (it is then wrapped
with a header bar). Sub-pages (editors, details, logins) are pushed on the content's
Adw.NavigationView.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from ..core.models import ProfileColor  # noqa: E402
from . import widgets  # noqa: E402

NO_TRAY_TEXT = ("Sem área de ícones: no Ubuntu, ative a extensão AppIndicator; "
                "no Pop!_OS, o painel do COSMIC já mostra ícones")

# Navigation titles of the section pages (Mac: the root pages' navigationTitle).
SECTION_TITLES = {
    "main": "Visão geral", "git": "Git", "github": "GitHub", "claude": "Claude", "keys": "Chaves de API",
    "verify": "Verificar", "settings": "Ajustes",
}
# Page names the tray menu uses: "Contas…" opens Claude, as on the Mac.
PAGE_ALIASES = {"accounts": "claude"}
# The Contas entries, each a page of its own (S-02, S-03).
ACCOUNT_KINDS = ("git", "github", "claude", "keys")
# `current_section` while a profile's editor is the root of the content (its sidebar row).
PROFILE_SECTION = "profile"

# SF Symbols of the Mac sidebar → icon names (inventory B1). branch, </> and sparkle do
# not exist in Adwaita: they are the app's own symbolic SVGs (widgets.load_icons).
ICONS = {
    "overview": "view-grid-symbolic",          # Linux extra
    "new-profile": "list-add-symbolic",        # plus
    "git": "multicred-branch-symbolic",        # arrow.triangle.branch
    "github": "multicred-code-symbolic",       # chevron.left.forwardslash.chevron.right
    "claude": "multicred-sparkle-symbolic",    # sparkle
    "keys": "dialog-password-symbolic",        # key
    "verify": "security-high-symbolic",        # checkmark.shield
    "settings": "emblem-system-symbolic",      # gearshape
}


@dataclass(frozen=True)
class SidebarItem:
    """One sidebar row as plain data: the rows are rebuilt only when these change."""

    # "overview", "profile:<UUID>", "new-profile", "git", "github", "claude", "keys",
    # "verify" or "settings".
    key: str
    section: str  # "Perfis" | "Contas" | "Sistema"
    title: str
    icon: str | None = None
    # Profiles show their badge (letter on the profile color) instead of an icon.
    letter: str | None = None
    color: str | None = None
    active: bool = False
    count: int | None = None
    warning: bool = False
    # Verificar: None before the first verification, then "ok" or "warning".
    dot: str | None = None


def _profiles_page(window):
    from .pages.profiles import ProfilesPage
    return ProfilesPage(window)


def _claude_page(window):
    """S-03: the Mac's Claude page (pages/claude.py)."""
    from .pages.claude import ClaudePage
    return ClaudePage(window)


def _account_list_page(class_name: str) -> Callable:
    """S-02: Git, GitHub and Chaves de API are pages of their own (pages/accounts.py)."""
    def factory(window):
        from .pages import accounts
        return getattr(accounts, class_name)(window)
    return factory


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
    "git": _account_list_page("GitAccountsPage"),
    "github": _account_list_page("GitHubAccountsPage"),
    "claude": _claude_page,
    "keys": _account_list_page("APIKeysPage"),
    "verify": _verify_page,
    "settings": _settings_page,
}

# Pages opened on top of a section: name → (section, callable(window)).
PUSHED_PAGES: dict[str, tuple[str, Callable]] = {
    "new-profile": ("main", lambda window: window.show_profile_editor(None)),
    "add-claude": ("claude", lambda window: window.on_login_requested("add", None)),
}


def _section_key(section: str) -> str:
    """The sidebar row of a section's root page."""
    return "overview" if section == "main" else section


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MultiCred", default_width=1000, default_height=700)
        widgets.load_css()
        widgets.load_icons()
        self.model = app.model
        self.last_toast = None
        # Login pages (S-04) opened from Contas, the tray or the profile page: the last
        # one pushed, and what the last one reported.
        self.login_page = None
        self.last_login_result = None
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
        self.nav.connect("notify::visible-page", lambda *_: self._sync_sidebar_selection())
        self.content_page = Adw.NavigationPage(title="Visão geral", child=self.nav)
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
        self.show_section(self.start_section())
        if self.model.config is not None:
            self.model.refresh_claude_statuses()
            self.model.load_folder_summaries()
        if self.model.config is None or app.requested_page == "onboarding":
            self.show_onboarding()

    # MARK: sidebar

    def _build_sidebar(self) -> Adw.NavigationPage:
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar_rows: dict[str, Gtk.ListBoxRow] = {}
        self._sidebar_items: list[SidebarItem] | None = None
        self.sidebar.connect("row-selected", self._sidebar_selected)
        self.sidebar.connect("row-activated", self._sidebar_activated)
        self._rebuild_sidebar()

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

    def sidebar_model(self) -> list[SidebarItem]:
        """The rows, in the Mac's order, with counts and warnings by the Mac rule
        (MainWindowView.swift:37-72)."""
        model, config = self.model, self.model.config
        profiles = config.profiles if config is not None else []
        items = [SidebarItem("overview", "Perfis", "Visão geral", icon=ICONS["overview"])]
        for profile in profiles:
            items.append(SidebarItem(f"profile:{profile.id}", "Perfis", profile.name,
                                     letter=widgets.profile_letter(profile), color=profile.color.value,
                                     active=profile.id == model.state.profile_id))
        items.append(SidebarItem("new-profile", "Perfis", "Novo perfil", icon=ICONS["new-profile"]))
        if config is not None:
            counts = {"git": len(config.git_identities), "github": len(config.github_accounts),
                      "claude": len(config.claude_accounts), "keys": len(config.api_keys)}
            warnings = {
                "git": False,
                # Only logins gh knows and cannot use (an account gh never saw is not flagged).
                "github": any(login is not None and not login.is_usable
                              for login in map(model.github_login_for, config.github_accounts)),
                # The profiles, not every account: an unused account without token is no warning.
                "claude": any(model.needs_claude_login(profile) for profile in profiles),
                "keys": any(not model.has_secret(key) for key in config.api_keys),
            }
        else:
            counts, warnings = dict.fromkeys(ACCOUNT_KINDS, 0), dict.fromkeys(ACCOUNT_KINDS, False)
        for key, title in (("git", "Git"), ("github", "GitHub"), ("claude", "Claude"), ("keys", "Chaves de API")):
            items.append(SidebarItem(key, "Contas", title, icon=ICONS[key], count=counts[key],
                                     warning=warnings[key]))
        report = model.verification
        dot = None if report is None else ("warning" if report.problems else "ok")
        items.append(SidebarItem("verify", "Sistema", "Verificar", icon=ICONS["verify"], dot=dot))
        items.append(SidebarItem("settings", "Sistema", "Ajustes", icon=ICONS["settings"]))
        return items

    def _rebuild_sidebar(self) -> None:
        items = self.sidebar_model()
        if items == self._sidebar_items:
            return
        self._sidebar_items = items
        self._selecting = True
        try:
            self.sidebar.remove_all()
            self.sidebar_rows = {}
            for item in items:
                row = self._make_sidebar_row(item)
                self.sidebar.append(row)
                self.sidebar_rows[item.key] = row
        finally:
            self._selecting = False
        self._sync_sidebar_selection()

    def _make_sidebar_row(self, item: SidebarItem) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2, margin_start=2, margin_end=2)
        row = Gtk.ListBoxRow(child=box)
        row.item, row.title = item, item.title
        row.avatar = row.count_label = row.warning_icon = row.check_icon = row.dot = None
        row.dot_state = item.dot
        if item.letter is not None:
            row.avatar = widgets.avatar(item.letter, ProfileColor(item.color), 20)
            row.avatar.update_state([Gtk.AccessibleState.HIDDEN], [True])  # Mac: accessibilityHidden
            box.append(row.avatar)
        else:
            box.append(Gtk.Image.new_from_icon_name(item.icon))
        title = widgets.label(item.title)
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title)
        if item.active:
            row.check_icon = self._badge_icon("object-select-symbolic", "mc-sidebar-check", "ativo")
            box.append(row.check_icon)
        if item.count is not None:
            row.count_label = widgets.label(str(item.count), "mc-sidebar-count")
            box.append(row.count_label)
        if item.warning:
            row.warning_icon = self._badge_icon("dialog-warning-symbolic", "mc-sidebar-warning",
                                                "precisa de atenção")
            box.append(row.warning_icon)
        if item.dot is not None:
            row.dot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                              tooltip_text="tudo certo" if item.dot == "ok" else "inconsistente")
            row.dot.add_css_class("mc-sidebar-dot")
            row.dot.add_css_class(item.dot)
            row.dot.update_property([Gtk.AccessibleProperty.LABEL], [row.dot.get_tooltip_text()])
            box.append(row.dot)
        return row

    @staticmethod
    def _badge_icon(name: str, css: str, description: str) -> Gtk.Image:
        image = Gtk.Image(icon_name=name, pixel_size=12, tooltip_text=description, valign=Gtk.Align.CENTER)
        image.add_css_class(css)
        image.update_property([Gtk.AccessibleProperty.LABEL], [description])
        return image

    @staticmethod
    def _sidebar_header(row, before) -> None:
        if before is None or before.item.section != row.item.section:
            if row.get_header() is None or row.get_header().get_label() != row.item.section:
                row.set_header(widgets.label(row.item.section, "mc-sidebar-heading"))
        elif row.get_header() is not None:
            row.set_header(None)

    def sidebar_sections(self) -> list[tuple[str, list[str]]]:
        """[(section, [row titles])] in sidebar order (tests, screenshots)."""
        sections: list[tuple[str, list[str]]] = []
        for item in self._sidebar_items or []:
            if not sections or sections[-1][0] != item.section:
                sections.append((item.section, []))
            sections[-1][1].append(item.title)
        return sections

    def sidebar_titles(self) -> list[str]:
        return [item.title for item in self._sidebar_items or []]

    def sidebar_row(self, key: str) -> Gtk.ListBoxRow | None:
        return self.sidebar_rows.get(key)

    def selected_sidebar_key(self) -> str | None:
        row = self.sidebar.get_selected_row()
        return row.item.key if row is not None else None

    def _select_sidebar(self, key: str | None) -> None:
        row = self.sidebar_rows.get(key) if key is not None else None
        if row is None or self.sidebar.get_selected_row() is row:
            return
        self._selecting = True
        try:
            self.sidebar.select_row(row)
        finally:
            self._selecting = False

    def _visible_key(self) -> str | None:
        """The row that matches what the content shows: a profile's editor, or a section root.
        None for other pushed pages (an account editor keeps its entry selected)."""
        page = self.nav.get_visible_page()
        if page is None:
            return None
        if hasattr(page, "draft") and hasattr(page, "is_new"):  # ProfileEditorPage
            return "new-profile" if page.is_new else f"profile:{page.draft.id}"
        if self.current_section is not None and page is self._section_pages.get(self.current_section):
            return _section_key(self.current_section)
        return None

    def _sync_sidebar_selection(self) -> None:
        if not hasattr(self, "sidebar_rows"):
            return
        key = self._visible_key()
        if key is not None and key.startswith("profile:") and key not in self.sidebar_rows:
            # The profile on screen no longer exists: back to the start page.
            self.show_section(self.start_section())
            return
        if key is None and self.current_section is not None:
            # A sub-page (login, account editor) keeps its section's row selected, also
            # after a rebuild: otherwise the list box selects the focused row by itself
            # later and that "click" would clear the stack.
            key = _section_key(self.current_section)
        self._select_sidebar(key)

    def _sidebar_selected(self, _listbox, row) -> None:
        key = row.item.key if row is not None else None
        previous, self._selected_key = getattr(self, "_selected_key", None), key
        # GTK emits row-selected again for the row already selected when it takes the
        # focus: navigating then would clear the section's stack (a login page, an
        # editor). Going back to the section's root on purpose is _sidebar_activated.
        if key is None or self._selecting or key == previous:
            return
        self._navigate(key)
        self.split.set_show_content(True)

    def _sidebar_activated(self, _listbox, row) -> None:
        """A click on the row already selected: back to its page from one of its sub-pages,
        and to the content when the split view is collapsed."""
        if self._selecting or row is not self.sidebar.get_selected_row():
            return
        if self._visible_key() != row.item.key:
            self._navigate(row.item.key)
        self.split.set_show_content(True)

    def _navigate(self, key: str) -> None:
        if key.startswith("profile:"):
            self.show_profile_editor(uuid.UUID(key.removeprefix("profile:")))
        elif key == "new-profile":
            self.show_profile_editor(None)
        else:
            self.show_section("main" if key == "overview" else key)

    # MARK: navigation

    def start_section(self) -> str:
        """Visão geral (Linux extra) when there are profiles; Claude otherwise, as on the Mac."""
        config = self.model.config
        return "main" if config is not None and config.profiles else "claude"

    def _section_page(self, name: str) -> Adw.NavigationPage:
        page = self._section_pages.get(name)
        if page is None:
            widget = PAGE_FACTORIES[name](self)
            if isinstance(widget, Adw.NavigationPage):
                page = widget
            else:
                page, _header = widgets.page(SECTION_TITLES.get(name, name), widget)
            if page.get_tag() is None:
                page.set_tag(name)
            self._section_pages[name] = page
        return page

    def show_section(self, name: str) -> Adw.NavigationPage:
        page = self._section_page(name)
        self.current_section = name
        self.nav.replace([page])
        self.content_page.set_title(SECTION_TITLES.get(name, page.get_title()))
        self._select_sidebar(_section_key(name))
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
        name = PAGE_ALIASES.get(name, name)
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
    def rows(self):
        """Profile rows of the Visão geral dashboard, in configuration order."""
        return self.profiles_page.rows

    def show_profile_editor(self, profile_id):
        """A profile's editor (None = new profile) as the page of its sidebar row, like the
        Mac (MainWindowView.swift:90-94): the root of the content, no back button. Opening
        it never switches."""
        from .pages.profile_editor import ProfileEditorPage
        page = ProfileEditorPage(self, profile_id)
        self.current_section = PROFILE_SECTION
        self.nav.replace([page])
        self.content_page.set_title(page.get_title())
        return page

    # MARK: logins (pages pushed in the content, as on the Mac)

    def on_login_requested(self, kind: str, account, host: str = "github.com") -> None:
        """kinds: "add" (new Claude account), "login" (sign an existing Claude account in
        again), "folder" (a Claude folder's own login; `account` is the folder or None),
        "github" (gh auth login on `host`).

        Like MainNavigator.swift: Claude pages open in the Claude section and the GitHub
        login in GitHub (switching section clears the stack); already there, the page is
        pushed over what is shown."""
        if kind == "add" or (kind == "login" and account is not None):
            from .pages.add_claude import AddClaudeAccountPage
            self._push_login("claude", AddClaudeAccountPage(self, account if kind == "login" else None))
        elif kind == "folder":
            # The Mac's "Entrar no Claude": `claude auth login` in that folder, inside the app.
            from .pages.claude import ClaudeFolderLoginPage
            self._push_login("claude", ClaudeFolderLoginPage(self, account))
        elif kind == "github":
            from .pages.login import GitHubLoginPage
            self._push_login("github", GitHubLoginPage(self, host))

    def _push_login(self, section: str, page) -> None:
        if self.onboarding is not None:
            return
        if self.current_section != section:
            self.show_section(section)
        self.last_login_result = None
        self.login_page = page
        self.push(page)
        self.split.set_show_content(True)

    def pop_to_section_root(self) -> None:
        """"Concluir" on a login page (Mac: navigator.path = [])."""
        stack = self.nav.get_navigation_stack()
        if stack.get_n_items() > 1:
            self.nav.pop_to_page(stack.get_item(0))

    def login_finished(self, result) -> None:
        """Called once per login page: on success, or when it is left without one."""
        from .pages.login import LoginKind

        self.last_login_result = result
        if not result.success:
            return
        if result.kind == LoginKind.GITHUB:
            # Mac didLogInToGitHub: add the logins gh now knows (addMissingGitHubAccounts),
            # then verify. The page itself says it is connected (no toast, as on the Mac).
            self.model.import_github_accounts(lambda _added, _error: self.model.verify())
            return
        account = result.account
        self.model.notify()
        # Signed in again while in use: the live login (or the token's removal) must reach the folder.
        if account is not None and self.model.state.applied_claude_account_id == account.id:
            self.model.switch_claude_only(account.id, lambda _result, _error: None)

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
        settings = Gio.SimpleAction.new("settings", None)
        settings.connect("activate", lambda *_: self._settings_shortcut())
        self.add_action(settings)
        for number in range(1, 10):
            app.set_accels_for_action(f"win.activate-profile({number})", [f"<Control>{number}"])
        app.set_accels_for_action("win.verify", ["<Control>r"])
        # The Mac's menu: "Ajustes…" ⌘, and "Sair do MultiCred" ⌘Q (inventory C, rows 17-18).
        app.set_accels_for_action("win.settings", ["<Control>comma"])
        app.set_accels_for_action("win.quit", ["<Control>q"])

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

    def _settings_shortcut(self) -> None:
        """Ctrl+, (Mac ⌘,): Ajustes; during the setup the onboarding stays."""
        if self.onboarding is None:
            self.open_page("settings")

    # MARK: state

    def on_close_request(self, _window):
        if self.get_application().tray_available:
            self.set_visible(False)  # stays in the tray; the tray menu brings it back
            return True
        self.get_application().window = None
        return False

    def refresh(self) -> None:
        self._rebuild_sidebar()
        for page in self._section_pages.values():
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
        profile = self.model.active_profile
        # Mac (MainWindowView.swift:24): the active profile's editor, or Claude without one.
        if profile is not None:
            self.show_profile_editor(profile.id)
        else:
            self.show_section("claude")
        self.model.refresh_claude_statuses()
        self.model.load_folder_summaries()
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

    def switch_claude_only(self, account_id, on_success: Callable[[], None] | None = None) -> None:
        def done(result, error):
            if error is not None:
                self.show_toast(widgets.describe_error(error))
            else:
                if on_success is not None:
                    on_success()
                applied = self.model.config.claude_account(self.model.state.applied_claude_account_id)
                self.show_toast(f"Claude agora usa {applied.nickname if applied else 'o login da pasta'}")

        self.model.switch_claude_only(account_id, done)
