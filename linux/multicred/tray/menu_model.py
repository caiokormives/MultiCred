"""The tray menu as a plain tree: the macOS menu-bar menu (AppDelegate.swift:71-194).

Pure: no D-Bus, no drawing. dbusmenu items are one line of text with an optional
icon, check mark and shortcut; there are no subtitles, section headers or custom
views. So the Mac's SwiftUI header (MenuHeaderView.swift) becomes disabled lines
with icons, section headers become disabled lines, and a subtitle joins its title
after " · " (as the Mac itself does before macOS 14.4, AppDelegate.swift:112).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Callable

from ..core.models import ActiveState, Config, Profile

TEST_MODE_LINE = "MODO DE TESTE · nada no sistema real muda"
CLAUDE_SUBMENU = "Trocar só a conta Claude"
QUIT = "Sair do MultiCred"
FIX = "Corrigir…"
NEEDS_CLAUDE_LOGIN = "⚠︎ Claude precisa entrar"

# SF Symbols → icon names. branch, </> and sparkle do not exist in Adwaita: they are
# the app's own SVGs, found through the menu's IconThemePath (dbusmenu.py).
ICON_BRANCH = "multicred-branch-symbolic"      # arrow.triangle.branch
ICON_CODE = "multicred-code-symbolic"          # chevron.left.forwardslash.chevron.right
ICON_SPARKLE = "multicred-sparkle-symbolic"    # sparkle
ICON_WARNING = "dialog-warning-symbolic"       # exclamationmark.triangle
ICON_PLUS = "list-add-symbolic"                # plus
ICON_FOLDER = "folder-symbolic"                # folder
ICON_VERIFY = "security-high-symbolic"         # checkmark.shield
ICON_ACCOUNTS = "avatar-default-symbolic"      # person
ICON_SETTINGS = "emblem-system-symbolic"       # gearshape
ICON_SETUP = "system-run-symbolic"             # wand.and.stars
# The status pill (MenuHeaderView.swift:59-64) is a colored capsule on the Mac.
STATUS_ICONS = {"Tudo certo": "emblem-ok-symbolic", "Inconsistente": ICON_WARNING,
                "Verificando": "emblem-synchronizing-symbolic", "Não verificado": "dialog-question-symbolic"}


@dataclass
class MenuItem:
    """One dbusmenu item. `action` is what a click asks for, e.g. ("profile", uuid)."""

    label: str = ""
    action: tuple | None = None
    enabled: bool = True
    separator: bool = False
    toggle: str | None = None  # "checkmark" (the Mac's ✓) or "radio"
    toggled: bool = False
    icon_png: bytes | None = None
    icon_name: str | None = None
    # Keys of one shortcut in dbusmenu terms, e.g. ("Control", "r"). Shown by some panels
    # only; it is never a global shortcut.
    shortcut: tuple[str, ...] | None = None
    children: list["MenuItem"] = field(default_factory=list)


def _text(label: str, icon_name: str | None = None, icon_png: bytes | None = None) -> MenuItem:
    return MenuItem(label=label, enabled=False, icon_name=icon_name, icon_png=icon_png)


def _separator() -> MenuItem:
    return MenuItem(separator=True)


def _default_folder_name(config_dir: str | None) -> str:
    return config_dir if config_dir is not None else "~/.claude"


def _ctrl(key: str) -> tuple[str, str]:
    return ("Control", key)


def build_menu(config: Config | None, state: ActiveState, *, inconsistent: bool = False, switching: bool = False,
               test_mode: bool = False, tz: tzinfo | None = None,
               icon: Callable[[Profile], bytes] | None = None,
               verification=None, verifying: bool = False,
               folder_status: Callable[[str | None], object] | None = None,
               has_token: Callable[[object], bool] | None = None,
               folder_name: Callable[[str | None], str] | None = None,
               account_icon: Callable[[object], bytes] | None = None) -> list[MenuItem]:
    """Top-level items. `config` None = not configured yet (only "Configurar" and "Sair").

    `verification` is the last Verificar report (None = never ran); `inconsistent` is the
    Linux half-switched state (a rollback failed), shown like a problem. `folder_status`
    gives a folder's ClaudeLoginStatus (None = not known yet, never "logged out"),
    `has_token` whether an account can be used, `folder_name` the folder as shown
    (~ abbreviated). `tz` None formats "Ativo desde" in the local time zone.
    """
    facts = _Facts(config, state, folder_status or (lambda _d: None), has_token or (lambda _a: True),
                   folder_name or _default_folder_name)
    items: list[MenuItem] = []
    if test_mode:
        items += [_text(TEST_MODE_LINE), _separator()]
    if config is None:
        return items + [MenuItem("Configurar MultiCred…", ("open", "onboarding"), icon_name=ICON_SETUP),
                        _separator(), _quit()]

    profile = config.profile(state.profile_id)
    problems = list(verification.problems) if verification is not None else []
    items += _header(facts, profile, tz, icon, _status(verification, problems, inconsistent, verifying))
    if problems:
        items.append(_text(problems[0].title, ICON_WARNING))
        if len(problems) > 1:
            items.append(_text(f"e mais {len(problems) - 1}"))
    if problems or inconsistent:
        items.append(MenuItem(FIX, ("open", "verify"), icon_name=ICON_WARNING))
    items.append(_separator())

    items.append(_text("Perfis"))
    for index, each in enumerate(config.profiles):
        label = each.name + (f"  {NEEDS_CLAUDE_LOGIN}" if facts.needs_claude_login(each) else "")
        items.append(MenuItem(label, ("profile", each.id), enabled=not switching, toggle="checkmark",
                              toggled=each.id == state.profile_id, icon_png=icon(each) if icon else None,
                              shortcut=_ctrl(str(index + 1)) if index < 9 else None))
    if not config.profiles:
        items.append(MenuItem("Criar um perfil…", ("open", "new-profile"), icon_name=ICON_PLUS))
    items.append(_separator())

    items.append(MenuItem(CLAUDE_SUBMENU, icon_name=ICON_SPARKLE,
                          children=_claude_submenu(facts, profile, switching, account_icon)))
    items.append(_separator())
    items += [
        MenuItem("Verificar…", ("open", "verify"), icon_name=ICON_VERIFY, shortcut=_ctrl("r")),
        MenuItem("Contas…", ("open", "claude"), icon_name=ICON_ACCOUNTS),
        MenuItem("Ajustes…", ("open", "settings"), icon_name=ICON_SETTINGS, shortcut=_ctrl("comma")),
        # Linux only: some panels open the menu on a click and never send Activate.
        MenuItem("Abrir MultiCred", ("open", "main")),
        _separator(),
        _quit(),
    ]
    return items


def _quit() -> MenuItem:
    return MenuItem(QUIT, ("quit", None), shortcut=_ctrl("q"))


@dataclass
class _Facts:
    config: Config
    state: ActiveState
    folder_status: Callable
    has_token: Callable
    folder_name: Callable

    def status_kind(self, config_dir: str | None) -> str | None:
        status = self.folder_status(config_dir)
        return status.kind if status is not None else None

    def needs_claude_login(self, profile: Profile) -> bool:
        """AppModel.needs_claude_login: a missing token, or a folder known to be logged out."""
        account = self.config.claude_account(profile.claude_account_id)
        if account is not None:
            return not self.has_token(account)
        return self.status_kind(profile.claude_config_dir) == "loggedOut"


def _status(verification, problems, inconsistent: bool, verifying: bool) -> str:
    """The header pill (MenuHeaderView.swift:59-64, AppModel.consistency)."""
    if problems or inconsistent:
        return "Inconsistente"
    if verification is not None:
        return "Tudo certo"
    return "Verificando" if verifying else "Não verificado"


def _header(facts: _Facts, profile, tz, icon, status: str) -> list[MenuItem]:
    """MenuHeaderView.swift:13-39 as lines."""
    if profile is None:
        return [_text("Nenhum perfil ativo"), _text("Escolha um perfil abaixo.")]
    config, state = facts.config, facts.state
    title = profile.name
    if state.activated_at is not None:
        title += f" · Ativo desde {state.activated_at.astimezone(tz).strftime('%H:%M')}"
    lines = [_text(title, icon_png=icon(profile) if icon else None), _text(status, STATUS_ICONS[status])]
    git = config.git_identity(profile.git_identity_id)
    if git is not None:
        lines.append(_text(git.email, ICON_BRANCH))
    github = config.github_account(profile.github_account_id)
    if github is not None:
        lines.append(_text(github.username, ICON_CODE))
    claude = (f"Claude · {facts.folder_name(profile.claude_config_dir)} · "
              f"{_credential_description(facts, profile)}")
    if state.claude_override_id is not None:
        claude += " (só Claude)"
    lines.append(_text(claude, ICON_SPARKLE))
    return lines


def _credential_description(facts: _Facts, profile) -> str:
    """AppModel.swift:102-106 claudeCredentialDescription."""
    applied = facts.config.claude_account(facts.state.applied_claude_account_id)
    if applied is not None:
        return applied.nickname
    status = facts.folder_status(profile.claude_config_dir)
    if status is not None and status.kind == "loggedIn" and status.email:
        return f"login da pasta ({status.email})"
    return "login da pasta"


def _claude_submenu(facts: _Facts, profile, switching: bool, account_icon) -> list[MenuItem]:
    """AppDelegate.swift:142-194 (texts as in the CLI's `multicred claude`)."""
    if profile is None:
        return [_text("Conta Claude"), _text("Ative um perfil primeiro")]
    config, state = facts.config, facts.state
    folder = profile.claude_config_dir
    items = [_text(f"Conta Claude · pasta {facts.folder_name(folder)} ({profile.name}) fica")]
    profile_account_id = profile.claude_account_id
    is_profile_default = " · do perfil" if profile_account_id is None else ""

    # The folder's own login; logged out, it opens that login instead.
    status = facts.folder_status(folder)
    kind = status.kind if status is not None else None
    if kind == "loggedOut":
        label, action = "Login da pasta · Entrar…", ("folder-login", folder)
    else:
        if kind == "loggedIn":
            subtitle = " · " + (status.email or "Logada")
        elif kind is not None:
            subtitle = " · Status indisponível"
        else:
            subtitle = ""  # not known yet: the refresh on opening the menu fills it
        label, action = "Login da pasta" + subtitle + is_profile_default, ("claude", None)
    items.append(MenuItem(label, action, enabled=not switching, toggle="checkmark",
                          toggled=state.applied_claude_account_id is None, icon_name=ICON_FOLDER))

    for account in config.claude_accounts:
        token = facts.has_token(account)
        label = account.nickname + ("" if token else " · sem token") + " · " + (account.email or "conta Claude")
        if account.id == profile_account_id:
            label += " · do perfil"
        items.append(MenuItem(label, ("claude", account.id), enabled=not switching and token, toggle="checkmark",
                              toggled=account.id == state.applied_claude_account_id,
                              icon_png=account_icon(account) if account_icon else None))
    return items + [
        _separator(),
        MenuItem("Adicionar conta Claude…", ("open", "add-claude"), icon_name=ICON_PLUS),
        _separator(),
        _text("Agents, skills e conversas não mudam."),
        _text("Vale para novos terminais; sessões abertas continuam."),
    ]
