"""Claude folders for profiles, and Claude accounts (token and full login).

Every function returns an updated copy of the configuration; the caller saves
it. Secrets go only to the Secret Service (tokens) or the login vault (logins).
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

from .. import claude_paths
from ..models import ClaudeAccount, ClaudeAccountKind, Config, ProfileColor, uuid_text
from ..validation import issues_in

_TOKEN = re.compile(r"[A-Za-z0-9._\-]+")


class ClaudeAccountError(Exception):
    """kinds: invalidPath, folderMissing, notADirectory, outsideSandbox (test mode
    keeps every folder inside the sandbox home), createFailed, invalidToken,
    secrets, invalid (validation issues in `detail`)."""

    def __init__(self, kind: str, detail=None):
        super().__init__(f"{kind}: {detail}" if detail is not None else kind)
        self.kind, self.detail = kind, detail

    def __eq__(self, other):
        return isinstance(other, ClaudeAccountError) and (self.kind, self.detail) == (other.kind, other.detail)

    __hash__ = Exception.__hash__


@dataclass(frozen=True)
class ClaudeFolderChoice:
    """Where a profile keeps its Claude folder: the default ~/.claude (used with
    CLAUDE_CONFIG_DIR unset), a folder that exists, or one MultiCred creates (0700)."""

    kind: str  # "default" | "existing" | "new"
    path: str | None = None

    @staticmethod
    def default() -> "ClaudeFolderChoice": return ClaudeFolderChoice("default")

    @staticmethod
    def existing(path: str) -> "ClaudeFolderChoice": return ClaudeFolderChoice("existing", path)

    @staticmethod
    def new(path: str) -> "ClaudeFolderChoice": return ClaudeFolderChoice("new", path)


def _copy(config: Config) -> Config:
    return Config.from_dict(config.to_dict())


# MARK: folders (belong to profiles)

def prepare_folder(choice: ClaudeFolderChoice, environment) -> str | None:
    """Canonical folder for the choice, creating it for "new" (and the default folder
    when it does not exist yet). None = the default ~/.claude."""
    if choice.kind == "default":
        config_dir = None
    else:
        try:
            config_dir = claude_paths.canonical_config_dir(choice.path, environment)
        except claude_paths.PathError:
            raise ClaudeAccountError("invalidPath", choice.path) from None
    directory = config_dir if config_dir is not None else claude_paths.default_config_dir(environment)
    if environment.mode != "real" and not directory.startswith(f"{environment.home}/"):
        raise ClaudeAccountError("outsideSandbox", directory)
    exists = os.path.lexists(directory)
    if exists and not os.path.isdir(directory):
        raise ClaudeAccountError("notADirectory", directory)
    if choice.kind == "existing" and not exists:
        raise ClaudeAccountError("folderMissing", directory)
    if choice.kind in ("new", "default") and not exists:
        try:
            os.makedirs(directory, mode=0o700)
            os.chmod(directory, 0o700)
        except OSError as error:
            raise ClaudeAccountError("createFailed", error.strerror) from error
        if config_dir is not None:
            # Uninstall may offer to log it out and delete it (D12).
            from ..install.uninstaller import remember_created_claude_folder
            remember_created_claude_folder(environment, config_dir)
    return config_dir


# MARK: token accounts

def is_plausible_token(token: str) -> bool:
    """Looks like an OAuth token from `claude setup-token`: one line, no spaces."""
    return 20 <= len(token) <= 2048 and _TOKEN.fullmatch(token) is not None


def _store_token(token: str, account: ClaudeAccount, secrets) -> None:
    try:
        secrets.set_secret(token.encode(), account.secret_account)
    except Exception as error:
        raise ClaudeAccountError("secrets", str(error)) from error


def _clean_email(email: str | None) -> str | None:
    cleaned = email.strip(" ") if email is not None else None
    return cleaned or None


def _append_validated(config: Config, account: ClaudeAccount) -> Config:
    updated = _copy(config)
    updated.claude_accounts.append(account)
    issues = issues_in(updated)
    if issues:
        raise ClaudeAccountError("invalid", issues)
    return updated


def add_token_account(nickname: str, email: str | None, color: ProfileColor, token: str, config: Config,
                      secrets) -> tuple[Config, ClaudeAccount]:
    """Validates first, then stores the token; on a Secret Service error nothing is added."""
    value = token.strip()
    if not is_plausible_token(value):
        raise ClaudeAccountError("invalidToken")
    account = ClaudeAccount(nickname=nickname.strip(" "), email=_clean_email(email), color=color,
                            kind=ClaudeAccountKind.TOKEN)
    updated = _append_validated(config, account)
    _store_token(value, account, secrets)
    return updated, account


def replace_token(account: ClaudeAccount, token: str, secrets) -> None:
    """Replaces the token of an existing account (new `claude setup-token`)."""
    value = token.strip()
    if not is_plausible_token(value):
        raise ClaudeAccountError("invalidToken")
    _store_token(value, account, secrets)


# MARK: login accounts (D39)

def add_login_account(nickname: str, email: str | None, color: ProfileColor, record, config: Config,
                      switcher) -> tuple[Config, ClaudeAccount]:
    """A login captured from a throwaway folder (ClaudeLoginSwitcher.capture) becomes an
    account. The e-mail of the login wins over the typed one. If saving the
    returned config fails, the caller deletes the vault entry again."""
    account = ClaudeAccount(nickname=nickname.strip(" "), email=record.email or _clean_email(email), color=color,
                            kind=ClaudeAccountKind.LOGIN)
    updated = _append_validated(config, account)
    switcher.store_new_account(account.id, record)
    return updated, account


def replace_login(account: ClaudeAccount, record, config: Config, switcher, secrets) -> Config:
    """Signs an existing account in again. A token account becomes a login account
    (live switching). Its old token is NOT deleted here: until the caller has saved
    the converted config, the saved config still says "token account" and needs it.
    The caller saves, then calls `discard_replaced_token`, then re-applies Claude
    when the token was the one in use, so shells stop exporting it."""
    switcher.replace_login(account.id, record)
    if account.kind == ClaudeAccountKind.LOGIN:
        return config
    updated = _copy(config)
    target = updated.claude_account(account.id)
    if target is not None:
        target.kind = ClaudeAccountKind.LOGIN
        target.email = target.email or record.email
    return updated


def discard_replaced_token(account: ClaudeAccount, secrets) -> None:
    """Deletes the token of an account `replace_login` converted, after the save."""
    if account.kind != ClaudeAccountKind.TOKEN:
        return
    try:
        secrets.delete_item(account.secret_account)
    except Exception:
        pass  # a leftover token is harmless: login accounts never export it


# MARK: removal

def release_login(account: ClaudeAccount, switcher):
    """Step 1 of removing an account: a login account live in a folder gives the folder
    its own login back (its rotated login is saved to its vault entry by the switch).
    Returns the undo, for when the save that follows fails; raises if the folder could
    not be given back (then nothing is removed)."""
    if account.kind != ClaudeAccountKind.LOGIN:
        return lambda: None
    key = switcher.occupancy().key_holding(account.id)
    if key is None:
        return lambda: None
    from .login_vault import ClaudeFolderOccupancy
    return switcher.place(None, ClaudeFolderOccupancy.config_dir_for_key(key))


def remove(account_id: uuid.UUID, config: Config) -> Config:
    """Step 2: the configuration without the account; profiles using it fall back to
    their folder's own login. Pure: the vault entry and the token stay until the caller
    has saved this config and calls `discard_removed_account` (step 3), so a failed
    save loses nothing."""
    updated = _copy(config)
    updated.claude_accounts = [a for a in updated.claude_accounts if a.id != account_id]
    for profile in updated.profiles:
        if profile.claude_account_id == account_id:
            profile.claude_account_id = None
    return updated


def discard_removed_account(account: ClaudeAccount, secrets, switcher) -> None:
    """Step 3, after the save: deletes the account's saved login and its token (a login
    account may still hold the token it had before being signed in again). A leftover
    is harmless: no account in the configuration points at it any more."""
    if account.kind == ClaudeAccountKind.LOGIN:
        try:
            switcher.store.delete_vault(uuid_text(account.id))
        except Exception:
            pass
    try:
        secrets.delete_item(account.secret_account)
    except Exception:
        pass


def profiles_using(account_id: uuid.UUID, config: Config) -> list:
    return [p for p in config.profiles if p.claude_account_id == account_id]
