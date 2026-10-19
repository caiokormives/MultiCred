"""The single main window (D31): sidebar + pages with back navigation, structured like the Mac.

The sidebar follows `MainWindowView.swift:35-100`: section "Perfis" (one row per profile
with its 20 px badge and a gray ✓ on the active one, then "Novo perfil"), "Contas" (Git,
GitHub, Claude, Chaves de API with a gray count and an orange ⚠ by the Mac rule) and
"Sistema" (Verificar with a green/orange dot once something was verified, Ajustes).
Linux extra (user decision): "Visão geral", the dashboard of `pages/profiles.py`, heads
the Perfis section and is the start page; without profiles the start page is Claude, as
on the Mac. Clicking a profile opens its editor and never switches (the editor's and the
dashboard's buttons do). Before setup, the onboarding replaces the whole content.

Pages are built from `PAGE_FACTORIES` (name → callable(window) returning a widget), so
the tray's `open_page(name)` and later slices can replace a page without touching the
navigation (S-02/S-03: `PAGE_FACTORIES["git"] = lambda window: GitAccountsPage(window)`).
A factory may return an Adw.NavigationPage or any widget (it is then wrapped with a
header bar). Sub-pages (editors) are pushed on the content's Adw.NavigationView.
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
# Until S-02/S-03 give each kind of account its own page, the four Contas entries share
# the tabbed AccountsPage: entry name → its tab.
ACCOUNT_TABS = {"git": "git", "github": "github", "claude": "claude", "keys": "keys"}

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


def _accounts_entry(window):
    return window.accounts_page


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
    "git": _accounts_entry,
    "github": _accounts_entry,
    "claude": _accounts_entry,
    "keys": _accounts_entry,
    "verify": _verify_page,
    "settings": _settings_page,
}

# Pages opened on top of a section: name → (section, callable(window)).
PUSHED_PAGES: dict[str, tuple[str, Callable]] = {
    "new-profile": ("main", lambda window: window.show_profile_editor(None)),
    "add-claude": ("claude", lambda window: window.accounts_page.request_login("add", None, tab="claude")),
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
        # Login flows (S-07) opened from Contas, the tray or the profile page.
        self.new_account_form = None
        self.login_dialog = None
        self.last_login_result = None
        self.folder_login_command = None
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
        self._accounts_page = None
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
            counts, warnings = dict.fromkeys(ACCOUNT_TABS, 0), dict.fromkeys(ACCOUNT_TABS, False)
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
        self._select_sidebar(key)

    def _sidebar_selected(self, _listbox, row) -> None:
        if row is None or self._selecting:
            return
        self._navigate(row.item.key)
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
        tab = ACCOUNT_TABS.get(name)
        if tab is not None and hasattr(page, "select_tab"):
            page.select_tab(tab)
            page.set_title(SECTION_TITLES[name])
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
    def accounts_page(self):
        """The tabbed Contas page shared by the Git/GitHub/Claude/Chaves entries (until S-02/S-03)."""
        if self._accounts_page is None:
            from .pages.accounts import AccountsPage
            self._accounts_page = AccountsPage(self)
        return self._accounts_page

    @property
    def rows(self):
        """Profile rows of the Visão geral dashboard, in configuration order."""
        return self.profiles_page.rows

    def show_profile_editor(self, profile_id):
        """A profile's editor (None = new profile), pushed over Visão geral: opening it never
        switches; its back button returns to the dashboard."""
        from .pages.profile_editor import ProfileEditorPage
        if self.current_section != "main":
            self.show_section("main")
        self.nav.pop_to_tag("main")
        return self.push(ProfileEditorPage(self, profile_id))

    # MARK: logins (pseudo-terminal flows of S-07)

    def on_login_requested(self, kind: str, account) -> None:
        """kinds: "add" (new Claude account), "login" (sign an existing Claude account in
        again), "folder" (a Claude folder's own login; `account` is the folder or None),
        "github" (gh auth login)."""
        if kind == "add":
            self._ask_new_claude_account()
        elif kind == "login" and account is not None:
            from ..core.models import ClaudeAccountKind
            from .pages.login import LoginKind
            mode = LoginKind.CLAUDE_TOKEN if account.kind == ClaudeAccountKind.TOKEN else LoginKind.CLAUDE_LOGIN
            self._open_login(mode, target=account)
        elif kind == "folder":
            self._show_folder_login(account)
        elif kind == "github":
            from .pages.login import LoginKind
            self._open_login(LoginKind.GITHUB)

    def _ask_new_claude_account(self) -> None:
        """Nickname, optional e-mail and the kind, then the login dialog (like the Mac's
        AddClaudeAccountPage, as one small form)."""
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        nickname = Adw.EntryRow(title="Apelido")
        email = Adw.EntryRow(title="E-mail (opcional, só para exibir)")
        kind = Adw.ComboRow(title="Tipo", model=Gtk.StringList.new(
            ["Login completo (troca ao vivo, como o /login)", "Token (setup-token, só terminais novos)"]))
        for row in (nickname, email, kind):
            rows.append(row)
        dialog = Adw.AlertDialog(heading="Adicionar conta Claude",
                                 body="O login acontece numa pasta temporária; agents, skills e conversas "
                                      "continuam na pasta de cada perfil.")
        dialog.set_extra_child(rows)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("continue", "Continuar")
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("continue")

        def responded(_dialog, response):
            self.new_account_form = None
            if response == "continue":
                self.confirm_new_account(nickname.get_text(), email.get_text(),
                                         "token" if kind.get_selected() == 1 else "login")

        dialog.connect("response", responded)
        self.new_account_form = dialog
        dialog.present(self)

    def confirm_new_account(self, nickname: str, email: str | None, kind: str) -> None:
        from ..core.claude.login_flow import NewClaudeAccount
        from ..core.models import ProfileColor
        from .pages.login import LoginKind

        nickname, email = nickname.strip(), (email or "").strip() or None
        if not nickname:
            self.show_toast("Dê um apelido para a conta")
            return
        palette = list(ProfileColor)
        used = len(self.model.config.claude_accounts) if self.model.config else 0
        target = NewClaudeAccount(nickname, email, palette[used % len(palette)])
        self._open_login(LoginKind.CLAUDE_TOKEN if kind == "token" else LoginKind.CLAUDE_LOGIN, target=target)

    def _open_login(self, kind, target=None) -> None:
        from .pages.login import LoginDialog

        self.last_login_result = None
        self.login_dialog = LoginDialog(self.model, kind, self.login_finished, target=target)
        self.login_dialog.present(self)

    def login_finished(self, result) -> None:
        """Called once per dialog: on success, or when it closes without one."""
        from .pages.login import LoginKind

        self.last_login_result = result
        if not result.success:
            return
        if result.kind == LoginKind.GITHUB:
            # The Mac adds the logins gh now knows (addMissingGitHubAccounts).
            def imported(added, error):
                if error is None and added:
                    self.show_toast(f"Conta do GitHub {result.login} adicionada")
            self.model.import_github_accounts(imported)
            self.show_toast(f"Entrou no GitHub como {result.login}" if result.login else "Entrou no GitHub")
            return
        account = result.account
        self.model.notify()
        self.show_toast(f"Conta Claude {account.nickname} conectada")
        # Signed in again while in use: the live login (or the token's removal) must reach the folder.
        if account is not None and self.model.state.applied_claude_account_id == account.id:
            self.model.switch_claude_only(account.id, lambda _result, _error: None)

    def _show_folder_login(self, config_dir: str | None) -> None:
        """A folder's own login: the user runs Claude Code's login in a terminal."""
        from ..core.quoting import single_quoted

        command = "claude auth login" if config_dir is None else \
            f"CLAUDE_CONFIG_DIR={single_quoted(config_dir)} claude auth login"
        self.folder_login_command = command
        dialog = Adw.AlertDialog(heading="Entrar na pasta do Claude",
                                 body="Rode este comando num terminal e conclua o login no navegador:")
        label = Gtk.Label(label=command, selectable=True, wrap=True, xalign=0)
        label.add_css_class("monospace")
        dialog.set_extra_child(label)
        dialog.add_response("close", "Fechar")
        dialog.add_response("copy", "Copiar comando")
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)

        def responded(_dialog, response):
            if response == "copy":
                self.get_clipboard().set(command)
                self.show_toast("Comando copiado")

        dialog.connect("response", responded)
        dialog.present(self)

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
        self._rebuild_sidebar()
        # The four Contas entries share one page: refresh each page once.
        for page in {id(p): p for p in self._section_pages.values()}.values():
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

    def switch_claude_only(self, account_id) -> None:
        def done(result, error):
            if error is not None:
                self.show_toast(widgets.describe_error(error))
            else:
                applied = self.model.config.claude_account(self.model.state.applied_claude_account_id)
                self.show_toast(f"Claude agora usa {applied.nickname if applied else 'o login da pasta'}")

        self.model.switch_claude_only(account_id, done)
