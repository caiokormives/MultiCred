"""App state shared by the window and the tray. No GTK here: `dispatch` (GLib.idle_add
in the app, a direct call in tests) brings results back to the UI thread."""

from __future__ import annotations

import threading
from typing import Callable

from ..core.engine.switch_engine import SwitchEngine
from ..core.engine.types import StepFailed, SwitchError
from ..core.models import ActiveState, ClaudeAccountKind, ClaudeFolder, Config
from ..core.storage import ConfigRepository, JSONStoreError


class ModelError(Exception):
    """kinds: toolMissing (detail = the tool's name)."""

    def __init__(self, kind: str, detail: str | None = None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


class AppModel:
    def __init__(self, environment, tools, log=None, dispatch: Callable = lambda fn, *a: fn(*a)):
        self.environment = environment
        self.tools = tools
        self.log = log
        self.dispatch = dispatch
        self.repository = ConfigRepository(environment)
        self.engine = SwitchEngine(environment, tools, log)
        self.config: Config | None = None
        self.state = ActiveState()
        self.load_error: Exception | None = None
        self.switching = False
        # A switch failed and could not undo everything: the machine is half switched.
        # Cleared by the next successful switch (the tray shows the orange dot meanwhile).
        self.inconsistent = False
        self._listeners: list[Callable[[], None]] = []
        # Filled off the UI thread by refresh_github_logins / refresh_claude_statuses /
        # load_folder_summaries (S-06); empty means "not known yet", never "logged out".
        self.github_logins: list = []
        self.claude_statuses: dict = {}
        self.folder_summaries: dict = {}
        self._status_service = None
        # Last Verificar report (S-08): needs `ok_count` and `checks`; None = never ran.
        self.verification = None

    @property
    def is_test_mode(self) -> bool:
        return self.environment.mode == "dryRun"

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def load(self) -> None:
        try:
            self.config = self.repository.load()
            self.load_error = None
        except JSONStoreError as error:
            self.config, self.load_error = None, error
        try:
            self.state = self.engine.current_state()
        except SwitchError as error:
            self.load_error = error
        self.notify()

    def save(self, config: Config) -> None:
        self.repository.save(config)
        self.config = config
        self.notify()

    @property
    def active_profile(self):
        return self.config.profile(self.state.profile_id) if self.config else None

    def switch_profile(self, profile_id, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Runs the switch off the UI thread; `on_done(result, error)` runs on the UI thread."""
        self._run_switch(lambda config: self.engine.switch_profile(profile_id, config), on_done)

    def switch_claude_only(self, account_id, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Switches only the Claude account (None = the profile folder's own login), like the
        Mac `switchClaudeOnly(to:)`: choosing the profile's own account clears the override."""
        override = ClaudeFolder.override_for(account_id, self.active_profile)
        self._run_switch(lambda config: self.engine.switch_claude_only(override, config), on_done)

    def _run_switch(self, operation, on_done) -> None:
        config = self.config
        self.switching = True
        self.notify()

        def work():
            try:
                result, error = operation(config), None
            except SwitchError as failure:
                result, error = None, failure
            self.dispatch(self._finish, result, error, on_done)

        threading.Thread(target=work, name="multicred-switch", daemon=True).start()

    # MARK: onboarding (S-05)

    def detect_setup(self, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Runs the setup detection off the UI thread (it spawns git/gh/claude --version)."""
        from ..core.install.setup_detector import SetupDetector

        def work():
            try:
                setup, error = SetupDetector(self.environment, self.tools).detect(), None
            except Exception as failure:  # detection is best effort; the page shows the error
                setup, error = None, failure
            self.dispatch(_once, on_done, setup, error)

        threading.Thread(target=work, name="multicred-detect", daemon=True).start()

    def preview_install(self, options):
        """The per-file diff of step 3. Reads a few small files; raises InstallError."""
        from ..core.install.installer import Installer
        return Installer(self.environment).preview(options)

    def complete_onboarding(self, config: Config, options, initial_profile_id,
                            on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Onboarding's last step: install, save the configuration, activate the initial profile.

        The installer runs before the save (the Mac saves first): if it fails nothing
        is configured and the onboarding can simply be run again.
        """
        from ..core.install.installer import Installer
        from ..core.storage import InvalidConfig
        from ..core.validation import issues_in

        config.settings.enabled_shells = list(options.shells)

        def work():
            try:
                issues = issues_in(config)
                if issues:
                    raise InvalidConfig(issues)
                Installer(self.environment).apply(options)
                self.repository.save(config)
            except Exception as failure:
                self.dispatch(_once, on_done, None, failure)
                return
            self.dispatch(_once, self._onboarding_saved, config, initial_profile_id, on_done)

        threading.Thread(target=work, name="multicred-onboarding", daemon=True).start()

    def _onboarding_saved(self, config, initial_profile_id, on_done) -> None:
        self.config = config
        self.load_error = None
        self.notify()
        if initial_profile_id is None or config.profile(initial_profile_id) is None:
            on_done(None, None)
            return
        self.switch_profile(initial_profile_id, on_done)

    # MARK: configuration edits (S-06)

    def edit(self, change: Callable[[Config], Config]) -> Config:
        """Applies a ConfigEditor change, saves it through the repository and notifies.

        Raises what the change or the save raises (EditorError, InvalidConfig,
        OSError); nothing is kept in memory unless the file was written.
        """
        updated = change(self.config if self.config is not None else Config())
        self.repository.save(updated)
        self.config = updated
        self.notify()
        return updated

    def add_api_key(self, nickname, provider, env_var, secret, color):
        """Stores the value in the secret store, then saves the configuration. A failed
        save deletes the secret again: no orphan secret without a configuration."""
        from ..core.editors import ConfigEditor

        updated, key = ConfigEditor.add_api_key(nickname, provider, env_var, secret, color,
                                                self.config or Config(), self.environment.secrets)
        try:
            self.repository.save(updated)
        except Exception:
            try:
                self.environment.secrets.delete_item(key.secret_account)
            except Exception:
                pass
            raise
        self.config = updated
        self.notify()
        return key

    def replace_secret(self, key, secret: str) -> None:
        from ..core.editors import ConfigEditor
        ConfigEditor.replace_secret(key, secret, self.environment.secrets)
        self.notify()

    def has_secret(self, key) -> bool:
        try:
            return bool(self.environment.secrets.contains_item(key.secret_account))
        except Exception:
            return False

    def remove_api_key(self, key_id) -> None:
        from ..core.editors import ConfigEditor
        self.edit(lambda config: ConfigEditor.remove_api_key(key_id, config, self.environment.secrets))

    def remove_claude_account(self, account_id) -> None:
        """Removes the account and its secret; a login account live in a folder gives the
        folder its own login back first. Shells stop exporting a removed token that was
        in use (Claude-only switch back to the folder login), like the Mac."""
        from ..core.claude import account_editor
        from ..core.claude.login_vault import ClaudeLoginSwitcher

        was_applied = self.state.applied_claude_account_id == account_id
        switcher = ClaudeLoginSwitcher(self.environment)
        self.edit(lambda config: account_editor.remove(account_id, config, self.environment.secrets, switcher))
        if was_applied and self.active_profile is not None and not self.switching:
            self.switch_claude_only(None, lambda _result, _error: None)

    def has_token(self, account) -> bool:
        """Login accounts live in the vault (checked by the switch preflight, not on every
        render); token accounts need their secret."""
        if account.kind == ClaudeAccountKind.LOGIN:
            return True
        return self.has_secret(account)

    # MARK: slow lookups, off the UI thread (S-06)

    def _background(self, name: str, work: Callable[[], object], on_done) -> None:
        def run():
            try:
                result, error = work(), None
            except Exception as failure:
                result, error = None, failure
            self.dispatch(_once, on_done, result, error)

        threading.Thread(target=run, name=f"multicred-{name}", daemon=True).start()

    def _github_cli(self):
        from ..core.adapters.github_cli import GitHubCLI
        from ..core.tools import Tool

        gh = self.tools.locate(Tool.GH) if self.tools is not None else None
        if gh is None:
            raise ModelError("toolMissing", "gh")
        return GitHubCLI(self.environment.runner, gh,
                         self.environment.base_process_environment(self.tools.search_directories()))

    def github_login_for(self, account):
        return next((login for login in self.github_logins if login.host == account.host
                     and login.login.lower() == account.username.lower()), None)

    def refresh_github_logins(self, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """`gh auth status` tests every token online: never on the UI thread."""
        def finished(logins, error):
            if logins is not None:
                self.github_logins = logins
                self.notify()
            on_done(logins, error)

        self._background("gh-logins", lambda: self._github_cli().logins(), finished)

    def import_github_accounts(self, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Adds the gh logins MultiCred does not know yet; `on_done(added_count, error)`."""
        from ..core.editors import ConfigEditor

        def finished(logins, error):
            if error is not None:
                on_done(None, error)
                return
            self.github_logins = logins
            before = len(self.config.github_accounts) if self.config else 0
            try:
                updated = self.edit(lambda config: ConfigEditor.add_missing_github_accounts(logins, config))
            except Exception as failure:
                on_done(None, failure)
                return
            on_done(len(updated.github_accounts) - before, None)

        self._background("gh-import", lambda: self._github_cli().logins(), finished)

    @property
    def claude_folders_in_use(self) -> list[str | None]:
        """The default folder plus every profile's folder, in order, without repeats."""
        folders: list[str | None] = [None]
        for profile in (self.config.profiles if self.config else []):
            if profile.claude_config_dir not in folders:
                folders.append(profile.claude_config_dir)
        return folders

    def folder_name(self, config_dir: str | None) -> str:
        return self.environment.abbreviate(config_dir) if config_dir is not None else "~/.claude"

    def folder_status(self, config_dir: str | None):
        return self.claude_statuses.get(ClaudeFolder.own_login(config_dir).id)

    def needs_claude_login(self, profile) -> bool:
        """A profile cannot really be used while its Claude credential is missing."""
        account = self.config.claude_account(profile.claude_account_id) if self.config else None
        if account is not None:
            return not self.has_token(account)
        status = self.folder_status(profile.claude_config_dir)
        return status is not None and status.kind == "loggedOut"

    def refresh_claude_statuses(self, on_done: Callable[[], None] | None = None) -> None:
        """Login status of every folder in use (`claude auth status` takes ~1 s each)."""
        from ..core.claude.status import ClaudeStatusService

        if self._status_service is None:
            self._status_service = ClaudeStatusService(self.environment, self.tools)
        service, folders = self._status_service, self.claude_folders_in_use

        def work():
            return {ClaudeFolder.own_login(d).id: service.status(ClaudeFolder.own_login(d)) for d in folders}

        def finished(statuses, _error):
            if statuses:
                self.claude_statuses.update(statuses)
                self.notify()
            if on_done is not None:
                on_done()

        self._background("claude-status", work, finished)

    def load_folder_summaries(self, on_done: Callable[[], None] | None = None) -> None:
        """Conversation counts of the folders in use (reads metadata lines only)."""
        from ..core.claude import folder_inspector

        folders = self.claude_folders_in_use

        def work():
            return {d: folder_inspector.summarize(d, self.environment) for d in folders}

        def finished(summaries, _error):
            if summaries:
                self.folder_summaries.update(summaries)
                self.notify()
            if on_done is not None:
                on_done()

        self._background("claude-folders", work, finished)

    # MARK: Verificar

    def verify(self, on_done: Callable[[object | None, Exception | None], None] | None = None,
               deep: bool = False) -> None:
        """Runs the checklist off the UI thread and keeps the report in `verification`
        (the sidebar badge, the Perfis footer and the tray read it)."""
        from ..core.engine.verifier import Verifier
        from .pages.verify import VerifyPage

        config, state = self.config, self.state
        if config is None:
            if on_done is not None:
                on_done(None, None)
            return
        verifier = Verifier(self.environment, self.tools, VerifyPage._default_scanner(self.environment),
                            engine=self.engine, log=self.log)

        def finished(report, error):
            if report is not None:
                self.verification = report
                self.notify()
            if on_done is not None:
                on_done(report, error)

        self._background("verify", lambda: verifier.run(config, state, deep=deep), finished)

    def _finish(self, result, error, on_done):
        self.switching = False
        if result is not None:
            self.state = result.state
            self.inconsistent = False
        elif isinstance(error, StepFailed) and not error.rollback.complete:
            self.inconsistent = True
        on_done(result, error)
        self.notify()
        return False  # GLib.idle_add: run once


def _once(fn, *args):
    """Runs `fn` and tells GLib.idle_add not to call it again."""
    fn(*args)
    return False
