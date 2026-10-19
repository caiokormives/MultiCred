"""Configuration and state, with the same JSON shape as the macOS app.

Field names in JSON are the Swift ones (camelCase); UUIDs are upper case like
Swift's `UUID.uuidString`; dates are ISO 8601 in UTC.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ProfileColor(str, Enum):
    """The eight profile colors, the same hex values as the macOS `ProfileColor`."""

    INDIGO = "indigo"
    BLUE = "blue"
    TEAL = "teal"
    GREEN = "green"
    ORANGE = "orange"
    RED = "red"
    PINK = "pink"
    GRAPHITE = "graphite"

    @property
    def hex(self) -> str:
        return _COLOR_HEX[self]


_COLOR_HEX = {
    ProfileColor.INDIGO: "#5856D6",
    ProfileColor.BLUE: "#0064E0",
    ProfileColor.TEAL: "#0E8A9E",
    ProfileColor.GREEN: "#1C8A36",
    ProfileColor.ORANGE: "#D9770B",
    ProfileColor.RED: "#C8101E",
    ProfileColor.PINK: "#B23A8F",
    ProfileColor.GRAPHITE: "#6E6E73",
}


class ShellKind(str, Enum):
    ZSH = "zsh"
    BASH = "bash"
    FISH = "fish"


class ClaudeAccountKind(str, Enum):
    # Config v2: a credential only (`claude setup-token`), exported as CLAUDE_CODE_OAUTH_TOKEN.
    TOKEN = "token"
    # A full claude.ai login kept in MultiCred's vault and placed into the
    # profile folder's own credentials, like `/login` does (D39).
    LOGIN = "login"
    # Config v1 only: an account was a whole CLAUDE_CONFIG_DIR folder.
    FOLDER = "folder"


class APIProvider(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    MISTRAL = "mistral"
    GROQ = "groq"
    OTHER = "other"

    @property
    def display_name(self) -> str:
        return {"openai": "OpenAI", "gemini": "Gemini", "anthropic": "Anthropic API", "mistral": "Mistral",
                "groq": "Groq", "other": "Outra"}[self.value]

    @property
    def default_env_var(self) -> str:
        return {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                "mistral": "MISTRAL_API_KEY", "groq": "GROQ_API_KEY", "other": ""}[self.value]


class SwitchFeedback(str, Enum):
    # On Linux "hud" is a short desktop notification (Wayland has no positioned overlay).
    HUD = "hud"
    NOTIFICATION = "notification"
    NONE = "none"


# MARK: coding helpers

def uuid_text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value).upper()


def parse_uuid(text: str | None) -> uuid.UUID | None:
    return None if text is None else uuid.UUID(text)


def date_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _drop_none(data: dict) -> dict:
    # Swift's synthesized Codable omits nil optionals.
    return {k: v for k, v in data.items() if v is not None}


@dataclass(frozen=True)
class ProfileIcon:
    """What the tray shows for a profile: a letter, or a symbol name."""

    kind: str  # "letter" | "symbol"
    value: str

    def to_dict(self) -> dict:
        return {self.kind: {"_0": self.value}}

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileIcon":
        (kind, payload), = data.items()
        if kind not in ("letter", "symbol"):
            raise ValueError(f"unknown icon kind {kind}")
        return cls(kind, payload["_0"])


@dataclass
class GitIdentity:
    nickname: str
    name: str
    email: str
    color: ProfileColor
    # Private key for core.sshCommand; None leaves the user's SSH setup alone.
    ssh_key_path: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def to_dict(self) -> dict:
        return _drop_none({"id": uuid_text(self.id), "nickname": self.nickname, "name": self.name,
                           "email": self.email, "sshKeyPath": self.ssh_key_path, "color": self.color.value})

    @classmethod
    def from_dict(cls, d: dict) -> "GitIdentity":
        return cls(id=parse_uuid(d["id"]), nickname=d["nickname"], name=d["name"], email=d["email"],
                   ssh_key_path=d.get("sshKeyPath"), color=ProfileColor(d["color"]))


@dataclass
class GitHubAccount:
    nickname: str
    username: str
    color: ProfileColor
    host: str = "github.com"
    # Commits and PRs made by Claude Code in profiles using this account carry
    # Claude as co-author (Claude Code's default); D38.
    claude_co_author: bool = True
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def to_dict(self) -> dict:
        return {"id": uuid_text(self.id), "nickname": self.nickname, "username": self.username, "host": self.host,
                "color": self.color.value, "claudeCoAuthor": self.claude_co_author}

    @classmethod
    def from_dict(cls, d: dict) -> "GitHubAccount":
        return cls(id=parse_uuid(d["id"]), nickname=d["nickname"], username=d["username"], host=d["host"],
                   color=ProfileColor(d["color"]), claude_co_author=d.get("claudeCoAuthor", True))


@dataclass
class ClaudeAccount:
    """Who authenticates and pays. Not where the chats live."""

    nickname: str
    color: ProfileColor
    # E-mail typed by the user; display only.
    email: str | None = None
    kind: ClaudeAccountKind = ClaudeAccountKind.TOKEN
    # Only for FOLDER (config v1) and for a folder's own login: canonical
    # absolute path, or None for the default ~/.claude.
    config_dir: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def secret_account(self) -> str:
        """Secret Service account name of the token."""
        return uuid_text(self.id)

    @property
    def is_default(self) -> bool:
        return self.config_dir is None

    def to_dict(self) -> dict:
        return _drop_none({"id": uuid_text(self.id), "nickname": self.nickname, "email": self.email,
                           "color": self.color.value, "kind": self.kind.value, "configDir": self.config_dir})

    @classmethod
    def from_dict(cls, d: dict) -> "ClaudeAccount":
        # Config v1 had no kind: every account was a folder.
        return cls(id=parse_uuid(d["id"]), nickname=d["nickname"], email=d.get("email"),
                   color=ProfileColor(d["color"]), kind=ClaudeAccountKind(d.get("kind", "folder")),
                   config_dir=d.get("configDir"))


class ClaudeFolder:
    """A Claude Code config folder as used by a profile. None = the default ~/.claude."""

    TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
    # Override meaning "the folder's own login", even when the profile's default is an account.
    OWN_LOGIN_OVERRIDE = uuid.UUID(int=1)

    @staticmethod
    def override_for(account_id: uuid.UUID | None, profile: "Profile | None") -> uuid.UUID | None:
        """The override to store for "use this account now" (None = folder login)."""
        if account_id == (profile.claude_account_id if profile else None):
            return None
        return account_id if account_id is not None else ClaudeFolder.OWN_LOGIN_OVERRIDE

    @staticmethod
    def own_login(config_dir: str | None) -> ClaudeAccount:
        """A pseudo account for a folder's own login; its id is derived from the path."""
        digest = hashlib.sha256(f"multicred-folder:{config_dir or '~/.claude'}".encode()).digest()
        return ClaudeAccount(id=uuid.UUID(bytes=digest[:16]), nickname="", color=ProfileColor.GRAPHITE,
                             kind=ClaudeAccountKind.FOLDER, config_dir=config_dir)


@dataclass
class APIKeyAccount:
    """An API key exported as an environment variable. The value lives only in the Secret Service."""

    nickname: str
    provider: APIProvider
    color: ProfileColor
    env_var: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self):
        if not self.env_var:
            self.env_var = self.provider.default_env_var

    @property
    def secret_account(self) -> str:
        return uuid_text(self.id)

    def to_dict(self) -> dict:
        return {"id": uuid_text(self.id), "nickname": self.nickname, "provider": self.provider.value,
                "envVar": self.env_var, "color": self.color.value}

    @classmethod
    def from_dict(cls, d: dict) -> "APIKeyAccount":
        return cls(id=parse_uuid(d["id"]), nickname=d["nickname"], provider=APIProvider(d["provider"]),
                   env_var=d["envVar"], color=ProfileColor(d["color"]))


@dataclass
class Profile:
    """A combination of accounts. A None git/GitHub reference means "do not switch that item"."""

    name: str
    color: ProfileColor
    icon: ProfileIcon | None = None
    git_identity_id: uuid.UUID | None = None
    github_account_id: uuid.UUID | None = None
    # Claude folder of this profile: canonical absolute path, None = ~/.claude.
    claude_config_dir: str | None = None
    # Account to authenticate with; None = the folder's own login.
    claude_account_id: uuid.UUID | None = None
    api_key_ids: list[uuid.UUID] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self):
        if self.icon is None:
            self.icon = ProfileIcon("letter", self.name.strip()[:1].upper())

    def to_dict(self) -> dict:
        return _drop_none({
            "id": uuid_text(self.id), "name": self.name, "color": self.color.value, "icon": self.icon.to_dict(),
            "gitIdentityId": uuid_text(self.git_identity_id), "githubAccountId": uuid_text(self.github_account_id),
            "claudeConfigDir": self.claude_config_dir, "claudeAccountId": uuid_text(self.claude_account_id),
            "apiKeyIds": [uuid_text(k) for k in self.api_key_ids],
        })

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(id=parse_uuid(d["id"]), name=d["name"], color=ProfileColor(d["color"]),
                   icon=ProfileIcon.from_dict(d["icon"]), git_identity_id=parse_uuid(d.get("gitIdentityId")),
                   github_account_id=parse_uuid(d.get("githubAccountId")), claude_config_dir=d.get("claudeConfigDir"),
                   claude_account_id=parse_uuid(d.get("claudeAccountId")),
                   api_key_ids=[parse_uuid(k) for k in d.get("apiKeyIds", [])])


@dataclass
class Settings:
    switch_feedback: SwitchFeedback = SwitchFeedback.HUD
    # Linux default: bash is the login shell of Ubuntu and Pop!_OS.
    enabled_shells: list[ShellKind] = field(default_factory=lambda: [ShellKind.BASH])
    open_at_login: bool = True

    def to_dict(self) -> dict:
        return {"switchFeedback": self.switch_feedback.value,
                "enabledShells": [s.value for s in self.enabled_shells], "openAtLogin": self.open_at_login}

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        return cls(switch_feedback=SwitchFeedback(d.get("switchFeedback", "hud")),
                   enabled_shells=[ShellKind(s) for s in d.get("enabledShells", ["bash"])],
                   open_at_login=d.get("openAtLogin", True))


@dataclass
class Config:
    """Everything in config.json. No secrets."""

    # 2: Claude accounts are credentials; the Claude folder belongs to the profile.
    current_version = 2

    git_identities: list[GitIdentity] = field(default_factory=list)
    github_accounts: list[GitHubAccount] = field(default_factory=list)
    claude_accounts: list[ClaudeAccount] = field(default_factory=list)
    api_keys: list[APIKeyAccount] = field(default_factory=list)
    # Order is the menu order; the first nine get Ctrl+1…9.
    profiles: list[Profile] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    version: int = 2

    @staticmethod
    def _find(items, item_id):
        if item_id is None:
            return None
        return next((item for item in items if item.id == item_id), None)

    def profile(self, item_id): return self._find(self.profiles, item_id)
    def git_identity(self, item_id): return self._find(self.git_identities, item_id)
    def github_account(self, item_id): return self._find(self.github_accounts, item_id)
    def claude_account(self, item_id): return self._find(self.claude_accounts, item_id)
    def api_key(self, item_id): return self._find(self.api_keys, item_id)

    @property
    def managed_env_vars(self) -> list[str]:
        """Every variable MultiCred manages; all are unset before a profile's keys are exported."""
        return sorted({k.env_var for k in self.api_keys} | {ClaudeFolder.TOKEN_VARIABLE})

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "gitIdentities": [x.to_dict() for x in self.git_identities],
            "githubAccounts": [x.to_dict() for x in self.github_accounts],
            "claudeAccounts": [x.to_dict() for x in self.claude_accounts],
            "apiKeys": [x.to_dict() for x in self.api_keys],
            "profiles": [x.to_dict() for x in self.profiles],
            "settings": self.settings.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        return cls(version=d["version"],
                   git_identities=[GitIdentity.from_dict(x) for x in d.get("gitIdentities", [])],
                   github_accounts=[GitHubAccount.from_dict(x) for x in d.get("githubAccounts", [])],
                   claude_accounts=[ClaudeAccount.from_dict(x) for x in d.get("claudeAccounts", [])],
                   api_keys=[APIKeyAccount.from_dict(x) for x in d.get("apiKeys", [])],
                   profiles=[Profile.from_dict(x) for x in d.get("profiles", [])],
                   settings=Settings.from_dict(d.get("settings", {})))


class ConfigMigration:
    """Moves a config v1 (Claude account = folder) to v2 (folder on the profile)."""

    @staticmethod
    def migrate(config: Config) -> tuple[Config, bool]:
        if config.version >= Config.current_version and not any(
                a.kind == ClaudeAccountKind.FOLDER for a in config.claude_accounts):
            return config, False
        updated = Config.from_dict(config.to_dict())
        for profile in updated.profiles:
            account = config.claude_account(profile.claude_account_id)
            if account is not None and account.kind == ClaudeAccountKind.FOLDER:
                profile.claude_config_dir = account.config_dir
                profile.claude_account_id = None
        updated.claude_accounts = [a for a in updated.claude_accounts if a.kind != ClaudeAccountKind.FOLDER]
        updated.version = Config.current_version
        return updated, True


@dataclass
class ActiveState:
    """What is active right now (state.json). `generation` mirrors the file the shell hooks watch."""

    current_version = 1

    profile_id: uuid.UUID | None = None
    # Set by "switch only the Claude account"; cleared by the next profile switch.
    claude_override_id: uuid.UUID | None = None
    # The Claude account actually applied last (profile or override).
    applied_claude_account_id: uuid.UUID | None = None
    activated_at: datetime | None = None
    generation: int = 0
    version: int = 1

    def to_dict(self) -> dict:
        return _drop_none({
            "version": self.version, "profileId": uuid_text(self.profile_id),
            "claudeOverrideId": uuid_text(self.claude_override_id),
            "appliedClaudeAccountId": uuid_text(self.applied_claude_account_id),
            "activatedAt": date_text(self.activated_at), "generation": self.generation,
        })

    @classmethod
    def from_dict(cls, d: dict) -> "ActiveState":
        return cls(version=d["version"], profile_id=parse_uuid(d.get("profileId")),
                   claude_override_id=parse_uuid(d.get("claudeOverrideId")),
                   applied_claude_account_id=parse_uuid(d.get("appliedClaudeAccountId")),
                   activated_at=parse_date(d.get("activatedAt")), generation=d.get("generation", 0))
