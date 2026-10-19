""""Adicionar conta Claude" / "Entrar de novo em X", a page pushed on Claude (port of
AddClaudeAccountPage.swift).

Three states, as on the Mac:
- setup: the intro text, the form (only for a new account: "Apelido", "E-mail da
  conta", "Cor"), the choice "Entrar pelo navegador" | "Já tenho um token" (only for a
  new account), and per choice its text, "Abrir o link em" and "Continuar no
  navegador", or the pasted token and "Salvar" (no CLI runs);
- running: avatar and name, the three steps (the link and the code field under step 2,
  "Concluindo o login…" and the stall notice under step 3), the failure text,
  "Detalhes técnicos" (collapsed) and "Tentar de novo" / "Cancelar" (back to setup);
- done: "Conta X adicionada." / "Login de X atualizado.", the Mac's text, "Usar agora
  em <perfil>" (with an active profile) and "Concluir" (back to the section's root).

The browser flow is a full login (`claude auth login --claudeai` in a throwaway folder,
D39), also when signing a token account in again: it becomes a login account, as on
the Mac. Linux difference: the code is not picked up from the clipboard (Wayland does
not let an unfocused app read it), so the texts ask for it to be pasted here.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ...core.claude.login_flow import (  # noqa: E402
    ClaudeAccountSaver, ClaudeLoginSession, LoginMode, LoginPhase, NewClaudeAccount,
)
from ...core.engine.types import SwitchError  # noqa: E402
from ...core.models import ClaudeAccount, ClaudeAccountKind, ProfileColor  # noqa: E402
from .. import widgets  # noqa: E402
from ..messages import describe_switch_error  # noqa: E402
from .login import (  # noqa: E402
    AuthorizationLinkActions, BrowserChoice, BrowserChoiceRow, LoginKind, LoginResult, StepRow, TerminalOutput,
    caption, idle, in_navigation_stack, load_css, notice, page_body, round_avatar,
)

INTRO = ("Uma conta Claude é só o login. Usada num perfil, ela mantém os agents, skills e conversas da pasta do "
         "perfil, e a troca vale até nas conversas já abertas.")
# Mac: "… O MultiCred pega o código copiado sozinho e guarda o login no Keychain." The
# clipboard is not read here (R-04, out of scope on Wayland): the code is pasted.
BROWSER_TEXT = ("O link do Claude abre no navegador escolhido abaixo. Entre com a conta que você quer adicionar, "
                "clique em Autorizar e copie o código mostrado. Cole o código aqui e o MultiCred guarda o login "
                "no chaveiro do sistema.")
TOKEN_TEXT = ("Se você já rodou \"claude setup-token\" num terminal, cole o token (começa com sk-ant-oat). Um "
              "token vale só para terminais novos: as conversas já abertas não trocam.")
TEST_NOTICE = "Modo de teste: simulação; digite qualquer código."
STEP_TITLES = ["Entre no navegador com a conta que você quer usar", "Clique em Autorizar e copie o código",
               "Salvar o login"]
STALL_TEXT = ("Está demorando mais que o normal. Abra os detalhes técnicos abaixo ou tente de novo; a saída (sem "
              "códigos nem tokens) foi registrada no log.")
REJECTED_CODE = ("O Claude recusou o código. Ele vale uma vez e por poucos minutos, e só para o link aberto nesta "
                 "tentativa. Tente de novo.")
LOGIN_DONE = ("O login está só no chaveiro do sistema. Use agora no perfil ativo: a pasta continua a mesma e até as "
              "conversas já abertas passam para esta conta.")
TOKEN_DONE = ("O token está só no chaveiro do sistema. Vale para terminais novos; escolha esta conta no editor de "
              "um perfil ou use agora no perfil ativo.")


def _row_with(title: str, widget: Gtk.Widget) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    row.add_suffix(widget)
    return row


def _buttons(*buttons: Gtk.Button) -> Gtk.Box:
    """A right-aligned row of buttons (SwiftUI `HStack { Spacer(); … }`)."""
    box = Gtk.Box(spacing=8)
    box.append(Gtk.Box(hexpand=True))
    for button in buttons:
        box.append(button)
    return box


class AddClaudeAccountPage(Adw.NavigationPage):
    """`replacing`: None for a new account; an existing ClaudeAccount to sign in again."""

    # SetupTokenSession.stallAfter: no answer this long after the code → the stall notice.
    STALL_AFTER_MS = 25_000

    def __init__(self, window, replacing: ClaudeAccount | None = None):
        super().__init__(title=f"Entrar de novo em {replacing.nickname}" if replacing is not None
                         else "Adicionar conta Claude")
        load_css()
        self.window = window
        self.model = window.model
        self.existing = replacing
        self.color = ProfileColor.TEAL
        self.session: ClaudeLoginSession | None = None
        self.saved: ClaudeAccount | None = None
        self.state = "setup"
        self.stalled = False
        self._stall_source = None
        self._opened_url: str | None = None
        self._left = False
        # Three boxes shown one at a time (a Gtk.Stack would size to the widest natural
        # width of all three, so the wrapped texts would not wrap).
        self.views = {"setup": self._build_setup(), "running": self._build_running(), "done": self._build_done()}
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=24, margin_bottom=24, margin_start=24,
                       margin_end=24)
        body.add_css_class("mc-login-page")
        for view in self.views.values():
            body.append(view)
        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=640, halign=Gtk.Align.START, child=body)
        self.set_child(page_body(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)))
        self.connect("hidden", lambda _page: GLib.idle_add(self._check_left))
        self._show("setup")

    @property
    def is_test_mode(self) -> bool:
        return self.model.environment.mode != "real"

    @property
    def display_name(self) -> str:
        return self.existing.nickname if self.existing is not None else self.nickname_entry.get_text().strip()

    @property
    def display_color(self) -> ProfileColor:
        return self.existing.color if self.existing is not None else self.color

    # MARK: setup

    def _build_setup(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.intro_label = widgets.label(INTRO, "dim-label", wrap=True)
        box.append(self.intro_label)

        new = self.existing is None
        self.form_group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, visible=new)
        self.form_group.add_css_class("boxed-list")
        self.nickname_entry = Gtk.Entry(placeholder_text="Ex.: Marco", valign=Gtk.Align.CENTER, width_chars=26)
        self.nickname_entry.connect("changed", lambda _e: self._refresh_setup())
        self.nickname_entry.connect("activate", lambda _e: self._default_action())
        self.email_entry = Gtk.Entry(placeholder_text="opcional", valign=Gtk.Align.CENTER, width_chars=26)
        swatches, self.color_buttons = widgets.color_swatches(self.color, self._color_changed)
        self.nickname_row = _row_with("Apelido", self.nickname_entry)
        self.email_row = _row_with("E-mail da conta", self.email_entry)
        self.color_row = _row_with("Cor", swatches)
        for row in (self.nickname_row, self.email_row, self.color_row):
            self.form_group.append(row)
        box.append(self.form_group)

        self.source_box = Gtk.Box(halign=Gtk.Align.FILL, homogeneous=True, visible=new)
        self.source_box.add_css_class("mc-segmented")
        self.browser_toggle = Gtk.ToggleButton(label="Entrar pelo navegador", active=True)
        self.token_toggle = Gtk.ToggleButton(label="Já tenho um token", group=self.browser_toggle)
        for toggle in (self.browser_toggle, self.token_toggle):
            toggle.connect("toggled", lambda _t: self._refresh_setup())
            self.source_box.append(toggle)
        box.append(self.source_box)

        # "Entrar pelo navegador"
        self.browser_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.browser_text = widgets.label(BROWSER_TEXT, wrap=True)
        self.browser_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.browser_list.add_css_class("boxed-list")
        self.browser_row = BrowserChoiceRow()
        self.browser_list.append(self.browser_row)
        self.test_notice, self.test_notice_label = notice(TEST_NOTICE, "applications-science-symbolic", "mc-orange")
        self.test_notice.set_visible(self.is_test_mode)
        self.setup_cancel_button = widgets.button("Cancelar", lambda: self.window.nav.pop())
        self.continue_button = widgets.button("Continuar no navegador", self.start, "suggested-action")
        for widget in (self.browser_text, self.browser_list, self.test_notice,
                       _buttons(self.setup_cancel_button, self.continue_button)):
            self.browser_box.append(widget)
        box.append(self.browser_box)

        # "Já tenho um token"
        self.token_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.token_text = widgets.label(TOKEN_TEXT, wrap=True)
        self.token_entry = Gtk.PasswordEntry(placeholder_text="sk-ant-oat01-…", show_peek_icon=True)
        self.token_entry.add_css_class("monospace")
        self.token_entry.connect("changed", lambda _e: self._refresh_setup())
        self.token_entry.connect("activate", lambda _e: self._default_action())
        self.token_cancel_button = widgets.button("Cancelar", lambda: self.window.nav.pop())
        self.save_button = widgets.button("Salvar", self.save_token, "suggested-action")
        for widget in (self.token_text, self.token_entry, _buttons(self.token_cancel_button, self.save_button)):
            self.token_box.append(widget)
        box.append(self.token_box)
        return box

    def browser_options(self) -> list[str]:
        """The "Abrir o link em" options, in order (tests)."""
        return list(self.browser_row.labels)

    def _color_changed(self, color: ProfileColor) -> None:
        self.color = color

    @property
    def uses_token(self) -> bool:
        return self.existing is None and self.token_toggle.get_active()

    def _refresh_setup(self) -> None:
        can_start = bool(self.display_name)
        self.browser_box.set_visible(not self.uses_token)
        self.token_box.set_visible(self.uses_token)
        self.continue_button.set_sensitive(can_start)
        self.save_button.set_sensitive(can_start and bool(self.token_entry.get_text().strip()))

    def _default_action(self) -> None:
        button = self.save_button if self.uses_token else self.continue_button
        if button.get_sensitive():
            button.emit("clicked")

    # MARK: running

    def _build_running(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        identity = Gtk.Box(spacing=12)
        self.avatar = round_avatar("?", ProfileColor.TEAL, 40)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.identity_title = widgets.label("", "mc-login-name")
        self.identity_subtitle = widgets.label("", "dim-label")
        names.append(self.identity_title)
        names.append(self.identity_subtitle)
        identity.append(self.avatar)
        identity.append(names)
        box.append(identity)

        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        steps.add_css_class("mc-login-steps")
        self.steps = [StepRow(i + 1, title) for i, title in enumerate(STEP_TITLES)]
        for step in self.steps:
            steps.append(step)
        box.append(steps)

        # Step 2: the link and the code.
        self.link_status_label = caption()
        self.link_actions = AuthorizationLinkActions()
        self.link_actions.set_halign(Gtk.Align.START)
        code_line = Gtk.Box(spacing=8)
        self.code_entry = Gtk.Entry(placeholder_text="Cole o código aqui", hexpand=True)
        self.code_entry.add_css_class("monospace")
        self.code_entry.connect("changed", lambda _e: self._refresh_running())
        self.code_entry.connect("activate", lambda _e: self.send_code())
        self.send_button = widgets.button("Enviar", self.send_code)
        code_line.append(self.code_entry)
        code_line.append(self.send_button)
        for widget in (self.link_status_label, self.link_actions, code_line):
            self.steps[1].detail.append(widget)

        # Step 3: saving.
        self.connecting_box = Gtk.Box(spacing=8)
        self.connecting_box.append(Gtk.Spinner(spinning=True))
        self.connecting_label = caption("Concluindo o login…")
        self.connecting_box.append(self.connecting_label)
        self.stall_label = caption(STALL_TEXT, "mc-orange")
        self.steps[2].detail.append(self.connecting_box)
        self.steps[2].detail.append(self.stall_label)

        self.error_box, self.error_label = notice("", "dialog-warning-symbolic", "mc-red")
        self.error_label.set_selectable(True)
        box.append(self.error_box)

        self.terminal = TerminalOutput("pasta temporária (apagada no fim)")
        self.terminal.set_margin_top(6)
        self.details_expander = Gtk.Expander(label="Detalhes técnicos", child=self.terminal)
        self.details_expander.add_css_class("mc-details")
        box.append(self.details_expander)

        self.retry_button = widgets.button("Tentar de novo", self.start)
        self.running_cancel_button = widgets.button("Cancelar", self.cancel)
        box.append(_buttons(self.retry_button, self.running_cancel_button))
        return box

    def start(self) -> None:
        """"Continuar no navegador" / "Tentar de novo": a new attempt (full login)."""
        if not self.display_name:
            return
        self._stop_session()
        self._opened_url = None
        self.stalled = False
        self.code_entry.set_text("")
        target = self.existing if self.existing is not None else NewClaudeAccount(
            self.display_name, self.email_entry.get_text().strip() or None, self.color)
        self.session = ClaudeLoginSession(self.model.environment, self.model.tools, LoginMode.LOGIN, target,
                                          self._saver(), dispatch=idle, on_change=self._session_changed,
                                          on_finished=self._session_finished, log=getattr(self.model, "log", None))
        self.avatar_update()
        self._show("running")
        self.session.start()
        self._refresh_running()

    def _saver(self) -> ClaudeAccountSaver:
        engine = getattr(self.model, "engine", None)
        return ClaudeAccountSaver(self.model.environment, lambda: self.model.config, self.model.save,
                                  exclusive=engine.exclusive if engine is not None else None)

    def avatar_update(self) -> None:
        widgets.set_avatar(self.avatar, self.display_name[:1], self.display_color)
        self.identity_title.set_label(self.display_name)
        email = self.existing.email if self.existing is not None else self.email_entry.get_text().strip()
        self.identity_subtitle.set_label(email or "Conta Claude")

    def send_code(self) -> None:
        session = self.session
        code = self.code_entry.get_text().strip()
        if session is None or not code or not session.phase.accepts_code:
            return
        session.submit_code(code)
        self.code_entry.set_text("")
        self.stalled = False
        self._cancel_stall()
        self._stall_source = GLib.timeout_add(self.STALL_AFTER_MS, self._stall)
        self._refresh_running()

    def _stall(self) -> bool:
        self._stall_source = None
        if self.session is not None and self.session.phase == LoginPhase.CONNECTING:
            self.stalled = True
            log = getattr(self.model, "log", None)
            if log is not None:
                log.log("info", f"claude login: no answer {self.STALL_AFTER_MS // 1000} s after the code")
            self._refresh_running()
        return False

    def _cancel_stall(self) -> None:
        if self._stall_source is not None:
            GLib.source_remove(self._stall_source)
            self._stall_source = None

    def cancel(self) -> None:
        """"Cancelar" while running: stops the attempt and goes back to the form (Mac)."""
        self._stop_session()
        self.session = None
        self._show("setup")

    def _stop_session(self) -> None:
        self._cancel_stall()
        if self.session is not None:
            if not self.session.phase.is_finished:
                self.session.cancel()
            self.session.stop()

    def _session_changed(self, session) -> None:
        if session is not self.session or self._left:
            return
        url = session.authorization_url
        if url and url != self._opened_url and not self.is_test_mode:
            # The CLI does not open a browser (BROWSER is a no-op): open it here, once per link.
            self._opened_url = url
            BrowserChoice.open(url, BrowserChoice.current)
        if session.phase.is_finished:
            self._cancel_stall()
            self.stalled = False
        self._refresh_running()

    def _session_finished(self, session) -> None:
        if session is not self.session or self._left:
            return
        self._refresh_running()
        if session.phase == LoginPhase.CONNECTED and session.result is not None:
            self._saved(session.result, LoginKind.CLAUDE_LOGIN)

    def _failure_text(self) -> str | None:
        session = self.session
        if session is None or not session.phase.is_failure:
            return None
        if isinstance(session.save_error, SwitchError):  # e.g. Busy: a switch was running, nothing changed
            return f"Não consegui guardar o login: {describe_switch_error(session.save_error)}"
        if session.launch_error:
            return session.launch_error
        reason = session.phase.message or ""
        if "400" in reason:
            return REJECTED_CODE
        return f"Não deu certo ({reason})."

    def _link_status(self) -> str:
        if BrowserChoice.current == BrowserChoice.COPY_ONLY:
            return "O link foi copiado: cole no navegador (ou janela anônima) em que você quer entrar."
        return (f"O link abriu em {BrowserChoice.label(BrowserChoice.current)}. Abriu no navegador errado, ou já "
                "logado em outra conta? Copie o link e cole em outro navegador ou numa janela anônima.")

    def _refresh_running(self) -> None:
        session = self.session
        if session is None:
            return
        phase = session.phase
        kind = phase.kind
        waiting = not phase.is_finished and kind != "connecting"  # SetupTokenSession.isWaitingForCode
        step1, step2, step3 = self.steps
        step1.set_state("active" if kind == "starting" else "done")
        step2.set_state("pending" if kind == "starting" else "active" if waiting else "done")
        step3.set_state({"connecting": "active", "connected": "done", "failed": "failed"}.get(kind, "pending"))
        self.link_status_label.set_label(self._link_status())
        self.link_actions.set_visible(not self.is_test_mode)
        self.link_actions.set_url(session.authorization_url)
        self.code_entry.set_sensitive(phase.accepts_code)
        self.send_button.set_sensitive(phase.accepts_code and bool(self.code_entry.get_text().strip()))
        self.connecting_box.set_visible(kind == "connecting")
        self.stall_label.set_visible(self.stalled)
        failure = self._failure_text()
        self.error_box.set_visible(failure is not None)
        self.error_label.set_label(failure or "")
        command = session.command.display if session.command is not None else "claude auth login --claudeai"
        self.terminal.set_output(session.output, command)
        self.retry_button.set_visible(phase.is_failure or self.stalled)

    # MARK: token

    def save_token(self) -> None:
        """"Já tenho um token" → "Salvar": stores the pasted token, no CLI."""
        token = self.token_entry.get_text().strip()
        if not self.display_name or not token:
            return
        target = self.existing if self.existing is not None else NewClaudeAccount(
            self.display_name, self.email_entry.get_text().strip() or None, self.color)
        try:
            account = self._saver().save_token(target, token)
        except Exception as error:  # validation or Secret Service: nothing was added
            self.window.show_toast(widgets.describe_error(error))
            return
        self.token_entry.set_text("")
        self._saved(account, LoginKind.CLAUDE_TOKEN)

    # MARK: done

    def _build_done(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        title_box = Gtk.Box(spacing=8)
        title_box.append(Gtk.Image(icon_name="emblem-ok-symbolic", pixel_size=20))
        self.done_title = widgets.label("", "mc-login-name")
        title_box.append(self.done_title)
        title_box.add_css_class("mc-green")
        box.append(title_box)
        self.done_text = widgets.label("", "dim-label", wrap=True)
        box.append(self.done_text)
        self.use_now_button = widgets.button("Usar agora", self.use_now)
        self.done_button = widgets.button("Concluir", self.window.pop_to_section_root, "suggested-action")
        box.append(_buttons(self.use_now_button, self.done_button))
        return box

    def _saved(self, account: ClaudeAccount, kind: LoginKind) -> None:
        self.saved = account
        self.done_title.set_label(f"Login de {account.nickname} atualizado." if self.existing is not None
                                  else f"Conta {account.nickname} adicionada.")
        self.done_text.set_label(LOGIN_DONE if account.kind == ClaudeAccountKind.LOGIN else TOKEN_DONE)
        self._show("done")
        self.window.login_finished(LoginResult(kind, True, account=account))

    def use_now(self) -> None:
        """"Usar agora em P": only Claude switches; back to the section's root on success."""
        if self.saved is not None:
            self.window.switch_claude_only(self.saved.id, on_success=self.window.pop_to_section_root)

    def _refresh_done(self) -> None:
        profile = self.model.active_profile
        self.use_now_button.set_visible(profile is not None)
        if profile is not None:
            self.use_now_button.set_label(f"Usar agora em {profile.name}")

    # MARK: navigation

    def _show(self, state: str) -> None:
        self.state = state
        for name, view in self.views.items():
            view.set_visible(name == state)
        self.refresh()
        default = {"setup": None, "running": None, "done": self.done_button}[state]
        if default is not None:
            default.grab_focus()

    def refresh(self) -> None:
        """Model changes (window.refresh): the active profile for "Usar agora em P"."""
        if self.state == "setup":
            self._refresh_setup()
        elif self.state == "running":
            self._refresh_running()
        else:
            self._refresh_done()

    def _check_left(self) -> bool:
        """After a pop or a section change: stop the login (Mac: .onDisappear { session?.stop() })."""
        if self._left or in_navigation_stack(self):
            return False
        self._left = True
        self._stop_session()
        if self.saved is None:
            self.window.login_finished(LoginResult(LoginKind.CLAUDE_LOGIN, False, error="cancelado"))
        return False
