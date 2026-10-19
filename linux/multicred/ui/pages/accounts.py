"""Contas: the Git, GitHub and Chaves de API pages and their editors, like the Mac
(`MultiCred/Views/AccountsPages.swift`). The Claude page is `claude.py`; it shares
`AccountRow` from here.

Each account kind has its own page (a sidebar entry), with the Mac's `PageHeader`
inside the content (title, subtitle, prominent action), an empty state as a status page
(`ContentUnavailableView`), rows like the Mac's `AccountRow` (round avatar, title and
subtitle, a "usado em" column, a monospaced detail column, the status on the right) and
the `InfoNote` pinned at the bottom. Activating a row edits it (the Mac's double-click);
the Mac's context menus are the rows' right-click menus. Editors are pages pushed on the
same navigation stack, ending with "Remover / Cancelar / Salvar" (Salvar off while a
required field is empty) and with the Mac's examples in the fields. API key values are
typed into a password field and go only to the secret store (`AppModel.add_api_key` /
`replace_secret`); they are never kept here. Texts are the Mac's, with "Keychain" →
"chaveiro do sistema".
"""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ...core.editors import ConfigEditor, EditorError  # noqa: E402
from ...core.models import APIProvider, GitHubAccount, GitIdentity, ProfileColor  # noqa: E402
from .. import widgets  # noqa: E402
from ..widgets import escape  # noqa: E402

# AccountsPages.swift:85, 209, 484 ("no Keychain dele" / "Keychain do macOS" → chaveiro do sistema;
# gh on Linux keeps its token in the keyring or in its own hosts.yml, so the GitHub note just
# says "com o próprio gh").
GIT_NOTE = "O MultiCred escreve só o arquivo incluído no fim do seu gitconfig global. O seu ~/.gitconfig continua seu."
GITHUB_NOTE = "O login e o token ficam com o próprio gh. O MultiCred só escolhe a conta ativa."
KEYS_NOTE = ("O valor de cada chave fica só no chaveiro do sistema. O MultiCred guarda apenas o nome da variável e "
             "qual item usar; o terminal lê o valor quando o perfil muda. As chaves não são passadas para apps "
             "gráficos.")
# SF Symbols → icon names (the sidebar's, window.ICONS; info.circle, shield, key).
GIT_ICON, GITHUB_ICON, KEY_ICON = "multicred-branch-symbolic", "multicred-code-symbolic", "dialog-password-symbolic"
INFO_ICON, SHIELD_ICON = "help-about-symbolic", "security-low-symbolic"


def _used_in(profiles) -> str:
    return ", ".join(p.name for p in profiles)


def _used_in_column(profiles) -> str:
    """Mac `usedIn` (AccountsPages.swift:34-36): the profiles' names, or "—"."""
    return _used_in(profiles) or "—"


def _next_color(count: int) -> ProfileColor:
    palette = list(ProfileColor)
    return palette[count % len(palette)]


# MARK: list chrome


class AccountRow(Gtk.ListBoxRow):
    """One row of an accounts list (Mac `AccountRow`, AccountsPages.swift:7-32): round
    avatar, title/subtitle, "usado em", monospaced detail, then the trailing status and
    buttons. `menu` is the Mac's context menu: (label, callback) pairs, opened with a
    right click, a long press or Shift+F10; a None entry is the Mac's Divider."""

    def __init__(self, name: str, color: ProfileColor, title: str, subtitle: str, used_in: str, detail: str,
                 on_activate=None, menu=()):
        super().__init__(activatable=on_activate is not None)
        self.on_activate, self.menu = on_activate, list(menu)
        self.title, self.subtitle, self.used_in, self.detail = title, subtitle, used_in, detail
        self.status: str | None = None
        self.buttons: dict[str, Gtk.Button] = {}
        box = Gtk.Box(spacing=12)
        box.add_css_class("mc-account-row")
        self.avatar = widgets.account_avatar(name, color, 34)
        box.append(self.avatar)
        # Columns like the Mac's frames: the title column takes the free width (at least
        # 200 px), "usado em", detail and the trailing area have fixed widths so the columns
        # line up across rows whatever each row's status is. Small natural widths: the size
        # requests alone set each column.
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        texts.set_size_request(200, -1)
        for text, css in ((title, "heading"), (subtitle, "dim-label")):
            line = widgets.label(text, css)
            line.set_ellipsize(Pango.EllipsizeMode.END)
            line.set_max_width_chars(8)
            texts.append(line)
        box.append(texts)
        columns = ((used_in, 120, ("dim-label",)), (detail, 150, ("monospace", "caption", "dim-label")))
        for text, width, classes in columns:
            column = widgets.label(text, *classes, wrap=True)
            column.set_size_request(width, -1)
            column.set_max_width_chars(8)
            column.set_lines(2)
            column.set_ellipsize(Pango.EllipsizeMode.END)
            column.set_valign(Gtk.Align.CENTER)
            box.append(column)
        self.trailing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.trailing.set_size_request(110, -1)
        box.append(self.trailing)
        self.set_child(box)
        if self.menu:
            self._install_menu()

    # trailing

    def set_status(self, text: str, tone: str) -> None:
        self.status = text
        pill = widgets.pill(text, tone)
        pill.set_halign(Gtk.Align.END)
        self.trailing.append(pill)

    def set_busy(self) -> None:
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.END)
        self.trailing.append(spinner)

    def add_button(self, text: str, on_click) -> Gtk.Button:
        button = widgets.button(text, on_click, "mc-small-button")
        button.set_halign(Gtk.Align.END)
        self.trailing.append(button)
        self.buttons[text] = button
        return button

    # context menu

    def _install_menu(self) -> None:
        actions = Gio.SimpleActionGroup()
        self.menu_model = Gio.Menu()
        section = Gio.Menu()
        for index, entry in enumerate(self.menu):
            if entry is None:  # Divider: the items so far become a section
                self.menu_model.append_section(None, section)
                section = Gio.Menu()
                continue
            text, callback = entry
            action = Gio.SimpleAction.new(f"a{index}", None)
            action.connect("activate", lambda *_a, cb=callback: cb())
            actions.add_action(action)
            section.append(text, f"row.a{index}")
        self.menu_model.append_section(None, section)
        self.insert_action_group("row", actions)
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda _g, _n, x, y: self.popup_menu(x, y))
        self.add_controller(click)
        press = Gtk.GestureLongPress(touch_only=True)
        press.connect("pressed", lambda _g, x, y: self.popup_menu(x, y))
        self.add_controller(press)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Shift>F10|Menu"),
                                       action=Gtk.CallbackAction.new(lambda *_: self.popup_menu() or True)))
        self.add_controller(keys)

    def popup_menu(self, x: float | None = None, y: float | None = None) -> Gtk.PopoverMenu:
        # Made per opening and unparented once closed: a row must not keep a child popover
        # past its life (GTK warns when a widget is finalized with children left).
        popover = Gtk.PopoverMenu.new_from_model(self.menu_model)
        popover.set_has_arrow(False)
        popover.set_parent(self)
        if x is not None and y is not None:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(lambda: p.unparent() or False))
        popover.popup()
        return popover

    def activate_menu(self, text: str) -> None:
        """Runs the context-menu item called `text` (what choosing it in the menu does)."""
        callback = next(entry[1] for entry in self.menu if entry is not None and entry[0] == text)
        callback()

    def info(self) -> dict:
        return {"title": self.title, "subtitle": self.subtitle, "used_in": self.used_in, "detail": self.detail,
                "status": self.status, "buttons": list(self.buttons),
                "menu": [entry[0] for entry in self.menu if entry is not None]}


class _AccountListPage(Adw.NavigationPage):
    """Shared shape of the Git, GitHub and Chaves de API pages (AccountsPages.swift:56-90,
    167-236, 450-489): header, divider, the list or the empty state, the note at the bottom."""

    TAG = TITLE = HEADER_TITLE = HEADER_SUBTITLE = ADD_LABEL = ""
    EMPTY_TITLE = EMPTY_DESCRIPTION = EMPTY_ICON = NOTE = NOTE_ICON = ""

    def __init__(self, window):
        super().__init__(title=self.TITLE, tag=self.TAG)
        self.window, self.model = window, window.model
        self.rows: list[AccountRow] = []
        self.add_button = widgets.icon_button(self.ADD_LABEL, "list-add-symbolic", self.add, "suggested-action")
        self._actions = self.header_actions()
        header, self._header_title, self._header_subtitle = widgets.page_header(
            self.HEADER_TITLE, self.HEADER_SUBTITLE, self._actions)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda _list, row: row.on_activate and row.on_activate())
        self.list.set_margin_top(16)
        self.list.set_margin_bottom(16)
        self.list.set_margin_start(20)
        self.list.set_margin_end(20)
        scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.empty_state = Adw.StatusPage(title=self.EMPTY_TITLE, description=self.EMPTY_DESCRIPTION,
                                          icon_name=self.EMPTY_ICON, vexpand=True)
        self.empty_state.add_css_class("compact")
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.empty_state, "empty")

        self.note = widgets.info_note(self.NOTE, self.NOTE_ICON)
        self.note.set_margin_start(20)
        self.note.set_margin_end(20)
        self.note.set_margin_top(4)
        self.note.set_margin_bottom(20)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(header)
        content.append(Gtk.Separator())
        content.append(self.stack)
        content.append(self.note)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(content)
        self.set_child(view)
        self.refresh()

    def header_actions(self) -> list[Gtk.Widget]:
        return [self.add_button]

    # what tests and the window read

    def header_texts(self) -> tuple[str, str]:
        return self._header_title.get_label(), self._header_subtitle.get_label()

    def action_labels(self) -> list[str]:
        return [widgets.button_text(button) for button in self._actions]

    def showing_empty(self) -> bool:
        return self.stack.get_visible_child_name() == "empty"

    def note_text(self) -> str:
        return self.note.text_label.get_label()

    def note_icon(self) -> str:
        return self.note.icon_name

    # building

    def items(self, config) -> list:  # pragma: no cover - overridden
        raise NotImplementedError

    def make_row(self, item, config) -> AccountRow:  # pragma: no cover - overridden
        raise NotImplementedError

    def refresh(self) -> None:
        config = self.model.config
        if config is None:
            return
        for row in self.rows:
            self.list.remove(row)
        items = self.items(config)
        self.rows = [self.make_row(item, config) for item in items]
        for row in self.rows:
            self.list.append(row)
        self.stack.set_visible_child_name("list" if items else "empty")

    def add(self) -> None:
        self.open_editor(None)

    def open_editor(self, item_id) -> Adw.NavigationPage:  # pragma: no cover - overridden
        raise NotImplementedError


class GitAccountsPage(_AccountListPage):
    """AccountsPages.swift:56-90: "Editar" on the right, no context menu."""

    TAG, TITLE = "git", "Git"
    HEADER_TITLE, HEADER_SUBTITLE = "Identidades git", "Nome e e-mail dos commits (e, se quiser, a chave SSH)."
    ADD_LABEL = "Adicionar identidade"
    EMPTY_TITLE, EMPTY_ICON = "Nenhuma identidade git", GIT_ICON
    EMPTY_DESCRIPTION = "Adicione o nome e o e-mail que cada perfil usa nos commits."
    NOTE, NOTE_ICON = GIT_NOTE, INFO_ICON

    def items(self, config):
        return config.git_identities

    def make_row(self, identity, config) -> AccountRow:
        environment = self.model.environment
        detail = (environment.abbreviate(environment.expand_tilde(identity.ssh_key_path))
                  if identity.ssh_key_path else "SSH padrão")
        row = AccountRow(identity.nickname, identity.color, identity.nickname, f"{identity.name} · {identity.email}",
                         _used_in_column(p for p in config.profiles if p.git_identity_id == identity.id), detail,
                         on_activate=lambda i=identity: self.open_editor(i.id))
        row.add_button("Editar", lambda i=identity: self.open_editor(i.id))
        return row

    def open_editor(self, item_id) -> Adw.NavigationPage:
        return self.window.push(GitIdentityEditorPage(self.window, item_id))


class GitHubAccountsPage(_AccountListPage):
    """AccountsPages.swift:167-236: "Conferir logins" re-reads gh's logins; each row shows
    Conectada / Precisa entrar / Não está no gh / Status indisponível, with "Entrar…" on
    the orange ones. Opening the page re-reads the logins too (the Mac's `.task`)."""

    TAG, TITLE = "github", "GitHub"
    HEADER_TITLE, HEADER_SUBTITLE = "Contas GitHub", "Contas logadas no GitHub CLI (gh). A troca usa gh auth switch."
    ADD_LABEL = "Adicionar conta"
    EMPTY_TITLE, EMPTY_ICON = "Nenhuma conta GitHub", GITHUB_ICON
    EMPTY_DESCRIPTION = "Adicione uma conta que já está no gh ou entre com outra."
    NOTE, NOTE_ICON = GITHUB_NOTE, SHIELD_ICON

    def __init__(self, window):
        self.refreshing = False
        self.check_button = widgets.icon_button("Conferir logins", "view-refresh-symbolic", self.check_logins)
        super().__init__(window)
        self.connect("showing", lambda *_: self.model.refresh_github_logins(lambda _logins, _error: None))

    def header_actions(self):
        return [self.check_button, self.add_button]

    def check_logins(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.check_button.set_sensitive(False)
        self.refresh()

        def done(_logins, error):
            self.refreshing = False
            self.check_button.set_sensitive(True)
            self.refresh()
            if error is not None:  # asked for by the user: say why nothing changed
                self.window.show_toast(widgets.describe_error(error))

        self.model.refresh_github_logins(done)

    def items(self, config):
        return config.github_accounts

    def make_row(self, account, config) -> AccountRow:
        login = self.model.github_login_for(account)
        log_in = lambda a=account: self.window.on_login_requested("github", a)  # noqa: E731
        row = AccountRow(account.nickname, account.color, account.nickname, f"{account.username} · {account.host}",
                         _used_in_column(p for p in config.profiles if p.github_account_id == account.id),
                         "ativa no gh" if login is not None and login.active else "",
                         on_activate=lambda a=account: self.open_editor(a.id),
                         menu=(("Editar", lambda a=account: self.open_editor(a.id)),
                               ("Entrar de novo no GitHub…", log_in)))
        if self.refreshing:
            row.set_busy()
        elif login is not None:
            if login.is_usable:
                row.set_status("Conectada", "ok")
            else:
                row.set_status("Precisa entrar", "warning")
                row.add_button("Entrar…", log_in)
        elif not self.model.github_logins:
            row.set_status("Status indisponível", "neutral")
        else:
            row.set_status("Não está no gh", "warning")
            row.add_button("Entrar…", log_in)
        return row

    def open_editor(self, item_id) -> Adw.NavigationPage:
        return self.window.push(GitHubAccountEditorPage(self.window, item_id))


class APIKeysPage(_AccountListPage):
    """AccountsPages.swift:450-489: status No chaveiro / Sem valor; the menu has only
    "Editar" (the value is replaced and the key removed in the editor)."""

    TAG, TITLE = "keys", "Chaves de API"
    HEADER_TITLE, HEADER_SUBTITLE = "Chaves de API", "Exportadas como variáveis de ambiente nos terminais do perfil."
    ADD_LABEL = "Adicionar chave"
    EMPTY_TITLE, EMPTY_ICON = "Nenhuma chave", KEY_ICON
    EMPTY_DESCRIPTION = "Adicione chaves da OpenAI, Gemini e outras. O valor fica só no chaveiro do sistema."
    NOTE, NOTE_ICON = KEYS_NOTE, KEY_ICON

    def items(self, config):
        return config.api_keys

    def make_row(self, key, config) -> AccountRow:
        row = AccountRow(key.nickname, key.color, key.nickname, key.provider.display_name,
                         _used_in_column(p for p in config.profiles if key.id in p.api_key_ids), key.env_var,
                         on_activate=lambda k=key: self.open_editor(k.id),
                         menu=(("Editar", lambda k=key: self.open_editor(k.id)),))
        if self.model.has_secret(key):
            row.set_status("No chaveiro", "ok")
        else:
            row.set_status("Sem valor", "warning")
        return row

    def open_editor(self, item_id) -> Adw.NavigationPage:
        return self.window.push(APIKeyEditorPage(self.window, item_id))


# MARK: editors


class _EditorPage(Adw.NavigationPage):
    """Shared shape of the Mac's account editors: a grouped form, then the footer
    "Remover" (existing items only) · "Cancelar" · "Salvar" (the default action, off while
    `can_save()` is false). Escape cancels, Enter in a field saves."""

    def __init__(self, window, title: str):
        super().__init__(title=title)
        self.window, self.model = window, window.model
        self.errors = widgets.FieldErrors()
        self.form_rows: list[widgets.FormEntryRow] = []
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.remove_button = widgets.button("Remover", lambda: None, "flat", "destructive-action")
        self.remove_button.set_visible(False)
        self.cancel_button = widgets.button("Cancelar", self.cancel)
        self.save_button = widgets.button("Salvar", self.save, "suggested-action")
        footer = Gtk.Box(spacing=8)
        footer.add_css_class("mc-editor-footer")
        footer.append(self.remove_button)
        footer.append(Gtk.Box(hexpand=True))
        footer.append(self.cancel_button)
        footer.append(self.save_button)
        self._footer = footer
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(widgets.scrolled(self.content))
        view.add_bottom_bar(footer)
        self.set_child(view)
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.CallbackAction.new(lambda *_: self.cancel() or True)))
        self.add_controller(keys)

    def entry(self, group, field: str, title: str, text: str = "", placeholder: str = "", password: bool = False,
              monospace: bool = False) -> widgets.FormEntryRow:
        row = widgets.FormEntryRow(title, text, placeholder, password=password, monospace=monospace)
        self.errors.register(field, row)
        row.entry.connect("changed", lambda *_: self.update_save())
        row.entry.connect("activate", lambda *_: self.save_button.get_sensitive() and self.save())
        group.add(row)
        self.form_rows.append(row)
        return row

    def color_row(self, group, selected: ProfileColor) -> None:
        row = Adw.ActionRow(title="Cor")
        swatches, self.color_buttons = widgets.color_swatches(selected, self._color_changed)
        row.add_suffix(swatches)
        group.add(row)
        self.color = selected

    def _color_changed(self, color) -> None:
        self.color = color

    def section(self, group: Adw.PreferencesGroup, footer: str | None = None) -> Gtk.Label | None:
        """Adds a form section; `footer` is the Mac Section footer, below the rows (an
        Adw.PreferencesGroup description would sit above them)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(group)
        footer_label = None
        if footer is not None:
            footer_label = widgets.label(footer, "dim-label", "mc-section-footer", wrap=True)
            box.append(footer_label)
        self.content.append(box)
        return footer_label

    def show_remove(self, on_click) -> None:
        self.remove_button.connect("clicked", lambda *_: on_click())
        self.remove_button.set_visible(True)

    def can_save(self) -> bool:  # pragma: no cover - overridden
        return True

    def update_save(self) -> None:
        self.save_button.set_sensitive(self.can_save())

    def cancel(self) -> None:
        if self.window.nav.get_visible_page() is self:
            self.window.nav.pop()

    # what tests read

    def footer_labels(self) -> list[str]:
        labels, child = [], self._footer.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button) and child.get_visible():
                labels.append(child.get_label())
            child = child.get_next_sibling()
        return labels

    def placeholders(self) -> dict[str, str]:
        return {row.plain_title: row.placeholder for row in self.form_rows if not row.password}

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
    """AccountsPages.swift:92-163."""

    def __init__(self, window, identity_id):
        existing = window.model.config.git_identity(identity_id)
        super().__init__(window, "Nova identidade git" if existing is None else "Editar identidade git")
        self.draft = copy.deepcopy(existing) if existing else GitIdentity(
            nickname="", name="", email="", color=_next_color(len(window.model.config.git_identities)))
        group = Adw.PreferencesGroup()
        self.nickname_row = self.entry(group, "nickname", "Apelido", self.draft.nickname, "Trabalho")
        self.name_row = self.entry(group, "name", "Nome nos commits", self.draft.name, "Seu Nome")
        self.email_row = self.entry(group, "email", "E-mail nos commits", self.draft.email, "voce@empresa.com")
        self.color_row(group, self.draft.color)
        self.content.append(group)
        self.ssh_group = Adw.PreferencesGroup()
        self.ssh_row = self.entry(self.ssh_group, "ssh", "Chave SSH (opcional)", self.draft.ssh_key_path or "",
                                  "~/.ssh/id_ed25519_trabalho")
        self.ssh_row.add_suffix(widgets.button("Escolher…", self._choose_key))
        self.ssh_footer = self.section(self.ssh_group, (
            "Com uma chave, o git usa core.sshCommand = ssh -i <chave> -o IdentitiesOnly=yes. Sem chave, vale a "
            "sua configuração de SSH."))
        if existing is not None:
            self.show_remove(lambda: widgets.confirm(
                self, f"Remover a identidade {self.draft.nickname}?", "Os perfis que a usam deixam de trocar o git.",
                "Remover", self._remove))
        self.update_save()

    def can_save(self) -> bool:
        return bool(self.nickname_row.get_text() and self.name_row.get_text() and self.email_row.get_text())

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


COAUTHOR_ON = ("Commits e PRs feitos pelo Claude Code nos perfis com esta conta terminam com "
               "\"Co-Authored-By: Claude\" (padrão do Claude Code).")
COAUTHOR_OFF = ("Commits e PRs feitos pelo Claude Code nos perfis com esta conta saem só com o seu nome. O MultiCred "
                "grava isso no settings.json da pasta do Claude do perfil ao ativá-lo.")


class GitHubAccountEditorPage(_EditorPage):
    """AccountsPages.swift:238-337. A new account first offers "Contas que o gh já
    conhece" (the gh logins not added yet) and "Entrar com outra conta no GitHub…",
    which gives this page's place to the GitHub login."""

    def __init__(self, window, account_id):
        existing = window.model.config.github_account(account_id)
        super().__init__(window, "Adicionar conta GitHub" if existing is None else "Editar conta GitHub")
        self.existing = existing
        self.draft = copy.deepcopy(existing) if existing else GitHubAccount(
            nickname="", username="", color=_next_color(len(window.model.config.github_accounts)))
        self.known_group = None
        self.other_login_row = None
        self._known_rows: list[Gtk.Widget] = []
        self._checks: list[tuple[object, Gtk.CheckButton]] = []
        self._choosing = False
        if existing is None:
            self.known_group = Adw.PreferencesGroup(title="Contas que o gh já conhece")
            self.content.append(self.known_group)
        group = Adw.PreferencesGroup()
        self.nickname_row = self.entry(group, "nickname", "Apelido", self.draft.nickname, "Trabalho")
        self.username_row = self.entry(group, "username", "Usuário no GitHub", self.draft.username, "seu-usuario")
        self.host_row = self.entry(group, "host", "Servidor", self.draft.host)
        # Like the Mac: an existing account keeps its user and host (they identify the gh login).
        self.username_row.set_sensitive(existing is None)
        self.host_row.set_sensitive(existing is None)
        self.username_row.entry.connect("changed", lambda *_: self._sync_checks())
        self.color_row(group, self.draft.color)
        self.content.append(group)
        self.coauthor_group = Adw.PreferencesGroup()
        self.coauthor_row = Adw.SwitchRow(title="Incluir a Claude como co-autora dos commits",
                                          active=self.draft.claude_co_author)
        self.coauthor_row.connect("notify::active", lambda *_: self._coauthor_changed())
        self.coauthor_group.add(self.coauthor_row)
        self.coauthor_footer = self.section(self.coauthor_group, "")
        self._coauthor_changed()
        if existing is not None:
            self.show_remove(lambda: widgets.confirm(
                self, f"Remover a conta {self.draft.nickname}?",
                "Ela continua logada no gh; os perfis que a usam deixam de trocar o GitHub.", "Remover", self._remove))
        self.refresh()
        self.update_save()
        if existing is None and not self.model.github_logins:
            # The Mac's `.task`: the model notifies when gh answers, and refresh() rebuilds the list.
            self.model.refresh_github_logins(lambda _logins, _error: None)

    def can_save(self) -> bool:
        return bool(self.nickname_row.get_text() and self.username_row.get_text())

    def _coauthor_changed(self) -> None:
        self.coauthor_footer.set_label(COAUTHOR_ON if self.coauthor_row.get_active() else COAUTHOR_OFF)

    # known gh logins

    def _available_logins(self) -> list:
        accounts = self.model.config.github_accounts if self.model.config else []
        return [login for login in self.model.github_logins
                if not any(a.username.lower() == login.login.lower() and a.host == login.host for a in accounts)]

    def refresh(self) -> None:
        """Rebuilds only the known logins (the window calls this on every model change;
        what the user typed stays)."""
        if self.known_group is None:
            return
        for row in self._known_rows:
            self.known_group.remove(row)
        self._known_rows, self._checks = [], []
        available = self._available_logins()
        if not available:
            message = Adw.ActionRow(title=escape("Nenhuma conta encontrada no gh." if not self.model.github_logins
                                                 else "Todas as contas do gh já foram adicionadas."),
                                    activatable=False)
            message.add_css_class("dim-label")
            self._add_known(message)
        # A hidden button anchors the radio group: one login still looks like a radio, and
        # "none chosen" (the user field matches no login) is a state of the group.
        self._no_login = Gtk.CheckButton()
        for login in available:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self._no_login)
            row = Adw.ActionRow(title=escape(f"{login.login} · {login.host}"))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            if login.active:
                row.add_suffix(widgets.label("ativa", "dim-label", "caption"))
            check.connect("toggled", lambda button, l=login: button.get_active() and self.choose_login(l.login))
            self._checks.append((login, check))
            self._add_known(row)
        self.other_login_row = Adw.ActionRow(title=escape("Entrar com outra conta no GitHub…"), activatable=True)
        self.other_login_row.add_prefix(Gtk.Image.new_from_icon_name("contact-new-symbolic"))
        self.other_login_row.connect("activated", lambda *_: self._log_in_with_another())
        self._add_known(self.other_login_row)
        self._sync_checks()

    def _add_known(self, row) -> None:
        self.known_group.add(row)
        self._known_rows.append(row)

    def known_texts(self) -> list[str]:
        available = self._available_logins()
        texts = [f"{login.login} · {login.host}" + (" ativa" if login.active else "") for login in available]
        if not available:
            texts.append("Nenhuma conta encontrada no gh." if not self.model.github_logins
                         else "Todas as contas do gh já foram adicionadas.")
        return texts + ["Entrar com outra conta no GitHub…"]

    def choose_login(self, name: str) -> None:
        login = next((login for login, _check in self._checks if login.login == name), None)
        if login is None or self._choosing:
            return
        self._choosing = True
        try:
            self.username_row.set_text(login.login)
            self.host_row.set_text(login.host)
            if not self.nickname_row.get_text():
                self.nickname_row.set_text(login.login)
        finally:
            self._choosing = False
        self._sync_checks()

    def chosen_logins(self) -> list[str]:
        return [login.login for login, check in self._checks if check.get_active()]

    def _sync_checks(self) -> None:
        """The radio follows the user field, like the Mac's filled circle."""
        username = self.username_row.get_text() if hasattr(self, "username_row") else ""
        chosen = next((check for login, check in self._checks if login.login == username), None)
        target = chosen if chosen is not None else getattr(self, "_no_login", None)
        if target is not None and not target.get_active():
            target.set_active(True)

    def _log_in_with_another(self) -> None:
        self.cancel()
        self.window.on_login_requested("github", None)

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
    """AccountsPages.swift:491-580: the value is typed here (new or replacement); Remover
    deletes it from the chaveiro do sistema."""

    def __init__(self, window, key_id):
        existing = window.model.config.api_key(key_id)
        super().__init__(window, "Nova chave de API" if existing is None else "Editar chave de API")
        self.existing = existing
        self.providers = list(APIProvider)
        provider = existing.provider if existing else APIProvider.OPENAI
        group = Adw.PreferencesGroup()
        self.nickname_row = self.entry(group, "nickname", "Apelido", existing.nickname if existing else "",
                                       "Trabalho")
        self.provider_row = widgets.combo("Provedor", [p.display_name for p in self.providers],
                                          self.providers.index(provider))
        self._provider = provider
        self.provider_row.connect("notify::selected", self._provider_changed)
        group.add(self.provider_row)
        self.env_var_row = self.entry(group, "envVar", "Variável de ambiente",
                                      existing.env_var if existing else provider.default_env_var, "OPENAI_API_KEY",
                                      monospace=True)
        self.color_row(group, existing.color if existing else _next_color(len(window.model.config.api_keys)))
        self.content.append(group)
        if existing is None:
            description = "O valor vai direto para o chaveiro do sistema e não aparece mais aqui."
        elif self.model.has_secret(existing):
            description = "Há uma chave guardada no chaveiro do sistema. Deixe em branco para mantê-la."
        else:
            description = "Não há valor no chaveiro do sistema para esta chave."
        self.secret_group = Adw.PreferencesGroup()
        self.secret_row = self.entry(self.secret_group, "secret",
                                     "Cole a chave" if existing is None else "Cole uma nova chave para substituir",
                                     password=True, monospace=True)
        self.secret_footer = self.section(self.secret_group, description)
        if existing is not None:
            self.show_remove(lambda: widgets.confirm(
                self, f"Remover a chave {existing.nickname}?",
                f"O valor é apagado do chaveiro do sistema e os perfis deixam de exportar {existing.env_var}.",
                "Remover", self._remove))
        self.connect("hidden", lambda *_: self.secret_row.set_text(""))
        self.update_save()

    def can_save(self) -> bool:
        return bool(self.nickname_row.get_text() and self.env_var_row.get_text()
                    and (self.existing is not None or self.secret_row.get_text()))

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
