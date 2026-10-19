"""Login pages pushed in the window's content, as on the Mac (MainNavigator.swift:18-125).

Shared pieces of the macOS login views, ported for the pages of this package:
`BrowserChoice` / `AuthorizationLinkActions` (Login/BrowserChoice.swift), `StepRow`
(AddClaudeAccountPage.swift:325-365), `TerminalOutput` (LoginWindowView.swift:209-235),
plus the "Entrar no GitHub" page (`GitHubLoginPage`, AccountsPages.swift:340-446).
"Adicionar conta Claude" / "Entrar de novo em X" lives in `add_claude.py`.

Sub-steps are pages, never dialogs: the window pushes them on the section's
Adw.NavigationView (`MainWindow.on_login_requested`). Leaving a login page stops its
process (Mac: stopSessionsNoLongerShown). In test mode the simulators run instead of
the real `claude`/`gh` (D30) and the browser never opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ...core.adapters.github_login_flow import GitHubLoginSession  # noqa: E402
from ...core.models import ClaudeAccount, ProfileColor  # noqa: E402
from .. import widgets  # noqa: E402


class LoginKind(str, Enum):
    CLAUDE_LOGIN = "claude-login"
    CLAUDE_TOKEN = "claude-token"
    GITHUB = "github"


@dataclass(frozen=True)
class LoginResult:
    """What a login page reports to the window, once: on success, or when it is left
    without one."""

    kind: LoginKind
    success: bool
    # Claude: the account stored (new, or the existing one updated).
    account: ClaudeAccount | None = None
    # GitHub: who gh logged in, and whether the active account went back to the profile's.
    login: str | None = None
    switched_back: bool = False
    error: str | None = None


_CSS = """
.mc-login-page { font-size: 13.5px; }
.mc-login-heading { font-size: 15px; font-weight: 700; }
.mc-login-name { font-size: 17px; font-weight: 600; }
.mc-login-round { border-radius: 999px; }
.mc-login-icon-circle { min-width: 40px; min-height: 40px; border-radius: 999px; background: alpha(currentColor, 0.08); }
.mc-login-steps { background: alpha(currentColor, 0.04); border-radius: 12px; padding: 16px; }
.mc-step-badge { min-width: 24px; min-height: 24px; border-radius: 999px; font-size: 11px; font-weight: 800;
                 background: alpha(currentColor, 0.08); color: alpha(currentColor, 0.6); }
.mc-step.active .mc-step-badge { background: @accent_bg_color; color: @accent_fg_color; }
.mc-step.done .mc-step-badge { background: @green_4; color: white; }
.mc-step.failed .mc-step-badge { background: @red_3; color: white; }
.mc-step-title { font-size: 13.5px; }
.mc-step.active .mc-step-title { font-weight: 600; }
.mc-step.pending .mc-step-title { color: alpha(currentColor, 0.55); }
.mc-caption { font-size: 12px; color: alpha(currentColor, 0.6); }
.mc-orange { color: @orange_4; }
.mc-red { color: @red_3; }
.mc-green { color: @green_5; }
.mc-gh-code { font-family: monospace; font-size: 30pt; font-weight: 600; }
expander-widget.mc-details > box > title { color: alpha(currentColor, 0.6); }
.mc-terminal-frame { border-radius: 10px; background: #141414; }
textview.mc-terminal, textview.mc-terminal text { background-color: #141414; color: #ebebeb; font-size: 12px; }
"""

_css_loaded = False


def load_css() -> None:
    global _css_loaded
    display = Gdk.Display.get_default()
    if _css_loaded or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def idle(fn, *args) -> None:
    """Hands a pseudo-terminal callback to the GTK main loop (runs once, in order)."""
    def run():
        fn(*args)
        return False
    GLib.idle_add(run)


def copy_text(text: str | None) -> None:
    display = Gdk.Display.get_default()
    if display is not None and text:
        display.get_clipboard().set(text)


def caption(text: str = "", *classes: str) -> Gtk.Label:
    return widgets.label(text, "mc-caption", *classes, wrap=True)


def notice(text: str, icon: str, *classes: str) -> tuple[Gtk.Box, Gtk.Label]:
    """A label with a leading icon (SwiftUI `Label(text, systemImage:)`)."""
    box = Gtk.Box(spacing=8)
    image = Gtk.Image(icon_name=icon, valign=Gtk.Align.START)
    text_label = widgets.label(text, wrap=True)
    text_label.set_hexpand(True)
    for css in classes:
        box.add_css_class(css)
    box.append(image)
    box.append(text_label)
    return box, text_label


def round_avatar(name: str, color: ProfileColor, size: int = 40) -> Gtk.Label:
    """AccountAvatar (Shared.swift:30-46): a circle in the account color with the initial."""
    avatar = widgets.avatar((name[:1] or "?").upper(), color, size)
    avatar.add_css_class("mc-login-round")
    return avatar


def in_navigation_stack(page: Adw.NavigationPage) -> bool:
    """Whether `page` is still on its Adw.NavigationView (False once popped or replaced)."""
    view = page.get_ancestor(Adw.NavigationView)
    if view is None:
        return False
    stack = view.get_navigation_stack()
    return any(stack.get_item(i) is page for i in range(stack.get_n_items()))


# MARK: browser choice (BrowserChoice.swift)

class BrowserChoice:
    """Which browser opens Claude's authorization link. The CLI runs with a no-op
    BROWSER, so the app opens the link itself, in the chosen browser, or only copies it.

    `current`: "" = default browser, COPY_ONLY, or a desktop app id. Kept for the life
    of the app (the Mac keeps it in @AppStorage)."""

    COPY_ONLY = "copy"
    current = ""

    @staticmethod
    def installed() -> list[Gio.AppInfo]:
        """Installed apps that open https links, one per name (Mac: urlsForApplications)."""
        seen, browsers = set(), []
        for app in Gio.AppInfo.get_all_for_type("x-scheme-handler/https"):
            name = app.get_display_name()
            if name not in seen:
                seen.add(name)
                browsers.append(app)
        return browsers

    @staticmethod
    def default() -> Gio.AppInfo | None:
        return Gio.AppInfo.get_default_for_uri_scheme("https")

    @classmethod
    def _app(cls, choice: str) -> Gio.AppInfo | None:
        return next((app for app in cls.installed() if app.get_id() == choice), None) if choice else None

    @classmethod
    def label(cls, choice: str) -> str:
        if choice == cls.COPY_ONLY:
            return "Não abrir, só copiar o link"
        app = cls._app(choice)
        if app is not None:
            return app.get_display_name()
        default = cls.default()
        return "Navegador padrão" + (f" ({default.get_display_name()})" if default is not None else "")

    @classmethod
    def open(cls, url: str, choice: str) -> bool:
        """Opens `url` by the choice; False when it only copies the link (or nothing opened it)."""
        if choice == cls.COPY_ONLY:
            copy_text(url)
            return False
        try:
            app = cls._app(choice)
            if app is not None:
                app.launch_uris([url], None)
            else:
                Gio.AppInfo.launch_default_for_uri(url, None)
            return True
        except GLib.Error:
            copy_text(url)  # no browser could open it: the link is on the clipboard
            return False

    @staticmethod
    def open_in(url: str, app: Gio.AppInfo) -> None:
        try:
            app.launch_uris([url], None)
        except GLib.Error:
            copy_text(url)


class BrowserChoiceRow(Adw.ComboRow):
    """"Abrir o link em" (BrowserChoicePicker): default browser, the installed ones, and
    "Não abrir, só copiar o link"."""

    def __init__(self):
        super().__init__(title="Abrir o link em")
        self.choices = [""] + [app.get_id() for app in BrowserChoice.installed()] + [BrowserChoice.COPY_ONLY]
        self.labels = [BrowserChoice.label(choice) for choice in self.choices]
        self.set_model(Gtk.StringList.new(self.labels))
        current = BrowserChoice.current if BrowserChoice.current in self.choices else ""
        self.set_selected(self.choices.index(current))
        self.connect("notify::selected", self._changed)

    def _changed(self, *_args) -> None:
        index = self.get_selected()
        if 0 <= index < len(self.choices):
            BrowserChoice.current = self.choices[index]


class AuthorizationLinkActions(Gtk.Box):
    """"Copiar link" (2 s "Link copiado") + "Abrir em" for a link already issued."""

    def __init__(self):
        super().__init__(spacing=8)
        self.url: str | None = None
        self.copy_button = Gtk.Button()
        self.copy_content = Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copiar link")
        self.copy_button.set_child(self.copy_content)
        self.copy_button.connect("clicked", lambda _b: self._copy())
        self.open_button = Gtk.MenuButton(child=Adw.ButtonContent(icon_name="web-browser-symbolic", label="Abrir em"))
        self.open_button.set_popover(self._popover())
        for button in (self.copy_button, self.open_button):
            button.add_css_class("flat")
            self.append(button)
        self._reset_source = None
        self.set_url(None)

    def set_url(self, url: str | None) -> None:
        self.url = url
        self.set_sensitive(url is not None)

    def _copy(self) -> None:
        if not self.url:
            return
        copy_text(self.url)
        self.copy_content.set_label("Link copiado")
        self.copy_content.set_icon_name("object-select-symbolic")
        if self._reset_source is not None:
            GLib.source_remove(self._reset_source)
        self._reset_source = GLib.timeout_add_seconds(2, self._reset)

    def _reset(self) -> bool:
        self._reset_source = None
        self.copy_content.set_label("Copiar link")
        self.copy_content.set_icon_name("edit-copy-symbolic")
        return False

    def _popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover(child=box)

        def item(text, action):
            button = Gtk.Button(label=text)
            button.add_css_class("flat")
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda _b: (popover.popdown(), self.url and action(self.url)))
            box.append(button)

        default = BrowserChoice.default()
        if default is not None:
            item(f"Navegador padrão ({default.get_display_name()})", lambda url: BrowserChoice.open(url, ""))
            box.append(Gtk.Separator(margin_top=3, margin_bottom=3))
        for app in BrowserChoice.installed():
            item(app.get_display_name(), lambda url, a=app: BrowserChoice.open_in(url, a))
        return popover


# MARK: steps and terminal

class StepRow(Gtk.Box):
    """A numbered step: circle (number, ✓ or ✗), the title and, while active or failed,
    its detail. States: pending, active, done, failed."""

    SYMBOLS = {"done": "✓", "failed": "✕"}

    def __init__(self, number: int, title: str):
        super().__init__(spacing=12)
        self.number = number
        self.title_text = title
        self.state = "pending"
        self.add_css_class("mc-step")
        self.badge = widgets.label(str(number), "mc-step-badge", xalign=0.5)
        self.badge.set_valign(Gtk.Align.START)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.title = widgets.label(title, "mc-step-title", wrap=True)
        self.title.set_margin_top(3)
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        column.append(self.title)
        column.append(self.detail)
        self.append(self.badge)
        self.append(column)
        self.set_state("pending")

    def set_state(self, state: str) -> None:
        for name in ("pending", "active", "done", "failed"):
            self.remove_css_class(name)
        self.add_css_class(state)
        self.state = state
        self.badge.set_label(self.SYMBOLS.get(state, str(self.number)))
        self.detail.set_visible(state in ("active", "failed"))


class TerminalOutput(Gtk.ScrolledWindow):
    """The CLI's output: folder (gray), "$ command" and the masked text (dark box, 190 px)."""

    def __init__(self, directory: str, command: str = ""):
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        self.view.add_css_class("mc-terminal")
        super().__init__(min_content_height=190, max_content_height=190, child=self.view)
        self.add_css_class("mc-terminal-frame")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Saída do terminal"])
        buffer = self.view.get_buffer()
        buffer.create_tag("dim", foreground="#8e8e93")
        buffer.create_tag("prompt", foreground="#64a0ff")
        self.directory = directory
        self.command = command
        self._output = None
        self.set_output("")

    def set_output(self, text: str, command: str | None = None) -> None:
        text = (text or "").replace("\r", "")
        command = self.command if command is None else command
        if text == self._output and command == self.command:
            return
        self._output, self.command = text, command
        buffer = self.view.get_buffer()
        buffer.set_text("")
        end = buffer.get_end_iter()
        buffer.insert_with_tags_by_name(end, self.directory + "\n", "dim")
        buffer.insert_with_tags_by_name(buffer.get_end_iter(), "$ ", "prompt")
        buffer.insert(buffer.get_end_iter(), command + "\n" + text)
        GLib.idle_add(self._scroll_to_end)

    def text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _scroll_to_end(self) -> bool:
        adjustment = self.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return False


def set_pill(pill: Gtk.Label, text: str, tone: str) -> None:
    for name in ("ok", "warning", "error", "neutral"):
        pill.remove_css_class(name)
    pill.add_css_class(tone)
    pill.set_label(text)


def page_body(content: Gtk.Widget) -> Adw.ToolbarView:
    """The header bar (back button from the navigation view) over `content`."""
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(content)
    return view


# MARK: Entrar no GitHub

class GitHubLoginPage(Adw.NavigationPage):
    """"Entrar no GitHub" (AccountsPages.swift:340-446): gh's one-time code, "Copiar
    código e abrir o GitHub", a status pill, the collapsible output, and Concluir /
    Voltar + Tentar de novo / Cancelar. Starts `gh auth login --web` when opened."""

    STATUS = {"starting": ("Iniciando", "warning"), "showingCode": ("Aguardando você", "warning"),
              "waitingForBrowser": ("Aguardando navegador", "warning"), "connected": ("Conectada", "ok"),
              "failed": ("Erro", "error")}

    def __init__(self, window, host: str = "github.com"):
        super().__init__(title="Entrar no GitHub")
        load_css()
        self.window = window
        self.model = window.model
        self.host = host
        self.session: GitHubLoginSession | None = None
        self.show_output = False
        self.reported = False
        self._left = False
        self._build()
        self.connect("hidden", lambda _page: GLib.idle_add(self._check_left))
        GLib.idle_add(self._start)

    @property
    def is_test_mode(self) -> bool:
        return self.model.environment.mode != "real"

    def _build(self) -> None:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=20, margin_bottom=20,
                       margin_start=20, margin_end=20)
        body.add_css_class("mc-login-page")

        header = Gtk.Box(spacing=12)
        icon = Gtk.Image(icon_name="multicred-code-symbolic", pixel_size=20, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)
        circle = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        circle.add_css_class("mc-login-icon-circle")
        icon.set_hexpand(True)
        circle.set_hexpand(False)  # the icon centers itself; the circle keeps its 40 px
        circle.append(icon)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.heading_label = widgets.label("Entrar no GitHub", "mc-login-heading")
        self.host_label = widgets.label(self.host, "dim-label")
        titles.append(self.heading_label)
        titles.append(self.host_label)
        self.status_pill = widgets.pill("Iniciando", "warning")
        header.append(circle)
        header.append(titles)
        header.append(self.status_pill)
        body.append(header)

        self.test_notice, self.test_notice_label = notice(
            "Simulação do modo de teste: o navegador não abre e nenhum login real acontece.",
            "applications-science-symbolic", "mc-orange")
        self.test_notice.set_visible(self.is_test_mode)
        body.append(self.test_notice)

        # Starting.
        self.starting_box = Gtk.Box(spacing=8)
        self.starting_box.append(Gtk.Spinner(spinning=True))
        self.starting_box.append(widgets.label("Iniciando o GitHub CLI…", "dim-label"))
        body.append(self.starting_box)

        # The code (showingCode, waitingForBrowser).
        self.code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, visible=False)
        self.instructions_label = widgets.label(
            "1. Copie este código. 2. Abra o GitHub e cole o código. 3. Autorize o GitHub CLI.", "dim-label",
            wrap=True)
        line = Gtk.Box(spacing=14)
        # Not selectable: a selectable label takes the focus and shows the code selected;
        # the button copies it anyway.
        self.code_label = widgets.label("", "mc-gh-code")
        self.open_button = Gtk.Button(label="Copiar código e abrir o GitHub", valign=Gtk.Align.CENTER)
        self.open_button.add_css_class("suggested-action")
        self.open_button.connect("clicked", lambda _b: self._open_github())
        line.append(self.code_label)
        line.append(self.open_button)
        self.waiting_box = Gtk.Box(spacing=8, visible=False)
        self.waiting_box.append(Gtk.Spinner(spinning=True))
        self.waiting_label = widgets.label("Aguardando a autorização no navegador…", "dim-label")
        self.waiting_box.append(self.waiting_label)
        for widget in (self.instructions_label, line, self.waiting_box):
            self.code_box.append(widget)
        body.append(self.code_box)

        # Connected / failed.
        self.done_box, self.done_label = notice("", "emblem-ok-symbolic", "mc-green")
        self.done_box.set_visible(False)
        body.append(self.done_box)
        self.error_label = widgets.label("", "mc-red", wrap=True, selectable=True)
        self.error_label.set_visible(False)
        body.append(self.error_label)

        self.terminal = TerminalOutput("~")
        self.terminal.set_visible(False)
        body.append(self.terminal)

        body.append(Gtk.Box(vexpand=True))
        footer = Gtk.Box(spacing=8)
        self.output_toggle = Gtk.Button()
        self.output_toggle.add_css_class("flat")
        toggle = Gtk.Box(spacing=6)
        self.output_toggle_icon = Gtk.Image(icon_name="pan-end-symbolic")
        self.output_toggle_label = widgets.label("Mostrar saída")
        toggle.append(self.output_toggle_icon)
        toggle.append(self.output_toggle_label)
        self.output_toggle.set_child(toggle)
        self.output_toggle.add_css_class("dim-label")
        self.output_toggle.connect("clicked", lambda _b: self._toggle_output())
        footer.append(self.output_toggle)
        footer.append(Gtk.Box(hexpand=True))
        self.done_button = widgets.button("Concluir", self._finish, "suggested-action")
        self.back_button = widgets.button("Voltar", self._back)
        self.retry_button = widgets.button("Tentar de novo", self._retry, "suggested-action")
        self.cancel_button = widgets.button("Cancelar", self._cancel)
        for button in (self.back_button, self.retry_button, self.cancel_button, self.done_button):
            footer.append(button)
        body.append(footer)

        self.set_child(page_body(Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER)))
        self._refresh()

    # MARK: session

    def _switch_back_user(self) -> str | None:
        """The active profile's gh user on this host (D34); None leaves gh as the login left it."""
        config, profile = self.model.config, self.model.active_profile
        if config is None or profile is None:
            return None
        account = config.github_account(profile.github_account_id)
        return account.username if account is not None and account.host == self.host else None

    def _start(self) -> bool:
        if self._left:
            return False
        self.reported = False
        self.session = GitHubLoginSession(self.model.environment, self.model.tools, self.host,
                                          self._switch_back_user(), dispatch=idle,
                                          on_change=lambda _s: self._refresh(), on_finished=self._finished,
                                          log=getattr(self.model, "log", None))
        self.session.start()
        self._refresh()
        return False

    def _finished(self, session) -> None:
        if session is not self.session or self._left:
            return
        self.reported = True
        self._refresh()
        if session.phase.kind == "connected":
            self.window.login_finished(LoginResult(LoginKind.GITHUB, True, login=session.login,
                                                   switched_back=session.switched_back,
                                                   error=session.switch_back_error))

    def _check_left(self) -> bool:
        """After a pop or a section change: stop gh (Mac: stopSessionsNoLongerShown)."""
        if self._left or in_navigation_stack(self):
            return False
        self._left = True
        self.stop()
        return False

    def stop(self) -> None:
        session = self.session
        if session is None:
            return
        if not session.phase.is_finished:
            session.cancel()
        session.stop()

    # MARK: actions

    def _open_github(self) -> None:
        session = self.session
        if session is None or session.phase.kind != "showingCode":
            return
        copy_text(session.code)
        session.open_browser()
        self._refresh()

    def _toggle_output(self) -> None:
        self.show_output = not self.show_output
        self._refresh()

    def _finish(self) -> None:
        self.window.pop_to_section_root()

    def _back(self) -> None:
        self.window.nav.pop()

    def _cancel(self) -> None:
        if self.session is not None:
            self.session.cancel()
        self.window.nav.pop()

    def _retry(self) -> None:
        if self.session is not None:
            self.session.stop()
        self._start()

    # MARK: state → widgets

    def _done_text(self, session) -> str:
        text = f"Pronto: {session.login or 'conta'} está logada no gh."
        if session.switched_back:
            text += " A conta ativa voltou para a do perfil atual."
        elif session.switch_back_error:
            text += f" Não consegui voltar a conta ativa para {session.switch_back_to}: {session.switch_back_error}"
        return text

    def _refresh(self) -> None:
        session = self.session
        kind = session.phase.kind if session is not None else "starting"
        text, tone = self.STATUS.get(kind, ("Iniciando", "warning"))
        set_pill(self.status_pill, text, tone)
        self.starting_box.set_visible(kind == "starting")
        self.code_box.set_visible(kind in ("showingCode", "waitingForBrowser"))
        code = session.code if session is not None else None
        self.code_label.set_label(code or "")
        self.open_button.set_sensitive(kind == "showingCode")
        self.waiting_box.set_visible(kind == "waitingForBrowser")
        self.done_box.set_visible(kind == "connected")
        if kind == "connected":
            self.done_label.set_label(self._done_text(session))
        failed = kind == "failed"
        self.error_label.set_visible(failed)
        if failed:
            self.error_label.set_label(session.launch_error or f"Não deu certo ({session.phase.value}).")

        shown = self.show_output or failed
        self.terminal.set_visible(shown)
        if session is not None and session.command is not None:
            self.terminal.set_output(session.output, session.command.display)
        self.output_toggle_label.set_label("Ocultar saída" if self.show_output else "Mostrar saída")
        self.output_toggle_icon.set_from_icon_name("pan-down-symbolic" if self.show_output else "pan-end-symbolic")

        self.done_button.set_visible(kind == "connected")
        self.back_button.set_visible(failed)
        self.retry_button.set_visible(failed)
        self.cancel_button.set_visible(kind not in ("connected", "failed"))

    def refresh(self) -> None:
        """Model changes (window.refresh): nothing on this page depends on them."""
