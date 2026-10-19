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
    # The user's $XDG_CONFIG_HOME, if set. git reads its global config from
    # $XDG_CONFIG_HOME/git/config (or ~/.config/git/config when unset).
    xdg_config_home: str | None = None
    # The session bus of the desktop session. `systemctl --user` needs it (without it:
    # "Failed to connect to bus: No medium found"); the Secret Service clients too.
    xdg_runtime_dir: str | None = None
    dbus_session_bus_address: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "home", Path(self.home))

    @property
    def config_home(self) -> Path:
        """The user's config home: $XDG_CONFIG_HOME, or ~/.config when unset (or not an
        absolute path, which the XDG spec says to ignore). fish conf.d, environment.d,
        autostart and git's XDG config live under it. MultiCred's own folder does not
        (`app_support_directory`): the shell hooks name it as $HOME/.config/multicred."""
        if self.xdg_config_home and self.xdg_config_home.startswith("/"):
            return Path(self.xdg_config_home)
        return self.home / ".config"

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
        """Minimal, explicit environment for child processes. Never the app's own.

        XDG_CONFIG_HOME goes along when the user set it, so git, gh and claude read
        the same config files the user's terminals do."""
        env = {
            "HOME": str(self.home),
            "USER": self.user_name,
            "LOGNAME": self.user_name,
            "SHELL": self.login_shell,
            "PATH": ":".join(path),
            "LANG": "C.UTF-8",
        }
        if self.xdg_config_home:
            env["XDG_CONFIG_HOME"] = self.xdg_config_home
        if self.xdg_runtime_dir:
            env["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        if self.dbus_session_bus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self.dbus_session_bus_address
        return env
