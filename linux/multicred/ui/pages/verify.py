"""Verificar (design 06), laid out like the Mac's `MultiCred/Views/VerifyPage.swift`.

Top (:73-92): the active profile's 44 px badge and name (or "Nenhum perfil ativo"),
"N de M itens de acordo · verificado às HH:MM", a spinner while checking and
"Corrigir tudo". Then the sections "Ferramentas", "Integração" and "Sessões"
(:21-23), each row with ✓ / ✗ / ! / i, the title, "Esperado **x**, encontrado y" or
the detail, "definido em <origem>" and its fix: "Corrigir", "Entrar…" or "Abrir
chave…" (:110-140). Every fix asks first, "Corrigir X?" with the command and
"Cancelar / Executar" (:48-57); "Corrigir tudo?" lists "• comando" per line
(:58-70). A fix that leads to a login or to a key opens that page (:148-160).
A footer fixed outside the scrolling list: "“Corrigir” mostra o comando antes de
executar." and "Verificar de novo" (:35-44).

The verifier runs off the UI thread; results come back through `model.dispatch`.
The window plugs the page into its navigation and listens to `on_report` (the
sidebar's Verificar dot, the Visão geral footer, the tray).
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ...core.engine.verifier import (  # noqa: E402
    Check, CheckSection, CheckStatus, Fix, FixKind, Verifier, VerifyReport,
)
from ...core.models import ProfileColor  # noqa: E402
from ..model import _default_scanner  # noqa: E402

# Components.swift:100-135 (StatusIcon24): symbol and accessibility label.
STATUS_GLYPH = {CheckStatus.OK: "✓", CheckStatus.ERROR: "✗", CheckStatus.WARNING: "!", CheckStatus.INFO: "i"}
STATUS_NAME = {CheckStatus.OK: "De acordo", CheckStatus.ERROR: "Problema", CheckStatus.WARNING: "Atenção",
               CheckStatus.INFO: "Informação"}
NO_PROFILE = "Nenhum perfil ativo"
FOOTER_NOTE = "“Corrigir” mostra o comando antes de executar."

_CSS = """
.mc-verify-status { min-width: 24px; min-height: 24px; border-radius: 999px; font-size: 13px; font-weight: 900; }
.mc-verify-status.ok { background: alpha(@success_color, 0.15); color: @success_color; }
.mc-verify-status.error { background: alpha(@error_color, 0.15); color: @error_color; }
.mc-verify-status.warning { background: alpha(@warning_color, 0.15); color: @warning_color; }
.mc-verify-status.info { background: alpha(@blue_3, 0.15); color: @blue_3; }
.mc-verify-avatar { min-width: 44px; min-height: 44px; border-radius: 11px; color: white; font-weight: 600;
                    font-size: 24px; background: #6E6E73; }
.mc-verify-header { padding: 16px 20px; }
.mc-verify-footer { padding: 10px 20px; }
""" + "".join(f".mc-verify-color-{color.value} {{ background: {color.hex}; }}\n" for color in ProfileColor)

_css_loaded = False


def _load_css() -> None:
    global _css_loaded
    display = Gdk.Display.get_default()
    if _css_loaded or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def subtitle_markup(check: Check) -> str:
    """Pango markup under the row title (VerifyPage.swift:115-123): what was expected and
    found, or the detail; then where it is defined. Linux adds the hint (what to do by
    hand) when there is one."""
    escape = GLib.markup_escape_text
    lines = []
    if check.expected is not None:
        lines.append(f"Esperado <b>{escape(check.expected)}</b>, encontrado {escape(check.found or 'nada')}")
    elif check.found:
        lines.append(escape(check.found))
    hint = check.hint
    if check.origin:
        origin = f'<span font_family="monospace" size="small">definido em {escape(check.origin)}</span>'
        lines.append(origin + (f", {escape(hint)}" if hint else ""))
        hint = None
    if hint:
        lines.append(escape(hint))
    return "\n".join(lines)


def runnable_fixes(report: VerifyReport | None) -> list[Fix]:
    """What "Corrigir tudo" runs, each repair once."""
    fixes: list[Fix] = []
    for check in (report.fixable if report else []):
        if check.fix.key not in {f.key for f in fixes}:
            fixes.append(check.fix)
    return fixes


def open_fix_in_window(window, fix: Fix) -> bool:
    """VerifyPage.swift:155-158 with the Linux window's entry points: sign a Claude
    account in again, a folder's own login, gh auth login, or the key's editor.
    False when `window` cannot do it (the page is not inside the main window)."""
    request = getattr(window, "on_login_requested", None)
    if fix.kind == FixKind.LOGIN_CLAUDE:
        config = getattr(getattr(window, "model", None), "config", None)
        account = config.claude_account(fix.target) if config is not None else None
        if request is None or account is None:
            return False
        request("login", account)
    elif fix.kind == FixKind.LOGIN_CLAUDE_FOLDER:
        if request is None:
            return False
        request("folder", fix.target)
    elif fix.kind == FixKind.LOGIN_GITHUB:
        if request is None:
            return False
        request("github", None, host=fix.target)
    elif fix.kind == FixKind.OPEN_API_KEY:
        show_section = getattr(window, "show_section", None)
        if show_section is None:
            return False
        page = show_section("keys")
        editor = getattr(page, "open_editor", None)  # APIKeysPage: the key's editor
        if editor is not None:
            editor(fix.target)
    else:
        return False
    return True


class VerifyRow(Adw.ActionRow):
    """One check. `fix_button` is None when there is nothing to offer."""

    def __init__(self, page: "VerifyPage", check: Check):
        subtitle = subtitle_markup(check)
        super().__init__(title=GLib.markup_escape_text(check.title), subtitle=subtitle,
                         subtitle_lines=0, title_lines=0)
        self.check = check
        self.subtitle_markup = subtitle
        self.add_css_class("mc-verify-row")
        status = Gtk.Label(label=STATUS_GLYPH[check.status], halign=Gtk.Align.CENTER,
                           valign=Gtk.Align.START if subtitle else Gtk.Align.CENTER, margin_top=10 if subtitle else 0)
        status.add_css_class("mc-verify-status")
        status.add_css_class(check.status.value)
        self.status_name = STATUS_NAME[check.status]
        status.update_property([Gtk.AccessibleProperty.LABEL], [self.status_name])
        self.status_label = status
        self.add_prefix(status)
        self.fix_button = None
        if check.fix is not None:
            self.fix_button = Gtk.Button(label=check.fix.button_title, valign=Gtk.Align.CENTER,
                                         tooltip_text=check.fix.command, sensitive=not page.busy)
            self.fix_button.connect("clicked", lambda *_: page.confirm_fix(check))
            self.add_suffix(self.fix_button)


class VerifyPage(Adw.NavigationPage):
    def __init__(self, model, processes=None, verifier: Verifier | None = None,
                 on_report: Callable[[VerifyReport], None] | None = None,
                 open_fix: Callable[[Fix], None] | None = None):
        super().__init__(title="Verificar", tag="verify")
        _load_css()
        self.model = model
        self.on_report = on_report
        # Where a fix that leads to a login or a key goes; default: the main window.
        self.open_fix = open_fix or self._open_fix_in_window
        self.last_report: VerifyReport | None = None
        self.busy = False
        self.dialog: Adw.AlertDialog | None = None
        if processes is None:
            # The same scanner as the model (one place decides real /proc vs. test mode).
            processes = getattr(model, "processes", None) or _default_scanner(model.environment)
        self.verifier = verifier or Verifier(model.environment, model.tools, processes,
                                             engine=getattr(model, "engine", None), log=getattr(model, "log", None))
        self.rows: dict[str, VerifyRow] = {}

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.header = self._build_header()
        view.add_top_bar(self.header)
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self._build_report(), "report")
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                          halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=24, height_request=24)
        loading.append(spinner)
        loading.append(Gtk.Label(label="Verificando…"))
        self.stack.add_named(loading, "loading")
        self.unconfigured = Adw.StatusPage(title="MultiCred ainda não foi configurado",
                                           icon_name="view-list-bullet-symbolic")
        self.stack.add_named(self.unconfigured, "unconfigured")
        view.set_content(self.stack)

        # Fixed footer, outside the scrolling list (the Mac design overlapped the last row).
        self.footer = self._build_footer()
        view.add_bottom_bar(self.footer)
        view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        self.set_child(view)
        self.refresh()

    # MARK: building

    def _build_header(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=14)
        box.add_css_class("mc-verify-header")
        self.avatar = Gtk.Label(label="?", valign=Gtk.Align.CENTER, visible=False)
        self.avatar.add_css_class("mc-verify-avatar")
        box.append(self.avatar)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.title_label = Gtk.Label(label=NO_PROFILE, xalign=0, wrap=True)
        self.title_label.add_css_class("title-2")
        texts.append(self.title_label)
        self.subtitle_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.subtitle_label.add_css_class("dim-label")
        texts.append(self.subtitle_label)
        box.append(texts)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        box.append(self.spinner)
        self.fix_all_button = Gtk.Button(label="Corrigir tudo", valign=Gtk.Align.CENTER, sensitive=False)
        self.fix_all_button.add_css_class("suggested-action")
        self.fix_all_button.connect("clicked", lambda *_: self.confirm_fix_all())
        box.append(self.fix_all_button)
        return box

    def _build_report(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=20, margin_bottom=24,
                          margin_start=16, margin_end=16)
        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.error_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.error_label.add_css_class("error")
        content.append(self.error_label)
        self.groups: dict[CheckSection, Adw.PreferencesGroup] = {}
        for section in CheckSection:
            group = Adw.PreferencesGroup(title=section.title, visible=False)
            content.append(group)
            self.groups[section] = group
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=560, child=content)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)
        return self.scroll

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(spacing=10)
        footer.add_css_class("mc-verify-footer")
        self.footer_note = Gtk.Label(label=FOOTER_NOTE, xalign=0, hexpand=True, wrap=True, valign=Gtk.Align.CENTER)
        self.footer_note.add_css_class("caption")
        self.footer_note.add_css_class("dim-label")
        footer.append(self.footer_note)
        self.again_button = Gtk.Button(label="Verificar de novo", valign=Gtk.Align.CENTER)
        self.again_button.connect("clicked", lambda *_: self.refresh(deep=True))
        footer.append(self.again_button)
        return footer

    # MARK: confirmations

    def _alert(self, heading: str, body: str, on_run: Callable[[], None]) -> Adw.AlertDialog:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("run", "Executar")
        dialog.set_response_appearance("run", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("run")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: on_run() if response == "run" else None)
        self.dialog = dialog
        dialog.present(self)
        return dialog

    def confirm_fix(self, check: Check) -> None:
        """"Corrigir X?" with the command (VerifyPage.swift:48-57); runs only on "Executar"."""
        self._alert(f"Corrigir {check.title}?", check.fix.command, lambda: self._run_fix(check))

    def confirm_fix_all(self) -> None:
        fixes = runnable_fixes(self.last_report)
        if not fixes or self.busy:
            return
        self._alert("Corrigir tudo?", "\n".join(f"• {fix.command}" for fix in fixes), self.fix_all)

    def _run_fix(self, check: Check) -> None:
        if check.fix.opens_page:
            self.open_fix(check.fix)
        else:
            self.fix(check)

    def _open_fix_in_window(self, fix: Fix) -> None:
        if not open_fix_in_window(self.get_root(), fix):
            self.error_label.set_label(f"Não consegui abrir: {fix.command}")
            self.error_label.set_visible(True)

    # MARK: actions

    def refresh(self, deep: bool = True) -> None:
        """Runs the verifier off the UI thread. Like the Mac page (`.task` and "Verificar de
        novo" both use checkTokens: true), it also asks gh whether the token works."""
        config, state = self.model.config, self.model.state
        if config is None:
            self.title_label.set_label(NO_PROFILE)
            self.subtitle_label.set_visible(False)
            self.stack.set_visible_child_name("unconfigured")
            return
        tools = self.model.tools

        def work():
            if deep and hasattr(tools, "invalidate"):
                tools.invalidate()  # re-read the login shell PATH too
            return self.verifier.run(config, state, deep=deep)

        self._start(work)

    def fix(self, check: Check) -> None:
        config = self.model.config
        self._start(lambda: self.verifier.fix(check, config))

    def fix_all(self) -> None:
        config, report = self.model.config, self.last_report
        self._start(lambda: self.verifier.fix_all(report, config))

    def _start(self, work) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def thread():
            try:
                report, error = work(), None
            except Exception as failure:  # shown on the page instead of killing the thread silently
                report, error = None, failure
            self.model.dispatch(self._finish, report, error)

        threading.Thread(target=thread, name="multicred-verify", daemon=True).start()

    def _finish(self, report: VerifyReport | None, error: Exception | None):
        self._set_busy(False)
        if error is not None:
            self.stack.set_visible_child_name("report")
            self.error_label.set_label(f"Não consegui verificar: {error}")
            self.error_label.set_visible(True)
            return False
        self._show(report)
        self._sync_model_state(report)
        if self.on_report is not None:
            self.on_report(report)
        return False  # GLib.idle_add: run once

    def _sync_model_state(self, report: VerifyReport) -> None:
        """A "reapply" fix bumps the generation: keep the app's model in step."""
        state = getattr(self.model, "state", None)
        if state is None or report.state == state:
            return
        if report.state.profile_id != state.profile_id or report.state.generation < state.generation:
            return  # a switch happened meanwhile; the model already knows better
        self.model.state = report.state
        notify = getattr(self.model, "notify", None)
        if notify is not None:
            notify()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.spinner.set_spinning(busy)
        self.spinner.set_visible(busy)
        if busy and self.last_report is None:
            self.stack.set_visible_child_name("loading")
        self.again_button.set_sensitive(not busy)
        self.fix_all_button.set_sensitive(not busy and bool(runnable_fixes(self.last_report)))
        for row in self.rows.values():
            if row.fix_button is not None:
                row.fix_button.set_sensitive(not busy)

    # MARK: rendering

    def _show(self, report: VerifyReport) -> None:
        self.last_report = report
        self.stack.set_visible_child_name("report")
        self.title_label.set_label(report.profile_name or NO_PROFILE)
        checked = report.checked_at.astimezone().strftime("%H:%M")
        self.subtitle_label.set_label(f"{report.ok_count} de {report.total} itens de acordo · verificado às {checked}")
        self.subtitle_label.set_visible(True)
        self._show_avatar(report)
        if report.errors:
            self.error_label.set_label("Não consegui corrigir: " + "; ".join(report.errors))
            self.error_label.set_visible(True)
        else:
            self.error_label.set_visible(False)

        for row in self.rows.values():
            self.groups[row.check.section].remove(row)
        self.rows = {}
        for check in report.checks:
            row = VerifyRow(self, check)
            self.groups[check.section].add(row)
            self.rows[check.id] = row
        for section, group in self.groups.items():
            group.set_visible(bool(report.in_section(section)))

        fixes = runnable_fixes(report)
        self.fix_all_button.set_sensitive(bool(fixes) and not self.busy)
        self.fix_all_button.set_tooltip_text(None if fixes else "Nada para corrigir automaticamente")

    def _show_avatar(self, report: VerifyReport) -> None:
        config = self.model.config
        profile = config.profile(report.state.profile_id) if config else None
        for color in ProfileColor:
            self.avatar.remove_css_class(f"mc-verify-color-{color.value}")
        self.avatar.set_visible(profile is not None)
        if profile is None:
            return
        letter = profile.icon.value if profile.icon and profile.icon.kind == "letter" else profile.name
        self.avatar.set_label(letter[:1].upper())
        self.avatar.add_css_class(f"mc-verify-color-{profile.color.value}")
