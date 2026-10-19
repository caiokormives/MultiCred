"""Contas (approved Linux design E): Git, GitHub, Claude and Chaves tabs with counts.

Each tab lists its accounts (avatar, name, subtitle, status, menu with Editar/Remover)
and "+ Adicionar conta" adds to the visible tab. Editors are pages pushed on the same
navigation stack. API key values are typed into a password row and go only to the
secret store (`AppModel.add_api_key` / `replace_secret`); they are never kept here.
The Claude login itself (pseudo-terminal) belongs to S-07, which plugs into
`MainWindow.on_login_requested`.
"""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ...core.editors import ConfigEditor, EditorError  # noqa: E402
from ...core.models import (APIProvider, ClaudeAccountKind, GitHubAccount, GitIdentity,  # noqa: E402
                            ProfileColor)
from .. import widgets  # noqa: E402
from ..widgets import escape  # noqa: E402

TABS = (("git", "Git"), ("github", "GitHub"), ("claude", "Claude"), ("keys", "Chaves"))
CLAUDE_NOTE = ("A conta é só a credencial: trocar a conta não muda agents, skills nem conversas, que ficam na "
               "pasta do perfil. O MultiCred nunca mostra tokens; logins completos ficam no chaveiro do sistema "
               "(Secret Service).")
GIT_NOTE = "O MultiCred escreve só o arquivo incluído no fim do seu gitconfig global. O seu ~/.gitconfig continua seu."
GITHUB_NOTE = "O login e o token ficam com o próprio gh. O MultiCred só escolhe a conta ativa (gh auth switch)."
KEYS_NOTE = ("O valor de cada chave fica só no chaveiro do sistema (Secret Service). O MultiCred guarda apenas o nome "
             "da variável e qual item usar; o terminal lê o valor quando o perfil muda. As chaves não são passadas "
             "para apps gráficos.")


def _used_in(profiles) -> str:
    return ", ".join(p.name for p in profiles)


def _next_color(count: int) -> ProfileColor:
    palette = list(ProfileColor)
    return palette[count % len(palette)]


class AccountsPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Contas", tag="accounts")
        self.window, self.model = window, window.model
        self.current_tab = "git"
        self._rows: dict[str, list[Adw.ActionRow]] = {name: [] for name, _ in TABS}
        self._folder_titles: list[str] = []
        self._github_checked = False

        self.add_button = widgets.button("+ Adicionar conta", self.add_account, "suggested-action")
        segmented = Gtk.Box(halign=Gtk.Align.START)
        segmented.add_css_class("mc-segmented")
        self.tab_buttons: dict[str, Gtk.ToggleButton] = {}
        self.count_labels: dict[str, Gtk.Label] = {}
        group = None
        for name, title in TABS:
            box = Gtk.Box(spacing=6)
            box.append(widgets.label(title))
            count = widgets.label("", "mc-count")
            box.append(count)
            toggle = Gtk.ToggleButton(child=box)
            if group is None:
                group = toggle
            else:
                toggle.set_group(group)
            toggle.connect("toggled", self._tab_toggled, name)
            segmented.append(toggle)
            self.tab_buttons[name], self.count_labels[name] = toggle, count
        self.stack = Gtk.Stack()
        self.tab_boxes: dict[str, Gtk.Box] = {}
        for name, _title in TABS:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            self.tab_boxes[name] = box
            self.stack.add_named(box, name)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.append(segmented)
        content.append(self.stack)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(self.add_button)
        view.add_top_bar(header)
        view.set_content(widgets.scrolled(content))
        self.set_child(view)
        self.tab_buttons["git"].set_active(True)
        self.refresh()

    # MARK: tabs

    def _tab_toggled(self, toggle, name) -> None:
        if not toggle.get_active():
            return
        self.current_tab = name
        self.stack.set_visible_child_name(name)
        if name == "github" and not self._github_checked:
            self._github_checked = True
            self.model.refresh_github_logins(lambda _logins, _error: None)

    def select_tab(self, name: str) -> None:
        self.tab_buttons[name].set_active(True)
        self._tab_toggled(self.tab_buttons[name], name)

    def tab_labels(self) -> list[str]:
        result = []
        for name, title in TABS:
            count = self.count_labels[name].get_label()
            result.append(f"{title} {count}".strip())
        return result

    def row_titles(self, tab: str) -> list[str]:
        return [row.get_title() for row in self._rows[tab]]

    def folder_titles(self) -> list[str]:
        return list(self._folder_titles)

    def refresh(self) -> None:
        config = self.model.config
        if config is None:
            return
        warnings = {
            "git": False,
            "github": any(login is not None and not login.is_usable
                          for login in map(self.model.github_login_for, config.github_accounts)),
            "claude": any(not self.model.has_token(a) for a in config.claude_accounts)
                      or any(self.model.needs_claude_login(p) for p in config.profiles),
            "keys": any(not self.model.has_secret(k) for k in config.api_keys),
        }
        counts = {"git": len(config.git_identities), "github": len(config.github_accounts),
                  "claude": len(config.claude_accounts), "keys": len(config.api_keys)}
        for name, _title in TABS:
            label = self.count_labels[name]
            label.set_label(f"{counts[name]} ⚠" if warnings[name] else str(counts[name]))
            if warnings[name]:
                label.add_css_class("warning")
            else:
                label.remove_css_class("warning")
        self._build_git()
        self._build_github()
        self._build_claude()
        self._build_keys()

    # MARK: rows

    def _clear(self, tab: str) -> Gtk.Box:
        box = self.tab_boxes[tab]
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()
        self._rows[tab] = []
        return box

    def _row(self, tab: str, title: str, subtitle: str, letter: str, color: ProfileColor, suffixes=(),
             menu=()) -> Adw.ActionRow:
        """subtitle is Pango markup (callers escape their text)."""
        row = Adw.ActionRow(title=escape(title), subtitle=subtitle)
        row.add_prefix(widgets.avatar(letter, color, 36))
        for widget in suffixes:
            row.add_suffix(widget)
        if menu:
            actions = Gio.SimpleActionGroup()
            items = Gio.Menu()
            for index, (text, callback) in enumerate(menu):
                action = Gio.SimpleAction.new(f"a{index}", None)
                action.connect("activate", lambda *_a, cb=callback: cb())
                actions.add_action(action)
                items.append(text, f"row.a{index}")
            row.insert_action_group("row", actions)
            more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=items, valign=Gtk.Align.CENTER,
                                  tooltip_text="Mais ações")
            more.add_css_class("flat")
            row.add_suffix(more)
        self._rows[tab].append(row)
        return row

    @staticmethod
    def _empty(text: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=escape(text), activatable=False)
        row.add_css_class("dim-label")
        return row

    def _build_git(self) -> None:
        config, box = self.model.config, self._clear("git")
        group = Adw.PreferencesGroup()
        for identity in config.git_identities:
            key = (escape(self.model.environment.abbreviate(self.model.environment.expand_tilde(identity.ssh_key_path)))
                   if identity.ssh_key_path else "SSH padrão")
            used = _used_in(p for p in config.profiles if p.git_identity_id == identity.id)
            subtitle = f"{escape(identity.name)} · <tt>{escape(identity.email)}</tt> · {key}"
            if used:
                subtitle += f" · {escape(used)}"
            group.add(self._row("git", identity.nickname, subtitle, identity.nickname, identity.color, menu=(
                ("Editar", lambda i=identity: self.open_editor("git", i.id)),
                ("Remover", lambda i=identity: self._remove_git(i)))))
        if not config.git_identities:
            group.add(self._empty("Nenhuma identidade git. Adicione o nome e o e-mail que cada perfil usa nos commits."))
        box.append(group)
        box.append(widgets.note(GIT_NOTE))

    def _build_github(self) -> None:
        config, box = self.model.config, self._clear("github")
        group = Adw.PreferencesGroup()
        actions = Gtk.Box(spacing=6)
        actions.append(widgets.button("Importar contas do gh", self.import_github, "flat"))
        actions.append(widgets.button("Entrar no GitHub…", lambda: self.request_login("github", None), "flat"))
        group.set_header_suffix(actions)
        for account in config.github_accounts:
            login = self.model.github_login_for(account)
            subtitle = f"{escape(account.username)} · {escape(account.host)}"
            if login is not None and login.active:
                subtitle += " · ativa no gh"
            subtitle += " · Claude coautora" if account.claude_co_author else " · sem coautoria da Claude"
            suffixes = []
            if login is not None:
                suffixes.append(widgets.pill("conectada", "ok") if login.is_usable
                                else widgets.pill("precisa entrar", "warning"))
            elif self.model.github_logins:
                suffixes.append(widgets.pill("não está no gh", "warning"))
            group.add(self._row("github", account.nickname, subtitle, account.nickname, account.color, suffixes, (
                ("Editar", lambda a=account: self.open_editor("github", a.id)),
                ("Entrar de novo no GitHub…", lambda: self.request_login("github", None)),
                ("Remover", lambda a=account: self._remove_github(a)))))
        if not config.github_accounts:
            group.add(self._empty("Nenhuma conta GitHub. Importe as contas que o gh já conhece."))
        box.append(group)
        box.append(widgets.note(GITHUB_NOTE))

    def _build_claude(self) -> None:
        config, box = self.model.config, self._clear("claude")
        group = Adw.PreferencesGroup()
        group.set_header_suffix(widgets.button("Adicionar conta Claude…", lambda: self.request_login("add", None),
                                               "flat"))
        for account in config.claude_accounts:
            in_use = self.model.state.applied_claude_account_id == account.id
            parts = [escape(account.email)] if account.email else []
            if account.kind == ClaudeAccountKind.LOGIN:
                parts.append("login completo")
            else:
                parts.append("token (setup-token) · só terminais novos")
            used = _used_in(p for p in config.profiles if p.claude_account_id == account.id)
            if used:
                parts.append(escape(used))
            suffixes = []
            if in_use:
                suffixes.append(widgets.pill("em uso", "ok"))
            elif not self.model.has_token(account):
                suffixes.append(widgets.pill("precisa entrar", "warning"))
                suffixes.append(widgets.button("Entrar…", lambda a=account: self.request_login("login", a)))
            else:
                suffixes.append(widgets.pill("conectada", "ok"))
            group.add(self._row("claude", account.nickname, " · ".join(parts), account.nickname, account.color,
                                suffixes, (
                                    ("Entrar de novo…", lambda a=account: self.request_login("login", a)),
                                    ("Remover", lambda a=account: self._remove_claude(a)))))
        if not config.claude_accounts:
            group.add(self._empty("Nenhuma conta extra. Adicione uma para usar outro login sem trocar de pasta."))
        box.append(group)

        folders = Adw.PreferencesGroup(title="Pastas do Claude usadas pelos perfis")
        self._folder_titles = []
        for config_dir in self.model.claude_folders_in_use:
            name = self.model.folder_name(config_dir)
            plain = f"{name} (padrão)" if config_dir is None else name
            self._folder_titles.append(plain)
            title = f"<tt>{escape(name)}</tt>" + (" <span alpha='55%'>(padrão)</span>" if config_dir is None else "")
            profiles = [p.name for p in config.profiles if p.claude_config_dir == config_dir]
            summary = self.model.folder_summaries.get(config_dir)
            parts = [escape(", ".join(profiles)) if profiles else "nenhum perfil"]
            if summary is not None:
                parts.append(f"{summary.conversation_count} conversas recentes")
            row = Adw.ActionRow(title=title, subtitle=" · ".join(parts), activatable=True)
            status = self.model.folder_status(config_dir)
            if status is not None and status.kind == "loggedIn":
                row.add_suffix(widgets.pill(status.email or "logada", "ok"))
            elif status is not None and status.kind == "loggedOut":
                row.add_suffix(widgets.pill("sem login", "warning"))
                row.add_suffix(widgets.button("Entrar…", lambda d=config_dir: self.request_login("folder", d)))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, d=config_dir: self.window.push(ClaudeFolderPage(self.window, d)))
            folders.add(row)
        box.append(folders)
        box.append(widgets.note(CLAUDE_NOTE))

    def _build_keys(self) -> None:
        config, box = self.model.config, self._clear("keys")
        group = Adw.PreferencesGroup()
        for key in config.api_keys:
            used = _used_in(p for p in config.profiles if key.id in p.api_key_ids)
            subtitle = f"{escape(key.provider.display_name)} · <tt>{escape(key.env_var)}</tt>"
            if used:
                subtitle += f" · {escape(used)}"
            pill = (widgets.pill("no chaveiro", "ok") if self.model.has_secret(key)
                    else widgets.pill("sem valor", "warning"))
            group.add(self._row("keys", key.nickname, subtitle, key.nickname, key.color, [pill], (
                ("Editar", lambda k=key: self.open_editor("keys", k.id)),
                ("Trocar valor", lambda k=key: self.replace_value(k)),
                ("Remover", lambda k=key: self._remove_key(k)))))
        if not config.api_keys:
            group.add(self._empty("Nenhuma chave. Adicione chaves da OpenAI, Gemini e outras."))
        box.append(group)
        box.append(widgets.note(KEYS_NOTE))

    # MARK: actions

    def add_account(self) -> None:
        if self.current_tab == "claude":
            self.request_login("add", None)
        else:
            self.open_editor(self.current_tab, None)

    def open_editor(self, kind: str, item_id) -> Adw.NavigationPage:
        page = {"git": GitIdentityEditorPage, "github": GitHubAccountEditorPage, "keys": APIKeyEditorPage}[kind]
        return self.window.push(page(self.window, item_id))

    def request_login(self, kind: str, account, tab: str | None = None) -> None:
        if tab is not None:
            self.select_tab(tab)
        self.window.on_login_requested(kind, account)

    def import_github(self) -> None:
        def done(added, error):
            if error is not None:
                self.window.show_toast(widgets.describe_error(error))
            elif added:
                self.window.show_toast(f"{added} conta(s) do gh adicionada(s)")
            else:
                self.window.show_toast("Nenhuma conta nova no gh")

        self.model.import_github_accounts(done)

    def replace_value(self, key) -> None:
        dialog = Adw.AlertDialog(heading=f"Trocar o valor de {key.nickname}",
                                 body=f"O novo valor vai direto para o chaveiro do sistema e o terminal passa a "
                                      f"exportar {key.env_var} com ele na próxima troca.")
        entry_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        entry_list.add_css_class("boxed-list")
        entry = Adw.PasswordEntryRow(title="Novo valor")
        entry_list.append(entry)
        dialog.set_extra_child(entry_list)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("save", "Trocar valor")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_dialog, answer):
            value = entry.get_text()
            entry.set_text("")
            if answer != "save":
                return
            try:
                self.model.replace_secret(key, value)
            except Exception as error:
                self.window.show_toast(widgets.describe_error(error))
                return
            self.window.show_toast(f"Valor de {key.nickname} trocado")

        dialog.connect("response", response)
        dialog.present(self.window)

    def _apply(self, change, done_text: str) -> bool:
        try:
            self.model.edit(change)
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return False
        self.window.show_toast(done_text)
        return True

    def _remove_git(self, identity) -> None:
        widgets.confirm(self, f"Remover a identidade {identity.nickname}?",
                        "Os perfis que a usam deixam de trocar o git.", "Remover",
                        lambda: self._apply(lambda c: ConfigEditor.remove_git_identity(identity.id, c),
                                            f"Identidade {identity.nickname} removida"))

    def _remove_github(self, account) -> None:
        widgets.confirm(self, f"Remover a conta {account.nickname}?",
                        "Ela continua logada no gh; os perfis que a usam deixam de trocar o GitHub.", "Remover",
                        lambda: self._apply(lambda c: ConfigEditor.remove_github_account(account.id, c),
                                            f"Conta {account.nickname} removida"))

    def _remove_key(self, key) -> None:
        widgets.confirm(self, f"Remover a chave {key.nickname}?",
                        f"O valor é apagado do chaveiro do sistema e os perfis deixam de exportar {key.env_var}.",
                        "Remover", lambda: self._remove_key_now(key))

    def _remove_key_now(self, key) -> None:
        try:
            self.model.remove_api_key(key.id)
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return
        self.window.show_toast(f"Chave {key.nickname} removida")

    def _remove_claude(self, account) -> None:
        def remove():
            try:
                self.model.remove_claude_account(account.id)
            except Exception as error:
                self.window.show_toast(widgets.describe_error(error))
                return
            self.window.show_toast(f"Conta {account.nickname} removida")

        widgets.confirm(self, f"Remover a conta Claude {account.nickname}?",
                        "O login guardado é apagado; os perfis que a usam voltam para o login da própria pasta.",
                        "Remover", remove)


# MARK: editors

class _EditorPage(Adw.NavigationPage):
    """Shared shape: a form, Salvar in the header bar, Remover at the bottom for existing items."""

    def __init__(self, window, title: str):
        super().__init__(title=title)
        self.window, self.model = window, window.model
        self.errors = widgets.FieldErrors()
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.save_button = widgets.button("Salvar", self.save, "suggested-action")
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(self.save_button)
        view.add_top_bar(header)
        view.set_content(widgets.scrolled(self.content))
        self.set_child(view)

    def entry(self, group, field: str, title: str, text: str = "") -> Adw.EntryRow:
        row = Adw.EntryRow(title=title, text=text)
        self.errors.register(field, row)
        group.add(row)
        return row

    def color_row(self, group, selected: ProfileColor) -> None:
        row = Adw.ActionRow(title="Cor")
        swatches, self.color_buttons = widgets.color_swatches(selected, self._color_changed)
        row.add_suffix(swatches)
        group.add(row)
        self.color = selected

    def _color_changed(self, color) -> None:
        self.color = color

    def remove_button(self, on_click) -> None:
        button = widgets.button("Remover", on_click, "flat", "destructive-action")
        button.set_halign(Gtk.Align.CENTER)
        self.content.append(button)

    def error_text(self, field: str) -> str | None:
        return self.errors.text(field)

    def show_issues(self, issues, field_for) -> None:
        unplaced = [issue for issue in issues if not self.errors.show(field_for(issue), widgets.issue_message(issue))]
        if unplaced:
            self.window.show_toast(widgets.describe_issues(unplaced))

    def finish(self, text: str) -> None:
        self.window.nav.pop()
        self.window.show_toast(text)

    def save(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


class GitIdentityEditorPage(_EditorPage):
    def __init__(self, window, identity_id):
        existing = window.model.config.git_identity(identity_id)
        super().__init__(window, "Nova identidade git" if existing is None else "Editar identidade git")
        self.draft = copy.deepcopy(existing) if existing else GitIdentity(
            nickname="", name="", email="", color=_next_color(len(window.model.config.git_identities)))
        group = Adw.PreferencesGroup()
        self.nickname_row = self.entry(group, "nickname", "Apelido", self.draft.nickname)
        self.name_row = self.entry(group, "name", "Nome nos commits", self.draft.name)
        self.email_row = self.entry(group, "email", "E-mail nos commits", self.draft.email)
        self.color_row(group, self.draft.color)
        self.content.append(group)
        ssh = Adw.PreferencesGroup(description=escape("Com uma chave, o git usa core.sshCommand = ssh -i <chave> -o "
                                                      "IdentitiesOnly=yes. Sem chave, vale a sua configuração de SSH."))
        self.ssh_row = self.entry(ssh, "ssh", "Chave SSH (opcional)", self.draft.ssh_key_path or "")
        self.ssh_row.add_suffix(widgets.button("Escolher…", self._choose_key, "flat"))
        self.content.append(ssh)
        if existing is not None:
            self.remove_button(lambda: widgets.confirm(
                self, f"Remover a identidade {self.draft.nickname}?", "Os perfis que a usam deixam de trocar o git.",
                "Remover", self._remove))

    def _choose_key(self) -> None:
        dialog = Gtk.FileDialog(title="Chave SSH", modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(str(self.model.environment.home / ".ssh")))
        dialog.open(self.window, None, self._key_chosen)

    def _key_chosen(self, dialog, result) -> None:
        try:
            chosen = dialog.open_finish(result)
        except GLib.Error:
            return
        if chosen is not None and chosen.get_path():
            self.ssh_row.set_text(self.model.environment.abbreviate(chosen.get_path()))

    def save(self) -> bool:
        self.errors.clear()
        identity = copy.deepcopy(self.draft)
        identity.nickname = self.nickname_row.get_text().strip(" ")
        identity.name = self.name_row.get_text().strip(" ")
        identity.email = self.email_row.get_text().strip(" ")
        identity.ssh_key_path = self.ssh_row.get_text().strip() or None
        identity.color = self.color
        fields = {"emptyField": None, "controlCharacters": None, "invalidEmail": "email", "invalidSSHKeyPath": "ssh"}
        try:
            self.model.edit(lambda config: ConfigEditor.upsert(identity, config))
        except EditorError as error:
            self.show_issues(error.issues, lambda i: fields.get(i.kind) or i.detail)
            return False
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return False
        self.finish(f"Identidade {identity.nickname} salva")
        return True

    def _remove(self) -> None:
        try:
            self.model.edit(lambda c: ConfigEditor.remove_git_identity(self.draft.id, c))
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return
        self.finish(f"Identidade {self.draft.nickname} removida")


class GitHubAccountEditorPage(_EditorPage):
    def __init__(self, window, account_id):
        existing = window.model.config.github_account(account_id)
        super().__init__(window, "Adicionar conta GitHub" if existing is None else "Editar conta GitHub")
        self.existing = existing
        self.draft = copy.deepcopy(existing) if existing else GitHubAccount(
            nickname="", username="", color=_next_color(len(window.model.config.github_accounts)))
        group = Adw.PreferencesGroup()
        self.nickname_row = self.entry(group, "nickname", "Apelido", self.draft.nickname)
        self.username_row = self.entry(group, "username", "Usuário no GitHub", self.draft.username)
        self.host_row = self.entry(group, "host", "Servidor", self.draft.host)
        # Like the Mac: an existing account keeps its user and host (they identify the gh login).
        self.username_row.set_sensitive(existing is None)
        self.host_row.set_sensitive(existing is None)
        self.color_row(group, self.draft.color)
        self.content.append(group)
        coauthor = Adw.PreferencesGroup(description=escape(
            "Ligado: commits e PRs feitos pelo Claude Code nos perfis com esta conta terminam com "
            "\"Co-Authored-By: Claude\" (padrão do Claude Code). Desligado: saem só com o seu nome; o MultiCred "
            "grava isso no settings.json da pasta do Claude do perfil ao ativá-lo."))
        self.coauthor_row = Adw.SwitchRow(title="Claude como coautora", subtitle="dos commits e PRs feitos pelo Claude Code",
                                          active=self.draft.claude_co_author)
        coauthor.add(self.coauthor_row)
        self.content.append(coauthor)
        if existing is not None:
            self.remove_button(lambda: widgets.confirm(
                self, f"Remover a conta {self.draft.nickname}?",
                "Ela continua logada no gh; os perfis que a usam deixam de trocar o GitHub.", "Remover", self._remove))

    def save(self) -> bool:
        self.errors.clear()
        account = copy.deepcopy(self.draft)
        account.nickname = self.nickname_row.get_text().strip(" ")
        account.username = self.username_row.get_text().strip()
        account.host = self.host_row.get_text().strip() or "github.com"
        account.color = self.color
        account.claude_co_author = self.coauthor_row.get_active()
        fields = {"invalidGitHubUsername": "username", "invalidHost": "host"}
        try:
            self.model.edit(lambda config: ConfigEditor.upsert(account, config))
        except EditorError as error:
            self.show_issues(error.issues, lambda i: fields.get(i.kind) or i.detail)
            return False
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return False
        self.finish(f"Conta {account.nickname} salva")
        # The active profile uses it: apply the co-author choice now (D38), like the Mac.
        active = self.model.active_profile
        changed = self.existing is not None and self.existing.claude_co_author != account.claude_co_author
        if changed and active is not None and active.github_account_id == account.id and not self.model.switching:
            self.window.on_profile(None, active.id)
        return True

    def _remove(self) -> None:
        try:
            self.model.edit(lambda c: ConfigEditor.remove_github_account(self.draft.id, c))
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return
        self.finish(f"Conta {self.draft.nickname} removida")


class APIKeyEditorPage(_EditorPage):
    def __init__(self, window, key_id):
        existing = window.model.config.api_key(key_id)
        super().__init__(window, "Nova chave de API" if existing is None else "Editar chave de API")
        self.existing = existing
        self.providers = list(APIProvider)
        provider = existing.provider if existing else APIProvider.OPENAI
        group = Adw.PreferencesGroup()
        self.nickname_row = self.entry(group, "nickname", "Apelido", existing.nickname if existing else "")
        self.provider_row = widgets.combo("Provedor", [p.display_name for p in self.providers],
                                          self.providers.index(provider))
        self._provider = provider
        self.provider_row.connect("notify::selected", self._provider_changed)
        group.add(self.provider_row)
        self.env_var_row = self.entry(group, "envVar", "Variável de ambiente",
                                      existing.env_var if existing else provider.default_env_var)
        self.env_var_row.add_css_class("monospace")
        self.color_row(group, existing.color if existing else _next_color(len(window.model.config.api_keys)))
        self.content.append(group)
        if existing is None:
            description = "O valor vai direto para o chaveiro do sistema (Secret Service) e não aparece mais aqui."
        elif self.model.has_secret(existing):
            description = "Há um valor guardado no chaveiro do sistema. Deixe em branco para mantê-lo."
        else:
            description = "Não há valor no chaveiro do sistema para esta chave."
        secret_group = Adw.PreferencesGroup(description=escape(description))
        self.secret_row = Adw.PasswordEntryRow(
            title="Cole a chave" if existing is None else "Cole uma nova chave para substituir")
        self.errors.register("secret", self.secret_row)
        secret_group.add(self.secret_row)
        self.content.append(secret_group)
        if existing is not None:
            self.remove_button(lambda: widgets.confirm(
                self, f"Remover a chave {existing.nickname}?",
                f"O valor é apagado do chaveiro do sistema e os perfis deixam de exportar {existing.env_var}.",
                "Remover", self._remove))
        self.connect("hidden", lambda *_: self.secret_row.set_text(""))

    def set_provider(self, value: str) -> None:
        self.provider_row.set_selected(self.providers.index(APIProvider(value)))

    def _provider_changed(self, row, _param) -> None:
        new = self.providers[row.get_selected()]
        current = self.env_var_row.get_text().strip()
        if not current or current == self._provider.default_env_var:
            self.env_var_row.set_text(new.default_env_var)
        self._provider = new

    def save(self) -> bool:
        self.errors.clear()
        nickname = self.nickname_row.get_text().strip(" ")
        provider = self.providers[self.provider_row.get_selected()]
        env_var = self.env_var_row.get_text().strip()
        secret = self.secret_row.get_text()
        fields = {"invalidEnvVar": "envVar", "reservedEnvVar": "envVar"}
        try:
            if self.existing is not None:
                updated = copy.deepcopy(self.existing)
                updated.nickname, updated.provider, updated.env_var, updated.color = nickname, provider, env_var, self.color
                self.model.edit(lambda config: ConfigEditor.update_api_key(updated, config))
                if secret.strip():
                    self.model.replace_secret(updated, secret)
            else:
                self.model.add_api_key(nickname, provider, env_var, secret, self.color)
        except EditorError as error:
            if error.kind == "invalid":
                self.show_issues(error.issues, lambda i: fields.get(i.kind) or i.detail)
            elif error.kind == "emptySecret":
                self.errors.show("secret", "Cole o valor da chave.")
            else:
                self.window.show_toast(widgets.describe_error(error))
            return False
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return False
        self.secret_row.set_text("")
        self.finish(f"Chave {nickname} salva")
        return True

    def _remove(self) -> None:
        try:
            self.model.remove_api_key(self.existing.id)
        except Exception as error:
            self.window.show_toast(widgets.describe_error(error))
            return
        self.finish(f"Chave {self.existing.nickname} removida")


class ClaudeFolderPage(Adw.NavigationPage):
    """What a Claude folder holds (local metadata only: login e-mail, recent conversation titles)."""

    def __init__(self, window, config_dir):
        name = window.model.folder_name(config_dir)
        super().__init__(title=name)
        self.window, self.model, self.config_dir = window, window.model, config_dir
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        summary = self.model.folder_summaries.get(config_dir)
        info = Adw.PreferencesGroup(title=widgets.escape(name))
        profiles = [p.name for p in self.model.config.profiles if p.claude_config_dir == config_dir]
        info.add(Adw.ActionRow(title="Perfis", subtitle=escape(", ".join(profiles) or "nenhum")))
        if summary is not None:
            info.add(Adw.ActionRow(title="Login da pasta", subtitle=escape(summary.email or "sem login")))
            info.add(Adw.ActionRow(title="Conversas", subtitle=f"{summary.project_count} projetos · "
                                                              f"{summary.conversation_count} conversas"))
        content.append(info)
        recent = Adw.PreferencesGroup(title="Conversas recentes")
        for conversation in (summary.recent_conversations if summary else []):
            when = conversation.last_activity.astimezone().strftime("%d/%m %H:%M")
            recent.add(Adw.ActionRow(title=escape(conversation.title),
                                     subtitle=escape(" · ".join(x for x in (conversation.project, when) if x))))
        if summary is None or not summary.recent_conversations:
            recent.add(AccountsPage._empty("Nenhuma conversa nesta pasta."))
        content.append(recent)
        content.append(widgets.note("Lido só neste computador: títulos e datas das conversas, nunca o conteúdo."))
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(widgets.scrolled(content))
        self.set_child(view)
