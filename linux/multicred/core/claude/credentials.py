"""Raw access to Claude Code's folder credentials and MultiCred's login vault (D39).

On Linux a folder's login is the file `<folder>/.credentials.json`; the vault
lives in the Secret Service. Tests and test mode use the in-memory store.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Protocol

from ..storage import AtomicFileWriter, WriteFailed

# Secret Service `service` attribute of MultiCred's Claude login vault (D39).
CLAUDE_LOGIN_SERVICE = "app.multicred.claude-login"
# Claude Code creates `.credentials.json` with this mode.
CREDENTIALS_MODE = 0o600


class ClaudeCredentialStore(Protocol):
    def read_folder_item(self, config_dir: str | None) -> bytes | None: ...
    def write_folder_item(self, config_dir: str | None, data: bytes) -> None: ...
    def delete_folder_item(self, config_dir: str | None) -> None: ...
    def read_vault(self, key: str) -> bytes | None: ...
    def write_vault(self, key: str, data: bytes) -> None: ...
    def delete_vault(self, key: str) -> None: ...


def folder_key(config_dir: str | None) -> str:
    return config_dir or "~/.claude"


class InMemoryClaudeCredentialStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.folders: dict[str, bytes] = {}
        self.vault: dict[str, bytes] = {}

    def read_folder_item(self, config_dir):
        with self._lock:
            return self.folders.get(folder_key(config_dir))

    def write_folder_item(self, config_dir, data):
        with self._lock:
            self.folders[folder_key(config_dir)] = data

    def delete_folder_item(self, config_dir):
        with self._lock:
            self.folders.pop(folder_key(config_dir), None)

    def read_vault(self, key):
        with self._lock:
            return self.vault.get(key)

    def write_vault(self, key, data):
        with self._lock:
            self.vault[key] = data

    def delete_vault(self, key):
        with self._lock:
            self.vault.pop(key, None)


class InMemoryVaultBackend:
    """Same calls as SecretServiceBackend (store/lookup/clear), for tests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, bytes] = {}

    def store(self, account: str, value: bytes, label: str = "") -> None:
        with self._lock:
            self._items[account] = value

    def lookup(self, account: str) -> bytes | None:
        with self._lock:
            return self._items.get(account)

    def clear(self, account: str) -> None:
        with self._lock:
            self._items.pop(account, None)

    def accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._items)


class FileClaudeCredentialStore:
    """The real store on Linux.

    A folder's login is the file Claude Code itself reads and rewrites:
    `<folder>/.credentials.json` (`~/.claude/.credentials.json` for the default
    folder). The vault is the Secret Service (`app.multicred.claude-login`),
    keyed by account UUID or by a folder's own-login key.

    Mode policy: the file holds refresh tokens, so every write leaves it at
    0600, even when it existed with a looser mode (another tool may have
    created it 0644). A stricter existing mode that the owner can still read
    (e.g. 0400) is kept, because someone chose it on purpose and it is safe.
    """

    def __init__(self, home, vault=None):
        self.home = Path(home)
        self._vault = vault
        self._vault_lock = threading.Lock()

    @property
    def vault(self):
        """Built on first use, so creating the store never talks to D-Bus."""
        with self._vault_lock:
            if self._vault is None:
                from ..secret_service import SecretServiceBackend
                self._vault = SecretServiceBackend(CLAUDE_LOGIN_SERVICE)
            return self._vault

    def folder_file(self, config_dir: str | None) -> Path:
        folder = self.home / ".claude" if config_dir is None else Path(config_dir)
        return folder / ".credentials.json"

    def read_folder_item(self, config_dir):
        try:
            return self.folder_file(config_dir).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    def write_folder_item(self, config_dir, data):
        path = self.folder_file(config_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise WriteFailed(path.parent, error.strerror) from error
        target = AtomicFileWriter.resolve_target(path, follow_symlink=True)
        mode = CREDENTIALS_MODE
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode) & CREDENTIALS_MODE
        except FileNotFoundError:
            pass
        if not mode & stat.S_IRUSR:  # a file Claude Code could not read is no choice to keep
            mode = CREDENTIALS_MODE
        AtomicFileWriter.write(data, target, permissions=mode, keep_mode=False)

    def delete_folder_item(self, config_dir):
        try:
            self.folder_file(config_dir).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise WriteFailed(self.folder_file(config_dir), error.strerror) from error

    def read_vault(self, key):
        return self.vault.lookup(key)

    def write_vault(self, key, data):
        self.vault.store(key, data, label=f"MultiCred: login do Claude {key}")

    def delete_vault(self, key):
        self.vault.clear(key)
