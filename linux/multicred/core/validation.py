"""Problems that make a configuration unsafe or impossible to apply.

The UI maps each issue kind to a message; the core stays language-neutral.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass

from .models import ClaudeAccountKind, Config


@dataclass(frozen=True)
class Issue:
    kind: str
    owner: uuid.UUID
    detail: str | None = None


# env.sh/env.fish export API keys into every shell, so a key may never be named after
# a variable that controls the shell (code run at each prompt, word splitting, startup
# files), the dynamic loader, or the desktop session the terminal talks to.
RESERVED_ENV_VARS = {
    "PATH", "HOME", "SHELL", "USER", "LOGNAME", "TMPDIR", "LANG", "TERM",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN",
    # shells
    "PROMPT_COMMAND", "PS0", "PS1", "PS2", "PS3", "PS4", "PROMPT", "RPROMPT", "IFS", "BASH_ENV", "ENV",
    "ZDOTDIR", "SHELLOPTS", "BASHOPTS", "CDPATH", "FPATH", "GLOBIGNORE", "HISTFILE", "HISTCONTROL",
    "HISTSIZE", "HISTFILESIZE", "PWD", "OLDPWD", "MAIL", "MAILPATH", "POSIXLY_CORRECT", "INPUTRC",
    "LC_ALL", "LANGUAGE",
    # dynamic loader
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    # desktop session
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "XDG_CONFIG_DIRS", "XDG_DATA_DIRS", "SSH_AUTH_SOCK", "DISPLAY", "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
}

_ENV_VAR = re.compile(r"[A-Z_][A-Z0-9_]*")
_GITHUB_USER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
# Must start with a letter or digit: a leading "-" would be read as an option by gh.
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


def has_control_characters(value: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def is_valid_env_var_name(name: str) -> bool:
    return _ENV_VAR.fullmatch(name) is not None


def is_valid_github_username(name: str) -> bool:
    return _GITHUB_USER.fullmatch(name) is not None


def is_valid_host(host: str) -> bool:
    return _HOST.fullmatch(host) is not None


def is_plausible_email(email: str) -> bool:
    return _EMAIL.fullmatch(email) is not None


def is_canonical_directory(path: str) -> bool:
    from .claude_paths import standardize
    return path.startswith("/") and standardize(path) == path and not has_control_characters(path)


def issues_in(config: Config) -> list[Issue]:
    """Every issue in the configuration; empty means it can be applied."""
    issues: list[Issue] = []

    def text(value, owner, field, required=True):
        if required and not value.strip():
            issues.append(Issue("emptyField", owner, field))
        if has_control_characters(value):
            issues.append(Issue("controlCharacters", owner, field))

    seen_ids = set()
    every = (config.git_identities + config.github_accounts + config.claude_accounts + config.api_keys
             + config.profiles)
    for item in every:
        if item.id in seen_ids:
            issues.append(Issue("duplicateID", item.id))
        seen_ids.add(item.id)

    for identity in config.git_identities:
        text(identity.nickname, identity.id, "nickname")
        text(identity.name, identity.id, "name")
        text(identity.email, identity.id, "email")
        if not is_plausible_email(identity.email):
            issues.append(Issue("invalidEmail", identity.id))
        key = identity.ssh_key_path
        if key is not None and (not key or has_control_characters(key)
                                or not (key.startswith("/") or key.startswith("~/"))):
            issues.append(Issue("invalidSSHKeyPath", identity.id))

    for account in config.github_accounts:
        text(account.nickname, account.id, "nickname")
        if not is_valid_github_username(account.username):
            issues.append(Issue("invalidGitHubUsername", account.id))
        if not is_valid_host(account.host):
            issues.append(Issue("invalidHost", account.id))

    folder_dirs = set()
    for account in config.claude_accounts:
        text(account.nickname, account.id, "nickname")
        if account.email and not is_plausible_email(account.email):
            issues.append(Issue("invalidEmail", account.id))
        # Folder accounts only exist before migration (config v1).
        if account.kind != ClaudeAccountKind.FOLDER:
            continue
        if account.config_dir is not None and not is_canonical_directory(account.config_dir):
            issues.append(Issue("invalidClaudeDirectory", account.id))
        if account.config_dir in folder_dirs:
            issues.append(Issue("duplicateClaudeDirectory", account.id))
        folder_dirs.add(account.config_dir)

    for key in config.api_keys:
        text(key.nickname, key.id, "nickname")
        if not is_valid_env_var_name(key.env_var):
            issues.append(Issue("invalidEnvVar", key.id, key.env_var))
        elif key.env_var in RESERVED_ENV_VARS:
            issues.append(Issue("reservedEnvVar", key.id, key.env_var))

    for profile in config.profiles:
        text(profile.name, profile.id, "name")
        if profile.git_identity_id is not None and config.git_identity(profile.git_identity_id) is None:
            issues.append(Issue("missingReference", profile.id, "git"))
        if profile.github_account_id is not None and config.github_account(profile.github_account_id) is None:
            issues.append(Issue("missingReference", profile.id, "github"))
        if profile.claude_account_id is not None and config.claude_account(profile.claude_account_id) is None:
            issues.append(Issue("missingReference", profile.id, "claude"))
        if profile.claude_config_dir is not None and not is_canonical_directory(profile.claude_config_dir):
            issues.append(Issue("invalidClaudeDirectory", profile.id))
        seen_vars = set()
        for key_id in profile.api_key_ids:
            key = config.api_key(key_id)
            if key is None:
                issues.append(Issue("missingReference", profile.id, "apiKey"))
                continue
            if key.env_var in seen_vars:
                issues.append(Issue("duplicateEnvVarInProfile", profile.id, key.env_var))
            seen_vars.add(key.env_var)
    return issues
