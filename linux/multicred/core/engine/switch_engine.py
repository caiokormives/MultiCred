"""Applies profiles transactionally (docs/PLAN.md §5.2).

    preflight (no changes) -> git -> github -> session -> claudeSettings
        -> claudeLogin -> envFiles -> state   <- commit point: generation bump

A failure at step k undoes k-1 … 1 in reverse order and reports the step and
whether the rollback was complete. One switch at a time: a second request
while one runs gets Busy instead of queueing behind it.
"""

from __future__ import annotations

import os
import threading
import uuid

from ..adapters.github_cli import GitHubCLI
from ..adapters.session_env import SessionEnvironment
from ..models import ActiveState, ClaudeFolder, Config
from ..paths import AppPaths
from ..rendering import gitconfig
from ..rendering.envfile import SECRET_TOOL, APIKeyExport, ShellEnvironment, render_fish, render_posix
from ..storage import AtomicFileWriter, StateStore, read_bytes, restore_file
from ..tools import Tool
from ..validation import issues_in
from .types import (
    Busy, ClaudeTarget, InvalidConfiguration, Preflight, PreflightFailure, RollbackOutcome, StateUnreadable,
    StepFailed, SwitchResult, SwitchStep, SwitchStepKind,
)

CLAUDE_VARIABLE = "CLAUDE_CONFIG_DIR"


class _Operation:
    def __init__(self, kind: str, target: uuid.UUID | None = None):
        self.kind = kind  # "profile" | "claudeOnly" | "reapply"
        self.target = target

    @property
    def starts_new_activation(self) -> bool:
        """"Active since" restarts only when a profile is chosen."""
        return self.kind == "profile"


class SwitchEngine:
    def __init__(self, environment, tools, log=None, secret_tool: str = SECRET_TOOL):
        self.environment = environment
        self.paths = AppPaths(environment)
        self.tools = tools
        self.state_store = StateStore(self.paths, environment.clock)
        self.log = log
        self.secret_tool = secret_tool
        self._lock = threading.Lock()

    # MARK: public API

    def switch_profile(self, profile_id: uuid.UUID, config: Config) -> SwitchResult:
        """Switches everything to `profile_id` and clears any Claude-only override."""
        return self._perform(config, _Operation("profile", profile_id))

    def switch_claude_only(self, account_id: uuid.UUID | None, config: Config) -> SwitchResult:
        """Switches only the Claude account; git, gh and API keys stay."""
        return self._perform(config, _Operation("claudeOnly", account_id))

    def reapply_active_profile(self, config: Config) -> SwitchResult | None:
        """Re-applies the active profile (at session start). None when none was ever activated."""
        if self._load_state().profile_id is None:
            return None
        return self._perform(config, _Operation("reapply"))

    def current_state(self) -> ActiveState:
        return self._load_state()

    # MARK: transaction

    def _perform(self, config: Config, operation: _Operation) -> SwitchResult:
        if not self._lock.acquire(blocking=False):
            raise Busy()
        try:
            return self._transaction(config, operation)
        finally:
            self._lock.release()

    def _transaction(self, config: Config, operation: _Operation) -> SwitchResult:
        transaction = uuid.uuid4().hex[:8]
        issues = issues_in(config)
        if issues:
            self._log("error", f"switch {transaction}: invalid configuration ({len(issues)} issues)")
            raise InvalidConfiguration(issues)
        previous = self._load_state()

        if operation.kind == "profile":
            profile_id, override, touches_git_and_github = operation.target, None, True
        elif operation.kind == "claudeOnly":
            profile_id, override, touches_git_and_github = previous.profile_id, operation.target, False
        else:
            # An override for an account that no longer exists is dropped, not fatal.
            kept = previous.claude_override_id
            if kept is not None and kept != ClaudeFolder.OWN_LOGIN_OVERRIDE and config.claude_account(kept) is None:
                kept = None
            profile_id, override, touches_git_and_github = previous.profile_id, kept, True

        profile = config.profile(profile_id)
        if profile_id is not None and profile is None:
            raise Preflight(PreflightFailure("profileNotFound"))
        if (override is not None and override != ClaudeFolder.OWN_LOGIN_OVERRIDE
                and config.claude_account(override) is None):
            raise Preflight(PreflightFailure("claudeAccountNotFound"))

        if profile is None:
            claude_target = ClaudeTarget.unchanged()
        elif profile.claude_config_dir is None:
            claude_target = ClaudeTarget.default()
        else:
            claude_target = ClaudeTarget.directory(profile.claude_config_dir)

        now = self.environment.clock()
        new_state = ActiveState(
            profile_id=profile_id,
            claude_override_id=override,
            activated_at=now if operation.starts_new_activation else (previous.activated_at or now),
            generation=previous.generation + 1,
        )
        api_keys = [config.api_key(k) for k in (profile.api_key_ids if profile else [])]
        api_keys = [k for k in api_keys if k is not None]
        shell_environment = ShellEnvironment(
            generation=new_state.generation,
            managed_vars=config.managed_env_vars,
            claude=claude_target,
            api_keys=[APIKeyExport(k.env_var, k.secret_account) for k in api_keys],
        )
        child_environment = self.environment.base_process_environment(self.tools.search_directories())

        # Preflight: everything that can be checked without changing anything.
        # gh token validity is deliberately not checked: `gh auth status` tests
        # every token over the network, which would make switching slow and
        # impossible offline. An account gh does not know makes `gh auth switch`
        # fail, and the transaction rolls back. Token health belongs to Verificar.
        github_plan = None
        github_account = config.github_account(profile.github_account_id if profile else None)
        if touches_git_and_github and github_account is not None:
            gh = self.tools.locate(Tool.GH)
            if gh is None:
                raise Preflight(PreflightFailure("toolMissing", "gh"))
            github_plan = (GitHubCLI(self.environment.runner, gh, child_environment), github_account)
        if claude_target.kind == "directory" and not os.path.isdir(claude_target.path):
            raise Preflight(PreflightFailure("claudeDirectoryMissing", claude_target.path))
        for key in api_keys:
            try:
                present = self.environment.secrets.contains_item(key.secret_account)
            except Exception as error:
                raise Preflight(PreflightFailure("secretsUnavailable", str(error))) from error
            if not present:
                raise Preflight(PreflightFailure("apiKeyMissing", key.nickname, key.env_var))

        steps: list[SwitchStep] = []
        if touches_git_and_github:
            identity = config.git_identity(profile.git_identity_id if profile else None)
            steps.append(self._git_step(gitconfig.render(identity, self.environment)))
        if github_plan is not None:
            steps.append(self._github_step(*github_plan))
        # Switching only the Claude account never moves the folder.
        if touches_git_and_github and claude_target.kind != "unchanged":
            steps.append(self._session_step(claude_target, SessionEnvironment(self.environment, child_environment)))
        steps.append(self._env_files_step(shell_environment))
        steps.append(self._state_step(new_state))

        self._log("info", f"switch {transaction}: start profile={profile.name if profile else '-'} "
                          f"folder={claude_target} steps={[s.kind.value for s in steps]}")
        undo_stack = []
        for step in steps:
            try:
                undo = step.apply()
            except Exception as error:  # every failure becomes StepFailed with a rollback report
                reason = str(error)
                self._log("error", f"switch {transaction}: step {step.kind.value} failed: {reason}")
                not_undone = []
                for kind, pending_undo in reversed(undo_stack):
                    try:
                        pending_undo()
                    except Exception as undo_error:
                        not_undone.append(kind)
                        self._log("error", f"switch {transaction}: rollback of {kind.value} failed: {undo_error}")
                raise StepFailed(step.kind, reason, RollbackOutcome(tuple(not_undone))) from error
            undo_stack.append((step.kind, undo))
        self._log("info", f"switch {transaction}: done generation={new_state.generation}")
        return SwitchResult(new_state, claude_target, [s.kind for s in steps])

    def _load_state(self) -> ActiveState:
        try:
            return self.state_store.load()
        except Exception as error:
            raise StateUnreadable(str(error)) from error

    def _log(self, level, message):
        if self.log is not None:
            self.log.log(level, message)

    # MARK: steps

    def _git_step(self, content: str) -> SwitchStep:
        path = self.paths.gitconfig

        def apply():
            previous = read_bytes(path)
            AtomicFileWriter.write(content.encode(), path, permissions=0o644)
            return lambda: restore_file(previous, path)

        return SwitchStep(SwitchStepKind.GIT, apply)

    def _github_step(self, cli: GitHubCLI, account) -> SwitchStep:
        def apply():
            previous = cli.active_login(account.host)
            if previous is not None and previous.lower() == account.username.lower():
                return lambda: None
            cli.switch_account(account.host, account.username)

            def undo():
                if previous is not None:
                    cli.switch_account(account.host, previous)
            return undo

        return SwitchStep(SwitchStepKind.GITHUB, apply)

    def _session_step(self, target: ClaudeTarget, session: SessionEnvironment) -> SwitchStep:
        return SwitchStep(SwitchStepKind.SESSION, lambda: session.apply(CLAUDE_VARIABLE, target.desired_value))

    def _env_files_step(self, shell_environment: ShellEnvironment) -> SwitchStep:
        sh, fish = self.paths.env_sh, self.paths.env_fish
        posix = render_posix(shell_environment, self.secret_tool).encode()
        fish_text = render_fish(shell_environment, self.secret_tool).encode()

        def apply():
            previous_sh, previous_fish = read_bytes(sh), read_bytes(fish)
            AtomicFileWriter.write(posix, sh, permissions=0o644)
            try:
                AtomicFileWriter.write(fish_text, fish, permissions=0o644)
            except Exception:
                restore_file(previous_sh, sh)
                raise

            def undo():
                restore_file(previous_fish, fish)
                restore_file(previous_sh, sh)
            return undo

        return SwitchStep(SwitchStepKind.ENV_FILES, apply)

    def _state_step(self, new_state: ActiveState) -> SwitchStep:
        store = self.state_store

        def apply():
            snapshot = store.snapshot()
            try:
                store.commit(new_state)
            except Exception:
                try:
                    store.restore(snapshot)
                except Exception:
                    pass
                raise
            return lambda: store.restore(snapshot)

        return SwitchStep(SwitchStepKind.STATE, apply)
