"""Profile editor (approved Linux design D), also used for a new profile.

Name, the 8 tokens.json colors, icon letter and a tray preview; composition (git
identity — empty = git is not switched —, GitHub account, Claude folder, Claude account
— empty = the folder's own login —) and one key per API provider present in the
configuration. Validation problems are shown next to the field they belong to.
"""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ...core import claude_paths  # noqa: E402
from ...core.editors import ConfigEditor, EditorError  # noqa: E402
from ...core.models import APIProvider, ClaudeAccountKind, Profile, ProfileColor, ProfileIcon  # noqa: E402
from .. import widgets  # noqa: E402

KIND_TEXT = {ClaudeAccountKind.LOGIN: "login completo", ClaudeAccountKind.TOKEN: "token",
             ClaudeAccountKind.FOLDER: "pasta"}
FOOTER = ("“Nenhuma” deixa aquele item como está ao trocar para este perfil (o Claude sempre usa a pasta do "
          "perfil). Chaves de API ficam no chaveiro do sistema (Secret Service); o perfil guarda só qual item usar.")


def _auto_letter(name: str) -> str:
    return name.strip()[:1].upper()


class ProfileEditorPage(Adw.NavigationPage):
    def __init__(self, window, profile_id):
        config = window.model.config
        existing = config.profile(profile_id) if config is not None and profile_id is not None else None
        super().__init__(title="Novo perfil" if existing is None else "Editar perfil")
        self.window, self.model = window, window.model
        self.is_new = existing is None
        if existing is not None:
            self.draft = copy.deepcopy(existing)
        else:
            count = len(config.profiles) if config else 0
            palette = list(ProfileColor)
            self.draft = Profile(name="", color=palette[count % len(palette)], icon=ProfileIcon("letter", "?"))
        self.errors = widgets.FieldErrors()
        self._auto = _auto_letter(self.draft.name)

        self.save_button = widgets.button("Salvar", self.save, "suggested-action")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.append(self._header())
        content.append(self._appearance())
        content.append(self._composition())
        content.append(self._keys())
        content.append(widgets.label(FOOTER, "dim-label", "caption", wrap=True))
        if not self.is_new:
            self.delete_button = widgets.button("Apagar perfil", self._confirm_delete, "flat", "destructive-action")
            self.delete_button.set_halign(Gtk.Align.CENTER)
            content.append(self.delete_button)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(self.save_button)
        view.add_top_bar(header)
        view.set_content(widgets.scrolled(content))
        self.set_child(view)
        self._update_preview()

    # MARK: sections

    def _header(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=16)
        self.big_avatar = widgets.avatar("?", self.draft.color, 64)
        box.append(self.big_avatar)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.title_label = widgets.label("", "title-1")
        texts.append(self.title_label)
        position = self._position()
        shortcut = f"Ctrl+{position + 1}" if position is not None and position < 9 else "Sem atalho (só os 9 primeiros)"
        texts.append(widgets.label(shortcut if not self.is_new else "Novo perfil", "dim-label"))
        box.append(texts)
        preview = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        preview.add_css_class("mc-preview")
        self.small_avatar = widgets.avatar("?", self.draft.color, 18)
        preview.append(self.small_avatar)
        preview.append(widgets.label("prévia no painel"))
        box.append(preview)
        return box

    def _appearance(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Aparência")
        name_row = Adw.ActionRow(title="Nome")
        self.name_entry = Gtk.Entry(text=self.draft.name, placeholder_text="Trabalho", width_chars=24,
                                    valign=Gtk.Align.CENTER)
        self.name_entry.connect("changed", self._name_changed)
        name_row.add_suffix(self.name_entry)
        name_row.set_activatable_widget(self.name_entry)
        self.errors.register("name", name_row)
        group.add(name_row)

        color_row = Adw.ActionRow(title="Cor")
        swatches, self.color_buttons = widgets.color_swatches(self.draft.color, self._color_changed)
        color_row.add_suffix(swatches)
        group.add(color_row)

        letter_row = Adw.ActionRow(title="Letra do ícone")
        letter = self.draft.icon.value if self.draft.icon and self.draft.icon.kind == "letter" else ""
        self.letter_entry = Gtk.Entry(text="" if letter == "?" else letter, max_length=1, width_chars=3,
                                      xalign=0.5, valign=Gtk.Align.CENTER)
        self.letter_entry.connect("changed", lambda *_: self._update_preview())
        letter_row.add_suffix(self.letter_entry)
        letter_row.set_activatable_widget(self.letter_entry)
        group.add(letter_row)
        return group

    def _composition(self) -> Gtk.Widget:
        config = self.model.config
        group = Adw.PreferencesGroup(title="Composição")
        self.git_ids = [None] + [i.id for i in config.git_identities]
        self.git_row = widgets.combo("Identidade git", ["Nenhuma"] + [f"{i.nickname} · {i.email}"
                                                                     for i in config.git_identities],
                                     self._index(self.git_ids, self.draft.git_identity_id),
                                     subtitle="vazio = não troca o git")
        self.errors.register("git", self.git_row)
        group.add(self.git_row)

        self.github_ids = [None] + [a.id for a in config.github_accounts]
        self.github_row = widgets.combo("Conta GitHub", ["Nenhuma"] + [f"{a.username} · {a.host}"
                                                                     for a in config.github_accounts],
                                        self._index(self.github_ids, self.draft.github_account_id),
                                        subtitle="vazio = não troca o GitHub")
        self.errors.register("github", self.github_row)
        group.add(self.github_row)

        self.folder_row = Adw.ActionRow(title="Pasta do Claude", subtitle="agents, skills e conversas")
        self.folder_label = widgets.label("", "monospace", "dim-label")
        self.folder_label.set_valign(Gtk.Align.CENTER)
        self.folder_row.add_suffix(self.folder_label)
        self.folder_row.add_suffix(widgets.button("Escolher…", self._choose_folder))
        self.errors.register("folder", self.folder_row)
        group.add(self.folder_row)
        self._show_folder()

        self.claude_ids = [None] + [a.id for a in config.claude_accounts]
        self.claude_row = widgets.combo("Conta Claude", ["Login da própria pasta"] + [
            f"{a.nickname} ({KIND_TEXT[a.kind]})" for a in config.claude_accounts],
            self._index(self.claude_ids, self.draft.claude_account_id), subtitle="vazio = login da própria pasta")
        self.errors.register("claude", self.claude_row)
        group.add(self.claude_row)
        return group

    def _keys(self) -> Gtk.Widget:
        config = self.model.config
        group = Adw.PreferencesGroup(title="Chaves de API")
        self.key_rows: dict[APIProvider, tuple[Adw.ComboRow, list]] = {}
        providers = [p for p in APIProvider if any(k.provider == p for k in config.api_keys)]
        for provider in providers:
            keys = [k for k in config.api_keys if k.provider == provider]
            ids = [None] + [k.id for k in keys]
            current = next((k for k in self.draft.api_key_ids if k in ids), None)
            variables = sorted({k.env_var for k in keys})
            row = Adw.ComboRow(title=f"{widgets.escape(provider.display_name)} <span alpha='55%' font_family='monospace'>"
                                     f"{widgets.escape(' · '.join(variables))}</span>",
                               model=Gtk.StringList.new(["Nenhuma"] + [f"{k.nickname} · {k.env_var}" for k in keys]))
            row.set_selected(self._index(ids, current))
            row.provider_name = provider.display_name
            group.add(row)
            self.key_rows[provider] = (row, ids)
        if not providers:
            group.add(Adw.ActionRow(title="Nenhuma chave cadastrada", subtitle="Adicione em Contas › Chaves."))
        self.keys_group = group
        self.keys_error = widgets.label("", "mc-field-error", wrap=True)
        self.keys_error.set_visible(False)
        group.set_header_suffix(self.keys_error)
        return group

    # MARK: helpers

    @staticmethod
    def _index(values: list, value) -> int:
        return values.index(value) if value in values else 0

    def _position(self) -> int | None:
        profiles = self.model.config.profiles if self.model.config else []
        return next((i for i, p in enumerate(profiles) if p.id == self.draft.id), None)

    def _letter(self) -> str:
        return self.letter_entry.get_text().strip()[:1].upper() or _auto_letter(self.name_entry.get_text()) or "?"

    def _update_preview(self) -> None:
        name = self.name_entry.get_text().strip()
        self.title_label.set_label(name or "Novo perfil")
        letter = self._letter()
        if self.draft.icon is not None and self.draft.icon.kind == "symbol" and not self.letter_entry.get_text():
            letter = name[:1].upper() or "?"
        widgets.set_avatar(self.big_avatar, letter, self.draft.color)
        widgets.set_avatar(self.small_avatar, letter, self.draft.color)

    def _name_changed(self, entry) -> None:
        new_auto = _auto_letter(entry.get_text())
        current = self.letter_entry.get_text().strip().upper()
        # The letter follows the name until the user picks another one.
        if current in ("", self._auto):
            self.letter_entry.set_text(new_auto)
        self._auto = new_auto
        self._update_preview()

    def _color_changed(self, color: ProfileColor) -> None:
        self.draft.color = color
        self._update_preview()

    def _show_folder(self) -> None:
        self.folder_label.set_label(self.model.folder_name(self.draft.claude_config_dir)
                                    + (" (padrão)" if self.draft.claude_config_dir is None else ""))

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Pasta do Claude", modal=True)
        start = self.draft.claude_config_dir or claude_paths.default_config_dir(self.model.environment)
        dialog.set_initial_folder(Gio.File.new_for_path(start))
        dialog.select_folder(self.window, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        if folder is None or folder.get_path() is None:
            return
        self.set_folder(folder.get_path())

    def set_folder(self, path: str) -> bool:
        """Canonical form only: Claude Code treats /x and /x/ as different folders."""
        self.errors.clear()
        try:
            self.draft.claude_config_dir = claude_paths.canonical_config_dir(path, self.model.environment)
        except claude_paths.PathError:
            self.errors.show("folder", "Pasta inválida: use um caminho absoluto.")
            return False
        self._show_folder()
        return True

    def key_providers(self) -> list[str]:
        return [row.provider_name for row, _ids in self.key_rows.values()]

    def error_text(self, field: str) -> str | None:
        if field == "keys":
            return self.keys_error.get_label() if self.keys_error.get_visible() else None
        return self.errors.text(field)

    # MARK: save and delete

    def _collect(self) -> Profile:
        profile = copy.deepcopy(self.draft)
        profile.name = self.name_entry.get_text().strip(" ")
        letter = self.letter_entry.get_text().strip()[:1].upper()
        if letter or profile.icon is None or profile.icon.kind == "letter":
            profile.icon = ProfileIcon("letter", letter or _auto_letter(profile.name) or "?")
        profile.git_identity_id = self.git_ids[self.git_row.get_selected()]
        profile.github_account_id = self.github_ids[self.github_row.get_selected()]
        profile.claude_account_id = self.claude_ids[self.claude_row.get_selected()]
        for provider, (row, ids) in self.key_rows.items():
            profile = ConfigEditor.set_api_key(ids[row.get_selected()], provider, profile, self.model.config)
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
        self.window.nav.pop_to_tag("main")
        self.window.show_toast(f"Perfil {profile.name} salvo")
        # The active profile changed: apply it again so the machine matches (like the Mac).
        if self.model.state.profile_id == profile.id and not self.model.switching:
            self.window.on_profile(None, profile.id)
        return True

    def _confirm_delete(self) -> None:
        name = self.name_entry.get_text().strip() or self.draft.name
        widgets.confirm(self, f"Apagar o perfil {name}?",
                        "As contas continuam cadastradas. Se ele estiver ativo, o sistema fica como está até "
                        "você escolher outro perfil.", "Apagar perfil", self.delete)

    def delete(self) -> None:
        try:
            self.model.edit(lambda config: ConfigEditor.remove_profile(self.draft.id, config))
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return
        self.window.nav.pop_to_tag("main")
        self.window.show_toast(f"Perfil {self.draft.name} apagado")
