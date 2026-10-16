"""Live Claude account switching (D39).

A "login" Claude account is a full claude.ai login (from `claude auth login` in
a throwaway folder): access and refresh tokens, kept in MultiCred's vault.
Switching puts it into the `claudeAiOauth` key of the profile folder's own
credentials (`<folder>/.credentials.json` on Linux), the same place `/login`
writes, so sessions already open adopt it without restarting. Everything else
in that file (MCP logins, `mcpOAuth`) is kept, and in `.claude.json` only
`oauthAccount` (the "logged in as" shown by Claude Code) changes.

Refresh tokens rotate, so the copy that lives in a folder is always newer than
the vault's. Before a folder changes account, its current login is saved back
to whoever owns it: a MultiCred account, or the folder's own original login.
An account lives in at most one folder at a time.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .. import claude_paths
from ..models import ClaudeFolder, parse_uuid, uuid_text
from ..paths import AppPaths
from ..storage import AtomicFileWriter, read_bytes, restore_file

DEFAULT_FOLDER_KEY = "~/.claude"

Undo = Callable[[], None]


class ClaudeLoginError(Exception):
    pass


class NoLoginInFolder(ClaudeLoginError):
    def __init__(self):
        super().__init__("The folder has no Claude login")


class VaultEntryMissing(ClaudeLoginError):
    def __init__(self, key):
        super().__init__(f"No saved Claude login for {key}")
        self.key = key


class Unreadable(ClaudeLoginError):
    def __init__(self, what):
        super().__init__(f"Unreadable Claude login data ({what})")
        self.what = what


def _object(data: bytes, what: str = "json") -> dict:
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise Unreadable(what) from None
    if not isinstance(parsed, dict):
        raise Unreadable(what)
    return parsed


def _canonical(obj) -> bytes:
    """Vault records: compact, sorted, like the macOS app."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _credentials_bytes(obj: dict) -> bytes:
    """The folder's credentials file: compact like Claude Code writes it, key order kept."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _state_bytes(obj: dict) -> bytes:
    """`.claude.json`: pretty, key order kept, so only `oauthAccount` differs in a diff."""
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ClaudeLoginRecord:
    """What MultiCred keeps for one login: the `claudeAiOauth` object (secret) and
    the `oauthAccount` object Claude Code shows as "logged in as" (not secret)."""

    oauth: bytes
    account: bytes | None = None

    @property
    def email(self) -> str | None:
        if self.account is None:
            return None
        try:
            value = _object(self.account).get("emailAddress")
        except Unreadable:
            return None
        return value if isinstance(value, str) else None

    def encoded(self) -> bytes:
        record = {"claudeAiOauth": _object(self.oauth)}
        if self.account is not None:
            record["oauthAccount"] = _object(self.account)
        return _canonical(record)

    @staticmethod
    def decode(data: bytes) -> "ClaudeLoginRecord":
        record = _object(data, "vault entry")
        oauth = record.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            raise Unreadable("vault entry")
        account = record.get("oauthAccount")
        return ClaudeLoginRecord(_canonical(oauth), _canonical(account) if isinstance(account, dict) else None)


@dataclass
class ClaudeFolderOccupancy:
    """Which login each folder currently holds (`claude-folders.json`). Absent = the folder's own login."""

    account_by_folder: dict[str, uuid.UUID] = field(default_factory=dict)

    @staticmethod
    def key(config_dir: str | None) -> str:
        return config_dir or DEFAULT_FOLDER_KEY

    @staticmethod
    def config_dir_for_key(key: str) -> str | None:
        return None if key == DEFAULT_FOLDER_KEY else key

    def account_in(self, config_dir: str | None) -> uuid.UUID | None:
        return self.account_by_folder.get(self.key(config_dir))

    def key_holding(self, account: uuid.UUID) -> str | None:
        """The occupancy key of the folder holding `account`, or None when no folder does."""
        return next((k for k, v in self.account_by_folder.items() if v == account), None)

    def to_dict(self) -> dict:
        return {"accountByFolder": {k: uuid_text(v) for k, v in self.account_by_folder.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeFolderOccupancy":
        return cls({str(k): parse_uuid(v) for k, v in data.get("accountByFolder", {}).items()})


class ClaudeLoginSwitcher:
    def __init__(self, environment, store=None):
        self.environment = environment
        self.store = store if store is not None else environment.claude_credentials
        self.occupancy_file = AppPaths(environment).claude_folders

    @staticmethod
    def own_login_key(config_dir: str | None) -> str:
        """Vault key of a folder's own (original) login."""
        return uuid_text(ClaudeFolder.own_login(config_dir).id)

    def occupancy(self) -> ClaudeFolderOccupancy:
        """A missing or broken file reads as "every folder has its own login"."""
        data = read_bytes(self.occupancy_file)
        if data is None:
            return ClaudeFolderOccupancy()
        try:
            return ClaudeFolderOccupancy.from_dict(_object(data))
        except (Unreadable, ValueError, TypeError, AttributeError):
            return ClaudeFolderOccupancy()

    def _save_occupancy(self, value: ClaudeFolderOccupancy) -> None:
        self.occupancy_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        AtomicFileWriter.write(_canonical(value.to_dict()), self.occupancy_file, permissions=0o600)

    # MARK: capture (new account)

    def capture(self, scratch_dir: str) -> ClaudeLoginRecord:
        """Reads the login a throwaway folder just got from `claude auth login`."""
        item = self.store.read_folder_item(scratch_dir)
        oauth = _object(item).get("claudeAiOauth") if item is not None else None
        if not isinstance(oauth, dict) or "accessToken" not in oauth:
            raise NoLoginInFolder()
        return ClaudeLoginRecord(_canonical(oauth), self._displayed_account(scratch_dir))

    def store_new_account(self, account_id: uuid.UUID, record: ClaudeLoginRecord) -> None:
        self.store.write_vault(uuid_text(account_id), record.encoded())

    def replace_login(self, account_id: uuid.UUID, record: ClaudeLoginRecord) -> None:
        """A new login for an existing account (signed in again). If it is live in a
        folder, that folder gets it right away."""
        self.store_new_account(account_id, record)
        key = self.occupancy().key_holding(account_id)
        if key is not None:
            self._write(record, ClaudeFolderOccupancy.config_dir_for_key(key))

    def remove_account(self, account_id: uuid.UUID) -> None:
        # If it is live in a folder, that folder goes back to its own login first.
        key = self.occupancy().key_holding(account_id)
        if key is not None:
            self.place(None, ClaudeFolderOccupancy.config_dir_for_key(key))
        self.store.delete_vault(uuid_text(account_id))

    # MARK: switch

    def place(self, account: uuid.UUID | None, config_dir: str | None) -> Undo:
        """Puts `account` (None = the folder's own login) into the folder's
        credentials. Returns the undo for the transaction."""
        occupancy = self.occupancy()
        current = occupancy.account_in(config_dir)
        if current == account:
            return lambda: None

        original = ClaudeFolderOccupancy(dict(occupancy.account_by_folder))
        undo: list[Undo] = []

        def undo_all():
            for step in reversed(undo):
                step()
            self._save_occupancy(original)

        try:
            # The account may be live in another folder: take it back from there.
            other_key = occupancy.key_holding(account) if account is not None else None
            if other_key is not None and other_key != ClaudeFolderOccupancy.key(config_dir):
                other = ClaudeFolderOccupancy.config_dir_for_key(other_key)
                undo.append(self._save_back(other, account))
                undo.append(self._restore_own_login(other))
                del occupancy.account_by_folder[other_key]

            undo.append(self._save_back(config_dir, current))
            if account is not None:
                undo.append(self._write(self._vault_record(uuid_text(account)), config_dir))
            else:
                undo.append(self._restore_own_login(config_dir))
            if account is None:
                occupancy.account_by_folder.pop(ClaudeFolderOccupancy.key(config_dir), None)
            else:
                occupancy.account_by_folder[ClaudeFolderOccupancy.key(config_dir)] = account
            self._save_occupancy(occupancy)
        except Exception:
            # A failed step inside this one must not leave half a switch behind.
            try:
                undo_all()
            except Exception:
                pass
            raise
        return undo_all

    def _save_back(self, config_dir: str | None, owner: uuid.UUID | None) -> Undo:
        """Saves the folder's live login to its owner's vault entry (rotated tokens)."""
        item = self.store.read_folder_item(config_dir)
        if item is None:
            return lambda: None
        oauth = _object(item, "credentials").get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return lambda: None
        key = uuid_text(owner) if owner is not None else self.own_login_key(config_dir)
        previous = self.store.read_vault(key)
        record = ClaudeLoginRecord(_canonical(oauth), self._displayed_account(config_dir))
        self.store.write_vault(key, record.encoded())

        def undo():
            if previous is not None:
                self.store.write_vault(key, previous)
            else:
                self.store.delete_vault(key)
        return undo

    def _restore_own_login(self, config_dir: str | None) -> Undo:
        """Puts the folder's original login back. A folder that had no login of its own
        before MultiCred put an account in it goes back to having none (on Linux a
        credentials file left empty is removed, as if it never existed)."""
        saved = self.store.read_vault(self.own_login_key(config_dir))
        if saved is not None:
            return self._write(ClaudeLoginRecord.decode(saved), config_dir)

        previous_item = self.store.read_folder_item(config_dir)
        if previous_item is not None:
            remaining = _object(previous_item, "credentials")
            remaining.pop("claudeAiOauth", None)
            if remaining:
                self.store.write_folder_item(config_dir, _credentials_bytes(remaining))
            else:
                self.store.delete_folder_item(config_dir)
        try:
            state_target, previous_state = self._remove_displayed_account(config_dir)
        except Exception:
            if previous_item is not None:
                self.store.write_folder_item(config_dir, previous_item)
            raise

        def undo():
            if previous_item is not None:
                self.store.write_folder_item(config_dir, previous_item)
            if state_target is not None:
                restore_file(previous_state, state_target, permissions=0o600)
        return undo

    def _vault_record(self, key: str) -> ClaudeLoginRecord:
        data = self.store.read_vault(key)
        if data is None:
            raise VaultEntryMissing(key)
        return ClaudeLoginRecord.decode(data)

    def _write(self, record: ClaudeLoginRecord, config_dir: str | None) -> Undo:
        """Replaces only `claudeAiOauth` in the folder's credentials and the displayed
        `oauthAccount` in its .claude.json."""
        previous_item = self.store.read_folder_item(config_dir)
        credentials = _object(previous_item, "credentials") if previous_item is not None else {}
        credentials["claudeAiOauth"] = _object(record.oauth)
        self.store.write_folder_item(config_dir, _credentials_bytes(credentials))

        state_target, previous_state = None, None
        if record.account is not None:
            try:
                state_target, previous_state = self._set_displayed_account(config_dir, _object(record.account))
            except Exception:
                self._put_item_back(config_dir, previous_item)
                raise

        def undo():
            self._put_item_back(config_dir, previous_item)
            if state_target is not None:
                restore_file(previous_state, state_target, permissions=0o600)
        return undo

    def _put_item_back(self, config_dir, previous_item):
        # Unlike the macOS app, a folder that had no credentials gets none back.
        if previous_item is not None:
            self.store.write_folder_item(config_dir, previous_item)
        else:
            self.store.delete_folder_item(config_dir)

    # MARK: .claude.json

    def _state_target(self, config_dir):
        path = claude_paths.state_file(config_dir, self.environment)
        return AtomicFileWriter.resolve_target(path, follow_symlink=True)

    def _set_displayed_account(self, config_dir, account: dict):
        target = self._state_target(config_dir)
        previous = read_bytes(target)
        state = {}
        if previous is not None:
            try:
                state = _object(previous)
            except Unreadable:
                state = {}
        state["oauthAccount"] = account
        AtomicFileWriter.write(_state_bytes(state), target, permissions=0o600)
        return target, previous

    def _remove_displayed_account(self, config_dir):
        target = self._state_target(config_dir)
        previous = read_bytes(target)
        if previous is None:
            return None, None
        try:
            state = _object(previous)
        except Unreadable:
            return None, None
        if "oauthAccount" not in state:
            return None, None
        del state["oauthAccount"]
        AtomicFileWriter.write(_state_bytes(state), target, permissions=0o600)
        return target, previous

    def _displayed_account(self, config_dir) -> bytes | None:
        data = read_bytes(claude_paths.state_file(config_dir, self.environment))
        if data is None:
            return None
        try:
            account = _object(data).get("oauthAccount")
        except Unreadable:
            return None
        return _canonical(account) if isinstance(account, dict) else None
