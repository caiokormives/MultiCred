"""Adds MultiCred to the user's shells and git, transactionally.

```
 plan (read only) ──► preview ──► apply: backup originals ─► write each file
                                                │ failure: restore every file
                                                ▼          already written
                                          manifest.json
```
Running it again with the same options changes nothing (idempotent).
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from ..models import ShellKind, date_text, parse_date
from ..paths import AppPaths
from ..rendering import gitconfig, hooks
from ..storage import AtomicFileWriter, FileWriteError, JSONFileStore, JSONStoreError, read_bytes, restore_file
from . import managed_block
from .legacy import LegacyMigration

# Relative to $HOME so the same rc line works for any user (and in the sandbox).
SUPPORT_PATH_IN_HOME = ".config/multicred"


class Integrations:
    """Where MultiCred hooks into shells and git, for one user (Linux)."""

    GIT_INCLUDE_BLOCK = f'[include]\n\tpath = "~/{SUPPORT_PATH_IN_HOME}/gitconfig"'

    @staticmethod
    def rc_files(shell: ShellKind, environment) -> list[Path]:
        """rc files that source the hook.

        bash: `~/.bashrc` always (Ubuntu's `~/.profile` sources it for login
        shells); `~/.bash_profile` only when it already exists, because then bash
        reads it instead of `~/.profile` and would skip `~/.bashrc`. fish uses a
        conf.d file instead (`fish_conf_file`).
        """
        home = Path(environment.home)
        if shell == ShellKind.ZSH:
            return [home / ".zshrc"]
        if shell == ShellKind.BASH:
            files = [home / ".bashrc"]
            if os.path.lexists(home / ".bash_profile"):
                files.append(home / ".bash_profile")
            return files
        return []

    @staticmethod
    def fish_conf_file(environment) -> Path:
        """fish loads every file in conf.d; MultiCred owns this one entirely."""
        return Path(environment.home) / ".config/fish/conf.d/multicred.fish"

    @staticmethod
    def rc_block(shell: ShellKind) -> str:
        hook = f"$HOME/{SUPPORT_PATH_IN_HOME}/shell/hook.{shell.value}"
        if shell == ShellKind.ZSH:
            return f'[ -r "{hook}" ] && source "{hook}"'
        if shell == ShellKind.BASH:
            return f'[ -r "{hook}" ] && . "{hook}"'
        return f'test -r "{hook}"; and source "{hook}"'

    @staticmethod
    def fish_conf_contents() -> str:
        return (f"{managed_block.BEGIN}\n# Created by MultiCred; removed on uninstall.\n"
                f"{Integrations.rc_block(ShellKind.FISH)}\n{managed_block.END}\n")

    @staticmethod
    def global_git_config(environment) -> Path:
        """The global git config git itself would use: `~/.gitconfig` when it exists
        (even as a symlink, so a broken one is refused instead of bypassed), else
        `$XDG_CONFIG_HOME/git/config` (`~/.config/git/config` when unset) when that
        exists, else a new `~/.gitconfig`."""
        home = Path(environment.home)
        primary = home / ".gitconfig"
        if os.path.lexists(primary):
            return primary
        base = Path(environment.xdg_config_home) if environment.xdg_config_home else home / ".config"
        xdg = base / "git" / "config"
        if xdg.exists():
            return xdg
        return primary

    @staticmethod
    def all_block_files(environment) -> list[Path]:
        """Every file that may contain a MultiCred block, installed or not."""
        files = [f for shell in ShellKind for f in Integrations.rc_files(shell, environment)]
        return files + [Integrations.global_git_config(environment)]


@dataclass
class InstallOptions:
    shells: list[ShellKind]
    remove_legacy: bool = False


class ChangeKind(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


@dataclass(frozen=True)
class FileChange:
    """One file the user sees in "Revise antes de aplicar"."""

    path: str  # abbreviated with ~
    kind: ChangeKind
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)


@dataclass
class TouchedFile:
    path: str
    # The file did not exist before MultiCred created it.
    created_by_multicred: bool
    # Copy of the file before MultiCred first changed it.
    backup: str | None = None

    def to_dict(self) -> dict:
        data = {"path": self.path, "createdByMultiCred": self.created_by_multicred}
        if self.backup is not None:
            data["backup"] = self.backup
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "TouchedFile":
        return cls(path=d["path"], created_by_multicred=bool(d["createdByMultiCred"]), backup=d.get("backup"))


@dataclass
class InstallManifest:
    """What the installer did, kept so uninstall and later installs know (same JSON as the Mac)."""

    current_version = 1

    installed_at: datetime
    shells: list[ShellKind] = field(default_factory=list)
    files: list[TouchedFile] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return {"version": self.version, "installedAt": date_text(self.installed_at),
                "shells": [s.value for s in self.shells], "files": [f.to_dict() for f in self.files]}

    @classmethod
    def from_dict(cls, d: dict) -> "InstallManifest":
        return cls(version=d["version"], installed_at=parse_date(d["installedAt"]),
                   shells=[ShellKind(s) for s in d.get("shells", [])],
                   files=[TouchedFile.from_dict(f) for f in d.get("files", [])])


class InstallError(Exception):
    def __init__(self, path: str, reason: str):
        super().__init__(f"Could not install into {path}: {reason}")
        self.path, self.reason = path, reason


class MarkerMismatch(InstallError):
    """A MultiCred begin marker without its end marker (hand edited): nothing is written."""

    def __init__(self, path: str):
        super().__init__(path, "MultiCred block without its end marker")


@dataclass(frozen=True)
class _PlannedWrite:
    target: Path
    original: bytes | None
    new: bytes | None
    show_in_preview: bool


def _decode(data: bytes | None) -> str:
    # surrogateescape keeps bytes that are not UTF-8 exactly as they were.
    return "" if data is None else data.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _lines(data: bytes | None) -> list[str]:
    return [] if data is None else data.decode("utf-8", "replace").split("\n")


class Installer:
    def __init__(self, environment):
        self.environment = environment
        self.paths = AppPaths(environment)

    # MARK: planning

    def _plan(self, options: InstallOptions) -> list[_PlannedWrite]:
        enabled = set(options.shells)
        writes: list[_PlannedWrite] = []

        # Files inside the support directory (owned by MultiCred).
        for shell in ShellKind:
            hook = self.paths.hook(shell)
            wanted = hooks.render(shell).encode() if shell in enabled else None
            writes.append(_PlannedWrite(hook, read_bytes(hook), wanted, False))
        if not self.paths.gitconfig.exists():
            writes.append(_PlannedWrite(self.paths.gitconfig, None, gitconfig.HEADER.encode(), False))

        # rc files: block for enabled shells, no block for the others.
        for shell in ShellKind:
            for file in Integrations.rc_files(shell, self.environment):
                body = Integrations.rc_block(shell) if shell in enabled else None
                write = self._plan_block(file, body, options.remove_legacy)
                if write is not None:
                    writes.append(write)

        # fish: one whole file in conf.d.
        fish = Integrations.fish_conf_file(self.environment)
        fish_original = read_bytes(fish)
        fish_wanted = Integrations.fish_conf_contents().encode() if ShellKind.FISH in enabled else None
        if fish_original != fish_wanted and (fish_wanted is not None or self._is_ours(fish_original)):
            writes.append(_PlannedWrite(fish, fish_original, fish_wanted, True))

        # Global git config: include block at the very end.
        git = Integrations.global_git_config(self.environment)
        write = self._plan_block(git, Integrations.GIT_INCLUDE_BLOCK, options.remove_legacy)
        if write is not None:
            writes.append(write)
        return [w for w in writes if w.original != w.new]

    def _plan_block(self, file: Path, body: str | None, remove_legacy: bool) -> _PlannedWrite | None:
        """None when the file needs no change. A missing file without a block to add stays missing."""
        try:
            target = AtomicFileWriter.resolve_target(file, follow_symlink=True)
        except FileWriteError as error:
            raise InstallError(self.environment.abbreviate(str(file)), getattr(error, "reason", str(error))) from error
        original = read_bytes(target)
        if original is None and body is None:
            return None
        text = _decode(original)
        if remove_legacy:
            text = LegacyMigration.remove_legacy_lines(text)
        try:
            text = managed_block.append_at_end(body, text) if body is not None else managed_block.remove(text)
        except managed_block.Unterminated:
            raise MarkerMismatch(self.environment.abbreviate(str(target))) from None
        new = _encode(text)
        return None if new == original else _PlannedWrite(target, original, new, True)

    @staticmethod
    def _is_ours(data: bytes | None) -> bool:
        return data is not None and managed_block.contains(_decode(data))

    # MARK: preview

    def preview(self, options: InstallOptions) -> list[FileChange]:
        changes = []
        for write in self._plan(options):
            if not write.show_in_preview:
                continue
            before, after = _lines(write.original), _lines(write.new)
            added, removed = [], []
            matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag in ("delete", "replace"):
                    removed += before[i1:i2]
                if tag in ("insert", "replace"):
                    added += after[j1:j2]
            kind = (ChangeKind.DELETE if write.new is None
                    else ChangeKind.CREATE if write.original is None else ChangeKind.MODIFY)
            changes.append(FileChange(path=self.environment.abbreviate(str(write.target)), kind=kind,
                                      added_lines=[l for l in added if l], removed_lines=[l for l in removed if l]))
        return changes

    # MARK: apply

    def apply(self, options: InstallOptions) -> InstallManifest:
        writes = self._plan(options)
        stamp = self.stamp(self.environment.clock())
        manifest = self.manifest() or InstallManifest(installed_at=self.environment.clock())

        root = self.paths.root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        done: list[_PlannedWrite] = []
        current = root
        try:
            for write in writes:
                current = write.target
                known = any(f.path == str(write.target) for f in manifest.files)
                if write.show_in_preview and not known:
                    if write.original is not None:
                        backup = self._backup_copy(write.original, write.target, stamp)
                        manifest.files.append(TouchedFile(str(write.target), False, str(backup)))
                    else:
                        manifest.files.append(TouchedFile(str(write.target), True, None))
                self._perform(write.new, write.target)
                done.append(write)
            current = self.paths.manifest
            manifest.shells = list(options.shells)
            manifest.installed_at = self.environment.clock()
            self._store().save(manifest)
        except OSError as error:
            for write in reversed(done):
                try:
                    self._perform(write.original, write.target)
                except OSError:
                    pass
            reason = getattr(error, "reason", None) or error.strerror or str(error)
            raise InstallError(self.environment.abbreviate(str(current)), reason) from error
        return manifest

    def _perform(self, data: bytes | None, path: Path) -> None:
        if data is not None:
            AtomicFileWriter.write(data, path, permissions=0o644, follow_symlink=True)
        else:
            restore_file(None, path)

    def _backup_copy(self, data: bytes, file: Path, stamp: str) -> Path:
        name = self.environment.abbreviate(str(file)).replace("~/", "", 1).replace("/", "__")
        directory = self.paths.backups / stamp
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return AtomicFileWriter.write(data, directory / name, permissions=0o600)

    @staticmethod
    def stamp(date: datetime) -> str:
        """Backup folder name in local time, like the Mac (`yyyy-MM-dd-HHmmss`)."""
        return date.astimezone().strftime("%Y-%m-%d-%H%M%S")

    def _store(self) -> JSONFileStore:
        return JSONFileStore(self.paths.manifest, InstallManifest, self.environment.clock)

    def manifest(self) -> InstallManifest | None:
        """Installed means the manifest exists."""
        try:
            return self._store().load()
        except JSONStoreError:
            return None
