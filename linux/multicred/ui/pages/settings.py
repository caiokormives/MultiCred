"""Ajustes, like the Mac's SettingsPage.swift: one form, no sub-pages.

- "Geral" (:15-22): "Abrir ao iniciar a sessão" (the XDG autostart entry, only from
  the installed package) and "Ao trocar de perfil, mostrar" — the Mac's "HUD breve
  e notificação" is "Notificação breve" here (Wayland has no HUD), then "Só
  notificação" and "Nada".
- "Shells" (:23-36): a switch per shell with its files in monospace and the Mac's
  footer; each change re-runs the installer for the chosen set.
- "Ferramentas encontradas" (:37-42): the path of each tool, "procurando…" or
  "não encontrado".
- "Diagnóstico" (:43-54): "Abrir pasta de logs" and "Copiar diagnóstico".
- Only once installed (:55-71): "Desinstalar e restaurar arquivos originais", whose
  "Desinstalar…" pushes the uninstall page (pages/uninstall.py).
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
    (SwitchFeedback.HUD, "Notificação breve"),
    (SwitchFeedback.NOTIFICATION, "Só notificação"),
    (SwitchFeedback.NONE, "Nada"),
)
# Display order: bash first (the login shell of Ubuntu and Pop!_OS); the Mac lists zsh first.
SHELL_ORDER = (ShellKind.BASH, ShellKind.ZSH, ShellKind.FISH)

AUTOSTART_SUBTITLE = "Reaplica o perfil ativo e fica na bandeja"
AUTOSTART_UNAVAILABLE = (f"Só com o MultiCred instalado pelo pacote .deb ({INSTALLED_COMMAND}); "
                         "rodando de uma pasta de desenvolvimento, a sessão não teria o que abrir")
SHELLS_NOTE = ("Nos shells marcados, terminais já abertos também trocam de conta a cada prompt. Mudar aqui "
               "reescreve só o bloco do MultiCred nesses arquivos, com backup.")
UNINSTALL_TITLE = "Desinstalar e restaurar arquivos originais"
UNINSTALL_SUBTITLE = ("Remove o bloco do MultiCred dos arquivos de shell e do gitconfig, a variável do Claude Code "
                      "na sessão gráfica e as chaves de API do chaveiro do sistema. As contas continuam logadas "
                      "nas próprias ferramentas.")
SEARCHING = "procurando…"
NOT_FOUND = "não encontrado"

_CSS = """
row.mc-mono label.subtitle { font-family: monospace; font-size: 12px; }
row.mc-danger label.title { color: @error_color; font-weight: bold; }
row.mc-danger image.mc-danger-icon { color: @error_color; }
.mc-settings-footer { margin: 6px 12px 0 12px; font-size: 12px; }
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


class SettingsPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Ajustes")
        _load_css()
        self.window = window
        self.model = window.model
        self._refreshing = False
        self._tool_paths: dict | None = None
        self.groups: list[Adw.PreferencesGroup] = []

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.append(self._build_general())
        content.append(self._build_shells())
        content.append(self._build_tools())
        content.append(self._build_diagnostics())
        content.append(self._build_danger())
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.scroller = widgets.scrolled(content, 720)
        view.set_content(self.scroller)
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

    def _build_shells(self) -> Gtk.Widget:
        group = self._group("Shells")
        environment = self.model.environment
        self.shell_switches: dict[ShellKind, Adw.SwitchRow] = {}
        for shell in SHELL_ORDER:
            files = ([Integrations.fish_conf_file(environment)] if shell == ShellKind.FISH
                     else Integrations.rc_files(shell, environment))
            row = Adw.SwitchRow(title=shell.value,
                                subtitle=widgets.escape(", ".join(environment.abbreviate(str(f)) for f in files)))
            row.add_css_class("mc-mono")
            row.connect("notify::active", self._shell_toggled)
            self.shell_switches[shell] = row
            group.add(row)
        # The Mac's section footer sits under the rows (Adw's description would go above them).
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        self.shells_footer = widgets.label(SHELLS_NOTE, "dim-label", "mc-settings-footer", wrap=True)
        box.append(self.shells_footer)
        return box

    def _build_tools(self) -> Gtk.Widget:
        group = self._group("Ferramentas encontradas")
        self.tool_rows: dict[Tool, Adw.ActionRow] = {}
        self._tool_labels: dict[Tool, Gtk.Label] = {}
        for tool in Tool:
            row = Adw.ActionRow(title=tool.display_name)
            value = widgets.label(SEARCHING, "dim-label", selectable=True)
            value.set_valign(Gtk.Align.CENTER)
            row.add_suffix(value)
            self.tool_rows[tool] = row
            self._tool_labels[tool] = value
            group.add(row)
        return group

    def _build_diagnostics(self) -> Gtk.Widget:
        group = self._group("Diagnóstico")
        buttons = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.logs_button = widgets.button("Abrir pasta de logs", self.open_logs_folder)
        self.logs_button.set_tooltip_text(self._logs_text())
        self.diagnostics_button = widgets.button("Copiar diagnóstico", self.copy_diagnostics)
        buttons.append(self.logs_button)
        buttons.append(self.diagnostics_button)
        group.add(Gtk.ListBoxRow(child=buttons, activatable=False))
        return group

    def _build_danger(self) -> Gtk.Widget:
        # Untitled, like the Mac's last Section; only once installed (refresh()).
        self.danger_group = Adw.PreferencesGroup()
        self.uninstall_row = Adw.ActionRow(title=UNINSTALL_TITLE, subtitle=UNINSTALL_SUBTITLE)
        self.uninstall_row.add_css_class("mc-danger")
        self.uninstall_row.set_subtitle_lines(0)
        icon = Gtk.Image.new_from_icon_name("user-trash-symbolic")
        icon.set_pixel_size(20)
        icon.add_css_class("mc-danger-icon")
        self.uninstall_row.add_prefix(icon)
        self.uninstall_button = widgets.button("Desinstalar…", self.open_uninstall, "destructive-action")
        self.uninstall_row.add_suffix(self.uninstall_button)
        self.danger_group.add(self.uninstall_row)
        return self.danger_group

    def _logs_text(self) -> str:
        return self.model.environment.abbreviate(str(self.model.environment.logs_directory)) + "/"

    # MARK: state

    def group_titles(self) -> list[str]:
        return [group.get_title() for group in self.groups]

    def feedback_options(self) -> list[str]:
        model = self.feedback_row.get_model()
        return [model.get_string(i) for i in range(model.get_n_items())]

    def tool_value(self, tool: Tool) -> str:
        return self._tool_labels[tool].get_label()

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
                for shell, row in self.shell_switches.items():
                    row.set_active(shell in config.settings.enabled_shells)
            self.danger_group.set_visible(self.model.is_installed)
        finally:
            self._refreshing = False

    def _tools_located(self, paths, _error) -> None:
        self._tool_paths = paths or {}
        abbreviate = self.model.environment.abbreviate
        for tool, value in self._tool_labels.items():
            path = self._tool_paths.get(tool)
            value.set_label(abbreviate(path) if path else NOT_FOUND)

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

    # MARK: Shells

    def _shell_toggled(self, _row, _param) -> None:
        if self._refreshing:
            return
        chosen = [shell for shell, row in self.shell_switches.items() if row.get_active()]
        try:
            self.model.set_enabled_shells(chosen)
        except Exception as error:  # InstallError (e.g. a hand-edited block), a failed save
            self.window.show_toast(f"Não consegui mudar os shells: {widgets.describe_error(error)}")
            self.refresh()

    # MARK: Diagnóstico

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

    # MARK: Desinstalar

    def open_uninstall(self) -> Adw.NavigationPage:
        from .uninstall import UninstallPage
        return self.window.push(UninstallPage(self.window))
