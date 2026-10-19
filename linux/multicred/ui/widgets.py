"""Small building blocks shared by the main window pages (approved Linux designs A, D, E).

Avatars in the profile colors (ProfileColor), status pills, the Ctrl+N hint, color
swatches, per-field error labels, and the Portuguese text for core errors (the core
stays language-neutral; UserMessages.swift is the Mac counterpart).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ..core.models import ProfileColor  # noqa: E402

COLOR_NAMES = {
    ProfileColor.INDIGO: "Índigo", ProfileColor.BLUE: "Azul", ProfileColor.TEAL: "Verde-azulado",
    ProfileColor.GREEN: "Verde", ProfileColor.ORANGE: "Laranja", ProfileColor.RED: "Vermelho",
    ProfileColor.PINK: "Rosa", ProfileColor.GRAPHITE: "Grafite",
}

_CSS = """
.mc-avatar { color: white; font-weight: 800; border-radius: 9px; }
.mc-avatar-64 { min-width: 64px; min-height: 64px; border-radius: 14px; font-size: 26px; }
.mc-avatar-40 { min-width: 40px; min-height: 40px; border-radius: 9px; font-size: 17px; }
.mc-avatar-36 { min-width: 36px; min-height: 36px; border-radius: 8px; font-size: 15px; }
.mc-avatar-18 { min-width: 18px; min-height: 18px; border-radius: 5px; font-size: 11px; }
.mc-pill { border-radius: 999px; padding: 2px 9px; font-size: 11px; font-weight: bold; }
.mc-pill.ok { background: alpha(@success_color, 0.15); color: @success_color; }
.mc-pill.warning { background: alpha(@warning_color, 0.18); color: @warning_color; }
.mc-pill.error { background: alpha(@error_color, 0.15); color: @error_color; }
.mc-pill.neutral { background: alpha(currentColor, 0.08); color: alpha(currentColor, 0.7); }
.mc-kbd { font-family: monospace; font-size: 11px; border: 1px solid alpha(currentColor, 0.2);
          border-radius: 5px; padding: 1px 6px; color: alpha(currentColor, 0.6); }
.mc-key { color: alpha(currentColor, 0.55); min-width: 74px; }
row.mc-hero { padding-top: 12px; padding-bottom: 12px; }
row.mc-hero label.title { font-size: 20pt; font-weight: 800; color: @view_fg_color; }
row.mc-hero label.subtitle { font-size: 11pt; }
row.mc-detail, row.mc-detail > box.header { min-height: 38px; padding-top: 0; padding-bottom: 0; }
.mc-footer { border-top: 1px solid alpha(currentColor, 0.12); padding: 0 16px; min-height: 42px; }
.mc-dot { min-width: 8px; min-height: 8px; border-radius: 4px; background: alpha(currentColor, 0.3); }
.mc-dot.ok { background: @success_color; }
.mc-dot.warning { background: @warning_color; }
.mc-avatar-20 { min-width: 20px; min-height: 20px; border-radius: 5px; font-size: 11px; }
.mc-sidebar-heading { font-size: 11px; font-weight: bold; color: alpha(currentColor, 0.55);
                      margin: 12px 10px 4px 10px; }
.mc-sidebar-count { color: alpha(currentColor, 0.55); font-feature-settings: "tnum"; }
.mc-sidebar-check { color: alpha(currentColor, 0.55); }
.mc-sidebar-warning { color: @orange_3; }
.mc-sidebar-dot { min-width: 7px; min-height: 7px; border-radius: 4px; }
.mc-sidebar-dot.ok { background: @green_4; }
.mc-sidebar-dot.warning { background: @orange_3; }
.mc-preview { background: rgba(20, 20, 22, 0.92); color: #eeeeee; border-radius: 8px; padding: 6px 12px;
              font-size: 12px; }
.mc-segmented { background: alpha(currentColor, 0.08); border-radius: 9px; padding: 3px; }
.mc-segmented > button { border-radius: 7px; background: none; box-shadow: none; padding: 4px 14px;
                         font-weight: 600; min-height: 0; }
.mc-segmented > button:checked { background: @view_bg_color; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12); }
.mc-count { font-size: 11px; color: alpha(currentColor, 0.5); }
.mc-count.warning { color: @warning_color; }
.mc-note { background: alpha(currentColor, 0.06); border-radius: 10px; padding: 10px 14px; font-size: 12.5px;
           color: alpha(currentColor, 0.75); }
.mc-field-error { color: @error_color; font-size: smaller; }
.mc-new-row label { color: @accent_color; font-weight: 600; }
.mc-swatch { min-width: 24px; min-height: 24px; border-radius: 999px; padding: 0; margin: 0 2px; }
.mc-swatch:checked { box-shadow: 0 0 0 2px @window_bg_color, 0 0 0 4px @accent_color; }
""" + "".join(f".mc-color-{color.value} {{ background: {color.hex}; }}\n" for color in ProfileColor)

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


ICONS_DIR = Path(__file__).resolve().parent / "icons"
_icons_loaded = False


def load_icons() -> None:
    """The app's own symbolic icons (branch, </>, sparkle: Adwaita has none of them).

    `icons/` is an icon-theme search path; its hicolor/scalable/actions holds the
    `multicred-*-symbolic.svg` files, recolored by GTK like any symbolic icon."""
    global _icons_loaded
    display = Gdk.Display.get_default()
    if _icons_loaded or display is None:
        return
    Gtk.IconTheme.get_for_display(display).add_search_path(str(ICONS_DIR))
    _icons_loaded = True


def profile_letter(profile) -> str:
    """The badge letter (Mac `Profile.badgeLetter`): the icon's letter, else the name's first."""
    if profile.icon is not None and profile.icon.kind == "letter" and profile.icon.value:
        return profile.icon.value
    return profile.name[:1]


def escape(text: str | None) -> str:
    return GLib.markup_escape_text(text or "")


def label(text: str, *classes: str, wrap: bool = False, xalign: float = 0.0, selectable: bool = False,
          markup: bool = False) -> Gtk.Label:
    widget = Gtk.Label(xalign=xalign, wrap=wrap, selectable=selectable)
    if markup:
        widget.set_markup(text)
    else:
        widget.set_label(text)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for css in classes:
        widget.add_css_class(css)
    return widget


def avatar(letter: str, color: ProfileColor, size: int = 40) -> Gtk.Label:
    """A rounded square in the profile color with a white letter (designs A, D, E)."""
    widget = label((letter or "?")[:1].upper(), "mc-avatar", f"mc-avatar-{size}", f"mc-color-{color.value}",
                   xalign=0.5)
    widget.set_valign(Gtk.Align.CENTER)
    widget.set_halign(Gtk.Align.CENTER)
    return widget


def set_avatar(widget: Gtk.Label, letter: str, color: ProfileColor) -> None:
    for other in ProfileColor:
        widget.remove_css_class(f"mc-color-{other.value}")
    widget.add_css_class(f"mc-color-{color.value}")
    widget.set_label((letter or "?")[:1].upper())


def pill(text: str, tone: str) -> Gtk.Label:
    widget = label(text, "mc-pill", tone)
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def kbd(text: str) -> Gtk.Label:
    widget = label(text, "mc-kbd")
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def note(text: str) -> Gtk.Label:
    widget = label(text, "mc-note", wrap=True)
    widget.set_margin_top(14)
    return widget


def button(text: str, on_click: Callable, *classes: str) -> Gtk.Button:
    widget = Gtk.Button(label=text, valign=Gtk.Align.CENTER)
    for css in classes:
        widget.add_css_class(css)
    widget.connect("clicked", lambda *_: on_click())
    return widget


def scrolled(child: Gtk.Widget, maximum: int = 720) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=maximum, tightening_threshold=560, child=child)
    clamp.set_margin_top(22)
    clamp.set_margin_bottom(24)
    clamp.set_margin_start(16)
    clamp.set_margin_end(16)
    return Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)


def page(title: str, content: Gtk.Widget, tag: str | None = None, header_end: list | None = None,
         bottom: Gtk.Widget | None = None) -> tuple[Adw.NavigationPage, Adw.HeaderBar]:
    """A navigation page with its own header bar (back button comes from Adw.NavigationView)."""
    view = Adw.ToolbarView()
    header = Adw.HeaderBar()
    for widget in header_end or []:
        header.pack_end(widget)
    view.add_top_bar(header)
    view.set_content(content)
    if bottom is not None:
        view.add_bottom_bar(bottom)
    nav_page = Adw.NavigationPage(title=title, child=view)
    if tag:
        nav_page.set_tag(tag)
    return nav_page, header


def color_swatches(selected: ProfileColor, on_change: Callable[[ProfileColor], None]) -> tuple[Gtk.Box, dict]:
    """The eight profile colors as round toggle buttons; returns the box and buttons by value."""
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    buttons: dict[str, Gtk.ToggleButton] = {}
    group = None
    for color in ProfileColor:
        swatch = Gtk.ToggleButton(tooltip_text=COLOR_NAMES[color], valign=Gtk.Align.CENTER)
        swatch.add_css_class("mc-swatch")
        swatch.add_css_class(f"mc-color-{color.value}")
        swatch.update_property([Gtk.AccessibleProperty.LABEL], [COLOR_NAMES[color]])
        if group is None:
            group = swatch
        else:
            swatch.set_group(group)
        swatch.set_active(color == selected)
        swatch.connect("toggled", lambda button, c=color: button.get_active() and on_change(c))
        buttons[color.value] = swatch
        box.append(swatch)
    return box, buttons


def combo(title: str, options: list[str], selected: int = 0, subtitle: str | None = None) -> Adw.ComboRow:
    row = Adw.ComboRow(title=escape(title), model=Gtk.StringList.new(options))
    if subtitle:
        row.set_subtitle(escape(subtitle))
    row.set_selected(selected if 0 <= selected < len(options) else 0)
    return row


def confirm(parent: Gtk.Widget, heading: str, body: str, action: str, on_confirm: Callable[[], None]) -> Adw.AlertDialog:
    """A destructive confirmation (Adw.AlertDialog, libadwaita 1.5)."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancelar")
    dialog.add_response("confirm", action)
    dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: response == "confirm" and on_confirm())
    dialog.present(parent)
    return dialog


class FieldErrors:
    """Error messages shown next to the fields they belong to (suffix label on the row)."""

    def __init__(self):
        self.rows: dict[str, Gtk.Widget] = {}
        self.labels: dict[str, Gtk.Label] = {}

    def register(self, field: str, row: Gtk.Widget) -> None:
        message = label("", "mc-field-error", wrap=True, xalign=1.0)
        message.set_max_width_chars(28)
        message.set_valign(Gtk.Align.CENTER)
        message.set_visible(False)
        row.add_suffix(message)
        self.rows[field], self.labels[field] = row, message

    def clear(self) -> None:
        for field, message in self.labels.items():
            message.set_visible(False)
            message.set_label("")
            self.rows[field].remove_css_class("error")

    def show(self, field: str, text: str) -> bool:
        message = self.labels.get(field)
        if message is None:
            return False
        message.set_label(text)
        message.set_visible(True)
        self.rows[field].add_css_class("error")
        return True

    def text(self, field: str) -> str | None:
        message = self.labels.get(field)
        return message.get_label() if message is not None and message.get_visible() else None


# MARK: Portuguese messages for core errors

FIELD_NAMES = {"nickname": "apelido", "name": "nome", "email": "e-mail"}


def issue_message(issue, is_profile: bool = False) -> str:
    kind, detail = issue.kind, issue.detail
    if kind == "emptyField" and is_profile and detail == "name":
        return "Dê um nome ao perfil."
    return {
        "emptyField": f"O campo {FIELD_NAMES.get(detail, detail)} está vazio.",
        "controlCharacters": f"O campo {FIELD_NAMES.get(detail, detail)} tem quebra de linha ou caractere de controle.",
        "invalidEmail": "E-mail inválido.",
        "invalidGitHubUsername": "Usuário do GitHub inválido (só letras, números e hífen).",
        "invalidHost": "Servidor do GitHub inválido.",
        "invalidEnvVar": f"\"{detail}\" não é um nome de variável válido (use LETRAS_MAIUSCULAS e _).",
        "reservedEnvVar": f"{detail} é reservada e não pode ser usada para uma chave.",
        "duplicateEnvVarInProfile": f"O perfil tem duas chaves para {detail}.",
        "invalidClaudeDirectory": "Pasta do Claude inválida (use um caminho absoluto, sem / no fim).",
        "duplicateClaudeDirectory": "Duas contas Claude antigas usam a mesma pasta.",
        "invalidSSHKeyPath": "Caminho da chave SSH inválido (use caminho absoluto ou começando com ~/).",
        "missingReference": "O perfil aponta para uma conta que não existe mais.",
        "duplicateID": "Configuração com identificadores repetidos.",
    }.get(kind, f"Dados inválidos ({kind}).")


def describe_issues(issues) -> str:
    if not issues:
        return "Dados inválidos."
    text = issue_message(issues[0])
    return f"{text} (+{len(issues) - 1} problema(s))" if len(issues) > 1 else text


def describe_error(error: Exception) -> str:
    """Portuguese text for errors the pages can hit while saving or looking things up."""
    from ..core.adapters.github_cli import UnexpectedOutput
    from ..core.claude.account_editor import ClaudeAccountError
    from ..core.editors import EditorError
    from ..core.runner import CommandError
    from ..core.storage import InvalidConfig, JSONStoreError
    from ..core.claude.login_vault import ClaudeLoginError
    from ..core.engine.types import SwitchError
    from .messages import describe_switch_error
    from .model import ModelError

    if isinstance(error, (SwitchError, ClaudeLoginError)):
        return describe_switch_error(error)
    if isinstance(error, EditorError):
        if error.kind == "invalid":
            return describe_issues(error.issues)
        if error.kind == "emptySecret":
            return "Cole o valor da chave."
        return f"Não consegui gravar no chaveiro do sistema: {error.reason}"
    if isinstance(error, InvalidConfig):
        return describe_issues(error.issues)
    if isinstance(error, ModelError) and error.kind == "toolMissing":
        return "O GitHub CLI (gh) não foi encontrado." if error.detail == "gh" else f"{error.detail} não foi encontrado."
    if isinstance(error, UnexpectedOutput):
        return "O GitHub CLI respondeu algo inesperado."
    if isinstance(error, ClaudeAccountError):
        if error.kind == "invalid":
            return describe_issues(error.detail)
        return {"invalidPath": f"Caminho inválido: {error.detail}. Use um caminho absoluto ou começando com ~/.",
                "folderMissing": f"A pasta não existe: {error.detail}",
                "notADirectory": f"Não é uma pasta: {error.detail}",
                "outsideSandbox": f"No modo de teste, a pasta precisa ficar dentro do HOME de teste ({error.detail} está fora).",
                "secrets": f"Não consegui gravar no chaveiro do sistema: {error.detail}",
                }.get(error.kind, str(error))
    if isinstance(error, (CommandError, JSONStoreError)):
        return str(error)
    if isinstance(error, OSError):
        return f"Não consegui salvar: {error}"
    return str(error)
