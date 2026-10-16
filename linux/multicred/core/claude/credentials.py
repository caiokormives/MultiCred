"""Raw access to Claude Code's folder credentials and MultiCred's login vault (D39).

On Linux a folder's login is the file `<folder>/.credentials.json`; the vault
lives in the Secret Service. Tests and test mode use the in-memory store.
"""

from __future__ import annotations

import threading
from typing import Protocol


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
