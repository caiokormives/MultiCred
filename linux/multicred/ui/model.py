"""App state shared by the window and the tray. No GTK here: `dispatch` (GLib.idle_add
in the app, a direct call in tests) brings results back to the UI thread."""

from __future__ import annotations

import threading
from typing import Callable

from ..core.engine.switch_engine import SwitchEngine
from ..core.engine.types import StepFailed, SwitchError
from ..core.models import ActiveState, ClaudeFolder, Config
from ..core.storage import ConfigRepository, JSONStoreError


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
