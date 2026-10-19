"""Profile editor, laid out like the Mac's (`Views/ProfileEditorPage.swift`); also used for a
new profile.

Header (:64-87): the 64 px badge, the name, "No painel:" + "Ctrl+N no menu" (first nine
profiles) and a preview of the panel icon with the time. Form (:22-45): Nome, Cor and
Ícone (the letter or one of the 8 symbols of Components.swift:65, drawn by
`tray.icon` exactly as the panel shows them); "Composição" with Git, GitHub, the Claude
folder as a list of the known folders with their conversation counts + "Nova pasta…"
(:123-146), the Claude account with a color dot per account (:147-159), the legend, the
"A pasta X não tem login." warning + "Entrar…" (:162-169), and one row per API provider
— OpenAI and Gemini always (:215-220). Footer (:89-120): "Apagar perfil", "Ativar este
perfil", "Cancelar", "Salvar". Saving stays in the editor (a new profile becomes that
profile's editor); deleting goes to the first profile, or to Claude without profiles.
Validation problems are shown next to the field they belong to.
"""

from __future__ import annotations

import copy
import unicodedata
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GObject, Gtk, Pango  # noqa: E402

from ...core.claude import folder_inspector  # noqa: E402
from ...core.claude.account_editor import ClaudeAccountError, ClaudeFolderChoice, prepare_folder  # noqa: E402
from ...core.editors import ConfigEditor, EditorError  # noqa: E402
from ...core.models import APIProvider, Profile, ProfileColor, ProfileIcon  # noqa: E402
from ...tray import icon as tray_icon  # noqa: E402
from .. import widgets  # noqa: E402

FOOTER = ("“Nenhuma” deixa aquele item como está ao trocar para este perfil (o Claude sempre usa a pasta do "
          "perfil). Chaves de API ficam no chaveiro do sistema (Secret Service); o perfil guarda só qual item usar.")
LEGEND = "A pasta guarda agents, skills e conversas. A conta é só o login: trocar a conta não muda a pasta."
PREVIEW_LABEL = "Prévia no painel"
NEW_FOLDER = object()  # the "Nova pasta…" entry of the folder list
# Accessible names of the icon choices (the Mac reads the SF Symbol names).
SYMBOL_NAMES = {"briefcase": "Maleta", "house": "Casa", "building.2": "Prédios", "laptopcomputer": "Notebook",
                "star": "Estrela", "person": "Pessoa", "flask": "Frasco", "globe": "Globo"}
# ProfileBadge proportions (Components.swift:12-27): corner 0.24, letter 0.55, symbol 0.48.
BADGE = {"inset": 0, "radius": 0.24, "letter_scale": 0.55, "symbol_scale": 0.62}

_CSS = """
.mc-editor-header { padding: 20px 4px 12px 4px; }
.mc-editor-preview { background: alpha(currentColor, 0.06); border-radius: 8px; padding: 6px 10px; }
.mc-editor-footer { padding: 10px 20px; }
.mc-option-dot { min-width: 10px; min-height: 10px; border-radius: 999px; }
button.mc-icon-choice, button.mc-icon-choice:hover, button.mc-icon-choice:checked,
button.mc-icon-choice:active { padding: 0; min-width: 30px; min-height: 30px; border-radius: 7px;
                               background: none; box-shadow: none; }
.mc-editor-legend { font-size: smaller; padding: 8px 12px; }
.mc-folder-warning-row { padding: 6px 12px; }
.mc-folder-warning { color: @orange_4; font-size: smaller; }
"""
_css_loaded = False


def _load_css() -> None:
    global _css_loaded
    display = Gdk.Display.get_default()
    if _css_loaded or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    _css_loaded = True


def _trimmed(name: str) -> str:
    """Swift `trimmingCharacters(in: .whitespaces)`: spaces and tabs, not line breaks (the
    validator reports those)."""
    return name.strip(" \t")


def _name_letter(name: str) -> str:
    return _trimmed(name)[:1].upper()


def _slug(name: str) -> str:
    """ProfileEditorPage.swift:208-213: folded, lower case, other characters → "-"."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    mapped = "".join(ch if ch.isalnum() else "-" for ch in folded)
    return "-".join(part for part in mapped.split("-") if part) or "novo"


def _rgb(color: Gdk.RGBA) -> tuple[float, float, float]:
    return color.red, color.green, color.blue


class _Option(GObject.Object):
    """One entry of a picker: its text and the dot color (a ProfileColor value, or None)."""

    text = GObject.Property(type=str, default="")

    def __init__(self, text: str, color: str | None):
        super().__init__(text=text)
        self.color = color


class _Picker:
    """A combo row whose entries carry a color dot, like the Mac's `Label { Text } icon:
    { circle.fill }` options (ProfileEditorPage.swift:229-243). `values` holds what each
    entry stands for (an id, a folder, None)."""

    def __init__(self, title: str, icon_name: str, on_change=None):
        self.store = Gio.ListStore(item_type=_Option)
        self.row = Adw.ComboRow(title=widgets.escape(title), model=self.store)
        self.row.add_prefix(Gtk.Image(icon_name=icon_name))
        self.row.set_expression(Gtk.PropertyExpression.new(_Option, None, "text"))
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        self.row.set_factory(factory)
        self.values: list = []
        self._updating = False
        self._on_change = on_change
        self.row.connect("notify::selected", self._changed)

    @staticmethod
    def _setup(_factory, item) -> None:
        box = Gtk.Box(spacing=8)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("mc-option-dot")
        box.append(dot)
        box.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=40))
        item.set_child(box)

    @staticmethod
    def _bind(_factory, item) -> None:
        option, box = item.get_item(), item.get_child()
        dot = box.get_first_child()
        dot.get_next_sibling().set_label(option.text)
        for color in ProfileColor:
            dot.remove_css_class(f"mc-color-{color.value}")
        if option.color is not None:
            dot.add_css_class(f"mc-color-{option.color}")
        dot.set_visible(option.color is not None)

    def _changed(self, *_args) -> None:
        if not self._updating and self._on_change is not None:
            self._on_change(self.value)

    def set_options(self, options: list[tuple], selected) -> None:
        """options: (value, text, color value or None). Keeps `selected` chosen (else the first)."""
        self._updating = True
        try:
            self.values = [value for value, _text, _color in options]
            self.store.splice(0, self.store.get_n_items(), [_Option(text, color) for _v, text, color in options])
            self.select(selected)
        finally:
            self._updating = False

    def select(self, value) -> None:
        index = next((i for i, v in enumerate(self.values) if v is value or v == value), 0)
        self.row.set_selected(index)

    @property
    def value(self):
        index = self.row.get_selected()
        return self.values[index] if 0 <= index < len(self.values) else None

    def options(self) -> list[tuple[str, str | None]]:
        return [(self.store.get_item(i).text, self.store.get_item(i).color) for i in range(self.store.get_n_items())]


class ProfileEditorPage(Adw.NavigationPage):
    def __init__(self, window, profile_id):
        _load_css()
        self.window, self.model = window, window.model
        config = self.model.config
        existing = config.profile(profile_id) if config is not None and profile_id is not None else None
        self.profile_id = existing.id if existing is not None else None
        super().__init__(title="Novo perfil" if existing is None else "Editar perfil")
        self._load()
        self._build()
        self._load_missing_summaries()

    @property
    def is_new(self) -> bool:
        return self.profile_id is None

    def _load(self) -> None:
        """ProfileEditorPage.swift:247-256: the saved profile, or a new one in the next color."""
        config = self.model.config
        existing = config.profile(self.profile_id) if config is not None and self.profile_id is not None else None
        if existing is not None:
            self.draft = copy.deepcopy(existing)
        else:
            palette = list(ProfileColor)
            count = len(config.profiles) if config else 0
            self.draft = Profile(name="", color=palette[count % len(palette)], icon=ProfileIcon("letter", "?"))
        self.discovered = folder_inspector.discover_folders(self.model.environment)

    # MARK: building

    def _build(self) -> None:
        self.errors = widgets.FieldErrors()
        self._updating = False
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.append(self._form())
        content.append(self._composition())
        self.note_label = widgets.label(FOOTER, "dim-label", "caption", wrap=True)
        self.note_label.set_margin_start(12)
        self.note_label.set_margin_end(12)
        content.append(self.note_label)
        self.scroller = widgets.scrolled(content)
        self.scroller.get_child().get_child().set_margin_top(4)  # the clamp: the header is right above

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.Clamp(maximum_size=720, tightening_threshold=560, child=self._header())
        header.set_margin_start(16)
        header.set_margin_end(16)
        body.append(header)
        body.append(self.scroller)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(body)
        view.add_bottom_bar(self._footer())
        self.set_child(view)
        self.refresh()

    def _header(self) -> Gtk.Widget:
        """ProfileEditorPage.swift:64-87 ("Na barra de menus:" → "No painel:", ⌘ → Ctrl)."""
        box = Gtk.Box(spacing=16)
        box.add_css_class("mc-editor-header")
        self.big_badge = self._badge(64)
        box.append(self.big_badge)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        self.title_label = widgets.label("", "title-2")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(self.title_label)
        line = Gtk.Box(spacing=6)
        self.panel_label = widgets.label("No painel:", "dim-label")
        self.shortcut_label = widgets.label("", "dim-label", "caption")
        line.append(self.panel_label)
        line.append(self.shortcut_label)
        texts.append(line)
        box.append(texts)
        self.preview = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.preview.add_css_class("mc-editor-preview")
        self.small_badge = self._badge(18)
        self.preview.append(self.small_badge)
        self.time_label = widgets.label("", "numeric")
        self.preview.append(self.time_label)
        self.preview.update_property([Gtk.AccessibleProperty.LABEL], [PREVIEW_LABEL])
        box.append(self.preview)
        return box

    def _form(self) -> Gtk.Widget:
        """ProfileEditorPage.swift:23-29: Nome, Cor, Ícone."""
        self.form_group = Adw.PreferencesGroup()
        name_row = Adw.ActionRow(title="Nome")
        self.name_entry = Gtk.Entry(text=self.draft.name, placeholder_text="Trabalho", width_chars=24,
                                    valign=Gtk.Align.CENTER)
        self.name_entry.connect("changed", self._name_changed)
        self.name_entry.connect("activate", lambda *_: self.save_button.get_sensitive() and self.save())
        name_row.add_suffix(self.name_entry)
        name_row.set_activatable_widget(self.name_entry)
        self.errors.register("name", name_row)
        self.form_group.add(name_row)

        color_row = Adw.ActionRow(title="Cor")
        swatches, self.color_buttons = widgets.color_swatches(self.draft.color, self._color_changed)
        color_row.add_suffix(swatches)
        self.form_group.add(color_row)

        icon_row = Adw.ActionRow(title="Ícone")
        choices = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.icon_buttons: dict[str, Gtk.ToggleButton] = {}
        group = None
        for choice in ("letter",) + tray_icon.SYMBOLS:
            button = Gtk.ToggleButton(valign=Gtk.Align.CENTER)
            button.add_css_class("mc-icon-choice")
            button.update_property([Gtk.AccessibleProperty.LABEL], [SYMBOL_NAMES.get(choice, "Letra")])
            area = Gtk.DrawingArea(content_width=30, content_height=30)
            area.set_draw_func(self._draw_choice, choice)
            button.set_child(area)
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.set_active(choice == self._icon_choice())
            button.connect("toggled", self._icon_toggled, choice)
            self.icon_buttons[choice] = button
            choices.append(button)
        icon_row.add_suffix(choices)
        self.form_group.add(icon_row)
        self.form_rows = [name_row, color_row, icon_row]
        return self.form_group

    def _composition(self) -> Gtk.Widget:
        """ProfileEditorPage.swift:30-40 and 122-170."""
        group = Adw.PreferencesGroup(title="Composição")
        self.composition_group = group
        self.git_picker = _Picker("Git", "multicred-branch-symbolic")
        self.github_picker = _Picker("GitHub", "multicred-code-symbolic")
        self.folder_picker = _Picker("Pasta do Claude", "folder-symbolic", self._folder_chosen)
        self.claude_picker = _Picker("Conta Claude", "multicred-sparkle-symbolic", lambda _value: self._update_warning())
        self.git_row, self.github_row, self.claude_row = self.git_picker.row, self.github_picker.row, self.claude_picker.row
        for field, picker in (("git", self.git_picker), ("github", self.github_picker)):
            self.errors.register(field, picker.row)
            group.add(picker.row)
        self.errors.register("folder", self.folder_picker.row)
        group.add(self.folder_picker.row)

        self.new_folder_row = Adw.EntryRow(title="Caminho", visible=False)
        self.new_folder_row.add_css_class("monospace")
        self.errors.register("new_folder", self.new_folder_row)
        self.create_button = widgets.button("Criar", self.create_folder)
        self.cancel_folder_button = widgets.button("Cancelar", self.cancel_new_folder)
        self.new_folder_row.add_suffix(self.create_button)
        self.new_folder_row.add_suffix(self.cancel_folder_button)
        self.new_folder_row.connect("entry-activated", lambda *_: self.create_folder())
        group.add(self.new_folder_row)

        self.errors.register("claude", self.claude_picker.row)
        group.add(self.claude_picker.row)

        legend = Adw.PreferencesRow(activatable=False, focusable=False)
        self.legend_label = widgets.label(LEGEND, "dim-label", "mc-editor-legend", wrap=True)
        legend.set_child(self.legend_label)
        group.add(legend)

        self.warning_row = Adw.PreferencesRow(activatable=False, visible=False)
        warning = Gtk.Box(spacing=8)
        warning.add_css_class("mc-folder-warning-row")
        icon = Gtk.Image(icon_name="dialog-warning-symbolic")
        icon.add_css_class("mc-folder-warning")
        warning.append(icon)
        self.warning_label = widgets.label("", "mc-folder-warning", wrap=True)
        self.warning_label.set_hexpand(True)
        warning.append(self.warning_label)
        self.folder_login_button = widgets.button("Entrar…", self._folder_login)
        self.folder_login_button.add_css_class("caption")
        warning.append(self.folder_login_button)
        self.warning_row.set_child(warning)
        group.add(self.warning_row)

        self.key_pickers: dict[APIProvider, _Picker] = {}
        for provider in self._providers():
            picker = _Picker(provider.display_name, "dialog-password-symbolic")
            self.key_pickers[provider] = picker
            group.add(picker.row)
        self.keys_error = widgets.label("", "mc-field-error", wrap=True)
        self.keys_error.set_visible(False)
        group.set_header_suffix(self.keys_error)
        self.composition_rows = ([self.git_row, self.github_row, self.folder_picker.row, self.claude_row]
                                 + [picker.row for picker in self.key_pickers.values()])
        self._fill_pickers(initial=True)
        return group

    def _footer(self) -> Gtk.Widget:
        """ProfileEditorPage.swift:89-120."""
        box = Gtk.Box(spacing=8)
        box.add_css_class("mc-editor-footer")
        self.delete_button = widgets.button("Apagar perfil", self._confirm_delete)
        self.delete_button.add_css_class("error")
        self.activate_button = widgets.button("Ativar este perfil", self.activate_profile)
        self.cancel_button = widgets.button("Cancelar", self.cancel)
        self.save_button = widgets.button("Salvar", self.save, "suggested-action")
        box.append(self.delete_button)
        box.append(Gtk.Box(hexpand=True))
        for button in (self.activate_button, self.cancel_button, self.save_button):
            box.append(button)
        self.footer_buttons = [self.delete_button, self.activate_button, self.cancel_button, self.save_button]
        return box

    # MARK: badges and icon choices (drawn by tray.icon, as the panel shows them)

    def _badge(self, size: int) -> Gtk.DrawingArea:
        area = Gtk.DrawingArea(content_width=size, content_height=size, valign=Gtk.Align.CENTER)
        area.set_draw_func(self._draw_badge, size)
        return area

    def _draw_badge(self, _area, context, width, height, size) -> None:
        tray_icon.draw_badge(context, self.draft.color.hex, self.badge_glyph(), min(width, height), **BADGE)

    def _draw_choice(self, area, context, width, height, choice) -> None:
        selected = choice == self._icon_choice()
        glyph = self._letter() if choice == "letter" else choice
        foreground = _rgb(area.get_color())
        if selected:
            context.set_source_rgb(*tray_icon.hex_rgb(self.draft.color.hex))
        else:
            context.set_source_rgba(*foreground, 0.06)
        tray_icon.rounded_rect(context, 0, 0, width, height, 7)
        context.fill()
        tray_icon.draw_glyph(context, glyph, 0, 0, min(width, height), (1, 1, 1) if selected else foreground,
                             letter_scale=0.45, symbol_scale=0.6)

    def _icon_choice(self) -> str:
        icon = self.draft.icon
        return icon.value if icon is not None and icon.kind == "symbol" and icon.value in tray_icon.SYMBOLS \
            else "letter"

    def _letter(self) -> str:
        return _name_letter(self.name_entry.get_text()) or "?"

    def badge_glyph(self) -> str:
        """What the header, the preview and the panel show: the symbol, or the name's letter."""
        choice = self._icon_choice()
        return self._letter() if choice == "letter" else choice

    def _redraw_icons(self) -> None:
        self.big_badge.queue_draw()
        self.small_badge.queue_draw()
        for button in self.icon_buttons.values():
            button.get_child().queue_draw()

    def _icon_toggled(self, button, choice) -> None:
        if not button.get_active():
            return
        self.draft.icon = ProfileIcon("letter", self._letter()) if choice == "letter" else ProfileIcon("symbol", choice)
        self._redraw_icons()

    def select_icon(self, choice: str) -> None:
        """"letter" or one of tray.icon.SYMBOLS."""
        self.icon_buttons[choice].set_active(True)

    def selected_icon(self) -> str:
        return self._icon_choice()

    def icon_choices(self) -> list[str]:
        return [self._letter()] + list(tray_icon.SYMBOLS)

    # MARK: pickers

    def _providers(self) -> list[APIProvider]:
        """Providers with at least one key, plus OpenAI and Gemini (always, as on the Mac)."""
        keys = self.model.config.api_keys if self.model.config else []
        return [p for p in APIProvider
                if p in (APIProvider.OPENAI, APIProvider.GEMINI) or any(k.provider == p for k in keys)]

    def _folder_options(self) -> list[str | None]:
        """ProfileEditorPage.swift:186-193: the default folder, the discovered ones, the
        profiles' folders and the draft's, without repeats."""
        from ...core import claude_paths

        default = claude_paths.default_config_dir(self.model.environment)
        options: list[str | None] = [None]
        profiles = self.model.config.profiles if self.model.config else []
        for folder in self.discovered + [p.claude_config_dir for p in profiles] + [self.draft.claude_config_dir]:
            if folder not in options and folder != default:
                options.append(folder)
        return options

    def _folder_label(self, folder: str | None) -> str:
        label = self.model.folder_name(folder) + (" (padrão)" if folder is None else "")
        summary = self.model.folder_summaries.get(folder)
        if summary is not None:
            label += f" · {summary.conversation_count} conversas"
        return label

    def _own_login_label(self) -> str:
        status = self.model.folder_status(self.draft.claude_config_dir)
        if status is not None and status.kind == "loggedIn" and status.email:
            return f"Login da própria pasta · {status.email}"
        return "Login da própria pasta"

    def _fill_pickers(self, initial: bool = False) -> None:
        """(Re)fills every picker from the configuration, keeping what is chosen."""
        config = self.model.config
        if config is None:
            return
        draft = self.draft
        git = draft.git_identity_id if initial else self.git_picker.value
        github = draft.github_account_id if initial else self.github_picker.value
        claude = draft.claude_account_id if initial else self.claude_picker.value
        self.git_picker.set_options([(None, "Nenhuma", None)] + [
            (i.id, f"{i.nickname} · {i.email}", i.color.value) for i in config.git_identities], git)
        self.github_picker.set_options([(None, "Nenhuma", None)] + [
            (a.id, f"{a.username} · {a.host}", a.color.value) for a in config.github_accounts], github)
        self._fill_folders()
        self.claude_picker.set_options([(None, self._own_login_label(), None)] + [
            (a.id, a.nickname + (f" · {a.email}" if a.email else ""), a.color.value)
            for a in config.claude_accounts], claude)
        for provider, picker in self.key_pickers.items():
            keys = [k for k in config.api_keys if k.provider == provider]
            ids = [k.id for k in keys]
            current = next((k for k in draft.api_key_ids if k in ids), None) if initial else picker.value
            picker.set_options([(None, "Nenhuma", None)] + [
                (k.id, f"{k.nickname} · {k.env_var}", k.color.value) for k in keys], current)

    def _fill_folders(self) -> None:
        options = self._folder_options()
        self.folder_picker.set_options([(f, self._folder_label(f), None) for f in options]
                                       + [(NEW_FOLDER, "Nova pasta…", None)], self.draft.claude_config_dir)

    def _folder_chosen(self, value) -> None:
        if value is NEW_FOLDER:
            # The list keeps showing the current folder while the path is typed (:174-184).
            self.folder_picker._updating = True
            try:
                self.folder_picker.select(self.draft.claude_config_dir)
            finally:
                self.folder_picker._updating = False
            self.new_folder_row.set_text(f"~/.claude-{_slug(self.name_entry.get_text())}")
            self.new_folder_row.set_visible(True)
            self.new_folder_row.grab_focus()
            return
        self.draft.claude_config_dir = value
        self._update_claude_account()

    def _update_claude_account(self) -> None:
        """The own-login entry names the folder's e-mail; the warning depends on the folder."""
        config = self.model.config
        if config is not None:
            self.claude_picker.set_options([(None, self._own_login_label(), None)] + [
                (a.id, a.nickname + (f" · {a.email}" if a.email else ""), a.color.value)
                for a in config.claude_accounts], self.claude_picker.value)
        self._update_warning()

    def _update_warning(self) -> None:
        """ProfileEditorPage.swift:162-169."""
        status = self.model.folder_status(self.draft.claude_config_dir)
        logged_out = self.claude_picker.value is None and status is not None and status.kind == "loggedOut"
        self.warning_label.set_label(f"A pasta {self.model.folder_name(self.draft.claude_config_dir)} não tem login.")
        self.warning_row.set_visible(logged_out)

    def _folder_login(self) -> None:
        self.window.on_login_requested("folder", self.draft.claude_config_dir)

    def create_folder(self) -> bool:
        """"Criar": creates the folder (0700) and chooses it (:137-143)."""
        self.errors.clear()
        try:
            folder = prepare_folder(ClaudeFolderChoice.new(self.new_folder_row.get_text()), self.model.environment)
        except ClaudeAccountError as error:
            self.errors.show("new_folder", widgets.describe_error(error))
            return False
        self.draft.claude_config_dir = folder
        self.new_folder_row.set_visible(False)
        self.discovered = folder_inspector.discover_folders(self.model.environment)
        if folder not in self.model.folder_summaries:
            # A folder just created is empty: counting it is instant.
            self.model.folder_summaries[folder] = folder_inspector.summarize(folder, self.model.environment)
        self._fill_folders()
        self._update_claude_account()
        return True

    def cancel_new_folder(self) -> None:
        self.errors.clear()
        self.new_folder_row.set_visible(False)

    def _load_missing_summaries(self) -> None:
        """Conversation counts of the discovered folders nobody uses yet (off the UI thread;
        the model already counts the folders in use)."""
        missing = [f for f in self._folder_options() if f not in self.model.folder_summaries]
        if not missing:
            return
        environment = self.model.environment

        def finished(summaries, _error):
            if summaries:
                self.model.folder_summaries.update(summaries)
                self._fill_folders()

        self.model._background("claude-folders-editor",
                               lambda: {f: folder_inspector.summarize(f, environment) for f in missing}, finished)

    # MARK: state

    def _position(self) -> int | None:
        profiles = self.model.config.profiles if self.model.config else []
        return next((i for i, p in enumerate(profiles) if p.id == self.profile_id), None)

    def refresh(self) -> None:
        """Model changed (a switch started or ended, statuses arrived, accounts edited)."""
        config = self.model.config
        if config is None or (not self.is_new and config.profile(self.profile_id) is None):
            return  # deleted: the window leaves this page
        position = self._position()
        self.shortcut_label.set_label(f"Ctrl+{position + 1} no menu" if position is not None and position < 9 else "")
        self.shortcut_label.set_visible(position is not None and position < 9)
        self.time_label.set_label(datetime.now().strftime("%H:%M"))
        self.delete_button.set_visible(not self.is_new)
        self.activate_button.set_visible(not self.is_new and self.model.state.profile_id != self.profile_id)
        self.activate_button.set_sensitive(not self.model.switching)
        self._fill_pickers()
        self._update_warning()
        self._update_title()

    def _update_title(self) -> None:
        name = _trimmed(self.name_entry.get_text())
        self.title_label.set_label(self.name_entry.get_text() if name else "Novo perfil")
        self.save_button.set_sensitive(bool(name))

    def _name_changed(self, _entry) -> None:
        if self._icon_choice() == "letter":
            self.draft.icon = ProfileIcon("letter", self._letter())
        self._update_title()
        self._redraw_icons()

    def _color_changed(self, color: ProfileColor) -> None:
        self.draft.color = color
        self._redraw_icons()

    # MARK: what tests and the window read

    def header_texts(self) -> dict:
        return {"title": self.title_label.get_label(), "panel": self.panel_label.get_label(),
                "shortcut": self.shortcut_label.get_label() if self.shortcut_label.get_visible() else None,
                "preview_time": self.time_label.get_label(), "preview_label": PREVIEW_LABEL}

    def footer_labels(self) -> list[str]:
        return [b.get_label() for b in self.footer_buttons if b.get_visible()]

    def save_in_header_bar(self) -> bool:
        return self.save_button.get_ancestor(Adw.HeaderBar) is not None

    def group_titles(self) -> list[str]:
        return [self.form_group.get_title(), self.composition_group.get_title()]

    def form_titles(self) -> list[str]:
        return [row.get_title() for row in self.form_rows]

    def composition_titles(self) -> list[str]:
        return [row.get_title() for row in self.composition_rows]

    def composition_subtitles(self) -> list[str]:
        return [row.get_subtitle() for row in self.composition_rows if row.get_subtitle()]

    def _picker(self, name: str) -> _Picker:
        pickers = {"git": self.git_picker, "github": self.github_picker, "folder": self.folder_picker,
                   "claude": self.claude_picker}
        return pickers.get(name) or self.key_pickers[APIProvider(name)]

    def options(self, name: str) -> list[tuple[str, str | None]]:
        """(text, color) of a picker: git, github, folder, claude or a provider value."""
        return self._picker(name).options()

    def selected_option(self, name: str) -> int:
        return self._picker(name).row.get_selected()

    def select_option(self, name: str, index: int) -> None:
        self._picker(name).row.set_selected(index)

    def folder_warning(self) -> tuple[str, str] | None:
        if not self.warning_row.get_visible():
            return None
        return self.warning_label.get_label(), self.folder_login_button.get_label()

    def new_folder_buttons(self) -> list[str]:
        return [self.create_button.get_label(), self.cancel_folder_button.get_label()]

    def claude_legend(self) -> str:
        return self.legend_label.get_label()

    def footer_note(self) -> str:
        return self.note_label.get_label()

    def key_providers(self) -> list[str]:
        return [provider.display_name for provider in self.key_pickers]

    def error_text(self, field: str) -> str | None:
        if field == "keys":
            return self.keys_error.get_label() if self.keys_error.get_visible() else None
        return self.errors.text(field)

    # MARK: save, activate, cancel, delete

    def _collect(self) -> Profile:
        profile = copy.deepcopy(self.draft)
        profile.name = _trimmed(self.name_entry.get_text())
        if profile.icon is None or profile.icon.kind == "letter" or profile.icon.value not in tray_icon.SYMBOLS:
            # The letter comes from the name (ProfileEditorPage.swift:108).
            profile.icon = ProfileIcon("letter", _name_letter(profile.name) or "?")
        profile.git_identity_id = self.git_picker.value
        profile.github_account_id = self.github_picker.value
        profile.claude_account_id = self.claude_picker.value
        for provider, picker in self.key_pickers.items():
            profile = ConfigEditor.set_api_key(picker.value, provider, profile, self.model.config)
        return profile

    def _show_issues(self, issues, profile) -> None:
        unplaced = []
        for issue in issues:
            message = widgets.issue_message(issue, is_profile=issue.owner == profile.id)
            if issue.owner == profile.id and issue.kind in ("emptyField", "controlCharacters"):
                placed = self.errors.show("name", message)
            elif issue.kind == "invalidClaudeDirectory":
                placed = self.errors.show("folder", message)
            elif issue.kind == "missingReference" and issue.detail in ("git", "github", "claude"):
                placed = self.errors.show(issue.detail, message)
            elif issue.kind in ("duplicateEnvVarInProfile",) or (issue.kind == "missingReference"
                                                                  and issue.detail == "apiKey"):
                self.keys_error.set_label(message)
                self.keys_error.set_visible(True)
                placed = True
            else:
                placed = False
            if not placed:
                unplaced.append(issue)
        if unplaced:
            self.window.show_toast(widgets.describe_issues(unplaced))

    def save(self) -> bool:
        """"Salvar" (:105-116): stays in the editor; a new profile becomes its editor; the
        active profile is applied again so the machine matches."""
        self.errors.clear()
        self.keys_error.set_visible(False)
        profile = self._collect()
        try:
            self.model.edit(lambda config: ConfigEditor.upsert(profile, config))
        except EditorError as error:
            if error.kind == "invalid":
                self._show_issues(error.issues, profile)
            else:
                self.window.show_toast(widgets.describe_error(error))
            return False
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return False
        if self.is_new:
            self.window.show_profile_editor(profile.id)
        else:
            self.draft = copy.deepcopy(self.model.config.profile(profile.id))
            self.refresh()
        self.window.show_toast(f"Perfil {profile.name} salvo")
        if self.model.state.profile_id == profile.id and not self.model.switching:
            self.window.on_profile(None, profile.id)
        return True

    def activate_profile(self) -> None:
        """"Ativar este perfil" (:95-100)."""
        if not self.is_new and not self.model.switching:
            self.window.on_profile(None, self.profile_id)

    def cancel(self) -> None:
        """"Cancelar" (:101-103): a new profile goes to the first profile (or Claude); an
        existing one is loaded again."""
        if self.is_new:
            self._show_first_profile()
        else:
            self._load()
            self._build()

    def _show_first_profile(self) -> None:
        profiles = self.model.config.profiles if self.model.config else []
        if profiles:
            self.window.show_profile_editor(profiles[0].id)
        else:
            self.window.show_section("claude")

    def _confirm_delete(self) -> None:
        name = _trimmed(self.name_entry.get_text()) or self.draft.name
        widgets.confirm(self, f"Apagar o perfil {name}?",
                        "As contas continuam cadastradas. Se ele estiver ativo, o sistema fica como está até "
                        "você escolher outro perfil.", "Apagar perfil", self.delete)

    def delete(self) -> None:
        """ProfileEditorPage.swift:53-58: then the first profile, or Claude."""
        try:
            self.model.edit(lambda config: ConfigEditor.remove_profile(self.draft.id, config))
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return
        self._show_first_profile()
        self.window.show_toast(f"Perfil {self.draft.name} apagado")
