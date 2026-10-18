"""Verificar (design 06, approved Linux variant F; Mac MultiCred/Views/VerifyPage.swift).

Header "N de M ok para <perfil>", group "Sistema" with one row per check
(status, title, expected/found/origin, "Corrigir" whose popover shows the command
before it runs), group "Informativo", and a footer fixed outside the scrolling
list ("Verificar de novo", "Corrigir tudo"). The verifier runs off the UI thread;
results come back through `model.dispatch`.

Standalone: the window plugs it into its navigation and listens to `on_report`
(sidebar badge, tray) — S-06.
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ...core.adapters.process_scanner import FixedProcessScanner  # noqa: E402
from ...core.engine.verifier import Check, CheckStatus, Verifier, VerifyReport  # noqa: E402
from ...core.models import ProfileColor  # noqa: E402

STATUS_GLYPH = {CheckStatus.OK: "✓", CheckStatus.PROBLEM: "!", CheckStatus.INFO: "i"}
STATUS_NAME = {CheckStatus.OK: "ok", CheckStatus.PROBLEM: "problema", CheckStatus.INFO: "informativo"}

_CSS = """
.mc-verify-status { min-width: 22px; min-height: 22px; border-radius: 999px; font-size: 12px; font-weight: 800; }
.mc-verify-status.ok { background: alpha(@success_color, 0.16); color: @success_color; }
.mc-verify-status.problem { background: alpha(@warning_color, 0.20); color: @warning_color; }
.mc-verify-status.info { background: alpha(currentColor, 0.10); color: alpha(currentColor, 0.70); }
.mc-verify-avatar { min-width: 40px; min-height: 40px; border-radius: 9px; color: white; font-weight: bold;
                    font-size: 17px; background: #6E6E73; }
.mc-verify-footer { padding: 10px 24px; }
row.mc-verify-row label.title { font-weight: 600; }
button.mc-verify-again { color: @accent_color; }
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


def _mono(text: str, chip: bool = False) -> str:
    """Monospace; a `chip` gets a faint background that works in light and dark."""
    background = ' background="#808080" bgalpha="16%"' if chip else ""
    return f'<span font_family="monospace"{background}>{GLib.markup_escape_text(text)}</span>'


def subtitle_markup(check: Check) -> str:
    """Pango markup under the row title: what was expected and found, and where from."""
    escape = GLib.markup_escape_text
    lines = []
    if check.expected is not None:
        lines.append(f"Esperado {_mono(check.expected, True)} · encontrado {_mono(check.found or 'nada', True)}")
    elif check.found:
        monospace = check.id == "claude.shells" or check.id.startswith("shell.")
        lines.append(_mono(check.found) if monospace and check.status == CheckStatus.OK else escape(check.found))
    hint = check.hint
    if check.origin:
        lines.append(f"Origem: {_mono(check.origin, True)}" + (f", {escape(hint)}" if hint else ""))
        hint = None
    if hint:
        lines.append(escape(hint))
    return "\n".join(lines)


class VerifyRow(Adw.ActionRow):
    """One check. `fix_button` is None when there is nothing to run automatically."""

    def __init__(self, page: "VerifyPage", check: Check):
        super().__init__(title=GLib.markup_escape_text(check.title), subtitle=subtitle_markup(check),
                         subtitle_lines=0, title_lines=0)
        self.check = check
        self.add_css_class("mc-verify-row")
        status = Gtk.Label(label=STATUS_GLYPH[check.status], valign=Gtk.Align.CENTER if not check.expected
                           else Gtk.Align.START, margin_top=0 if not check.expected else 12)
        status.add_css_class("mc-verify-status")
        status.add_css_class(check.status.value)
        status.update_property([Gtk.AccessibleProperty.LABEL], [STATUS_NAME[check.status]])
        self.status_label = status
        self.add_prefix(status)
        self.fix_button = None
        self.confirm_button = None
        if check.fix is not None:
            self.fix_button, self.confirm_button = page.make_fix_button(check)
            self.add_suffix(self.fix_button)


class VerifyPage(Adw.NavigationPage):
    def __init__(self, model, processes=None, verifier: Verifier | None = None,
                 on_report: Callable[[VerifyReport], None] | None = None):
        super().__init__(title="Verificar", tag="verify")
        _load_css()
        self.model = model
        self.on_report = on_report
        self.last_report: VerifyReport | None = None
        self.busy = False
        if processes is None:
            processes = self._default_scanner(model.environment)
        self.verifier = verifier or Verifier(model.environment, model.tools, processes,
                                             engine=getattr(model, "engine", None), log=getattr(model, "log", None))
        self.rows: dict[str, VerifyRow] = {}

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=20, margin_bottom=24,
                          margin_start=16, margin_end=16)
        content.append(self._build_header())
        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.error_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.error_label.add_css_class("error")
        content.append(self.error_label)
        self.system_group = Adw.PreferencesGroup(title="Sistema")
        self.info_group = Adw.PreferencesGroup(title="Informativo", visible=False)
        content.append(self.system_group)
        content.append(self.info_group)
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=560, child=content)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)
        view.set_content(self.scroll)

        # Fixed footer, outside the scrolling list (the Mac design overlapped the last row).
        self.footer = self._build_footer()
        view.add_bottom_bar(self.footer)
        view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        self.set_child(view)
        self.refresh()

    # MARK: building

    @staticmethod
    def _default_scanner(environment):
        if environment.mode == "real":
            from ...core import real_env
            return real_env.make_process_scanner()
        return FixedProcessScanner(0)  # test mode: the real /proc is not the sandbox's

    def _build_header(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=14)
        self.avatar = Gtk.Label(label="?", valign=Gtk.Align.CENTER)
        self.avatar.add_css_class("mc-verify-avatar")
        box.append(self.avatar)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.headline_label = Gtk.Label(label="Verificando…", xalign=0, wrap=True)
        self.headline_label.add_css_class("title-2")
        texts.append(self.headline_label)
        subtitle = Gtk.Label(label="Comparado com o que o perfil espera, agora", xalign=0, wrap=True)
        subtitle.add_css_class("dim-label")
        texts.append(subtitle)
        box.append(texts)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        box.append(self.spinner)
        return box

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(spacing=10)
        footer.add_css_class("mc-verify-footer")
        self.again_button = Gtk.Button(label="Verificar de novo", valign=Gtk.Align.CENTER)
        self.again_button.add_css_class("flat")
        self.again_button.add_css_class("mc-verify-again")
        self.again_button.connect("clicked", lambda *_: self.refresh(deep=True))
        footer.append(self.again_button)
        footer.append(Gtk.Box(hexpand=True))

        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=8, margin_bottom=8,
                              margin_start=8, margin_end=8)
        popover_box.append(Gtk.Label(label="Vai executar, nesta ordem:", xalign=0))
        self.fix_all_preview = Gtk.Label(xalign=0, wrap=True, max_width_chars=60, selectable=True)
        self.fix_all_preview.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        popover_box.append(self.fix_all_preview)
        self.fix_all_confirm = Gtk.Button(label="Executar tudo", halign=Gtk.Align.END)
        self.fix_all_confirm.add_css_class("suggested-action")
        self.fix_all_confirm.connect("clicked", self._on_fix_all_confirmed)
        popover_box.append(self.fix_all_confirm)
        self.fix_all_popover = Gtk.Popover(child=popover_box)

        self.fix_all_button = Gtk.MenuButton(child=Gtk.Label(label="Corrigir tudo"), popover=self.fix_all_popover,
                                             valign=Gtk.Align.CENTER, sensitive=False)
        self.fix_all_button.add_css_class("suggested-action")
        footer.append(self.fix_all_button)
        return footer

    def make_fix_button(self, check: Check) -> tuple[Gtk.MenuButton, Gtk.Button]:
        """"Corrigir": the tooltip and the popover show the command; it runs only on "Executar"."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8)
        box.append(Gtk.Label(label="Vai executar:", xalign=0))
        command = Gtk.Label(label=check.fix.command, xalign=0, wrap=True, max_width_chars=60, selectable=True)
        command.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        command.add_css_class("monospace")
        box.append(command)
        confirm = Gtk.Button(label="Executar", halign=Gtk.Align.END)
        confirm.add_css_class("suggested-action")
        box.append(confirm)
        popover = Gtk.Popover(child=box)
        button = Gtk.MenuButton(child=Gtk.Label(label="Corrigir"), popover=popover, valign=Gtk.Align.CENTER,
                                tooltip_text=check.fix.command, sensitive=not self.busy)

        def run(*_):
            popover.popdown()
            self.fix(check)

        confirm.connect("clicked", run)
        return button, confirm

    # MARK: actions

    def refresh(self, deep: bool = False) -> None:
        """Runs the verifier off the UI thread. "Verificar de novo" also checks gh tokens."""
        config, state = self.model.config, self.model.state
        if config is None:
            self.headline_label.set_label("MultiCred ainda não foi configurado")
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

    def _on_fix_all_confirmed(self, *_):
        self.fix_all_popover.popdown()
        self.fix_all()

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
        self.again_button.set_sensitive(not busy)
        fixable = bool(self.last_report and self.last_report.fixable)
        self.fix_all_button.set_sensitive(not busy and fixable)
        for row in self.rows.values():
            if row.fix_button is not None:
                row.fix_button.set_sensitive(not busy)

    # MARK: rendering

    def _show(self, report: VerifyReport) -> None:
        self.last_report = report
        if report.profile_name is not None:
            self.headline_label.set_label(f"{report.ok_count} de {report.total} ok para {report.profile_name}")
        else:
            self.headline_label.set_label(f"{report.ok_count} de {report.total} ok · nenhum perfil ativo")
        self._show_avatar(report)
        if report.errors:
            self.error_label.set_label("Não consegui corrigir: " + "; ".join(report.errors))
            self.error_label.set_visible(True)
        else:
            self.error_label.set_visible(False)

        for row in self.rows.values():
            (self.info_group if row.check.status == CheckStatus.INFO else self.system_group).remove(row)
        self.rows = {}
        for check in report.checks:
            row = VerifyRow(self, check)
            (self.info_group if check.status == CheckStatus.INFO else self.system_group).add(row)
            self.rows[check.id] = row
        self.info_group.set_visible(bool(report.informative))

        fixes = []
        for check in report.fixable:
            if check.fix.key not in {f.key for f in fixes}:
                fixes.append(check.fix)
        self.fix_all_preview.set_label("\n".join(f"• {fix.command}" for fix in fixes))
        self.fix_all_button.set_sensitive(bool(fixes) and not self.busy)
        self.fix_all_button.set_tooltip_text(None if fixes else "Nada para corrigir automaticamente")

    def _show_avatar(self, report: VerifyReport) -> None:
        config = self.model.config
        profile = config.profile(report.state.profile_id) if config else None
        for color in ProfileColor:
            self.avatar.remove_css_class(f"mc-verify-color-{color.value}")
        if profile is None:
            self.avatar.set_label("?")
            return
        self.avatar.set_label(profile.icon.value[:1] if profile.icon and profile.icon.kind == "letter"
                              else profile.name[:1].upper())
        self.avatar.add_css_class(f"mc-verify-color-{profile.color.value}")
