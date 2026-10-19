"""Login dialog for Claude (full login or setup-token) and GitHub (approved Linux design H).

Port of the macOS Login/{LoginWindowView,LoginSession,GitHubLoginSession,
SetupTokenSession,BrowserChoice}.swift as one standalone `Adw.Dialog`: avatar
and title, three steps, the code field (sent to the CLI with Enter separately),
"Copiar link de novo", "Abrir em outro navegador", the collapsible terminal
output, and Cancelar / Copiar log / Conectar. The CLI never opens a browser by
itself (BROWSER is a no-op); the dialog opens the link in the default browser,
or in the one picked from the menu.

`on_result(LoginResult)` runs exactly once: when the login is saved (connected),
or when the dialog closes without success. In test mode the simulators run
instead of the real `claude`/`gh` (D30) and the browser never opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ...core.adapters.github_login_flow import GitHubLoginSession  # noqa: E402
from ...core.claude.login_flow import (  # noqa: E402
    ClaudeAccountSaver, ClaudeLoginSession, LoginMode, LoginPhase, NewClaudeAccount,
)
from ...core.engine.types import SwitchError  # noqa: E402
from ...core.models import ClaudeAccount, ClaudeAccountKind, ProfileColor  # noqa: E402
from ..messages import describe_switch_error  # noqa: E402


class LoginKind(str, Enum):
    CLAUDE_LOGIN = "claude-login"
    CLAUDE_TOKEN = "claude-token"
    GITHUB = "github"


@dataclass(frozen=True)
class LoginResult:
    kind: LoginKind
    success: bool
    # Claude: the account stored (new, or the existing one updated).
    account: ClaudeAccount | None = None
    # GitHub: who gh logged in, and whether the active account went back to the profile's.
    login: str | None = None
    switched_back: bool = False
    error: str | None = None


_CSS = """
.mc-login-avatar { min-width: 40px; min-height: 40px; border-radius: 10px; color: white; font-weight: 800;
                   font-size: 17px; }
.mc-login-title { font-weight: 800; font-size: 17px; }
.mc-login-step { font-size: 13.5px; }
.mc-login-step.todo { color: alpha(currentColor, 0.5); }
.mc-login-badge { min-width: 22px; min-height: 22px; border-radius: 11px; font-size: 12px; font-weight: 800;
                  background: alpha(currentColor, 0.1); }
.mc-login-step.done .mc-login-badge { background: alpha(@success_color, 0.18); color: @success_color; }
.mc-login-step.current .mc-login-badge { background: @accent_bg_color; color: @accent_fg_color; }
.mc-login-step.failed .mc-login-badge { background: alpha(@error_color, 0.18); color: @error_color; }
.mc-login-code { font-family: monospace; font-size: 22px; font-weight: 800; letter-spacing: 2px; }
.mc-terminal-frame { border-radius: 9px; background: #1e1e1e; }
textview.mc-terminal, textview.mc-terminal text { background-color: #1e1e1e; color: #d0d0d0; font-size: 11.5px; }
""" + "".join(f".mc-login-{color.value} {{ background: {color.hex}; }}\n" for color in ProfileColor)

_css_loaded = False

STEP_SYMBOLS = {"done": "✓", "failed": "!"}


def _load_css() -> None:
    global _css_loaded
    display = Gdk.Display.get_default()
    if _css_loaded or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def _idle(fn, *args) -> None:
    """Hands a pseudo-terminal callback to the GTK main loop (runs once, in order)."""
    def run():
        fn(*args)
        return False
    GLib.idle_add(run)


def https_browsers() -> list[Gio.AppInfo]:
    """Installed apps that open https links, one per name."""
    seen, browsers = set(), []
    for app in Gio.AppInfo.get_all_for_type("x-scheme-handler/https"):
        name = app.get_display_name()
        if name not in seen:
            seen.add(name)
            browsers.append(app)
    return browsers


def default_browser_name() -> str | None:
    app = Gio.AppInfo.get_default_for_uri_scheme("https")
    return app.get_display_name() if app is not None else None


class StepRow(Gtk.Box):
    """A numbered step: badge (number, ✓ or !) and title; state done/current/todo/failed."""

    def __init__(self, number: int, title: str):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.number = number
        self.state = "todo"
        self.add_css_class("mc-login-step")
        self.badge = Gtk.Label(label=str(number), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.badge.add_css_class("mc-login-badge")
        self.title = Gtk.Label(label=title, xalign=0, wrap=True, hexpand=True)
        self.append(self.badge)
        self.append(self.title)
        self.set_state("todo")

    def set_state(self, state: str) -> None:
        for name in ("done", "current", "todo", "failed"):
            self.remove_css_class(name)
        self.add_css_class(state)
        self.state = state
        self.badge.set_label(STEP_SYMBOLS.get(state, str(self.number)))

    def set_title(self, title: str) -> None:
        self.title.set_label(title)


class LoginDialog(Adw.Dialog):
    """`model` is the AppModel (environment, tools, config, save, active profile).
    `target`: a NewClaudeAccount or an existing ClaudeAccount (Claude kinds).
    `host`: the GitHub host (GitHub kind)."""

    def __init__(self, model, kind: LoginKind, on_result: Callable[[LoginResult], None], *,
                 target: NewClaudeAccount | ClaudeAccount | None = None, host: str = "github.com"):
        super().__init__()
        _load_css()
        self.model = model
        self.kind = LoginKind(kind)
        self.on_result = on_result
        self.target = target
        self.host = host
        self.session: ClaudeLoginSession | None = None
        self.github_session: GitHubLoginSession | None = None
        self._reported = False
        self._closed = False
        self._opened_url: str | None = None
        self._browser_name: str | None = None
        if self.is_claude and target is None:
            raise ValueError("a Claude login needs the account (new or existing)")
        self.set_title("Entrar no GitHub" if self.kind == LoginKind.GITHUB else "Entrar no Claude")
        self.set_content_width(560)
        self._build()
        self.connect("closed", self._on_closed)
        GLib.idle_add(self._start)

    @property
    def is_claude(self) -> bool:
        return self.kind != LoginKind.GITHUB

    @property
    def is_test_mode(self) -> bool:
        return self.model.environment.mode != "real"

    # MARK: layout

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=22, margin_bottom=18,
                       margin_start=28, margin_end=28)
        root.append(body)

        header = Gtk.Box(spacing=14)
        name, color = self._identity()
        self.avatar = Gtk.Label(label=(name[:1] or "?").upper(), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.avatar.add_css_class("mc-login-avatar")
        self.avatar.add_css_class(f"mc-login-{color.value}")
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title_label = Gtk.Label(label=self._title_text(name), xalign=0, wrap=True)
        self.title_label.add_css_class("mc-login-title")
        self.subtitle_label = Gtk.Label(label=self._subtitle_text(), xalign=0, wrap=True)
        self.subtitle_label.add_css_class("dim-label")
        titles.append(self.title_label)
        titles.append(self.subtitle_label)
        header.append(self.avatar)
        header.append(titles)
        body.append(header)

        self.test_notice = Gtk.Label(label=self._test_notice_text(), xalign=0, wrap=True, visible=self.is_test_mode)
        self.test_notice.add_css_class("warning")
        body.append(self.test_notice)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.steps = [StepRow(i + 1, title) for i, title in enumerate(self._step_titles())]
        for step in self.steps:
            steps_box.append(step)
        body.append(steps_box)

        # Claude: the authorization code, masked as it is typed.
        self.code_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, visible=self.is_claude)
        self.code_list.add_css_class("boxed-list")
        self.code_row = Adw.PasswordEntryRow(title="Código de autorização")
        self.code_row.connect("entry-activated", lambda _row: self._send_code())
        self.code_row.connect("changed", lambda _row: self._refresh())
        self.code_list.append(self.code_row)
        body.append(self.code_list)

        # GitHub: the one-time code to type on github.com/login/device.
        # Not selectable: a selectable label takes the focus and shows the code selected;
        # "Abrir o GitHub" copies it anyway.
        self.github_code_label = Gtk.Label(label="", visible=False, halign=Gtk.Align.START)
        self.github_code_label.add_css_class("mc-login-code")
        body.append(self.github_code_label)

        links = Gtk.Box(spacing=8)
        self.copy_link_button = Gtk.Button(label="Copiar link de novo")
        self.copy_link_button.add_css_class("flat")
        self.copy_link_button.connect("clicked", lambda _b: self._copy_link())
        self.browser_button = Gtk.MenuButton(label="Abrir em outro navegador")
        self.browser_button.add_css_class("flat")
        self.browser_button.set_popover(self._browser_popover())
        self.terminal_toggle = Gtk.Button(label="▸ Mostrar terminal")
        self.terminal_toggle.add_css_class("flat")
        self.terminal_toggle.connect("clicked", lambda _b: self._toggle_terminal())
        links.append(self.copy_link_button)
        links.append(self.browser_button)
        links.append(Gtk.Box(hexpand=True))
        links.append(self.terminal_toggle)
        body.append(links)

        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False, selectable=True)
        self.error_label.add_css_class("error")
        body.append(self.error_label)
        self.done_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.done_label.add_css_class("success")
        body.append(self.done_label)

        self.terminal_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                          wrap_mode=Gtk.WrapMode.CHAR, top_margin=10, bottom_margin=10,
                                          left_margin=12, right_margin=12)
        self.terminal_view.add_css_class("mc-terminal")
        # Fixed height (like the macOS panel): a height that follows the text would
        # make the dialog jump at every line.
        self.terminal_scroll = Gtk.ScrolledWindow(min_content_height=170, child=self.terminal_view)
        self.terminal_scroll.add_css_class("mc-terminal-frame")
        self.terminal_scroll.set_overflow(Gtk.Overflow.HIDDEN)
        self.terminal_revealer = Gtk.Revealer(child=self.terminal_scroll, reveal_child=False,
                                              transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        body.append(self.terminal_revealer)

        root.append(Gtk.Separator())
        footer = Gtk.Box(spacing=10, margin_top=14, margin_bottom=14, margin_start=28, margin_end=28)
        self.cancel_button = Gtk.Button(label="Cancelar")
        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.copy_log_button = Gtk.Button(label="Copiar log")
        self.copy_log_button.add_css_class("flat")
        self.copy_log_button.connect("clicked", lambda _b: self._copy(self.log_text()))
        self.retry_button = Gtk.Button(label="Tentar de novo", visible=False)
        self.retry_button.connect("clicked", lambda _b: self._retry())
        self.connect_button = Gtk.Button(label="Abrir o GitHub" if self.kind == LoginKind.GITHUB else "Conectar")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.connect("clicked", lambda _b: self._primary())
        self.done_button = Gtk.Button(label="Concluir", visible=False)
        self.done_button.add_css_class("suggested-action")
        self.done_button.connect("clicked", lambda _b: self.close())
        footer.append(self.cancel_button)
        footer.append(Gtk.Box(hexpand=True))
        for button in (self.copy_log_button, self.retry_button, self.connect_button, self.done_button):
            footer.append(button)
        root.append(footer)

        self.set_child(root)
        self.set_default_widget(self.connect_button)
        self._refresh()

    def _identity(self) -> tuple[str, ProfileColor]:
        if self.kind == LoginKind.GITHUB:
            return "GitHub", ProfileColor.GRAPHITE
        return self.target.nickname, self.target.color

    def _title_text(self, name: str) -> str:
        if self.kind == LoginKind.GITHUB:
            return "Entrar no GitHub" if self.host == "github.com" else f"Entrar no GitHub ({self.host})"
        return f"Entrar com {name}"

    def _subtitle_text(self) -> str:
        return {LoginKind.CLAUDE_LOGIN: "claude auth login numa pasta temporária",
                LoginKind.CLAUDE_TOKEN: "claude setup-token numa pasta temporária",
                LoginKind.GITHUB: "gh auth login no navegador"}[self.kind]

    def _test_notice_text(self) -> str:
        if self.kind == LoginKind.GITHUB:
            return "Modo de teste: simulação do gh; o navegador não abre. Clique em “Abrir o GitHub” para conectar."
        return ("Modo de teste: simulação; o navegador não abre. Digite qualquer código para conectar, "
                "ou “erro” para ver a tela de erro.")

    def _step_titles(self) -> list[str]:
        if self.kind == LoginKind.GITHUB:
            return ["Copie o código de uso único", "Abra o GitHub e cole o código", "Voltar para a conta do perfil"]
        return ["Abrir o navegador", "Cole o código que apareceu na página", "Conectar e guardar no chaveiro"]

    def _browser_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover(child=box)
        browsers = [] if self.is_test_mode else https_browsers()
        if not browsers:
            box.append(Gtk.Label(label="Nenhum navegador encontrado." if not self.is_test_mode
                                 else "No modo de teste o navegador não abre.", margin_top=6, margin_bottom=6))
        for app in browsers:
            button = Gtk.Button(label=app.get_display_name())
            button.add_css_class("flat")
            button.connect("clicked", lambda _b, a=app: (popover.popdown(), self._open_in(a)))
            box.append(button)
        return popover

    # MARK: sessions

    def _start(self) -> bool:
        if self._closed:
            return False
        environment, tools = self.model.environment, self.model.tools
        log = getattr(self.model, "log", None)
        if self.kind == LoginKind.GITHUB:
            self.github_session = GitHubLoginSession(environment, tools, self.host, self._switch_back_user(),
                                                     dispatch=_idle, on_change=self._session_changed,
                                                     on_finished=self._session_finished, log=log)
            self.github_session.start()
        else:
            engine = getattr(self.model, "engine", None)
            saver = ClaudeAccountSaver(environment, lambda: self.model.config, self.model.save,
                                       exclusive=engine.exclusive if engine is not None else None)
            mode = LoginMode.LOGIN if self.kind == LoginKind.CLAUDE_LOGIN else LoginMode.SETUP_TOKEN
            self.session = ClaudeLoginSession(environment, tools, mode, self.target, saver, dispatch=_idle,
                                              on_change=self._session_changed, on_finished=self._session_finished,
                                              log=log)
            self.session.start()
        self._refresh()
        return False

    def _switch_back_user(self) -> str | None:
        """The active profile's gh user on this host (D34); None leaves gh as the login left it."""
        config, profile = self.model.config, self.model.active_profile
        if config is None or profile is None:
            return None
        account = config.github_account(profile.github_account_id)
        return account.username if account is not None and account.host == self.host else None

    def _session_changed(self, _session) -> None:
        if self._closed:
            return
        if self.session is not None:
            url = self.session.authorization_url
            if url and url != self._opened_url and not self.is_test_mode:
                # The CLI does not open a browser (BROWSER is a no-op): open it here, once per link.
                self._opened_url = url
                self._open_default(url)
        self._refresh()

    def _session_finished(self, _session) -> None:
        if self._closed:
            return
        self._refresh()
        if self.session is not None and self.session.phase == LoginPhase.CONNECTED:
            self._report(LoginResult(self.kind, True, account=self.session.result))
        elif self.github_session is not None and self.github_session.phase.kind == "connected":
            gh = self.github_session
            self._report(LoginResult(self.kind, True, login=gh.login, switched_back=gh.switched_back,
                                     error=gh.switch_back_error))

    def _report(self, result: LoginResult) -> None:
        if self._reported:
            return
        self._reported = True
        self.on_result(result)

    def _on_closed(self, _dialog) -> None:
        self._closed = True
        if self.session is not None:
            if not self.session.phase.is_finished:
                self.session.cancel()
            self.session.stop()
        if self.github_session is not None:
            if not self.github_session.phase.is_finished:
                self.github_session.cancel()
            self.github_session.stop()
        self._report(LoginResult(self.kind, False, error=self._failure_reason() or "cancelado"))

    def _retry(self) -> None:
        if self.session is not None:
            self.session.stop()
        if self.github_session is not None:
            self.github_session.stop()
        self.session = self.github_session = None
        self._opened_url = None
        self.code_row.set_text("")
        self._start()

    # MARK: actions

    def _primary(self) -> None:
        if self.kind == LoginKind.GITHUB:
            gh = self.github_session
            if gh is None or gh.phase.kind != "showingCode":
                return
            self._copy(gh.code or "")
            gh.open_browser()
        else:
            self._send_code()

    def _send_code(self) -> None:
        if self.session is None:
            return
        code = self.code_row.get_text()
        self.session.submit_code(code)
        self.code_row.set_text("")
        self._refresh()

    def _authorization_link(self) -> str | None:
        if self.session is not None:
            return self.session.authorization_url
        if self.github_session is not None:
            return self.github_session.device_url
        return None

    def _copy_link(self) -> None:
        url = self._authorization_link()
        if url:
            self._copy(url)

    def _copy(self, text: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None and text:
            display.get_clipboard().set(text)

    def _open_default(self, url: str) -> None:
        self._browser_name = default_browser_name()
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error:
            self._copy(url)  # no browser could open it: the link is on the clipboard

    def _open_in(self, app: Gio.AppInfo) -> None:
        url = self._authorization_link()
        if not url or self.is_test_mode:
            return
        try:
            app.launch_uris([url], None)
            self._browser_name = app.get_display_name()
        except GLib.Error:
            self._copy(url)
        self._refresh()

    def _toggle_terminal(self) -> None:
        self.terminal_revealer.set_reveal_child(not self.terminal_revealer.get_reveal_child())
        self._refresh()

    # MARK: texts for tests and "Copiar log"

    def log_text(self) -> str:
        """Masked output: codes and tokens never appear."""
        if self.session is not None:
            return self.session.redacted_log
        if self.github_session is not None:
            return self.github_session.output
        return ""

    def terminal_text(self) -> str:
        buffer = self.terminal_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    # MARK: state → widgets

    def _phase_kind(self) -> str:
        if self.session is not None:
            return self.session.phase.kind
        if self.github_session is not None:
            return self.github_session.phase.kind
        return "starting"

    def _failure_reason(self) -> str | None:
        session = self.session or self.github_session
        if session is None or not session.phase.is_failure:
            return None
        save_error = getattr(session, "save_error", None)
        if isinstance(save_error, SwitchError):  # e.g. Busy: a switch was running, nothing changed
            return f"Não consegui guardar o login: {describe_switch_error(save_error)}"
        if session.launch_error:
            return session.launch_error
        reason = session.phase.message if self.session is not None else session.phase.value
        if self.is_claude and reason and "400" in reason:
            return ("O Claude recusou o código. Ele vale uma vez e por poucos minutos, e só para o link aberto "
                    "nesta tentativa. Tente de novo.")
        return f"Não deu certo ({reason})."

    def _refresh(self) -> None:
        kind = self._phase_kind()
        finished = kind in ("connected", "failed")
        failed = kind == "failed"
        if self.kind == LoginKind.GITHUB:
            self._refresh_github(kind)
        else:
            self._refresh_claude(kind)

        command = (self.session or self.github_session).command if (self.session or self.github_session) else None
        output = self.log_text().replace("\r", "")
        text = (f"$ {command.display}\n" if command is not None else "") + output
        if self.terminal_text() != text:
            self.terminal_view.get_buffer().set_text(text)
            GLib.idle_add(self._scroll_terminal_to_end)

        if failed and not self.terminal_revealer.get_reveal_child():
            self.terminal_revealer.set_reveal_child(True)  # the output explains the failure
        shown = self.terminal_revealer.get_reveal_child()
        self.terminal_toggle.set_label("▾ Ocultar terminal" if shown else "▸ Mostrar terminal")

        reason = self._failure_reason()
        self.error_label.set_visible(reason is not None)
        self.error_label.set_label(reason or "")
        self.retry_button.set_visible(failed)
        self.cancel_button.set_visible(kind != "connected")
        self.cancel_button.set_label("Fechar" if failed else "Cancelar")
        self.done_button.set_visible(kind == "connected")
        self.connect_button.set_visible(not finished)
        has_link = self._authorization_link() is not None
        self.copy_link_button.set_sensitive(has_link and not finished)
        self.browser_button.set_sensitive(has_link and not finished and not self.is_test_mode)

    def _refresh_claude(self, kind: str) -> None:
        phase = self.session.phase if self.session is not None else LoginPhase.STARTING
        step1, step2, step3 = self.steps
        step1.set_state("current" if kind == "starting" else "done")
        if kind == "starting":
            step1.set_title("Abrir o navegador")
        elif self.is_test_mode:
            step1.set_title("Navegador aberto (simulado)")
        else:
            step1.set_title(f"Navegador aberto ({self._browser_name})" if self._browser_name else "Navegador aberto")
        step2.set_state("todo" if kind == "starting" else "current" if phase.accepts_code else "done")
        step3.set_state({"connecting": "current", "connected": "done", "failed": "failed"}.get(kind, "todo"))
        self.code_list.set_visible(kind not in ("connected", "failed"))
        self.code_row.set_sensitive(phase.accepts_code)
        self.connect_button.set_sensitive(phase.accepts_code and bool(self.code_row.get_text().strip()))
        if kind == "connected" and self.session is not None and self.session.result is not None:
            account = self.session.result
            existing = isinstance(self.target, ClaudeAccount)
            first = f"Login de {account.nickname} atualizado." if existing else f"Conta {account.nickname} adicionada."
            rest = ("O login está só no chaveiro do MultiCred; até as conversas já abertas passam para esta conta "
                    "quando ela for usada num perfil." if account.kind == ClaudeAccountKind.LOGIN else
                    "O token está só no chaveiro e vale para terminais novos.")
            self.done_label.set_label(f"{first} {rest}")
        self.done_label.set_visible(kind == "connected")

    def _refresh_github(self, kind: str) -> None:
        gh = self.github_session
        step1, step2, step3 = self.steps
        step1.set_state("current" if kind in ("starting", "showingCode") else "done")
        step2.set_state({"starting": "todo", "showingCode": "todo", "waitingForBrowser": "current",
                         "connected": "done", "failed": "failed"}.get(kind, "todo"))
        reported = self._reported or (gh is not None and gh.phase.kind == "connected" and not gh.is_running
                                      and (gh.switched_back or gh.switch_back_error or not gh.switch_back_to))
        step3.set_state("done" if reported else "current" if kind == "connected" else
                        "failed" if kind == "failed" else "todo")
        if gh is not None and not gh.switch_back_to:
            step3.set_title("Guardar o login no gh")
        code = gh.code if gh is not None else None
        self.github_code_label.set_label(code or "")
        self.github_code_label.set_visible(code is not None and kind not in ("connected", "failed"))
        self.connect_button.set_sensitive(kind == "showingCode")
        if kind == "connected" and gh is not None:
            text = f"Entrou no GitHub como {gh.login or 'nova conta'}."
            if gh.switched_back:
                text += f" A conta ativa do gh voltou para {gh.switch_back_to}."
            elif gh.switch_back_error:
                text += f" Não consegui voltar a conta ativa para {gh.switch_back_to}: {gh.switch_back_error}"
            self.done_label.set_label(text)
        self.done_label.set_visible(kind == "connected")

    def _scroll_terminal_to_end(self) -> bool:
        adjustment = self.terminal_scroll.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return False
