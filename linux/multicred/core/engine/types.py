"""Types of the transactional switch (docs/PLAN.md §5.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..models import ActiveState


class SwitchStepKind(str, Enum):
    """Steps in the order they run. `state` is the commit point.

    `session` is the Linux counterpart of the macOS `launchd` step:
    `systemctl --user set-environment` plus `~/.config/environment.d`.
    """

    GIT = "git"
    GITHUB = "github"
    SESSION = "session"
    CLAUDE_SETTINGS = "claudeSettings"
    CLAUDE_LOGIN = "claudeLogin"
    ENV_FILES = "envFiles"
    STATE = "state"


@dataclass(frozen=True)
class ClaudeTarget:
    """What the Claude Code part of a switch should do with CLAUDE_CONFIG_DIR.

    kind "unchanged": leave it; "default": unset it (the default folder must
    never be selected by setting the variable to ~/.claude); "directory": set it.
    """

    kind: str
    path: str | None = None

    @staticmethod
    def unchanged() -> "ClaudeTarget": return ClaudeTarget("unchanged")

    @staticmethod
    def default() -> "ClaudeTarget": return ClaudeTarget("default")

    @staticmethod
    def directory(path: str) -> "ClaudeTarget": return ClaudeTarget("directory", path)

    @property
    def desired_value(self) -> str | None:
        return self.path if self.kind == "directory" else None

    def __str__(self):
        return self.path if self.kind == "directory" else self.kind


@dataclass(frozen=True)
class PreflightFailure:
    """Why a switch was refused before changing anything.

    kinds: profileNotFound, claudeAccountNotFound, toolMissing(tool),
    claudeDirectoryMissing(path), apiKeyMissing(nickname, envVar),
    claudeTokenMissing(nickname), claudeLoginMissing(nickname),
    secretsUnavailable(reason), noActiveProfile(nickname: a login account
    chosen with no active profile, so no folder to put it in),
    claudeFoldersUnreadable(path of claude-folders.json, which exists but cannot be read).
    """

    kind: str
    detail: str | None = None
    extra: str | None = None


@dataclass(frozen=True)
class RollbackOutcome:
    # Steps that could not be undone; empty = everything was undone.
    failed: tuple[SwitchStepKind, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.failed


class SwitchError(Exception):
    pass


class Busy(SwitchError):
    """Another switch is running."""

    def __init__(self):
        super().__init__("A switch is already running")


class InvalidConfiguration(SwitchError):
    def __init__(self, issues):
        super().__init__(f"Invalid configuration ({len(issues)} issues)")
        self.issues = issues


class StateUnreadable(SwitchError):
    def __init__(self, reason):
        super().__init__(f"state.json unreadable: {reason}")
        self.reason = reason


class Preflight(SwitchError):
    """Nothing was changed."""

    def __init__(self, failure: PreflightFailure):
        super().__init__(f"Preflight: {failure.kind} {failure.detail or ''}".strip())
        self.failure = failure


class StepFailed(SwitchError):
    def __init__(self, step: SwitchStepKind, reason: str, rollback: RollbackOutcome):
        super().__init__(f"Failed at {step.value}: {reason}")
        self.step, self.reason, self.rollback = step, reason, rollback


@dataclass
class SwitchResult:
    state: ActiveState
    claude_target: ClaudeTarget
    applied_steps: list[SwitchStepKind] = field(default_factory=list)


Undo = Callable[[], None]


@dataclass
class SwitchStep:
    """One reversible unit of work. `apply` returns the function that undoes it."""

    kind: SwitchStepKind
    apply: Callable[[], Undo]
