"""Secrets as private files, for Termux (Android has no Secret Service).

API keys and Claude tokens: one file per account, `~/.config/multicred/secrets/<UUID>`
(folder 0700, file 0600, the value's bytes with no trailing newline). The shell
hook reads it with `IFS= read -r` (see rendering/envfile.py), so no program runs
at each prompt. The Claude login vault (D39) lives in `secrets/claude-login/`,
same modes. Only validated upper-case UUIDs ever name a file, so a name can never
leave the folder. Like the Secret Service, this protects the values from other
users and apps, not from the account owner (the user chose plain 0600 files over
a password-protected store that would ask for a password at every prompt).
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from .secrets import EmptySecret, SecretError, validate_account
from .storage import AtomicFileWriter, FileWriteError

# Relative to MultiCred's folder (~/.config/multicred).
SECRETS_DIRECTORY_NAME = "secrets"
CLAUDE_LOGIN_DIRECTORY_NAME = "claude-login"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class SecretFileError(SecretError):
    pass


def _private_directories(directories: tuple[Path, ...]) -> None:
    """Creates each folder 0700 (outermost first) and tightens one that exists with
    another mode. A symlink or a file in their place is refused."""
    for directory in directories:
        try:
            try:
                directory.mkdir(parents=True, mode=DIRECTORY_MODE)
            except FileExistsError:
                pass
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode):
                raise SecretFileError(f"{directory} is not a folder")
            if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
                os.chmod(directory, DIRECTORY_MODE)
        except OSError as error:
            raise SecretFileError(f"{directory}: {error.strerror or error}") from error


class _SecretFiles:
    """One private file per validated account name in `directory`. `private` are the
    folders kept 0700 (the vault keeps both `secrets/` and `secrets/claude-login/`)."""

    def __init__(self, directory, private: tuple[Path, ...] = ()):
        self.directory = Path(directory)
        self._private = (*private, self.directory)
        self._lock = threading.Lock()

    def path(self, account: str) -> Path:
        validate_account(account)
        return self.directory / account

    def write(self, account: str, value: bytes) -> None:
        path = self.path(account)
        with self._lock:
            _private_directories(self._private)
            try:
                # Temp file + fsync + rename; a symlink in place of the file is refused.
                AtomicFileWriter.write(value, path, permissions=FILE_MODE, keep_mode=False)
            except FileWriteError as error:
                raise SecretFileError(str(error)) from error

    def read(self, account: str) -> bytes | None:
        path = self.path(account)
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise SecretFileError(f"{path} is not a regular file")
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SecretFileError(f"{path}: {error.strerror or error}") from error

    def exists(self, account: str) -> bool:
        try:
            path = self.path(account)
        except SecretError:
            return False
        try:
            return stat.S_ISREG(os.lstat(path).st_mode)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SecretFileError(f"{path}: {error.strerror or error}") from error

    def delete(self, account: str) -> None:
        """Deleting a missing item is not an error."""
        try:
            path = self.path(account)
        except SecretError:
            return
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise SecretFileError(f"{path}: {error.strerror or error}") from error

    def accounts(self) -> list[str]:
        try:
            entries = list(os.scandir(self.directory))
        except FileNotFoundError:
            return []
        except OSError as error:
            raise SecretFileError(f"{self.directory}: {error.strerror or error}") from error
        names = []
        for entry in entries:
            try:
                validate_account(entry.name)
            except SecretError:
                continue
            if entry.is_file(follow_symlinks=False):
                names.append(entry.name)
        return sorted(names)


class FileSecretStore:
    """API keys and Claude tokens on Termux. Same interface as SecretServiceStore:
    writes and checks; never returns a value (the shell hook reads the file)."""

    def __init__(self, directory):
        self._files = _SecretFiles(directory)

    @property
    def directory(self) -> Path:
        return self._files.directory

    def set_secret(self, value: bytes, account: str) -> None:
        validate_account(account)
        if not value:
            raise EmptySecret()
        if any(byte in value for byte in b"\n\r\x00"):
            # The hook reads one line: anything after a line break would be lost.
            raise SecretFileError("The value has a line break")
        self._files.write(account, value)

    def contains_item(self, account: str) -> bool:
        return self._files.exists(account)

    def delete_item(self, account: str) -> None:
        self._files.delete(account)

    def all_accounts(self) -> list[str]:
        return self._files.accounts()


class FileVaultBackend:
    """MultiCred's Claude login vault on Termux: the store/lookup/clear/accounts calls of
    SecretServiceBackend, one 0600 file per key (account UUID or a folder's own-login UUID)."""

    def __init__(self, directory):
        directory = Path(directory)
        self._files = _SecretFiles(directory, private=(directory.parent,))

    @property
    def directory(self) -> Path:
        return self._files.directory

    def store(self, account: str, value: bytes, label: str = "") -> None:
        self._files.write(account, value)

    def lookup(self, account: str) -> bytes | None:
        return self._files.read(account)

    def exists(self, account: str) -> bool:
        return self._files.exists(account)

    def clear(self, account: str) -> None:
        self._files.delete(account)

    def accounts(self) -> list[str]:
        return self._files.accounts()


def secrets_directory(environment) -> Path:
    """~/.config/multicred/secrets (named as $HOME/.config/multicred/secrets in env.sh)."""
    return environment.app_support_directory / SECRETS_DIRECTORY_NAME
