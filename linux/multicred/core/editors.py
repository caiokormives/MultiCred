"""Pure edits of `Config` for the Contas and profile screens (port of ConfigEditors.swift).

Every function returns an updated copy (Swift's value semantics: the argument and
the input configuration are never changed) and every result that adds or changes
an item is validated with the same rules the switch uses. The caller saves it.
API key values go only to the secret store, never into the configuration.
"""

from __future__ import annotations

import copy
import uuid

from .models import APIKeyAccount, APIProvider, ClaudeAccount, Config, GitHubAccount, GitIdentity, Profile, ProfileColor
from .validation import issues_in


class EditorError(Exception):
    """kinds: invalid (`issues` holds the validation issues), emptySecret,
    secrets (the secret store refused; `reason` never contains the value)."""

    def __init__(self, kind: str, issues=None, reason: str | None = None):
        detail = reason if reason is not None else (f"{len(issues)} validation issues" if issues else None)
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.issues, self.reason = kind, list(issues or []), reason

    @staticmethod
    def invalid(issues) -> "EditorError":
        return EditorError("invalid", issues=issues)

    @staticmethod
    def empty_secret() -> "EditorError":
        return EditorError("emptySecret")

    @staticmethod
    def secrets(reason: str) -> "EditorError":
        return EditorError("secrets", reason=reason)

    def __eq__(self, other):
        return (isinstance(other, EditorError)
                and (self.kind, self.issues, self.reason) == (other.kind, other.issues, other.reason))

    __hash__ = Exception.__hash__


def _secret_value(secret: str) -> bytes:
    value = secret.strip()
    if not value:
        raise EditorError.empty_secret()
    return value.encode()


def _replace_or_append(items: list, item) -> None:
    for index, existing in enumerate(items):
        if existing.id == item.id:
            items[index] = item
            return
    items.append(item)


def _validated(config: Config) -> Config:
    issues = issues_in(config)
    if issues:
        raise EditorError.invalid(issues)
    return config


class ConfigEditor:
    # MARK: generic upsert (Swift overloads `upsert` per type)

    @staticmethod
    def upsert(item, config: Config) -> Config:
        """Replaces the item with the same id, or appends it; validates the result."""
        item = copy.deepcopy(item)
        updated = copy.deepcopy(config)
        if isinstance(item, GitIdentity):
            # A blank key path means "no key", not an invalid path.
            if item.ssh_key_path is not None and not item.ssh_key_path.strip(" \t"):
                item.ssh_key_path = None
            _replace_or_append(updated.git_identities, item)
        elif isinstance(item, GitHubAccount):
            _replace_or_append(updated.github_accounts, item)
        elif isinstance(item, ClaudeAccount):
            _replace_or_append(updated.claude_accounts, item)
        elif isinstance(item, APIKeyAccount):
            _replace_or_append(updated.api_keys, item)
        elif isinstance(item, Profile):
            _replace_or_append(updated.profiles, item)
        else:
            raise TypeError(f"cannot upsert {type(item).__name__}")
        return _validated(updated)

    # MARK: git identities

    @staticmethod
    def remove_git_identity(identity_id: uuid.UUID, config: Config) -> Config:
        """Profiles using it stop switching git."""
        updated = copy.deepcopy(config)
        updated.git_identities = [i for i in updated.git_identities if i.id != identity_id]
        for profile in updated.profiles:
            if profile.git_identity_id == identity_id:
                profile.git_identity_id = None
        return updated

    # MARK: GitHub accounts

    @staticmethod
    def remove_github_account(account_id: uuid.UUID, config: Config) -> Config:
        """The account stays logged in to gh; profiles using it stop switching GitHub."""
        updated = copy.deepcopy(config)
        updated.github_accounts = [a for a in updated.github_accounts if a.id != account_id]
        for profile in updated.profiles:
            if profile.github_account_id == account_id:
                profile.github_account_id = None
        return updated

    @staticmethod
    def add_missing_github_accounts(logins, config: Config) -> Config:
        """Adds gh logins MultiCred does not know yet (host and user compared ignoring case)."""
        updated = copy.deepcopy(config)
        palette = list(ProfileColor)
        for login in logins:
            known = any(a.host.lower() == login.host.lower() and a.username.lower() == login.login.lower()
                        for a in updated.github_accounts)
            if known:
                continue
            updated.github_accounts.append(GitHubAccount(
                nickname=login.login, username=login.login, host=login.host,
                color=palette[len(updated.github_accounts) % len(palette)]))
        return updated

    # MARK: Claude accounts

    @staticmethod
    def update_claude_account(account: ClaudeAccount, config: Config) -> Config:
        return ConfigEditor.upsert(account, config)

    # MARK: API keys (value in the secret store only)

    @staticmethod
    def add_api_key(nickname: str, provider: APIProvider, env_var: str, secret: str, color: ProfileColor,
                    config: Config, secrets) -> tuple[Config, APIKeyAccount]:
        """Validates first, then stores the secret; on a secret store error nothing is added."""
        value = _secret_value(secret)
        account = APIKeyAccount(nickname=nickname.strip(" "), provider=provider, env_var=env_var, color=color)
        updated = copy.deepcopy(config)
        updated.api_keys.append(account)
        updated = _validated(updated)
        try:
            secrets.set_secret(value, account.secret_account)
        except Exception as error:
            raise EditorError.secrets(type(error).__name__ + ": " + str(error)) from None
        return updated, account

    @staticmethod
    def update_api_key(account: APIKeyAccount, config: Config) -> Config:
        return ConfigEditor.upsert(account, config)

    @staticmethod
    def replace_secret(account: APIKeyAccount, secret: str, secrets) -> None:
        value = _secret_value(secret)
        try:
            secrets.set_secret(value, account.secret_account)
        except Exception as error:
            raise EditorError.secrets(type(error).__name__ + ": " + str(error)) from None

    @staticmethod
    def remove_api_key(key_id: uuid.UUID, config: Config, secrets) -> Config:
        """Removes the key, its secret and its use in profiles. The secret goes first:
        if the store refuses, the configuration still points to it and nothing changes."""
        account = config.api_key(key_id)
        if account is None:
            return config
        try:
            secrets.delete_item(account.secret_account)
        except Exception as error:
            raise EditorError.secrets(type(error).__name__ + ": " + str(error)) from None
        updated = copy.deepcopy(config)
        updated.api_keys = [k for k in updated.api_keys if k.id != key_id]
        for profile in updated.profiles:
            profile.api_key_ids = [k for k in profile.api_key_ids if k != key_id]
        return updated

    # MARK: profiles

    @staticmethod
    def remove_profile(profile_id: uuid.UUID, config: Config) -> Config:
        """Accounts stay; an active profile leaves the system as it is until another is chosen."""
        updated = copy.deepcopy(config)
        updated.profiles = [p for p in updated.profiles if p.id != profile_id]
        return updated

    @staticmethod
    def set_api_key(key_id: uuid.UUID | None, provider: APIProvider, profile: Profile, config: Config) -> Profile:
        """Sets the key a profile uses for one provider (one key per provider)."""
        updated = copy.deepcopy(profile)
        updated.api_key_ids = [k for k in updated.api_key_ids
                               if (config.api_key(k).provider if config.api_key(k) else None) != provider]
        if key_id is not None:
            updated.api_key_ids.append(key_id)
        return updated
