"""Finds tool binaries for a desktop app, which does not inherit the shell's PATH.

Search order (first match wins):
1. Well-known install locations: /usr/local/bin, ~/.local/bin (Claude Code
   native installer), /usr/bin, /bin, /snap/bin, Homebrew on Linux.
2. Entries from the user's login shell PATH (npm globals, asdf, mise, nvm…).

Well-known locations come first on purpose: a login-shell PATH entry must not
be able to shadow /usr/bin/gh with something else named gh.
"""

from __future__ import annotations

import os
import threading
from enum import Enum

from .runner import CommandRequest


class Tool(str, Enum):
    GIT = "git"
    GH = "gh"
    CLAUDE = "claude"

    @property
    def display_name(self) -> str:
        return {"git": "git", "gh": "GitHub CLI", "claude": "Claude Code"}[self.value]


DEFAULT_SEARCH_DIRECTORIES = [
    "/usr/local/bin",
    "~/.local/bin",
    "/usr/bin",
    "/bin",
    "/snap/bin",
    "/home/linuxbrew/.linuxbrew/bin",
]

PATH_MARKER = "__MULTICRED_PATH__"


def parse_login_shell_output(output: str) -> list[str]:
    """PATH between markers (rc files may print banners around it); absolute entries only."""
    parts = output.split(PATH_MARKER)
    if len(parts) < 3:
        return []
    return [entry for entry in parts[1].split(":") if entry.startswith("/")]


def is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


class ToolLocator:
    def __init__(self, environment, well_known=None, login_shell_timeout: float = 5.0):
        self.environment = environment
        self.well_known = list(well_known if well_known is not None else DEFAULT_SEARCH_DIRECTORIES)
        self.login_shell_timeout = login_shell_timeout
        self._lock = threading.Lock()
        self._login_shell_path: list[str] | None = None

    def invalidate(self) -> None:
        """Forgets the cached login shell PATH ("Verificar de novo")."""
        with self._lock:
            self._login_shell_path = None

    def search_directories(self) -> list[str]:
        """Ordered, de-duplicated directories. Also the PATH for child processes."""
        fixed = [self.environment.expand_tilde(d) for d in self.well_known]
        seen, ordered = set(), []
        for directory in fixed + self.user_login_shell_path():
            if directory not in seen:
                seen.add(directory)
                ordered.append(directory)
        return ordered

    def locate(self, tool: Tool) -> str | None:
        for directory in self.search_directories():
            candidate = os.path.join(directory, tool.value)
            if is_executable_file(candidate):
                return candidate
        return None

    def version(self, tool: Tool) -> str | None:
        """First line of `<tool> --version`, or None when missing or failing."""
        path = self.locate(tool)
        if path is None:
            return None
        request = CommandRequest(path, ["--version"],
                                 self.environment.base_process_environment(self.search_directories()), timeout=10)
        try:
            result = self.environment.runner.run(request)
        except Exception:
            return None
        if not result.succeeded:
            return None
        lines = result.stdout.splitlines()
        return lines[0] if lines else None

    def user_login_shell_path(self) -> list[str]:
        """PATH as a new terminal sees it (login + interactive, so .bashrc runs).

        stdin is /dev/null and the call has a timeout, so a prompting rc file
        cannot hang us. Empty when the shell fails or hangs.
        """
        with self._lock:
            if self._login_shell_path is not None:
                return self._login_shell_path
        env = self.environment
        request = CommandRequest(
            env.login_shell,
            ["-l", "-i", "-c", f"printf '{PATH_MARKER}%s{PATH_MARKER}' \"$PATH\""],
            {"HOME": str(env.home), "USER": env.user_name, "LOGNAME": env.user_name, "SHELL": env.login_shell,
             "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "dumb"},
            timeout=self.login_shell_timeout,
        )
        try:
            result = env.runner.run(request)
            entries = parse_login_shell_output(result.stdout) if result.succeeded else []
        except Exception:
            entries = []
        with self._lock:
            self._login_shell_path = entries
        return entries
