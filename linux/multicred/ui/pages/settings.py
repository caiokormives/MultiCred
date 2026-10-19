"""Ajustes (approved Linux design G; SettingsPage.swift).

Geral: "Abrir ao iniciar a sessão" (the XDG autostart entry, only from the
installed package) and what a switch shows (notificação curta / notificação /
nada). Avançado: integrated shells (a sub-page that re-runs the installer for
the chosen set), the tools found, the logs folder and "Copiar diagnóstico".
Zona de perigo: "Desinstalar…", confirmed in an Adw.AlertDialog that lists
everything the uninstaller will remove; it runs off the UI thread and the app
quits afterwards.
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ...core.install.autostart import INSTALLED_COMMAND, AutostartUnavailable  # noqa: E402
from ...core.install.installer import Integrations  # noqa: E402
from ...core.models import ShellKind, SwitchFeedback  # noqa: E402
from ...core.tools import Tool  # noqa: E402
from .. import widgets  # noqa: E402

FEEDBACK_CHOICES = (
    (SwitchFeedback.HUD, "Notificação curta"),
    (SwitchFeedback.NOTIFICATION, "Notificação"),
    (SwitchFeedback.NONE, "Nada"),
)
# Display order: bash first (the login shell of Ubuntu and Pop!_OS).
SHELL_ORDER = (ShellKind.BASH, ShellKind.ZSH, ShellKind.FISH)

AUTOSTART_SUBTITLE = "Reaplica o perfil ativo e fica na bandeja"
AUTOSTART_UNAVAILABLE = (f"Só com o MultiCred instalado pelo pacote .deb ({INSTALLED_COMMAND}); "
                         "rodando de uma pasta de desenvolvimento, a sessão não teria o que abrir")
UNINSTALL_SUBTITLE = ("Remove só os blocos marcados, o environment.d, o autostart e os segredos do app. "
                      "As contas continuam logadas no gh e no Claude.")
SHELLS_NOTE = ("Nos shells marcados, terminais já abertos também trocam de conta a cada prompt. Mudar aqui "
               "reescreve só o bloco do MultiCred nesses arquivos, com backup.")

_CSS = """
row.mc-mono label.subtitle { font-family: monospace; }
.mc-danger-group label.heading { color: @error_color; }
.mc-danger-group list.boxed-list { box-shadow: 0 0 0 1px alpha(@error_color, 0.4); }
.mc-uninstall-list { font-size: 12.5px; }
"""
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


def shells_summary(enabled) -> str:
    """"bash · zsh e fish não instalados" (design G)."""
    on = [s.value for s in SHELL_ORDER if s in enabled]
    off = [s.value for s in SHELL_ORDER if s not in enabled]
    if not on:
        return "Nenhum shell integrado"
    parts = list(on)
    if off:
        names = off[0] if len(off) == 1 else ", ".join(off[:-1]) + " e " + off[-1]
        parts.append(f"{names} não instalado" + ("s" if len(off) > 1 else ""))
    return " · ".join(parts)


def describe_item(item) -> str:
    """One line of the uninstall confirmation, from an UninstallItem."""
    plural = "s" if item.count != 1 else ""
    return {
        "block": f"{item.path}: o bloco do MultiCred ({len(item.lines)} linhas); o resto do arquivo fica",
        "manual": f"{item.path}: bloco sem o marcador de fim; fica como está e você remove à mão",
        "file": f"Apagar {item.path}",
        "session": f"systemctl --user unset-environment {item.path}",
        "secrets": ("Apagar do chaveiro 1 segredo do MultiCred (chave de API ou token)" if item.count == 1
                    else f"Apagar do chaveiro {item.count} segredos do MultiCred (chaves de API e tokens)"),
        "claudeLogin": f"{item.path} volta para o próprio login do Claude",
        "claudeVault": f"Apagar {item.count} login{plural} do Claude guardado{plural} pelo MultiCred",
        "claudeFolder": f"Sair (claude auth logout) e apagar {item.path}",
        "backups": f"Os backups vão para {item.path}",
        "appDir": f"Apagar {item.path}",
    }.get(item.kind, item.path)


class SettingsPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Ajustes")
        _load_css()
        self.window = window
        self.model = window.model
        self._refreshing = False
        self._tool_paths: dict | None = None
        self.uninstall_dialog: Adw.AlertDialog | None = None
        self.claude_folders_check: Gtk.CheckButton | None = None
        self.result_dialog: Adw.AlertDialog | None = None
        self.result_text = ""
        self.groups: list[Adw.PreferencesGroup] = []

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.append(self._build_general())
        content.append(self._build_advanced())
        content.append(self._build_danger())
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(widgets.scrolled(content, 720))
        self.set_child(view)
        self.refresh()
        self.model.locate_tools(self._tools_located)

    # MARK: building

    def _group(self, title: str) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)
        self.groups.append(group)
        return group

    def _build_general(self) -> Gtk.Widget:
        group = self._group("Geral")
        self.autostart_row = Adw.SwitchRow(title="Abrir ao iniciar a sessão", subtitle=AUTOSTART_SUBTITLE)
        self.autostart_row.connect("notify::active", self._autostart_toggled)
        group.add(self.autostart_row)
        self.feedback_row = widgets.combo("Ao trocar de perfil, mostrar", [label for _v, label in FEEDBACK_CHOICES])
        self.feedback_row.connect("notify::selected", self._feedback_selected)
        group.add(self.feedback_row)
        return group

    def _build_advanced(self) -> Gtk.Widget:
        group = self._group("Avançado")
        self.shells_row = self._navigation_row("Shells integrados", self.open_shells_page)
        group.add(self.shells_row)
        self.tools_row = self._navigation_row("Ferramentas", self.open_tools_page)
        self.tools_row.add_css_class("mc-mono")
        self.tools_row.set_subtitle("procurando…")
        group.add(self.tools_row)
        self.logs_row = Adw.ActionRow(title="Logs", subtitle=self._logs_text())
        self.logs_row.add_css_class("mc-mono")
        self.logs_row.add_suffix(widgets.button("Abrir pasta", self.open_logs_folder))
        self.logs_row.add_suffix(widgets.button("Copiar diagnóstico", self.copy_diagnostics))
        group.add(self.logs_row)
        return group

    def _build_danger(self) -> Gtk.Widget:
        group = self._group("Zona de perigo")
        group.add_css_class("mc-danger-group")
        row = Adw.ActionRow(title="Desinstalar o MultiCred", subtitle=UNINSTALL_SUBTITLE)
        row.set_subtitle_lines(0)
        self.uninstall_button = widgets.button("Desinstalar…", self.open_uninstall, "destructive-action")
        row.add_suffix(self.uninstall_button)
        group.add(row)
        return group

    def _navigation_row(self, title: str, on_activate: Callable) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, activatable=True)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda *_: on_activate())
        return row

    def _logs_text(self) -> str:
        return self.model.environment.abbreviate(str(self.model.environment.logs_directory)) + "/"

    # MARK: state

    def group_titles(self) -> list[str]:
        return [group.get_title() for group in self.groups]

    def feedback_options(self) -> list[str]:
        model = self.feedback_row.get_model()
        return [model.get_string(i) for i in range(model.get_n_items())]

    def refresh(self) -> None:
        """Cheap: runs on every model change while the page is shown."""
        config = self.model.config
        autostart = self.model.autostart
        self._refreshing = True
        try:
            available = autostart.is_available()
            self.autostart_row.set_sensitive(available or autostart.is_enabled())
            self.autostart_row.set_subtitle(widgets.escape(AUTOSTART_SUBTITLE if available else AUTOSTART_UNAVAILABLE))
            self.autostart_row.set_active(autostart.is_enabled())
            if config is not None:
                index = next(i for i, (value, _l) in enumerate(FEEDBACK_CHOICES)
                             if value == config.settings.switch_feedback)
                if self.feedback_row.get_selected() != index:
                    self.feedback_row.set_selected(index)
                self.shells_row.set_subtitle(shells_summary(config.settings.enabled_shells))
        finally:
            self._refreshing = False

    def _tools_located(self, paths, _error) -> None:
        self._tool_paths = paths or {}
        abbreviate = self.model.environment.abbreviate
        self.tools_row.set_subtitle(widgets.escape(" · ".join(
            f"{tool.value} {abbreviate(self._tool_paths[tool]) if self._tool_paths.get(tool) else 'não encontrado'}"
            for tool in Tool)))

    # MARK: Geral

    def _autostart_toggled(self, row, _param) -> None:
        if self._refreshing:
            return
        try:
            self.model.set_open_at_login(row.get_active())
        except AutostartUnavailable:
            self.window.show_toast(AUTOSTART_UNAVAILABLE)
            self.refresh()
        except Exception as error:  # a write that failed: show it and put the switch back
            self.window.show_toast(f"Não consegui mudar a abertura ao iniciar a sessão: {error}")
            self.refresh()

    def _feedback_selected(self, row, _param) -> None:
        if self._refreshing or self.model.config is None:
            return
        value = FEEDBACK_CHOICES[row.get_selected()][0]
        if value == self.model.config.settings.switch_feedback:
            return
        try:
            self.model.set_switch_feedback(value)
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            self.refresh()

    # MARK: Avançado

    def open_shells_page(self) -> "ShellsPage":
        return self.window.push(ShellsPage(self.window))

    def open_tools_page(self) -> Adw.NavigationPage:
        group = Adw.PreferencesGroup(title="Ferramentas encontradas",
                                     description="O MultiCred procura nos lugares de instalação conhecidos e "
                                                 "depois no PATH do seu shell de login.")
        paths = self._tool_paths or {}
        for tool in Tool:
            path = paths.get(tool)
            row = Adw.ActionRow(title=tool.display_name,
                                subtitle=widgets.escape(self.model.environment.abbreviate(path) if path
                                                        else "não encontrado"))
            row.add_css_class("mc-mono")
            row.set_subtitle_selectable(True)
            group.add(row)
        page, _header = widgets.page("Ferramentas", widgets.scrolled(group, 720))
        return self.window.push(page)

    def open_logs_folder(self) -> None:
        directory = self.model.environment.logs_directory
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            self.window.show_toast(f"Não consegui criar a pasta de logs: {error}")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(directory)))

        def launched(source, result):
            try:
                source.launch_finish(result)
            except GLib.Error as error:
                self.window.show_toast(f"Não consegui abrir a pasta de logs: {error.message}")

        launcher.launch(self.window, None, launched)

    def copy_diagnostics(self, on_done: Callable[[str], None] | None = None) -> None:
        def ready(text: str) -> None:
            self.get_clipboard().set(text)
            self.window.show_toast("Diagnóstico copiado")
            if on_done is not None:
                on_done(text)

        self.model.diagnostics(ready)

    # MARK: Zona de perigo

    def uninstall_preview_text(self) -> str:
        remove = self.claude_folders_check is not None and self.claude_folders_check.get_active()
        return "\n".join(f"• {describe_item(item)}" for item in self.model.uninstall_preview(remove))

    def open_uninstall(self) -> Adw.AlertDialog:
        dialog = Adw.AlertDialog(
            heading="Desinstalar o MultiCred?",
            body="Só o que o MultiCred adicionou é removido; o resto de cada arquivo fica como você deixou. "
                 "As contas continuam logadas no gh e no Claude.")
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        folders = self.model.created_claude_folders()
        self.claude_folders_check = Gtk.CheckButton(
            label="Também sair e apagar as pastas do Claude criadas pelo MultiCred", active=False,
            sensitive=bool(folders))
        self.claude_folders_check.set_tooltip_text(
            ", ".join(self.model.environment.abbreviate(f) for f in folders) if folders
            else "Nenhuma pasta do Claude foi criada pelo MultiCred")
        items = widgets.label(self.uninstall_preview_text(), "mc-uninstall-list", wrap=True, selectable=True)
        self.claude_folders_check.connect("toggled", lambda *_: items.set_label(self.uninstall_preview_text()))
        extra.append(Gtk.ScrolledWindow(child=items, propagate_natural_height=True, max_content_height=280,
                                        hscrollbar_policy=Gtk.PolicyType.NEVER))
        extra.append(self.claude_folders_check)
        dialog.set_extra_child(extra)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("confirm", "Desinstalar")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._uninstall_response)
        self.uninstall_dialog = dialog
        dialog.present(self.window)
        return dialog

    def _uninstall_response(self, _dialog, response: str) -> None:
        if response != "confirm":
            return
        remove = self.claude_folders_check.get_active()
        self.uninstall_button.set_sensitive(False)
        self.window.show_toast("Desinstalando…")
        self.model.uninstall(remove, self._uninstalled)

    def _uninstalled(self, report, error) -> None:
        self.window.dismiss_toast()
        if error is not None:
            self.uninstall_button.set_sensitive(True)
            self.window.show_toast(f"Não consegui desinstalar: {error}")
            return
        lines = []
        if report.changed_files or report.removed_files:
            lines.append("Arquivos restaurados: " + ", ".join(report.changed_files + report.removed_files) + ".")
        if report.backups_kept_at:
            lines.append(f"Backups guardados em {report.backups_kept_at}.")
        if report.deleted_folders:
            lines.append("Pastas do Claude apagadas: " + ", ".join(report.deleted_folders) + ".")
        lines += report.manual_steps
        lines += [f"Problema: {problem}" for problem in report.problems]
        lines.append("Abra um novo terminal para que os shells deixem de carregar o MultiCred. "
                     "Para tirar o app: sudo apt remove multicred.")
        heading = "MultiCred desinstalado" if not report.problems else "MultiCred desinstalado, com avisos"
        body = "\n\n".join(lines)
        self.result_text = f"{heading}\n{body}"
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("quit", "Sair do MultiCred")
        dialog.set_default_response("quit")
        dialog.set_close_response("quit")
        dialog.connect("response", lambda *_: self.window.get_application().quit())
        self.result_dialog = dialog
        dialog.present(self.window)


class ShellsPage(Adw.NavigationPage):
    """One switch per shell; each change re-runs the installer for the chosen set."""

    def __init__(self, window):
        super().__init__(title="Shells integrados")
        self.window = window
        self.model = window.model
        self.switches: dict[ShellKind, Adw.SwitchRow] = {}
        self._refreshing = False
        group = Adw.PreferencesGroup(title="Shells integrados", description=SHELLS_NOTE)
        environment = self.model.environment
        for shell in SHELL_ORDER:
            files = ([Integrations.fish_conf_file(environment)] if shell == ShellKind.FISH
                     else Integrations.rc_files(shell, environment))
            row = Adw.SwitchRow(title=shell.value,
                                subtitle=widgets.escape(", ".join(environment.abbreviate(str(f)) for f in files)))
            row.add_css_class("mc-mono")
            row.connect("notify::active", self._toggled)
            self.switches[shell] = row
            group.add(row)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(widgets.scrolled(group, 720))
        self.set_child(view)
        self.refresh()

    def refresh(self) -> None:
        config = self.model.config
        if config is None:
            return
        self._refreshing = True
        try:
            for shell, row in self.switches.items():
                row.set_active(shell in config.settings.enabled_shells)
        finally:
            self._refreshing = False

    def _toggled(self, _row, _param) -> None:
        if self._refreshing:
            return
        chosen = [shell for shell, row in self.switches.items() if row.get_active()]
        try:
            self.model.set_enabled_shells(chosen)
        except Exception as error:  # InstallError (e.g. a hand-edited block), a failed save
            self.window.show_toast(f"Não consegui mudar os shells: {widgets.describe_error(error)}")
            self.refresh()
