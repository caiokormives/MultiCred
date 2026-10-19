"""App state shared by the window and the tray. No GTK here: `dispatch` (GLib.idle_add
in the app, a direct call in tests) brings results back to the UI thread."""

from __future__ import annotations

import threading
from typing import Callable

from ..core.engine.switch_engine import SwitchEngine
from ..core.engine.types import Busy, StepFailed, SwitchError
from ..core.models import ActiveState, ClaudeAccountKind, ClaudeFolder, Config
from ..core.storage import ConfigRepository, JSONStoreError


class ModelError(Exception):
    """kinds: toolMissing (detail = the tool's name)."""

    def __init__(self, kind: str, detail: str | None = None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


class AppModel:
    def __init__(self, environment, tools, log=None, dispatch: Callable = lambda fn, *a: fn(*a),
                 processes=None, autostart=None):
        from ..core.install.autostart import Autostart

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
        # Switch requests started here and not finished yet (UI thread only): a request
        # refused with Busy must not clear `switching` while another one still runs.
        self._switches_in_flight = 0
        # A switch failed and could not undo everything: the machine is half switched.
        # Cleared by the next successful switch (the tray shows the orange dot meanwhile).
        self.inconsistent = False
        # S-27: a re-apply asked for while a switch ran (UI thread only); it runs once that
        # switch ends.
        self._reapply_pending = False
        # The Claude account in use was removed during a switch: reset Claude after it.
        self._claude_reset_pending = False
        self._listeners: list[Callable[[], None]] = []
        # Filled off the UI thread by refresh_github_logins / refresh_claude_statuses /
        # load_folder_summaries (S-06); empty means "not known yet", never "logged out".
        self.github_logins: list = []
        self.claude_statuses: dict = {}
        self.folder_summaries: dict = {}
        self._status_service = None
        # Last Verificar report (S-08): needs `ok_count` and `checks`; None = never ran.
        self.verification = None
        # S-09: Claude Code sessions counted after each switch (the feedback warns about them),
        # the XDG autostart entry, and listeners: switch_listeners(SwitchOutcome) after every
        # successful switch from anywhere; reapply_listeners(error) when the re-apply at
        # session start failed.
        self.processes = processes if processes is not None else _default_scanner(environment)
        self.autostart = autostart if autostart is not None else Autostart(environment)
        self.switch_listeners: list[Callable] = []
        self.reapply_listeners: list[Callable] = []

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
        self._run_switch(lambda config: self.engine.switch_profile(profile_id, config), on_done, claude_only=False)

    def switch_claude_only(self, account_id, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Switches only the Claude account (None = the profile folder's own login), like the
        Mac `switchClaudeOnly(to:)`: choosing the profile's own account clears the override."""
        override = ClaudeFolder.override_for(account_id, self.active_profile)
        self._run_switch(lambda config: self.engine.switch_claude_only(override, config), on_done, claude_only=True)

    def _run_switch(self, operation, on_done, claude_only: bool) -> None:
        config, previous = self.config, self.state
        self._switches_in_flight += 1
        self.switching = True
        self.notify()

        def work():
            # Whatever happens here must reach _finish, or the UI stays in "trocando".
            outcome = None
            try:
                result, error = operation(config), None
            except Exception as failure:
                result, error = None, failure
                if not isinstance(failure, SwitchError):
                    self._log("error", f"switch crashed: {failure!r}")
            if result is not None:
                try:
                    outcome = self._outcome(config, previous, result, claude_only)
                except Exception as failure:
                    # The machine did switch; only the feedback is lost.
                    self._log("error", f"switch feedback failed: {failure!r}")
            self.dispatch(self._finish, result, error, on_done, outcome)

        threading.Thread(target=work, name="multicred-switch", daemon=True).start()

    def _outcome(self, config, previous, result, claude_only: bool):
        """What the switch feedback shows; runs on the switch thread (it scans /proc)."""
        from .feedback import SwitchOutcome

        before = config.profile(previous.profile_id)
        after = config.profile(result.state.profile_id)
        claude_changed = (claude_only or before is None
                          or before.claude_config_dir != (after.claude_config_dir if after else None)
                          or previous.applied_claude_account_id != result.state.applied_claude_account_id)
        try:
            sessions = self.processes.claude_code_session_count()
        except Exception:  # informative only; never fails a switch
            sessions = 0
        account = config.claude_account(result.state.applied_claude_account_id)
        return SwitchOutcome(profile_name=after.name if after else None,
                             claude_account_name=account.nickname if account else None,
                             steps=tuple(result.applied_steps), claude_only=claude_only,
                             claude_changed=claude_changed, running_claude_sessions=sessions)

    # MARK: session start (S-09)

    def reapply_active_profile(self, on_done: Callable[[object | None, Exception | None], None] | None = None) -> bool:
        """At session start: systemd forgets the user environment on reboot, so the active
        profile is applied again, off the UI thread. Any failure marks the machine
        inconsistent and is reported once to `reapply_listeners`; there is no retry.
        Returns False when there is nothing to re-apply."""
        config = self.config
        if config is None or self.state.profile_id is None or self.switching:
            return False
        self._switches_in_flight += 1
        self.switching = True
        self.notify()

        def work():
            try:
                result, error = self.engine.reapply_active_profile(config), None
            except Exception as failure:  # whatever it is, the machine may be half applied
                result, error = None, failure
            self.dispatch(_once, self._reapplied, result, error, on_done)

        threading.Thread(target=work, name="multicred-reapply", daemon=True).start()
        return True

    def _reapplied(self, result, error, on_done) -> None:
        self._switches_in_flight = max(0, self._switches_in_flight - 1)
        self.switching = self._switches_in_flight > 0
        if error is None:
            if result is not None:
                self.state = result.state
                self._log("info", "reapplied active profile at session start")
            else:
                self._log("info", "nothing to re-apply at session start")
            self.inconsistent = False
        else:
            self.inconsistent = True
            self._log("error", f"reapply failed: {error}")
            for listener in list(self.reapply_listeners):
                listener(error)
        if on_done is not None:
            on_done(result, error)
        self.notify()
        self._run_pending_reapply()

    def _run_pending_reapply(self) -> None:
        """Runs the re-apply a removal asked for during a switch, once no switch runs."""
        if self._reapply_pending and not self.switching:
            self._reapply_pending = False
            self.reapply_active_profile()
        if self._claude_reset_pending and not self.switching:
            self._claude_reset_pending = False
            self.switch_claude_only(None, lambda _result, _error: None)

    def _log(self, level: str, message: str) -> None:
        if self.log is not None:
            self.log.log(level, message)

    # MARK: settings (S-09)

    def _update_settings(self, change: Callable) -> Config:
        def edit(config: Config) -> Config:
            updated = Config.from_dict(config.to_dict())
            change(updated.settings)
            return updated
        return self.edit(edit)

    def set_switch_feedback(self, feedback) -> None:
        self._update_settings(lambda settings: setattr(settings, "switch_feedback", feedback))

    def set_open_at_login(self, enabled: bool) -> None:
        """Writes or removes the autostart entry, then saves the setting. Raises
        AutostartUnavailable (nothing changed) when enabling outside the installed package."""
        if enabled:
            self.autostart.enable()
        else:
            self.autostart.disable()
        self._update_settings(lambda settings: setattr(settings, "open_at_login", enabled))

    @property
    def is_installed(self) -> bool:
        from ..core.install.installer import Installer
        return Installer(self.environment).manifest() is not None

    def set_enabled_shells(self, shells) -> None:
        """Re-runs the installer for the chosen shells (only MultiCred's blocks change, with
        backup), then saves the setting. An installer error leaves the setting as it was."""
        from ..core.install.installer import Installer, InstallOptions

        shells = sorted(set(shells), key=lambda shell: shell.value)
        if self.is_installed:
            Installer(self.environment).apply(InstallOptions(shells=list(shells)))
        self._update_settings(lambda settings: setattr(settings, "enabled_shells", list(shells)))

    def locate_tools(self, on_done: Callable[[object | None, Exception | None], None]) -> None:
        """{Tool: path or None}, off the UI thread (the first lookup runs the login shell)."""
        from ..core.tools import Tool

        tools = self.tools
        self._background("tools", lambda: {tool: tools.locate(tool) for tool in Tool}, on_done)

    def _uninstaller(self):
        from ..core.install.uninstaller import Uninstaller
        return Uninstaller(self.environment, config=self.config, tools=self.tools, autostart=self.autostart)

    def uninstall_preview(self, remove_claude_folders: bool = False):
        return self._uninstaller().preview(remove_claude_folders)

    def created_claude_folders(self) -> list[str]:
        return self._uninstaller().created_claude_folders()

    def uninstall(self, remove_claude_folders: bool,
                  on_done: Callable[[object | None, Exception | None], None]) -> None:
        """Runs the uninstaller off the UI thread; `on_done(report, error)`."""
        uninstaller = self._uninstaller()

        def finished(report, error):
            if report is not None:
                self._log("info", f"uninstalled: changed={report.changed_files} removed={report.removed_files} "
                                  f"manual={report.unterminated} problems={len(report.problems)}")
                # Nothing is configured any more; the app quits right after.
                self.config, self.state, self.verification = None, ActiveState(), None
            on_done(report, error)

        self._background("uninstall", lambda: uninstaller.apply(remove_claude_folders), finished)

    def diagnostics(self, on_done: Callable[[str], None]) -> None:
        """"Copiar diagnóstico": versions, tool paths, distro and the last 50 log lines,
        built off the UI thread (it runs `<tool> --version`). Never a secret value."""
        from .. import VERSION
        from ..core.log import redact_credential_like
        from ..core.tools import Tool

        config, tools, environment, log, autostart = self.config, self.tools, self.environment, self.log, self.autostart

        def work():
            lines = [f"MultiCred {VERSION} ({environment.mode})", f"Sistema: {os_release_name()}"]
            for tool in Tool:
                path = tools.locate(tool) if tools is not None else None
                version = None
                if path is not None and hasattr(tools, "version"):
                    try:
                        version = tools.version(tool)
                    except Exception:
                        version = None
                where = environment.abbreviate(path) if path else "não encontrado"
                lines.append(f"{tool.value}: {where}" + (f" ({version})" if version else ""))
            shells = config.settings.enabled_shells if config is not None else []
            lines.append(f"shells: {', '.join(s.value for s in shells) or 'nenhum'}")
            if config is not None:
                lines.append(f"retorno da troca: {config.settings.switch_feedback.value}")
            lines.append(f"abrir ao iniciar a sessão: {'sim' if autostart.is_enabled() else 'não'}")
            lines.append("--- log ---")
            lines.extend(log.tail(50) if log is not None else [])
            return redact_credential_like("\n".join(lines))

        self._background("diagnostics", work,
                         lambda text, error: on_done(text if text is not None else f"Erro: {error}"))

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
        # "Abrir ao iniciar a sessão" is on by default, like the Mac's login item at setup;
        # only from the installed package (Ajustes explains otherwise).
        if config.settings.open_at_login and self.autostart.is_available():
            try:
                self.autostart.enable()
            except OSError as error:
                self._log("error", f"autostart not written: {error}")
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
        """Removes the key and its secret. When the active profile exported it, the profile
        is applied again at once: the next env files unset the variable only for one
        generation, so a terminal idle across two later switches would keep it otherwise."""
        from ..core.editors import ConfigEditor
        key = self.config.api_key(key_id) if self.config is not None else None
        if key is None:
            return
        active = self.active_profile
        exported = active is not None and key_id in active.api_key_ids
        # The secret goes only after the save: a failed save keeps both.
        self.edit(lambda config: ConfigEditor.remove_api_key(key_id, config))
        if not ConfigEditor.discard_removed_key(key, self.environment.secrets):
            self._log("warning", f"removed API key {key.nickname}: its secret could not be deleted")
        if exported:
            if self.switching:
                self._reapply_pending = True  # after the running switch, which used the old config
            else:
                self.reapply_active_profile()

    def remove_claude_account(self, account_id) -> None:
        """Removes the account and its secret, as a transaction: (1) a login account live
        in a folder gives the folder its own login back, (2) the config is saved without
        the account, (3) only then its saved login and token are deleted. A failed save
        puts the account back in its folder and loses nothing. Shells stop exporting a
        removed token that was in use (Claude-only switch back to the folder login),
        like the Mac. It runs under the switch lock (Busy while a switch runs, nothing
        changed): a switch moving logins at the same time could save over them."""
        from ..core.claude import account_editor
        from ..core.claude.login_vault import ClaudeLoginSwitcher

        account = self.config.claude_account(account_id) if self.config is not None else None
        if account is None:
            return
        was_applied = self.state.applied_claude_account_id == account_id
        switcher = ClaudeLoginSwitcher(self.environment)
        with self.engine.exclusive():
            undo = account_editor.release_login(account, switcher)
            try:
                self.edit(lambda config: account_editor.remove(account_id, config))
            except Exception:
                try:
                    undo()
                except Exception as error:
                    self._log("error", f"could not put the Claude account back in its folder: {error!r}")
                raise
            account_editor.discard_removed_account(account, self.environment.secrets, switcher)
        if was_applied:
            # Back to the folder's own login, also without an active profile (a token can be
            # applied with none); during a switch, once it ends.
            if self.switching:
                self._claude_reset_pending = True
            else:
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

        config, state = self.config, self.state
        if config is None:
            if on_done is not None:
                on_done(None, None)
            return
        # The model's scanner (no GTK here: the tray runs this too).
        verifier = Verifier(self.environment, self.tools, self.processes, engine=self.engine, log=self.log)

        def finished(report, error):
            if report is not None:
                self.verification = report
                self.notify()
            if on_done is not None:
                on_done(report, error)

        self._background("verify", lambda: verifier.run(config, state, deep=deep), finished)

    def _finish(self, result, error, on_done, outcome=None):
        self._switches_in_flight = max(0, self._switches_in_flight - 1)
        if isinstance(error, Busy):
            # Refused, nothing changed. Another switch started here still runs and owns
            # `switching`; if none does (the engine was busy with a Verificar fix), the
            # flag goes back to idle.
            on_done(result, error)
            if self._switches_in_flight == 0 and self.switching:
                self.switching = False
                self.notify()
            self._run_pending_reapply()
            return False
        self.switching = self._switches_in_flight > 0
        if result is not None:
            self.state = result.state
            self.inconsistent = False
        elif isinstance(error, StepFailed) and not error.rollback.complete:
            self.inconsistent = True
        on_done(result, error)
        if outcome is not None:
            for listener in list(self.switch_listeners):
                listener(outcome)
        self.notify()
        self._run_pending_reapply()
        return False  # GLib.idle_add: run once


def _default_scanner(environment):
    """The real /proc only for the real system; test mode's sandbox has no sessions."""
    if environment.mode == "real":
        from ..core import real_env
        return real_env.make_process_scanner()
    from ..core.adapters.process_scanner import FixedProcessScanner
    return FixedProcessScanner(0)


def os_release_name(path: str = "/etc/os-release") -> str:
    """PRETTY_NAME from os-release (e.g. "Pop!_OS 24.04 LTS"), for the diagnostics."""
    try:
        with open(path, encoding="utf-8") as file:
            for line in file:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "desconhecido"


def _once(fn, *args):
    """Runs `fn` and tells GLib.idle_add not to call it again."""
    fn(*args)
    return False
