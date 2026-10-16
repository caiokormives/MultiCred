"""Everything MultiCred needs from the machine, injected.

No module in multicred.core reads the real home directory, the process
environment or spawns processes on its own; they all receive a
SystemEnvironment. Tests build it with a temporary home, a fake runner and
in-memory secrets (see tests/test_architecture.py). Only real_env.py builds
the real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SystemEnvironment:
    # "real": touches the system. "dryRun": app test mode, sandbox home, commands
    # simulated. "test": unit tests.
    mode: str
    home: Path
    # The user's login shell, e.g. /bin/bash.
    login_shell: str
    user_name: str
    runner: Any
    secrets: Any
    # Claude Code's folder credentials and MultiCred's login vault (D39).
    claude_credentials: Any
    clock: Callable[[], datetime] = field(default=_utc_now)

    def __post_init__(self):
        object.__setattr__(self, "home", Path(self.home))

    @property
    def app_support_directory(self) -> Path:
        """~/.config/multicred (the macOS app uses ~/Library/Application Support/MultiCred)."""
        return self.home / ".config" / "multicred"

    @property
    def logs_directory(self) -> Path:
        """~/.local/state/multicred"""
        return self.home / ".local" / "state" / "multicred"

    def expand_tilde(self, path: str) -> str:
        """Expands a leading ~ or ~/ against the injected home. Other paths are returned as is."""
        if path == "~":
            return str(self.home)
        if path.startswith("~/"):
            return f"{self.home}/{path[2:]}"
        return path

    def abbreviate(self, path: str) -> str:
        """Shortens a path under home to ~/… for display."""
        home = str(self.home)
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~/" + path[len(home) + 1:]
        return path

    def base_process_environment(self, path: list[str]) -> dict[str, str]:
        """Minimal, explicit environment for child processes. Never the app's own."""
        return {
            "HOME": str(self.home),
            "USER": self.user_name,
            "LOGNAME": self.user_name,
            "SHELL": self.login_shell,
            "PATH": ":".join(path),
            "LANG": "C.UTF-8",
        }
