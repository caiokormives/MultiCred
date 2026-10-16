"""Atomic file writes, versioned JSON documents, config.json and state.json."""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import ActiveState, Config, ConfigMigration
from .paths import AppPaths


class FileWriteError(OSError):
    pass


class WriteFailed(FileWriteError):
    def __init__(self, path, reason):
        super().__init__(f"Could not write {path}: {reason}")
        self.path, self.reason = str(path), reason


class UnsafeTarget(FileWriteError):
    def __init__(self, path, reason):
        super().__init__(f"Refusing to write {path}: {reason}")
        self.path, self.reason = str(path), reason


class AtomicFileWriter:
    """Temp file in the same directory, fsync, rename: a crash leaves the old file or the new one."""

    @staticmethod
    def write(data: bytes, path, permissions: int = 0o644, follow_symlink: bool = False,
              keep_mode: bool = True) -> Path:
        """Writes `data`. A new file gets `permissions`; an existing file keeps its mode
        unless `keep_mode` is False (then it gets `permissions` too).

        With `follow_symlink`, a symlink target (stow, chezmoi) is written at its
        destination instead of replacing the link. Returns the path written.
        """
        target = AtomicFileWriter.resolve_target(Path(path), follow_symlink)
        directory = target.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise WriteFailed(directory, error.strerror) from error

        mode = permissions
        if keep_mode:
            try:
                mode = stat.S_IMODE(os.lstat(target).st_mode)
            except FileNotFoundError:
                pass

        temp = directory / f".{target.name}.multicred-{uuid.uuid4()}"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except OSError as error:
            raise WriteFailed(target, error.strerror) from error
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temp, target)
        except OSError as error:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise WriteFailed(target, error.strerror) from error
        return target

    @staticmethod
    def resolve_target(path: Path, follow_symlink: bool) -> Path:
        """The file a write to `path` would change, with the same symlink policy as `write`."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return path
        if stat.S_ISREG(info.st_mode):
            return path
        if not stat.S_ISLNK(info.st_mode):
            raise UnsafeTarget(path, "not a regular file")
        if not follow_symlink:
            raise UnsafeTarget(path, "is a symbolic link")
        destination = path.resolve()
        try:
            destination_info = os.lstat(destination)
        except FileNotFoundError:
            raise UnsafeTarget(path, "symbolic link points to a missing file") from None
        if not stat.S_ISREG(destination_info.st_mode):
            raise UnsafeTarget(path, "symbolic link does not point to a regular file")
        return destination


def read_bytes(path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def restore_file(data: bytes | None, path, permissions: int = 0o644) -> None:
    """Writes `data` back, or removes the file when it did not exist before."""
    path = Path(path)
    if data is not None:
        AtomicFileWriter.write(data, path, permissions)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise WriteFailed(path, error.strerror) from error


def dumps(data: dict) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


class JSONStoreError(Exception):
    pass


class Corrupt(JSONStoreError):
    """The file could not be decoded. It was moved aside so the user keeps a copy."""

    def __init__(self, moved_to: str, reason: str):
        super().__init__(f"Unreadable configuration ({reason}); a copy was kept at {moved_to}")
        self.moved_to, self.reason = moved_to, reason


class UnsupportedVersion(JSONStoreError):
    """Written by a newer MultiCred; left untouched."""

    def __init__(self, found: int, supported: int):
        super().__init__(f"Configuration version {found} is newer than supported version {supported}")
        self.found, self.supported = found, supported


class ReadFailed(JSONStoreError):
    def __init__(self, path, reason):
        super().__init__(f"Could not read {path}: {reason}")
        self.path, self.reason = str(path), reason


class JSONFileStore:
    """One JSON document with a top-level `version`. Files are 0600 inside a 0700 directory."""

    def __init__(self, path, document_type, clock: Callable[[], datetime]):
        self.path = Path(path)
        self.document_type = document_type
        self.clock = clock

    def load(self):
        """Returns None when the file does not exist."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ReadFailed(self.path, error.strerror) from error
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise Corrupt(self._move_aside(), str(error)) from None
        version = parsed.get("version") if isinstance(parsed, dict) else None
        if isinstance(version, int) and version > self.document_type.current_version:
            raise UnsupportedVersion(version, self.document_type.current_version)
        try:
            return self.document_type.from_dict(parsed)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise Corrupt(self._move_aside(), f"{type(error).__name__}: {error}") from None

    def save(self, document) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        AtomicFileWriter.write(dumps(document.to_dict()), self.path, permissions=0o600)

    def _move_aside(self) -> str:
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        destination = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, destination)
        except OSError as error:
            raise ReadFailed(self.path, f"corrupt and could not be moved aside: {error.strerror}") from error
        return str(destination)


class InvalidConfig(ValueError):
    def __init__(self, issues):
        super().__init__(f"{len(issues)} validation issues")
        self.issues = issues


class ConfigRepository:
    """Loads and saves config.json. Missing file = not configured yet.

    Older versions are migrated on load (a copy is kept as config.json.v<n>).
    """

    def __init__(self, environment):
        self.path = AppPaths(environment).config
        self.store = JSONFileStore(self.path, Config, environment.clock)

    def load(self) -> Config | None:
        loaded = self.store.load()
        if loaded is None:
            return None
        migrated, changed = ConfigMigration.migrate(loaded)
        if changed:
            backup = self.path.with_name(f"config.json.v{loaded.version}")
            if not backup.exists():
                try:
                    AtomicFileWriter.write(self.path.read_bytes(), backup, permissions=0o600)
                except OSError:
                    pass
            self.save(migrated)
        return migrated

    def save(self, config: Config) -> None:
        """Refuses to save a configuration that could not be applied."""
        from .validation import issues_in
        issues = issues_in(config)
        if issues:
            raise InvalidConfig(issues)
        self.store.save(config)


class StateStore:
    """state.json plus the `generation` file the shell hooks watch.

    Write order matters: state.json first, generation last. Bumping the
    generation is the commit point of a switch; shells only change after it.
    """

    def __init__(self, paths: AppPaths, clock: Callable[[], datetime]):
        self.paths = paths
        self.store = JSONFileStore(paths.state, ActiveState, clock)

    def load(self) -> ActiveState:
        """A missing state file means nothing was ever applied."""
        return self.store.load() or ActiveState()

    def commit(self, state: ActiveState) -> None:
        self.store.save(state)
        AtomicFileWriter.write(f"{state.generation}\n".encode(), self.paths.generation, permissions=0o644)

    def snapshot(self) -> tuple[bytes | None, bytes | None]:
        return read_bytes(self.paths.state), read_bytes(self.paths.generation)

    def restore(self, snapshot: tuple[bytes | None, bytes | None]) -> None:
        """Restores both files even when one of them fails, then reports the first failure."""
        state, generation = snapshot
        failures = []
        for data, path, mode in ((generation, self.paths.generation, 0o644), (state, self.paths.state, 0o600)):
            try:
                restore_file(data, path, permissions=mode)
            except OSError as error:
                failures.append(error)
        if failures:
            raise failures[0]

