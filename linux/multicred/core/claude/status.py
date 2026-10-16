"""Whether a Claude Code account is logged in.

Derived without ever reading a token: from `claude auth status --json`, or from
the non-secret `oauthAccount` entry of the folder's `.claude.json`.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import claude_paths
from ..runner import CommandRequest, run_checked
from ..tools import Tool


@dataclass(frozen=True)
class ClaudeLoginStatus:
    # "loggedIn" | "loggedOut" | "unknown" (could not tell: CLI missing,
    # unexpected output; never shown as "logged out").
    kind: str
    email: str | None = None
    reason: str | None = None

    @staticmethod
    def logged_in(email: str | None) -> "ClaudeLoginStatus":
        return ClaudeLoginStatus("loggedIn", email=email)

    @staticmethod
    def logged_out() -> "ClaudeLoginStatus":
        return ClaudeLoginStatus("loggedOut")

    @staticmethod
    def unknown(reason: str) -> "ClaudeLoginStatus":
        return ClaudeLoginStatus("unknown", reason=reason)

    @property
    def is_logged_in(self) -> bool:
        return self.kind == "loggedIn"


def state_file(account, environment) -> Path:
    """`.claude.json` of the account's folder: `~/.claude.json` in default mode,
    `<dir>/.claude.json` when `CLAUDE_CONFIG_DIR` is set."""
    return claude_paths.state_file(account.config_dir, environment)


def status_from_state_file(account, environment) -> ClaudeLoginStatus | None:
    """Reads only `oauthAccount.emailAddress`. None when the file is missing or
    unreadable; logged out when it exists without `oauthAccount`."""
    try:
        data = json.loads(state_file(account, environment).read_bytes())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("oauthAccount")
    if not isinstance(oauth, dict):
        return ClaudeLoginStatus.logged_out()
    email = oauth.get("emailAddress")
    return ClaudeLoginStatus.logged_in(email if isinstance(email, str) else None)


_FLAG_KEYS = ("loggedIn", "logged_in", "isLoggedIn", "authenticated", "isAuthenticated")


def _find_email(obj: dict) -> str | None:
    for key in ("email", "emailAddress"):
        value = obj.get(key)
        if isinstance(value, str) and "@" in value:
            return value
    for key in ("account", "oauthAccount", "user"):
        nested = obj.get(key)
        if isinstance(nested, dict):
            found = _find_email(nested)
            if found is not None:
                return found
    return None


class ClaudeCLI:
    """Wrapper over the `claude` binary for status and logout. Login runs in a
    pseudo-terminal owned by the app (S-07)."""

    def __init__(self, runner, executable: str, base_environment: dict[str, str]):
        self.runner = runner
        self.executable = executable
        self.base_environment = base_environment

    @staticmethod
    def environment_for(account, base: dict[str, str]) -> dict[str, str]:
        """`CLAUDE_CONFIG_DIR` set to the account's folder, or absent for the default one."""
        env = dict(base)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if account.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = account.config_dir
        env["NO_COLOR"] = "1"
        return env

    def auth_status(self, account) -> ClaudeLoginStatus:
        request = CommandRequest(self.executable, ["auth", "status", "--json"],
                                 self.environment_for(account, self.base_environment), timeout=20)
        try:
            result = self.runner.run(request)
        except Exception as error:
            return ClaudeLoginStatus.unknown(str(error))
        parsed = self.parse_status(result.stdout_bytes)
        if parsed is not None:
            return parsed
        return ClaudeLoginStatus.unknown(f"unexpected output (exit {result.exit_code})")

    def logout(self, account) -> None:
        """Logs the folder out through Claude Code itself."""
        run_checked(self.runner, CommandRequest(self.executable, ["auth", "logout"],
                                                self.environment_for(account, self.base_environment), timeout=20))

    @staticmethod
    def parse_status(data: bytes) -> ClaudeLoginStatus | None:
        """Tolerant: a boolean login flag under a few likely names, and an e-mail at
        the top level or inside an account object. None when the output says
        nothing recognizable, so callers fall back to the state file."""
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        flag = next((obj[k] for k in _FLAG_KEYS if isinstance(obj.get(k), bool)), None)
        email = _find_email(obj)
        if flag is True:
            return ClaudeLoginStatus.logged_in(email)
        if flag is False:
            return ClaudeLoginStatus.logged_out()
        return ClaudeLoginStatus.logged_in(email) if email is not None else None


class ClaudeStatusService:
    """Caches statuses so menus never wait for `claude` (node startup is ~1 s)."""

    def __init__(self, environment, tools, ttl: float = 300):
        self.environment = environment
        self.tools = tools
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[uuid.UUID, tuple[ClaudeLoginStatus, object]] = {}

    def cached(self, account_id: uuid.UUID) -> ClaudeLoginStatus | None:
        with self._lock:
            entry = self._cache.get(account_id)
        return entry[0] if entry else None

    def invalidate(self, account_id: uuid.UUID | None = None) -> None:
        with self._lock:
            if account_id is None:
                self._cache.clear()
            else:
                self._cache.pop(account_id, None)

    def status(self, account, force: bool = False) -> ClaudeLoginStatus:
        if not force:
            with self._lock:
                entry = self._cache.get(account.id)
            if entry is not None and (self.environment.clock() - entry[1]).total_seconds() < self.ttl:
                return entry[0]
        status = self._resolve(account)
        with self._lock:
            self._cache[account.id] = (status, self.environment.clock())
        return status

    def _resolve(self, account) -> ClaudeLoginStatus:
        from_file = status_from_state_file(account, self.environment)
        claude = self.tools.locate(Tool.CLAUDE)
        if claude is None:
            return from_file or ClaudeLoginStatus.unknown("Claude Code not found")
        cli = ClaudeCLI(self.environment.runner, claude,
                        self.environment.base_process_environment(self.tools.search_directories()))
        status = cli.auth_status(account)
        if status.kind == "loggedIn":
            if status.email is None and from_file is not None and from_file.kind == "loggedIn":
                return ClaudeLoginStatus.logged_in(from_file.email)
            return status
        if status.kind == "loggedOut":
            return status
        return from_file or status
