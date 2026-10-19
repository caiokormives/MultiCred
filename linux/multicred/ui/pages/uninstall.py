"""The "Desinstalar" page pushed from Ajustes (UninstallPage, SettingsPage.swift:114-174).

Like the Mac it is a page in the same window, not a dialog: "Desinstalar o
MultiCred", the diff of each file (only MultiCred's block goes away), one "Também:"
line for what is not a file, "Cancelar" / "Desinstalar"; the result replaces the
content of the same page and ends with "Sair do MultiCred".

Linux differences, all from the uninstaller's preview (core/install/uninstaller.py):
launchctl → `systemctl --user unset-environment`, Keychain → chaveiro do sistema,
the autostart entry instead of the login item, Claude folders getting their own
login back, and the D12 option to log out of and delete the Claude folders
MultiCred created. The uninstaller runs off the UI thread.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import widgets  # noqa: E402
from . import onboarding  # noqa: E402

INTRO = "Estas são as mudanças. Só o bloco do MultiCred é removido de cada arquivo; o resto fica como você deixou."
FINAL_NOTE = ("Abra um novo terminal para que os shells deixem de carregar o MultiCred. "
              "Para tirar o app: sudo apt remove multicred.")
D12_LABEL = "Também sair e apagar as pastas do Claude criadas pelo MultiCred"

_CSS = """
.mc-uninstall-done { color: @success_color; }
.mc-uninstall-manual { color: @warning_color; }
.mc-uninstall-problem { color: @error_color; }
"""
_css_loaded = False


def _load_css() -> None:
    global _css_loaded
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if _css_loaded or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def also_text(items, autostart_path: str) -> str:
    """The Mac's "Também: …" line (SettingsPage.swift:132), built from what this
    uninstall really does; empty when there is nothing besides the files."""
    pieces: list[str] = []
    autostart = False
    backups = None
    for item in items:
        if item.kind == "session":
            pieces.append(f"systemctl --user unset-environment {item.path}")
        elif item.kind == "secrets":
            pieces.append("apagar do chaveiro do sistema as chaves de API e os tokens das contas Claude "
                          "(pastas e conversas do Claude ficam)")
        elif item.kind == "claudeVault":
            plural = "s" if item.count != 1 else ""
            pieces.append(f"apagar {item.count} login{plural} do Claude guardado{plural} pelo MultiCred")
        elif item.kind == "claudeLogin":
            pieces.append(f"devolver a {item.path} o próprio login do Claude")
        elif item.kind == "claudeFolder":
            pieces.append(f"sair (claude auth logout) e apagar {item.path}")
        elif item.kind == "file" and item.path == autostart_path:
            autostart = True
        elif item.kind == "backups":
            backups = item.path
    if autostart:  # last, as on the Mac
        pieces.append("desligar a abertura ao iniciar a sessão")
    text = f"Também: {', '.join(pieces)}." if pieces else ""
    if backups:
        text = f"{text} Os backups vão para {backups}.".strip()
    return text


def file_changes(items, autostart_path: str) -> list[tuple[str, str, list[str]]]:
    """(path, summary, diff lines) per file, like the Mac's FileChangeView."""
    changes = []
    for item in items:
        if item.kind == "block":
            changes.append((item.path, f"−{_plural(len(item.lines), 'linha', 'linhas')}",
                            [f"− {line}" for line in item.lines]))
        elif item.kind == "manual":
            changes.append((item.path, "bloco sem o marcador de fim; fica como está e você remove à mão", []))
        elif item.kind in ("file", "appDir") and item.path != autostart_path:
            changes.append((item.path, "será apagado", []))
    return changes


class UninstallPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Desinstalar")
        onboarding.load_css()
        _load_css()
        self.window = window
        self.model = window.model
        self.report = None
        self.running = False
        self._changes: list[tuple[str, str, list[str]]] = []
        self._result_labels: list[Gtk.Label] = []

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_preview(), "preview")
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.stack.add_named(widgets.scrolled(self.result_box), "result")

        self.cancel_button = widgets.button("Cancelar", self.cancel)
        self.uninstall_button = widgets.button("Desinstalar", self.uninstall, "destructive-action")
        self.spinner = Gtk.Spinner(spinning=True, visible=False, valign=Gtk.Align.CENTER)
        self.quit_button = widgets.button("Sair do MultiCred", self.quit, "suggested-action")
        self.quit_button.set_visible(False)
        footer = Gtk.Box(spacing=8)
        footer.add_css_class("mc-editor-footer")
        footer.append(Gtk.Box(hexpand=True))
        footer.append(self.spinner)
        footer.append(self.cancel_button)
        footer.append(self.uninstall_button)
        footer.append(self.quit_button)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.stack)
        view.add_bottom_bar(footer)
        self.set_child(view)
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.CallbackAction.new(lambda *_: self.cancel() or True)))
        self.add_controller(keys)
        self.render_preview()

    # MARK: preview

    def _build_preview(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.heading_label = widgets.label("Desinstalar o MultiCred", "title-2", wrap=True)
        box.append(self.heading_label)
        self.intro_label = widgets.label(INTRO, "dim-label", wrap=True)
        box.append(self.intro_label)
        self.files_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.files_list.add_css_class("boxed-list")
        self.files_list.set_margin_top(6)
        box.append(self.files_list)
        self.also_label = widgets.label("", "dim-label", wrap=True, selectable=True)
        box.append(self.also_label)
        folders = self.model.created_claude_folders()
        self.claude_folders_check = Gtk.CheckButton(label=D12_LABEL, active=False, sensitive=bool(folders))
        self.claude_folders_check.set_tooltip_text(
            ", ".join(self.model.environment.abbreviate(f) for f in folders) if folders
            else "Nenhuma pasta do Claude foi criada pelo MultiCred")
        self.claude_folders_check.connect("toggled", lambda *_: self.render_preview())
        box.append(self.claude_folders_check)
        return widgets.scrolled(box)

    def _autostart_path(self) -> str:
        return self.model.environment.abbreviate(str(self.model.autostart.file))

    def render_preview(self) -> None:
        items = self.model.uninstall_preview(self.claude_folders_check.get_active())
        autostart = self._autostart_path()
        self._changes = file_changes(items, autostart)
        child = self.files_list.get_first_child()
        while child is not None:
            self.files_list.remove(child)
            child = self.files_list.get_first_child()
        for path, summary, lines in self._changes:
            self.files_list.append(onboarding.file_change_row(path, summary, lines))
        self.files_list.set_visible(bool(self._changes))
        text = also_text(items, autostart)
        self.also_label.set_label(text)
        self.also_label.set_visible(bool(text))

    def file_changes(self) -> list[tuple[str, str, list[str]]]:
        return list(self._changes)

    # MARK: actions

    def cancel(self) -> None:
        if self.running or self.report is not None:
            return
        self.window.nav.pop()

    def uninstall(self) -> None:
        if self.running or self.report is not None:
            return
        self.running = True
        self.uninstall_button.set_sensitive(False)
        self.cancel_button.set_sensitive(False)
        self.claude_folders_check.set_sensitive(False)
        self.spinner.set_visible(True)
        self.model.uninstall(self.claude_folders_check.get_active(), self._uninstalled)

    def _uninstalled(self, report, error) -> None:
        self.running = False
        self.spinner.set_visible(False)
        if error is not None:
            self.uninstall_button.set_sensitive(True)
            self.cancel_button.set_sensitive(True)
            self.window.show_toast(f"Não consegui desinstalar: {error}")
            return
        self.report = report
        self._render_result(report)

    def quit(self) -> None:
        self.window.get_application().quit()

    # MARK: result

    def _result_label(self, text: str, *classes: str, icon: str | None = None) -> None:
        label = widgets.label(text, *classes, wrap=True, selectable=True)
        self._result_labels.append(label)
        if icon is None:
            self.result_box.append(label)
            return
        line = Gtk.Box(spacing=8)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_valign(Gtk.Align.START)
        for css in classes:
            image.add_css_class(css)
        line.append(image)
        label.set_hexpand(True)
        line.append(label)
        self.result_box.append(line)

    def _render_result(self, report) -> None:
        title = Gtk.Box(spacing=10)
        image = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        image.set_pixel_size(24)
        image.add_css_class("mc-uninstall-done")
        title.append(image)
        heading = widgets.label("MultiCred desinstalado", "title-2", "mc-uninstall-done")
        self._result_labels.append(heading)
        title.append(heading)
        self.result_box.append(title)
        restored = report.changed_files + report.removed_files
        if restored:
            self._result_label("Arquivos restaurados: " + ", ".join(restored))
        if report.backups_kept_at:
            self._result_label(f"Backups guardados em {report.backups_kept_at}.", "dim-label")
        if report.deleted_folders:
            self._result_label("Pastas do Claude apagadas: " + ", ".join(report.deleted_folders) + ".")
        for step in report.manual_steps:
            self._result_label(step, "mc-uninstall-manual", icon="dialog-warning-symbolic")
        for problem in report.problems:
            self._result_label(problem, "mc-uninstall-problem", icon="dialog-error-symbolic")
        self._result_label(FINAL_NOTE, "dim-label")
        self.stack.set_visible_child_name("result")
        self.cancel_button.set_visible(False)
        self.uninstall_button.set_visible(False)
        self.quit_button.set_visible(True)
        self.quit_button.grab_focus()

    def result_texts(self) -> list[str]:
        return [label.get_label() for label in self._result_labels]
