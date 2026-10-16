"""The tray menu as a plain tree (design r2-2 "bandeja", macOS menu 1a).

Pure: no D-Bus, no drawing. Panels draw dbusmenu items themselves, so the macOS
SwiftUI header becomes disabled text lines and each profile carries its colored
square as the item image.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Callable

from ..core.models import ActiveState, Config, Profile

TEST_MODE_LINE = "MODO DE TESTE · nada no sistema real muda"
INCONSISTENT_LINE = "⚠ Inconsistente — Corrigir"
CLAUDE_SUBMENU = "Trocar só a conta Claude"
QUIT = "Sair do MultiCred"


@dataclass
class MenuItem:
    """One dbusmenu item. `action` is what a click asks for, e.g. ("profile", uuid)."""

    label: str = ""
    action: tuple | None = None
    enabled: bool = True
    separator: bool = False
    toggle: str | None = None  # "radio"
    toggled: bool = False
    icon_png: bytes | None = None
    children: list["MenuItem"] = field(default_factory=list)


def _text(label: str) -> MenuItem:
    return MenuItem(label=label, enabled=False)


def _separator() -> MenuItem:
    return MenuItem(separator=True)


def build_menu(config: Config | None, state: ActiveState, *, inconsistent: bool = False, switching: bool = False,
               test_mode: bool = False, tz: tzinfo | None = None,
               icon: Callable[[Profile], bytes] | None = None) -> list[MenuItem]:
    """Top-level items. `config` None = not configured yet (only "Configurar" and "Sair").

    `tz` None formats "ativo desde" in the local time zone.
    """
    items: list[MenuItem] = []
    if test_mode:
        items += [_text(TEST_MODE_LINE), _separator()]
    if config is None:
        return items + [MenuItem("Configurar MultiCred…", ("open", "onboarding")), _separator(),
                        MenuItem(QUIT, ("quit", None))]

    profile = config.profile(state.profile_id)
    items += _header(config, state, profile, tz, icon)
    if inconsistent:
        items.append(MenuItem(INCONSISTENT_LINE, ("open", "verify")))
    items.append(_separator())

    for each in config.profiles:
        items.append(MenuItem(each.name, ("profile", each.id), enabled=not switching, toggle="radio",
                              toggled=each.id == state.profile_id, icon_png=icon(each) if icon else None))
    if not config.profiles:
        items.append(MenuItem("Criar um perfil…", ("open", "new-profile")))
    items.append(_separator())

    items.append(MenuItem(CLAUDE_SUBMENU, children=_claude_submenu(config, state, profile, switching)))
    items.append(_separator())
    items += [
        MenuItem("Verificar…", ("open", "verify")),
        MenuItem("Abrir MultiCred", ("open", "main")),
        MenuItem("Contas…", ("open", "accounts")),
        MenuItem("Ajustes…", ("open", "settings")),
        _separator(),
        MenuItem(QUIT, ("quit", None)),
    ]
    return items


def _header(config, state, profile, tz, icon) -> list[MenuItem]:
    if profile is None:
        return [_text("Nenhum perfil ativo")]
    title = profile.name
    if state.activated_at is not None:
        title += f" · ativo desde {state.activated_at.astimezone(tz).strftime('%H:%M')}"
    lines = [MenuItem(title, enabled=False, icon_png=icon(profile) if icon else None)]
    git = config.git_identity(profile.git_identity_id)
    if git is not None:
        lines.append(_text(f"git {git.email}"))
    applied = config.claude_account(state.applied_claude_account_id)
    claude = f"Claude {applied.nickname if applied else 'login da pasta'}"
    if state.claude_override_id is not None:
        claude += " (só Claude)"
    github = config.github_account(profile.github_account_id)
    lines.append(_text(f"GitHub {github.username} · {claude}" if github else claude))
    return lines


def _claude_submenu(config, state, profile, switching) -> list[MenuItem]:
    if profile is None:
        return [_text("Ative um perfil primeiro")]
    default = profile.claude_account_id
    items = [MenuItem("Login da pasta" + (" · do perfil" if default is None else ""), ("claude", None),
                      enabled=not switching, toggle="radio", toggled=state.applied_claude_account_id is None)]
    for account in config.claude_accounts:
        items.append(MenuItem(account.nickname + (" · do perfil" if account.id == default else ""),
                              ("claude", account.id), enabled=not switching, toggle="radio",
                              toggled=account.id == state.applied_claude_account_id))
    return items + [_separator(), MenuItem("Adicionar conta Claude…", ("open", "add-claude"))]
