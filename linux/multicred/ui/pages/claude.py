"""Claude: the folders the profiles use and the extra Claude accounts, like the Mac
(`MultiCred/Views/ClaudePages.swift`, D37: a profile owns a Claude *folder* — agents,
skills, conversations, settings — and a Claude *account* is only a credential that can
log any folder in without changing it).

- `ClaudePage` (ClaudePages.swift:10-123): the Mac's header ("Adicionar conta"), the
  section "Pastas dos perfis" first (folder icon, monospaced name, "Perfis: …" or "Pasta
  padrão (nenhum perfil)", "N projetos · M conversas", the folder's status with
  "Entrar…" when it has no login), then "Contas Claude (só credencial)" (Em uso / Sem
  token / "Usar agora", the row's menu) and the note pinned at the bottom.
- `ClaudeAccountDetailPage` (:222-272): the account's data, "Remover conta…", "Entrar de
  novo…" and "Usar agora" (only Claude switches; the profile and its folder stay).
- `ClaudeFolderDetailPage` (:159-218 + ClaudeFolderPreview.swift): the folder's facts
  and recent conversations, "Sair do login da pasta…" (asks first, then `claude auth
  logout`) and "Entrar…".
- `ClaudeFolderLoginPage` (Login/LoginWindowView.swift): "Entrar no Claude" runs `claude
  auth login` with the folder's CLAUDE_CONFIG_DIR in a pseudo-terminal inside the app
  (`LoginMode.FOLDER`); Claude Code writes the login into the folder and MultiCred stores
  nothing. In test mode the simulator runs and the browser never opens.

Texts are the Mac's, with "Keychain" → "chaveiro do sistema" and "neste Mac" → "neste
computador".
"""

from __future__ import annotations

from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ...core.claude.login_flow import ClaudeLoginSession, LoginMode  # noqa: E402
from ...core.models import ClaudeAccountKind  # noqa: E402
from .. import widgets  # noqa: E402
from . import login  # noqa: E402
from .accounts import AccountRow  # noqa: E402
from .login import (  # noqa: E402
    AuthorizationLinkActions, BrowserChoice, TerminalOutput, copy_text, idle, in_navigation_stack, notice,
    page_body, set_pill,
)

SUBTITLE = "Pastas guardam agents, skills e conversas. Contas são só o login usado nelas."
FOLDERS_TITLE = "Pastas dos perfis"
FOLDERS_FOOTER = "Cada perfil usa uma pasta. Trocar a conta não muda a pasta."
ACCOUNTS_TITLE = "Contas Claude (só credencial)"
NO_ACCOUNTS = ("Nenhuma conta extra. Adicione uma para usar o login de outra pessoa ou de outra assinatura sem "
               "trocar de pasta.")
# ClaudePages.swift:57, "Keychain" → "chaveiro do sistema (Secret Service)".
CLAUDE_NOTE = ("O token de cada conta fica só no chaveiro do sistema (Secret Service) e vale nos terminais "
               "(CLAUDE_CODE_OAUTH_TOKEN). Apps gráficos continuam com o login da pasta.")
PREVIEW_CAPTION = "Lido só neste computador: e-mail do login e título, projeto e data de cada conversa."
FOLDER_LOGIN_CAPTION = ("Login da pasta: a conta usada quando o perfil não escolhe uma conta Claude extra (e sempre "
                        "nos apps gráficos).")
# SF Symbols → icon names: folder, lock, xmark.circle, testtube.2.
FOLDER_ICON, LOCK_ICON = "folder-symbolic", "changes-prevent-symbolic"
REMOVED_ICON, TEST_ICON = "process-stop-symbolic", "applications-science-symbolic"
MAX_CONVERSATIONS = 8

_CSS = """
.mc-claude-section { font-size: 13px; font-weight: 600; color: alpha(currentColor, 0.6); margin: 0 6px; }
.mc-claude-caption { font-size: 12px; color: alpha(currentColor, 0.6); }
.mc-claude-tertiary { font-size: 12px; color: alpha(currentColor, 0.45); }
.mc-claude-dim-icon { color: alpha(currentColor, 0.55); }
.mc-claude-title { font-size: 20px; font-weight: 600; }
.mc-claude-headline { font-size: 15px; font-weight: 700; }
.mc-claude-fact-title { font-size: 12px; color: alpha(currentColor, 0.6); }
.mc-claude-fact-value { font-weight: 500; }
.mc-claude-conversations { background: alpha(currentColor, 0.04); border-radius: 8px; padding: 0 10px; }
.mc-claude-conversation { padding: 6px 0; }
.mc-claude-red, button.mc-claude-red label { color: @error_color; }
.mc-avatar-44 { min-width: 44px; min-height: 44px; font-size: 19px; }
"""

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


# MARK: texts shared by the pages


def folder_pill(status) -> tuple[str, str]:
    """ClaudePages.swift:125-132: the login's e-mail, "Logada", "Sem login" or
    "Status indisponível"."""
    if status.kind == "loggedIn":
        return (status.email, "ok") if status.email else ("Logada", "ok")
    if status.kind == "loggedOut":
        return "Sem login", "warning"
    return "Status indisponível", "neutral"


def profiles_using_folder(model, config_dir) -> list[str]:
    return [p.name for p in model.config.profiles if p.claude_config_dir == config_dir] if model.config else []


def profiles_using_account(model, account_id) -> list[str]:
    return [p.name for p in model.config.profiles if p.claude_account_id == account_id] if model.config else []


def relative_date(when: datetime, now: datetime | None = None) -> str:
    """`Date.formatted(.relative(presentation: .named))` in Portuguese: "agora", "há 2 horas",
    "ontem", "semana passada", "há 3 meses"…"""
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - when).total_seconds()))

    def ago(count: int, one: str, many: str) -> str:
        return f"há {count} {one if count == 1 else many}"

    if seconds < 60:
        return "agora"
    if seconds < 3600:
        return ago(seconds // 60, "minuto", "minutos")
    if seconds < 86400:
        return ago(seconds // 3600, "hora", "horas")
    days = seconds // 86400
    if days < 7:
        return "ontem" if days == 1 else ago(days, "dia", "dias")
    if days < 30:
        weeks = days // 7
        return "semana passada" if weeks == 1 else ago(weeks, "semana", "semanas")
    if days < 365:
        months = days // 30
        return "mês passado" if months == 1 else ago(months, "mês", "meses")
    years = days // 365
    return "ano passado" if years == 1 else ago(years, "ano", "anos")


_MONTHS = ("jan.", "fev.", "mar.", "abr.", "mai.", "jun.", "jul.", "ago.", "set.", "out.", "nov.", "dez.")


def abbreviated_date(when: datetime) -> str:
    """`formatted(date: .abbreviated, time: .shortened)` in Portuguese: "19 de out. de 2026, 14:05"."""
    local = when.astimezone()
    return f"{local.day} de {_MONTHS[local.month - 1]} de {local.year}, {local:%H:%M}"


def removal_body(model, account) -> str:
    """ClaudePages.swift:150-152, "Keychain" → "chaveiro do sistema"."""
    used = profiles_using_account(model, account.id)
    prefix = f"Usada em: {', '.join(used)}. Esses perfis voltam a usar o login da própria pasta. " if used else ""
    return prefix + "O token é apagado do chaveiro do sistema. Nenhuma pasta ou conversa é tocada."


def confirm_removal(page, account, on_removed=None) -> None:
    """ClaudeRemovalDialog: "Remover a conta X?" with the Mac's text and a destructive "Remover"."""
    window, model = page.window, page.model

    def remove():
        try:
            model.remove_claude_account(account.id)
        except Exception as error:
            window.show_toast(widgets.describe_error(error))
            return
        if on_removed is not None:
            on_removed()

    widgets.confirm(page, f"Remover a conta {account.nickname}?", removal_body(model, account), "Remover", remove)


def use_now(window, account_id) -> None:
    """"Usar agora": only Claude switches (Mac switchClaudeOnly); the toast says who is in use."""
    window.switch_claude_only(account_id)


def _box(vertical: bool = True, spacing: int = 0, *classes: str, **props) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing,
                  **props)
    for css in classes:
        box.add_css_class(css)
    return box


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        box.remove(child)
        child = box.get_first_child()


def _ellipsized(text: str, *classes: str, width: int = 8) -> Gtk.Label:
    """One line, cut with "…" when there is no room (SwiftUI lineLimit(1)); `width` is the
    natural width in characters."""
    line = widgets.label(text, *classes)
    line.set_ellipsize(Pango.EllipsizeMode.END)
    line.set_max_width_chars(width)
    return line


def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    """The detail pages use the whole width, padded (the Mac's `.padding(20)`), not a clamp."""
    return Gtk.ScrolledWindow(child=child, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)


def _footer(*widgets_: Gtk.Widget) -> Gtk.Box:
    footer = _box(False, 8, "mc-editor-footer")
    for widget in widgets_:
        footer.append(widget)
    return footer


def _spacer() -> Gtk.Box:
    return Gtk.Box(hexpand=True)


# MARK: the root page


class FolderRow(Gtk.ListBoxRow):
    """A folder of the profiles (ClaudePages.swift:68-95)."""

    def __init__(self, title: str, subtitle: str, counts: str | None, status, on_activate, on_login):
        super().__init__(activatable=True)
        self.on_activate = on_activate
        self.title, self.subtitle, self.counts = title, subtitle, counts
        self.status: str | None = None
        self.busy = False
        self.buttons: dict[str, Gtk.Button] = {}
        box = _box(False, 12, "mc-account-row")
        icon = Gtk.Image(icon_name=FOLDER_ICON, pixel_size=20, valign=Gtk.Align.CENTER)
        icon.set_size_request(34, -1)
        icon.add_css_class("mc-claude-dim-icon")
        box.append(icon)
        texts = _box(True, 2, valign=Gtk.Align.CENTER, hexpand=True)
        texts.set_size_request(200, -1)
        texts.append(_ellipsized(title, "heading", "monospace"))
        texts.append(_ellipsized(subtitle, "dim-label"))
        if counts is not None:
            texts.append(_ellipsized(counts, "mc-claude-tertiary"))
        box.append(texts)
        trailing = _box(True, 4, valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        if status is None:
            self.busy = True
            trailing.append(Gtk.Spinner(spinning=True, halign=Gtk.Align.END))
        else:
            text, tone = folder_pill(status)
            self.status = text
            pill = widgets.pill(text, tone)
            pill.set_halign(Gtk.Align.END)
            trailing.append(pill)
            if not status.is_logged_in:
                button = widgets.button("Entrar…", on_login, "mc-small-button")
                button.set_halign(Gtk.Align.END)
                trailing.append(button)
                self.buttons["Entrar…"] = button
        box.append(trailing)
        self.set_child(box)

    def info(self) -> dict:
        return {"title": self.title, "subtitle": self.subtitle, "counts": self.counts, "status": self.status,
                "buttons": list(self.buttons), "busy": self.busy}


def _section_heading(text: str) -> Gtk.Label:
    heading = widgets.label(text, "mc-claude-section")
    heading.set_margin_top(6)
    return heading


class ClaudePage(Adw.NavigationPage):
    """ClaudePages.swift:10-123."""

    def __init__(self, window):
        super().__init__(title="Claude", tag="claude")
        load_css()
        login.load_css()
        self.window, self.model = window, window.model
        self.folder_rows: list[FolderRow] = []
        self.account_rows: list[AccountRow] = []
        self.add_button = widgets.icon_button("Adicionar conta", "list-add-symbolic", self.add_account,
                                              "suggested-action")
        self._actions = [self.add_button]
        header, self._header_title, self._header_subtitle = widgets.page_header("Claude", SUBTITLE, self._actions)

        body = _box(True, 8, margin_top=16, margin_bottom=16, margin_start=20, margin_end=20)
        self.folders_heading = _section_heading(FOLDERS_TITLE)
        self.folder_list = self._list()
        self.folder_footer = widgets.label(FOLDERS_FOOTER, "mc-claude-caption", wrap=True)
        self.folder_footer.set_margin_start(6)
        self.accounts_heading = _section_heading(ACCOUNTS_TITLE)
        self.accounts_heading.set_margin_top(18)
        self.account_list = self._list()
        self.empty_accounts = widgets.label(NO_ACCOUNTS, "dim-label", wrap=True)
        self.empty_accounts.set_margin_start(6)
        for widget in (self.folders_heading, self.folder_list, self.folder_footer, self.accounts_heading,
                       self.account_list, self.empty_accounts):
            body.append(widget)
        scroller = Gtk.ScrolledWindow(child=body, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

        self.note = widgets.info_note(CLAUDE_NOTE, LOCK_ICON)
        for side in ("start", "end", "bottom"):
            getattr(self.note, f"set_margin_{side}")(20)
        self.note.set_margin_top(4)

        content = _box(True)
        content.append(header)
        content.append(Gtk.Separator())
        content.append(scroller)
        content.append(self.note)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(content)
        self.set_child(view)
        self.connect("showing", lambda *_: self._showing())
        self.refresh()

    def _list(self) -> Gtk.ListBox:
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        listbox.add_css_class("boxed-list")
        listbox.connect("row-activated", lambda _list, row: row.on_activate and row.on_activate())
        return listbox

    def _showing(self) -> None:
        """The Mac's `.task`: statuses and folder summaries read again (cached, off the UI thread)."""
        self.refresh()
        if self.model.config is not None:
            self.model.refresh_claude_statuses()
            self.model.load_folder_summaries()

    # what tests and the window read

    def header_texts(self) -> tuple[str, str]:
        return self._header_title.get_label(), self._header_subtitle.get_label()

    def action_labels(self) -> list[str]:
        return [widgets.button_text(button) for button in self._actions]

    def section_titles(self) -> list[str]:
        return [self.folders_heading.get_label(), self.accounts_heading.get_label()]

    def folder_footer_text(self) -> str:
        return self.folder_footer.get_label()

    def empty_accounts_visible(self) -> bool:
        return self.empty_accounts.get_visible()

    def empty_accounts_text(self) -> str:
        return self.empty_accounts.get_label()

    def note_text(self) -> str:
        return self.note.text_label.get_label()

    def note_icon(self) -> str:
        return self.note.icon_name

    # building

    def refresh(self) -> None:
        config = self.model.config
        if config is None:
            return
        for row in self.folder_rows:
            self.folder_list.remove(row)
        for row in self.account_rows:
            self.account_list.remove(row)
        self.folder_rows = [self._folder_row(config_dir) for config_dir in self.model.claude_folders_in_use]
        for row in self.folder_rows:
            self.folder_list.append(row)
        self.account_rows = [self._account_row(account) for account in config.claude_accounts]
        for row in self.account_rows:
            self.account_list.append(row)
        self.account_list.set_visible(bool(self.account_rows))
        self.empty_accounts.set_visible(not self.account_rows)

    def _folder_row(self, config_dir) -> FolderRow:
        profiles = profiles_using_folder(self.model, config_dir)
        summary = self.model.folder_summaries.get(config_dir)
        counts = (f"{summary.project_count} projetos · {summary.conversation_count} conversas"
                  if summary is not None else None)
        return FolderRow(self.model.folder_name(config_dir),
                         f"Perfis: {', '.join(profiles)}" if profiles else "Pasta padrão (nenhum perfil)",
                         counts, self.model.folder_status(config_dir),
                         on_activate=lambda d=config_dir: self.window.push(ClaudeFolderDetailPage(self.window, d)),
                         on_login=lambda d=config_dir: self.window.on_login_requested("folder", d))

    def _account_row(self, account) -> AccountRow:
        """ClaudePages.swift:97-118, with the context menu of :45-50."""
        model = self.model
        has_token = model.has_token(account)
        if account.kind == ClaudeAccountKind.LOGIN:
            detail = "login completo · troca até nas conversas abertas"
        else:
            detail = "token · só terminais novos" if has_token else "sem token"
        used = profiles_using_account(model, account.id)
        row = AccountRow(account.nickname, account.color, account.nickname, account.email or "conta Claude",
                         ", ".join(used) or "—", detail,
                         on_activate=lambda a=account: self.open_account(a.id),
                         menu=(("Usar agora no perfil ativo", lambda a=account: use_now(self.window, a.id)),
                               ("Entrar de novo…", lambda a=account: self.window.on_login_requested("login", a)),
                               None,
                               ("Remover conta…", lambda a=account: confirm_removal(self, a))))
        if model.state.applied_claude_account_id == account.id:
            row.set_status("Em uso", "ok")
        elif not has_token:
            row.set_status("Sem token", "error")
        else:
            button = row.add_button("Usar agora", lambda a=account: use_now(self.window, a.id))
            button.set_sensitive(model.active_profile is not None and not model.switching)
        return row

    def add_account(self) -> None:
        self.window.on_login_requested("add", None)

    def open_account(self, account_id) -> Adw.NavigationPage:
        return self.window.push(ClaudeAccountDetailPage(self.window, account_id))


# MARK: account detail


class ClaudeAccountDetailPage(Adw.NavigationPage):
    """ClaudePages.swift:222-272; "Conta removida" once the account is gone."""

    def __init__(self, window, account_id):
        account = window.model.config.claude_account(account_id) if window.model.config else None
        super().__init__(title=account.nickname if account is not None else "Conta removida")
        load_css()
        self.window, self.model, self.account_id = window, window.model, account_id

        detail = _box(True, 16, margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)
        top = _box(False, 12)
        self.avatar = widgets.account_avatar("?", account.color if account else _graphite(), 44)
        names = _box(True, 2, valign=Gtk.Align.CENTER, hexpand=True)
        self.name_label = widgets.label("", "mc-claude-title")
        self.email_label = widgets.label("", "dim-label")
        names.append(self.name_label)
        names.append(self.email_label)
        self.status_pill = widgets.pill("", "ok")
        for widget in (self.avatar, names, self.status_pill):
            top.append(widget)
        detail.append(top)
        self.used_label = widgets.label("", "dim-label", wrap=True)
        self.use_now_label = widgets.label("", wrap=True)
        detail.append(self.used_label)
        detail.append(self.use_now_label)

        self.removed_state = Adw.StatusPage(title="Conta removida", icon_name=REMOVED_ICON, vexpand=True)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(_scrolled(detail), "detail")
        self.stack.add_named(self.removed_state, "removed")

        # Mac: a bordered button with red text (role: .destructive, .foregroundStyle(.red)).
        self.remove_button = widgets.button("Remover conta…", self._remove, "mc-claude-red")
        self.again_button = widgets.button("Entrar de novo…", self._again)
        self.use_now_button = widgets.button("Usar agora", self._use_now, "suggested-action")
        self.footer = _footer(self.remove_button, self.again_button, _spacer(), self.use_now_button)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.stack)
        view.add_bottom_bar(self.footer)
        self.set_child(view)
        self.refresh()

    @property
    def account(self):
        return self.model.config.claude_account(self.account_id) if self.model.config else None

    def refresh(self) -> None:
        account, model = self.account, self.model
        self.stack.set_visible_child_name("detail" if account is not None else "removed")
        self.footer.set_visible(account is not None)
        if account is None:
            self.set_title("Conta removida")
            return
        self.set_title(account.nickname)
        widgets.set_avatar(self.avatar, account.nickname[:1], account.color)
        self.name_label.set_label(account.nickname)
        self.email_label.set_label(account.email or "e-mail não informado")
        has_token = model.has_token(account)
        in_use = model.state.applied_claude_account_id == account.id
        if in_use:
            set_pill(self.status_pill, "Em uso", "ok")
        else:
            text = "Login salvo" if account.kind == ClaudeAccountKind.LOGIN else ("Token salvo" if has_token
                                                                                 else "Sem token")
            set_pill(self.status_pill, text, "ok" if has_token else "error")
        used = profiles_using_account(model, account.id)
        self.used_label.set_label(f"Padrão nos perfis: {', '.join(used)}." if used
                                  else "Nenhum perfil usa esta conta por padrão.")
        profile = model.active_profile
        self.use_now_label.set_visible(profile is not None)
        if profile is not None:
            self.use_now_label.set_label(
                f"\"Usar agora\" mantém o perfil {profile.name} e a pasta "
                f"{model.folder_name(profile.claude_config_dir)} (agents, skills, conversas). Só o login muda, até a "
                "próxima troca de perfil.")
        self.use_now_button.set_sensitive(profile is not None and not in_use and not model.switching and has_token)

    # what tests read

    def texts(self) -> dict:
        return {"name": self.name_label.get_label(), "email": self.email_label.get_label(),
                "status": self.status_pill.get_label(), "used": self.used_label.get_label(),
                "use_now": self.use_now_label.get_label() if self.use_now_label.get_visible() else None}

    def button_states(self) -> dict[str, bool]:
        return {button.get_label(): button.get_sensitive()
                for button in (self.remove_button, self.again_button, self.use_now_button)}

    def showing_removed(self) -> bool:
        return self.stack.get_visible_child_name() == "removed"

    # actions

    def _remove(self) -> None:
        account = self.account
        if account is not None:
            confirm_removal(self, account, on_removed=self._back)

    def _back(self) -> None:
        if self.window.nav.get_visible_page() is self:
            self.window.nav.pop()

    def _again(self) -> None:
        account = self.account
        if account is not None:
            self.window.on_login_requested("login", account)

    def _use_now(self) -> None:
        if self.account is not None:
            use_now(self.window, self.account_id)


def _graphite():
    from ...core.models import ProfileColor
    return ProfileColor.GRAPHITE


# MARK: folder detail


class ClaudeFolderDetailPage(Adw.NavigationPage):
    """ClaudePages.swift:159-218 with the preview of ClaudeFolderPreview.swift."""

    def __init__(self, window, config_dir):
        super().__init__(title=window.model.folder_name(config_dir))
        load_css()
        self.window, self.model, self.config_dir = window, window.model, config_dir
        self.conversation_rows: list[tuple[str, str | None, str]] = []
        self._facts: list[tuple[str, str]] = []
        self.empty_label: Gtk.Label | None = None
        self.preview_caption: Gtk.Label | None = None

        body = _box(True, 16, margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)
        top = _box(False, 12)
        icon = Gtk.Image(icon_name=FOLDER_ICON, pixel_size=32, valign=Gtk.Align.CENTER)
        icon.add_css_class("mc-claude-dim-icon")
        names = _box(True, 2, valign=Gtk.Align.CENTER, hexpand=True)
        self.name_label = widgets.label(self.model.folder_name(config_dir), "mc-claude-title", "monospace")
        self.profiles_label = widgets.label("", "dim-label", wrap=True)
        names.append(self.name_label)
        names.append(self.profiles_label)
        self.status_pill = widgets.pill("", "ok")
        for widget in (icon, names, self.status_pill):
            top.append(widget)
        body.append(top)

        self.preview = _box(True, 10)
        self.loading = _box(False, 8)
        self.loading.append(Gtk.Spinner(spinning=True))
        self.loading.append(widgets.label("Lendo a pasta…", "dim-label"))
        body.append(self.preview)
        body.append(self.loading)
        self.login_caption = widgets.label(FOLDER_LOGIN_CAPTION, "mc-claude-caption", wrap=True)
        body.append(self.login_caption)

        self.logout_button = widgets.button("Sair do login da pasta…", self.confirm_logout)
        self.login_button = widgets.button("Entrar…", self._log_in, "suggested-action")
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(_scrolled(body))
        view.add_bottom_bar(_footer(self.logout_button, _spacer(), self.login_button))
        self.set_child(view)
        self.connect("showing", lambda *_: self._showing())
        self.refresh()

    def _showing(self) -> None:
        """The Mac's `.task`: the folder's summary and status, read off the UI thread."""
        self.model.load_folder_summary(self.config_dir)
        self.model.refresh_folder_status(self.config_dir, force=False)

    def refresh(self) -> None:
        model = self.model
        used = profiles_using_folder(model, self.config_dir)
        self.profiles_label.set_label(f"Perfis: {', '.join(used)}" if used else "Nenhum perfil usa esta pasta.")
        status = model.folder_status(self.config_dir)
        self.status_pill.set_visible(status is not None)
        if status is not None:
            set_pill(self.status_pill, *folder_pill(status))
        self.logout_button.set_sensitive(status is not None and status.is_logged_in)
        self._build_preview(model.folder_summaries.get(self.config_dir))

    def _build_preview(self, summary) -> None:
        _clear(self.preview)
        self.conversation_rows, self._facts = [], []
        self.empty_label = self.preview_caption = None
        self.preview.set_visible(summary is not None)
        self.loading.set_visible(summary is None)
        if summary is None:
            return
        account = summary.email
        if account and summary.organization:
            account = f"{account} · {summary.organization}"
        self._facts = [("Conta", account or "sem login"), ("Projetos", str(summary.project_count)),
                       ("Conversas", str(summary.conversation_count)),
                       ("Última atividade", relative_date(summary.last_activity) if summary.last_activity
                        else "nunca")]
        facts = _box(False, 16)
        for title, value in self._facts:
            fact = _box(True, 2)
            fact.append(widgets.label(title, "mc-claude-fact-title"))
            fact.append(_ellipsized(value, "mc-claude-fact-value", width=40))
            facts.append(fact)
        self.preview.append(facts)
        conversations = summary.recent_conversations[:MAX_CONVERSATIONS]
        if not conversations:
            self.empty_label = widgets.label("Nenhuma conversa nesta pasta.", "dim-label")
            self.preview.append(self.empty_label)
        else:
            box = _box(True, 0, "mc-claude-conversations")
            for index, conversation in enumerate(conversations):
                if index > 0:
                    box.append(Gtk.Separator())
                when = abbreviated_date(conversation.last_activity)
                self.conversation_rows.append((conversation.title, conversation.project, when))
                line = _box(False, 8, "mc-claude-conversation")
                texts = _box(True, 2, hexpand=True)
                texts.append(_ellipsized(conversation.title))
                if conversation.project:
                    texts.append(_ellipsized(conversation.project, "monospace", "mc-claude-caption"))
                line.append(texts)
                date = widgets.label(when, "mc-claude-caption")
                date.set_valign(Gtk.Align.START)
                line.append(date)
                box.append(line)
            self.preview.append(box)
        self.preview_caption = widgets.label(PREVIEW_CAPTION, "mc-claude-tertiary", wrap=True)
        self.preview.append(self.preview_caption)

    # what tests read

    def header_info(self) -> dict:
        return {"name": self.name_label.get_label(), "profiles": self.profiles_label.get_label(),
                "status": self.status_pill.get_label() if self.status_pill.get_visible() else None}

    def facts(self) -> list[tuple[str, str]]:
        return list(self._facts)

    def conversation_titles(self) -> list[str]:
        return [title for title, _project, _when in self.conversation_rows]

    def empty_conversations_text(self) -> str | None:
        return self.empty_label.get_label() if self.empty_label is not None else None

    def captions(self) -> list[str]:
        texts = [self.preview_caption.get_label()] if self.preview_caption is not None else []
        return texts + [self.login_caption.get_label()]

    def button_states(self) -> dict[str, bool]:
        return {button.get_label(): button.get_sensitive() for button in (self.logout_button, self.login_button)}

    # actions

    def _log_in(self) -> None:
        self.window.on_login_requested("folder", self.config_dir)

    def confirm_logout(self) -> Adw.AlertDialog:
        """ClaudePages.swift:204-216: the default folder's logout is destructive."""
        default = self.config_dir is None
        name = self.model.folder_name(self.config_dir)
        if default:
            heading = "Sair do seu login principal do Claude Code (~/.claude)?"
            body = ("~/.claude é a pasta padrão do Claude Code. O login dela será apagado e será preciso entrar de "
                    "novo. Conversas, agents e skills ficam.")
        else:
            heading = f"Sair do login de {name}?"
            body = ("O Claude Code apaga o login desta pasta. Conversas, agents e skills ficam; para usar de novo, "
                    "entre outra vez.")
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("confirm", "Sair")
        if default:
            dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "confirm" and self.log_out())
        dialog.present(self)
        return dialog

    def log_out(self) -> None:
        def done(error):
            if error is not None:
                self.window.show_toast(widgets.describe_error(error))

        self.model.logout_claude_folder(self.config_dir, done)


# MARK: the folder's own login


class ClaudeFolderLoginPage(Adw.NavigationPage):
    """"Entrar no Claude" (LoginWindowView.swift:6-177): `claude auth login` with the folder's
    CLAUDE_CONFIG_DIR, its output collapsible; Concluir goes back to the list, Voltar and
    Cancelar one page back. Leaving the page stops the CLI."""

    PHASES = {"starting": ("Aguardando navegador", "warning"), "waitingForBrowser": ("Aguardando navegador", "warning"),
              "waitingForCode": ("Aguardando código", "warning"), "connecting": ("Conectando", "warning"),
              "connected": ("Conectada", "ok"), "failed": ("Erro", "error")}
    TEST_NOTICE = ("Simulação do modo de teste: o navegador não abre. Digite qualquer código para conectar, ou "
                   "\"erro\" para ver a tela de erro.")

    def __init__(self, window, config_dir):
        super().__init__(title="Entrar no Claude")
        load_css()
        login.load_css()
        self.window, self.model, self.config_dir = window, window.model, config_dir
        self.session: ClaudeLoginSession | None = None
        self.show_output = False
        self._opened_url: str | None = None
        self._left = False
        self._build()
        self.connect("hidden", lambda _page: GLib.idle_add(self._check_left))
        GLib.idle_add(self._start)

    @property
    def simulated(self) -> bool:
        """Test mode runs the simulator (D30): its link is fake and never opened."""
        return self.model.environment.mode != "real"

    @property
    def folder(self) -> str:
        return self.model.folder_name(self.config_dir)

    def _build(self) -> None:
        body = _box(True, 14, "mc-login-page", margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)

        header = _box(False, 12)
        icon = Gtk.Image(icon_name=FOLDER_ICON, pixel_size=24, valign=Gtk.Align.CENTER)
        icon.set_size_request(40, -1)
        icon.add_css_class("mc-claude-dim-icon")
        titles = _box(True, 2, valign=Gtk.Align.CENTER, hexpand=True)
        self.title_label = widgets.label(f"Login da pasta {self.folder}", "mc-claude-headline")
        used = profiles_using_folder(self.model, self.config_dir)
        self.profiles_label = widgets.label(f"Perfis: {', '.join(used) if used else 'nenhum'}", "dim-label")
        titles.append(self.title_label)
        titles.append(self.profiles_label)
        self.status_pill = widgets.pill("Aguardando navegador", "warning")
        for widget in (icon, titles, self.status_pill):
            header.append(widget)
        body.append(header)

        self.test_notice, self.test_notice_label = notice(self.TEST_NOTICE, TEST_ICON, "mc-orange")
        self.test_notice.set_visible(self.simulated)
        body.append(self.test_notice)
        self.message_label = widgets.label("", "dim-label", wrap=True)
        body.append(self.message_label)

        directory = self.model.environment.abbreviate(self.config_dir) if self.config_dir else "~/.claude"
        self.terminal = TerminalOutput(directory,
                                       "claude auth login (simulado)" if self.simulated else "claude auth login")
        body.append(self.terminal)

        self.stored_notice, self.stored_label = notice(
            "Credenciais guardadas pelo Claude Code. O MultiCred não as armazena.", LOCK_ICON, "mc-caption")
        body.append(self.stored_notice)

        self.browser_line = _box(False, 8)
        self.browser_label = login.caption()
        self.browser_label.set_hexpand(True)
        self.link_actions = AuthorizationLinkActions()
        self.link_actions.set_valign(Gtk.Align.CENTER)
        self.browser_line.append(self.browser_label)
        self.browser_line.append(self.link_actions)
        body.append(self.browser_line)

        self.code_row = _box(False, 8)
        code_label = widgets.label("Código", "dim-label")
        code_label.set_valign(Gtk.Align.CENTER)
        self._code_label = code_label
        self.code_entry = Gtk.Entry(placeholder_text="Cole o código de autorização", visibility=False, hexpand=True,
                                    input_purpose=Gtk.InputPurpose.PASSWORD)
        self.code_entry.add_css_class("monospace")
        self.code_entry.connect("changed", lambda _e: self._refresh())
        self.code_entry.connect("activate", lambda _e: self._code_action())
        self.connect_button = widgets.button("Conectar", self._code_action, "suggested-action")
        for widget in (code_label, self.code_entry, self.connect_button):
            self.code_row.append(widget)
        body.append(self.code_row)
        body.append(Gtk.Box(vexpand=True))

        self.output_toggle = Gtk.Button()
        self.output_toggle.add_css_class("flat")
        self.output_toggle.add_css_class("dim-label")
        toggle = _box(False, 6)
        self.output_toggle_icon = Gtk.Image(icon_name="pan-end-symbolic")
        self.output_toggle_label = widgets.label("Mostrar saída")
        toggle.append(self.output_toggle_icon)
        toggle.append(self.output_toggle_label)
        self.output_toggle.set_child(toggle)
        self.output_toggle.connect("clicked", lambda _b: self._toggle_output())
        self.copy_log_button = widgets.button("Copiar log", self._copy_log, "flat")
        self.copy_log_button.add_css_class("accent")
        self.done_button = widgets.button("Concluir", self.window.pop_to_section_root, "suggested-action")
        self.back_button = widgets.button("Voltar", self._back)
        self.cancel_button = widgets.button("Cancelar", self._cancel)
        self.footer = _footer(self.output_toggle, self.copy_log_button, _spacer(), self.back_button,
                              self.cancel_button, self.done_button)
        body.append(self.footer)
        self.set_child(page_body(Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER)))
        self._refresh()

    # MARK: session

    def _start(self) -> bool:
        if self._left:
            return False
        self._opened_url = None
        self.code_entry.set_text("")
        self.session = ClaudeLoginSession(self.model.environment, self.model.tools, LoginMode.FOLDER,
                                          self.config_dir, None, dispatch=idle, on_change=self._changed,
                                          on_finished=self._finished, log=getattr(self.model, "log", None))
        self.session.start()
        self._refresh()
        return False

    def _changed(self, session) -> None:
        if session is not self.session or self._left:
            return
        url = session.authorization_url
        if url and url != self._opened_url and not self.simulated:
            # The CLI does not open a browser (BROWSER is a no-op): open it here, once per link.
            self._opened_url = url
            BrowserChoice.open(url, BrowserChoice.current)
        self._refresh()

    def _finished(self, session) -> None:
        if session is not self.session or self._left:
            return
        self._refresh()
        if session.phase.kind == "connected":
            # Mac: the session's onFinished → refreshFolderStatus.
            self.model.refresh_folder_status(self.config_dir)
            self.model.load_folder_summary(self.config_dir)

    def _check_left(self) -> bool:
        """After a pop or a section change: stop the CLI (Mac: stopSessionsNoLongerShown)."""
        if self._left or in_navigation_stack(self):
            return False
        self._left = True
        self._stop()
        return False

    def _stop(self) -> None:
        session = self.session
        if session is None:
            return
        if not session.phase.is_finished:
            session.cancel()
        session.stop()

    # MARK: actions

    def _code_action(self) -> None:
        session = self.session
        if session is not None and session.phase.is_failure:
            self.retry()
            return
        code = self.code_entry.get_text().strip()
        if session is None or not code or not session.phase.accepts_code:
            return
        session.submit_code(code)
        self.code_entry.set_text("")
        self._refresh()

    def retry(self) -> None:
        """"Tentar de novo": a fresh attempt (Mac: showClaudeFolderLogin replacing the page)."""
        self._stop()
        self._start()

    def _toggle_output(self) -> None:
        self.show_output = not self.show_output
        self._refresh()

    def _copy_log(self) -> None:
        if self.session is not None:
            copy_text(self.session.redacted_log)

    def _back(self) -> None:
        self.window.nav.pop()

    def _cancel(self) -> None:
        if self.session is not None:
            self.session.cancel()
        self.window.nav.pop()

    # MARK: state → widgets

    def _message(self) -> str:
        session = self.session
        kind = session.phase.kind if session is not None else "starting"
        if kind == "waitingForCode":
            return ("Autorize no navegador. Se ele mostrar um código, cole abaixo; se a página disser que deu certo, "
                    "é só aguardar.")
        if kind == "connecting":
            return "Conectando ao Claude…"
        if kind == "connected":
            return (f"Pronto. Os perfis que usam {self.folder} entram com esta conta quando não escolhem outra conta "
                    "Claude.")
        if kind == "failed":
            return session.launch_error or (f"Não deu certo ({session.phase.message}). O código vale por poucos "
                                            "minutos: gere outro no navegador e tente de novo.")
        return ("O navegador vai abrir. A conta em que você entrar lá vira o login desta pasta. Se o navegador já "
                "estiver logado em outra conta Claude, saia dela antes ou abra o link numa janela anônima.")

    def _refresh(self) -> None:
        session = self.session
        phase = session.phase if session is not None else None
        kind = phase.kind if phase is not None else "starting"
        failed, connected = kind == "failed", kind == "connected"
        finished = failed or connected
        set_pill(self.status_pill, *self.PHASES.get(kind, self.PHASES["starting"]))
        self.message_label.set_label(self._message())
        self.message_label.remove_css_class("mc-red" if not failed else "dim-label")
        self.message_label.add_css_class("mc-red" if failed else "dim-label")

        self.terminal.set_visible(self.show_output or failed)
        if session is not None:
            self.terminal.set_output(session.output)
        self.stored_notice.set_visible(connected)
        self.browser_line.set_visible(not connected and not finished and not self.simulated)
        self.browser_label.set_label(
            f"Abriu em {BrowserChoice.label(BrowserChoice.current)}. Navegador errado ou outra conta logada? Copie "
            "o link e cole em outro navegador ou numa janela anônima.")
        self.link_actions.set_url(session.authorization_url if session is not None else None)

        self.code_row.set_visible(not connected)
        self.code_entry.set_sensitive(not finished)
        self.connect_button.set_label("Tentar de novo" if failed else "Conectar")
        self.connect_button.set_sensitive(failed or (phase is not None and phase.accepts_code
                                                     and bool(self.code_entry.get_text().strip())))

        self.output_toggle.set_visible(not failed)
        self.output_toggle_label.set_label("Ocultar saída" if self.show_output else "Mostrar saída")
        self.output_toggle_icon.set_from_icon_name("pan-down-symbolic" if self.show_output else "pan-end-symbolic")
        self.copy_log_button.set_visible(failed)
        self.done_button.set_visible(connected)
        self.back_button.set_visible(failed)
        self.cancel_button.set_visible(not finished)

    def refresh(self) -> None:
        """Model changes (window.refresh): only the profiles' names can change here."""
        used = profiles_using_folder(self.model, self.config_dir)
        self.profiles_label.set_label(f"Perfis: {', '.join(used) if used else 'nenhum'}")

    # what tests read

    def header_info(self) -> dict:
        return {"title": self.title_label.get_label(), "profiles": self.profiles_label.get_label(),
                "status": self.status_pill.get_label()}

    def test_notice_text(self) -> str | None:
        return self.test_notice_label.get_label() if self.test_notice.get_visible() else None

    def message_text(self) -> str:
        return self.message_label.get_label()

    def stored_notice_text(self) -> str | None:
        return self.stored_label.get_label() if self.stored_notice.get_visible() else None

    def code_label_text(self) -> str:
        return self._code_label.get_label()

    def footer_labels(self) -> list[str]:
        labels, child = [], self.footer.get_first_child()
        while child is not None:
            if child.get_visible() and isinstance(child, Gtk.Button):
                labels.append(self.output_toggle_label.get_label() if child is self.output_toggle
                              else child.get_label())
            child = child.get_next_sibling()
        return labels
