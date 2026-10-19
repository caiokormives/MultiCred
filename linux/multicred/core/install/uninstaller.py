"""Removes everything MultiCred added (docs/PLAN.md §5.7, D12).

```
 Claude folders: logins MultiCred put in them go back to each folder's own login,
                 co-author opt-out undone, login vault emptied
 [D12, opt-in]   folders MultiCred created: `claude auth logout`, then deleted
 rc files, global git config: only the marked block (a begin marker without its
                 end marker is refused for that file: manual step, no partial edit)
 fish conf.d file (only if ours), environment.d file, autostart file
 systemctl --user unset-environment CLAUDE_CONFIG_DIR (only if MultiCred set it)
 Secret Service: every API key / token item (`app.multicred.apikey`)
 ~/.config/multicred/backups -> ~/.config/multicred-backups-<yyyy-MM-dd-HHmmss>
 ~/.config/multicred: deleted
```
gh logins are never touched. Every step runs even when an earlier one failed;
failures end up in the report.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters.session_env import SessionEnvironment, environment_d_file
from ..claude import attribution
from ..claude.login_vault import ClaudeFolderOccupancy, ClaudeLoginSwitcher, OccupancyUnreadable
from ..models import ClaudeAccount, ClaudeAccountKind, Config, uuid_text
from ..paths import AppPaths
from ..runner import CommandRequest, run_checked
from ..storage import AtomicFileWriter, ConfigRepository, JSONStoreError, StateStore, read_bytes
from ..tools import Tool
from . import managed_block
from .autostart import Autostart
from .installer import Installer, Integrations

from ..engine.switch_engine import CLAUDE_VARIABLE  # noqa: E402
CREATED_FOLDERS_FILE = "created-claude-folders.json"


def _created_folders_file(environment) -> Path:
    return AppPaths(environment).root / CREATED_FOLDERS_FILE


def remember_created_claude_folder(environment, folder: str) -> None:
    """Records a Claude folder MultiCred created, so uninstall can offer to delete it (D12)."""
    path = _created_folders_file(environment)
    folders = _read_created(path)
    if folder not in folders:
        folders.append(folder)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        AtomicFileWriter.write((json.dumps({"folders": folders}, indent=2) + "\n").encode(), path,
                               permissions=0o600)


def _read_created(path: Path) -> list[str]:
    data = read_bytes(path)
    if data is None:
        return []
    try:
        folders = json.loads(data).get("folders", [])
    except (ValueError, UnicodeDecodeError, AttributeError):
        return []
    return [f for f in folders if isinstance(f, str)]


@dataclass(frozen=True)
class UninstallItem:
    """One line of the confirmation dialog.

    kinds: block (marked block in `path`, `lines` removed), manual (unterminated
    block in `path`: left as is), file (removed), session (the variable unset),
    secrets (`count` items), claudeLogin (folder `path` gets its own login back),
    claudeVault (`count` saved logins), claudeFolder (D12: logged out and deleted),
    backups (moved to `path`), appDir (deleted).
    """

    kind: str
    path: str = ""
    lines: tuple[str, ...] = ()
    count: int = 0


@dataclass
class UninstallReport:
    changed_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    # Files with a begin marker and no end marker, left untouched.
    unterminated: list[str] = field(default_factory=list)
    backups_kept_at: str | None = None
    deleted_folders: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def manual_steps(self) -> list[str]:
        """What the user must do by hand (the Mac's UninstallReport.manualSteps text)."""
        return [f"Remova à mão, em {path}, as linhas a partir de \"{managed_block.BEGIN}\"."
                for path in self.unterminated]


class Uninstaller:
    def __init__(self, environment, config: Config | None = None, tools=None, autostart: Autostart | None = None):
        self.environment = environment
        self.paths = AppPaths(environment)
        self._config = config
        self.tools = tools
        self.autostart = autostart or Autostart(environment)

    # MARK: what is there

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = ConfigRepository(self.environment).load() or Config()
            except (JSONStoreError, OSError):
                self._config = Config()
        return self._config

    def _block_files(self) -> list[tuple[Path, str]]:
        """(target, text) of every rc/git file that has a MultiCred begin marker."""
        found, seen = [], set()
        for file in Integrations.all_block_files(self.environment):
            try:
                target = AtomicFileWriter.resolve_target(file, follow_symlink=True)
            except OSError:
                continue
            data = read_bytes(target)
            if data is None or target in seen:
                continue
            text = data.decode("utf-8", "surrogateescape")
            if managed_block.contains(text):
                seen.add(target)
                found.append((target, text))
        return found

    def _fish_is_ours(self) -> bool:
        data = read_bytes(Integrations.fish_conf_file(self.environment))
        return data is not None and managed_block.contains(data.decode("utf-8", "replace"))

    @property
    def environment_d_file(self) -> Path:
        return environment_d_file(self.environment)

    def _multicred_set_session(self) -> bool:
        """MultiCred only ever sets CLAUDE_CONFIG_DIR in a switch: a state with a
        profile, or its environment.d file, says it did."""
        if os.path.lexists(self.environment_d_file):
            return True
        try:
            return StateStore(self.paths, self.environment.clock).load().profile_id is not None
        except (JSONStoreError, OSError):
            return False

    def _claude_folders(self) -> list[str | None]:
        """The default folder, every profile's folder and every folder holding a login."""
        folders: list[str | None] = [None]
        for profile in self.config.profiles:
            if profile.claude_config_dir not in folders:
                folders.append(profile.claude_config_dir)
        for key in self._occupancy().account_by_folder:
            folder = ClaudeFolderOccupancy.config_dir_for_key(key)
            if folder not in folders:
                folders.append(folder)
        return folders

    def _occupancy(self) -> ClaudeFolderOccupancy:
        """Unreadable reads as empty here; `_restore_claude` reports it and keeps the vault."""
        try:
            return ClaudeLoginSwitcher(self.environment).occupancy()
        except OccupancyUnreadable:
            return ClaudeFolderOccupancy()

    def _occupancy_readable(self) -> bool:
        try:
            ClaudeLoginSwitcher(self.environment).occupancy()
            return True
        except OccupancyUnreadable:
            return False

    def _login_accounts(self) -> list[ClaudeAccount]:
        return [a for a in self.config.claude_accounts if a.kind == ClaudeAccountKind.LOGIN]

    def created_claude_folders(self) -> list[str]:
        """Folders MultiCred created that the D12 option may delete. Never the default
        folder, never anything outside home, never home itself."""
        home = os.path.realpath(self.environment.home)
        default = os.path.realpath(self.environment.home / ".claude")
        safe = []
        for folder in _read_created(_created_folders_file(self.environment)):
            if not os.path.isabs(folder) or ".." in Path(folder).parts:
                continue
            real = os.path.realpath(folder)
            if real == default or not real.startswith(home + "/") or not os.path.isdir(real):
                continue
            safe.append(folder)
        return safe

    def _backups_destination(self) -> Path:
        stamp = Installer.stamp(self.environment.clock())
        return self.paths.root.parent / f"multicred-backups-{stamp}"

    def _has_backups(self) -> bool:
        try:
            return any(self.paths.backups.iterdir())
        except OSError:
            return False

    def _abbreviate(self, path) -> str:
        return self.environment.abbreviate(str(path))

    # MARK: preview

    def preview(self, remove_claude_folders: bool = False) -> list[UninstallItem]:
        """Everything apply() would remove, for the confirmation. Changes nothing, runs nothing."""
        items: list[UninstallItem] = []
        for key in self._occupancy().account_by_folder:
            folder = ClaudeFolderOccupancy.config_dir_for_key(key)
            items.append(UninstallItem("claudeLogin", self._folder_name(folder)))
        if remove_claude_folders:
            items += [UninstallItem("claudeFolder", self._abbreviate(f)) for f in self.created_claude_folders()]
        for target, text in self._block_files():
            body = managed_block.body(text)
            if body is None:
                items.append(UninstallItem("manual", self._abbreviate(target)))
            else:
                lines = (managed_block.BEGIN, *body.split("\n"), managed_block.END)
                items.append(UninstallItem("block", self._abbreviate(target), lines))
        if self._fish_is_ours():
            items.append(UninstallItem("file", self._abbreviate(Integrations.fish_conf_file(self.environment))))
        if os.path.lexists(self.environment_d_file):
            items.append(UninstallItem("file", self._abbreviate(self.environment_d_file)))
        if os.path.lexists(self.autostart.file):
            items.append(UninstallItem("file", self._abbreviate(self.autostart.file)))
        if self._multicred_set_session():
            items.append(UninstallItem("session", CLAUDE_VARIABLE))
        try:
            count = len(self.environment.secrets.all_accounts())
        except Exception:  # a locked keyring: apply() reports it
            count = 0
        if count:
            items.append(UninstallItem("secrets", count=count))
        logins = self._login_accounts()
        if logins and self._occupancy_readable():
            items.append(UninstallItem("claudeVault", count=len(logins)))
        if self._has_backups():
            items.append(UninstallItem("backups", self._abbreviate(self._backups_destination())))
        if self.paths.root.exists():
            items.append(UninstallItem("appDir", self._abbreviate(self.paths.root)))
        return items

    def _folder_name(self, folder: str | None) -> str:
        return self._abbreviate(folder) if folder is not None else "~/.claude"

    # MARK: apply

    def apply(self, remove_claude_folders: bool = False) -> UninstallReport:
        report = UninstallReport()
        # Read before anything is deleted.
        unset_session = self._multicred_set_session()
        created = self.created_claude_folders() if remove_claude_folders else []
        self._restore_claude(report)
        for folder in created:
            self._log_out_and_delete(folder, report)
        self._remove_blocks(report)
        self._remove_files(report)
        if unset_session:
            self._unset_session(report)
        self._delete_secrets(report)
        self._remove_app_directory(report)
        return report

    def _restore_claude(self, report: UninstallReport) -> None:
        switcher = ClaudeLoginSwitcher(self.environment)
        try:
            occupancy = switcher.occupancy()
        except OccupancyUnreadable as error:
            # Which folder holds which account is unknown: the vault may hold the only
            # copy of a folder's own login, so it is kept whole.
            occupancy = None
            report.problems.append(
                f"{error} Os logins do Claude guardados pelo MultiCred foram mantidos no chaveiro do sistema "
                "(serviço app.multicred.claude-login), e as pastas do Claude ficaram como estão.")
        # Listed before the own logins go back: that empties the occupancy, and a folder
        # no profile uses any more is known only from it.
        folders = self._claude_folders()
        # A folder holding a MultiCred login gets its own login back first: the own
        # login lives in the vault that is emptied right after. A folder whose restore
        # fails keeps both vault entries it still needs: its own login (the only copy)
        # and the account that is still live in it.
        kept: set[str] = set()
        for key, account in list(occupancy.account_by_folder.items() if occupancy else ()):
            folder = ClaudeFolderOccupancy.config_dir_for_key(key)
            try:
                switcher.place(None, folder)
            except Exception as error:
                kept.update({ClaudeLoginSwitcher.own_login_key(folder), uuid_text(account)})
                report.problems.append(
                    f"{self._folder_name(folder)}: não foi possível devolver o login próprio da pasta ({error}). "
                    f"Ela continua logada na conta {self._account_name(account)}; o login original dela e o "
                    "dessa conta foram mantidos no chaveiro do sistema, nada foi perdido.")
        # Undo the co-author opt-out MultiCred wrote (only that exact value, D38).
        for folder in folders:
            path = attribution.settings_file(folder, self.environment)
            try:
                target = AtomicFileWriter.resolve_target(path, follow_symlink=True)
                restored = attribution.updated(read_bytes(target), co_author=True)
                if restored is not None:
                    AtomicFileWriter.write(restored, target, permissions=0o644)
                    report.changed_files.append(self._abbreviate(target))
            except attribution.UnreadableSettings:
                continue
            except OSError as error:
                report.problems.append(f"{self._abbreviate(path)}: {error}")
        if occupancy is None:
            return
        keys = [uuid_text(a.id) for a in self._login_accounts()]
        keys += [ClaudeLoginSwitcher.own_login_key(folder) for folder in folders]
        store = self.environment.claude_credentials
        # Also own logins saved for folders no profile uses any more: the vault is listed.
        try:
            keys += [k for k in store.vault_keys() if k not in keys]
        except Exception as error:
            report.problems.append(f"Cofre de logins do Claude: {error}")
        for key in (k for k in keys if k not in kept):
            try:
                store.delete_vault(key)
            except Exception as error:
                report.problems.append(f"Cofre de logins do Claude: {error}")
                break

    def _account_name(self, account) -> str:
        found = next((a for a in self.config.claude_accounts if a.id == account), None)
        return f"\"{found.nickname}\"" if found is not None else uuid_text(account)

    def _log_out_and_delete(self, folder: str, report: UninstallReport) -> None:
        """D12: Claude Code logs the folder out itself, then the folder goes. A failed
        logout keeps the folder (its login would otherwise linger at Anthropic)."""
        name = self._abbreviate(folder)
        claude = self.tools.locate(Tool.CLAUDE) if self.tools is not None else None
        if claude is None:
            report.problems.append(f"{name}: Claude Code não encontrado; a pasta foi mantida")
            return
        search = self.tools.search_directories()
        environment = self.environment.base_process_environment(search)
        environment[CLAUDE_VARIABLE] = folder
        environment["NO_COLOR"] = "1"
        try:
            run_checked(self.environment.runner, CommandRequest(claude, ["auth", "logout"], environment, timeout=20))
        except Exception as error:
            report.problems.append(f"{name}: claude auth logout falhou ({error}); a pasta foi mantida")
            return
        try:
            shutil.rmtree(folder)
        except OSError as error:
            report.problems.append(f"{name}: {error}")
            return
        report.deleted_folders.append(name)

    def _remove_blocks(self, report: UninstallReport) -> None:
        manifest = Installer(self.environment).manifest()
        created = {f.path for f in manifest.files if f.created_by_multicred} if manifest else set()
        for target, text in self._block_files():
            name = self._abbreviate(target)
            try:
                cleaned = managed_block.remove(text)
            except managed_block.Unterminated:
                report.unterminated.append(name)
                continue
            try:
                if str(target) in created and not cleaned.strip():
                    target.unlink()
                    report.removed_files.append(name)
                else:
                    AtomicFileWriter.write(cleaned.encode("utf-8", "surrogateescape"), target,
                                           follow_symlink=True)
                    report.changed_files.append(name)
            except OSError as error:
                report.problems.append(f"{name}: {error}")

    def _remove_files(self, report: UninstallReport) -> None:
        files = []
        if self._fish_is_ours():
            files.append(Integrations.fish_conf_file(self.environment))
        files += [self.environment_d_file, self.autostart.file]
        for path in files:
            if not os.path.lexists(path):
                continue
            try:
                path.unlink()
                report.removed_files.append(self._abbreviate(path))
            except OSError as error:
                report.problems.append(f"{self._abbreviate(path)}: {error}")

    def _unset_session(self, report: UninstallReport) -> None:
        # The same path a switch uses (session bus included), so both stay in step.
        session = SessionEnvironment(self.environment, self.environment.base_process_environment(["/usr/bin", "/bin"]))
        try:
            session.apply(CLAUDE_VARIABLE, None)
        except Exception as error:
            report.problems.append(f"systemctl --user unset-environment {CLAUDE_VARIABLE}: {error}")

    def _delete_secrets(self, report: UninstallReport) -> None:
        try:
            for account in self.environment.secrets.all_accounts():
                self.environment.secrets.delete_item(account)
        except Exception as error:
            report.problems.append(f"Chaveiro do sistema: {error}")

    def _remove_app_directory(self, report: UninstallReport) -> None:
        if self._has_backups():
            destination = self._backups_destination()
            try:
                os.rename(self.paths.backups, destination)
                report.backups_kept_at = self._abbreviate(destination)
            except OSError as error:
                report.problems.append(f"backups: {error}")
                return  # never delete the folder that still holds the backups
        if self.paths.root.exists():
            try:
                shutil.rmtree(self.paths.root)
            except OSError as error:
                report.problems.append(f"{self._abbreviate(self.paths.root)}: {error}")
