"""Thin wrapper over `gh`. Never asks for tokens: `gh auth status --json hosts`
carries no token unless `--show-token` is passed, which this module never does."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..runner import CommandRequest, run_checked


@dataclass(frozen=True)
class GitHubLogin:
    host: str
    login: str
    active: bool
    # "success" when the stored token works; anything else means "log in again".
    state: str

    @property
    def is_usable(self) -> bool:
        return self.state == "success"


class UnexpectedOutput(Exception):
    pass


class GitHubCLI:
    def __init__(self, runner, executable: str, base_environment: dict[str, str]):
        self.runner = runner
        self.executable = executable
        self.environment = dict(base_environment, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")

    def _request(self, arguments: list[str]) -> CommandRequest:
        return CommandRequest(self.executable, arguments, self.environment, timeout=20)

    def logins(self) -> list[GitHubLogin]:
        """Every account gh knows. Slow and network-bound (gh tests each token
        online): for status screens, never on the switch path."""
        result = run_checked(self.runner, self._request(["auth", "status", "--json", "hosts"]))
        try:
            hosts = json.loads(result.stdout)["hosts"]
            logins = [GitHubLogin(e["host"], e["login"], bool(e.get("active")), e.get("state", ""))
                      for entries in hosts.values() for e in entries]
        except (ValueError, KeyError, TypeError):
            raise UnexpectedOutput(result.stdout[:200]) from None
        return sorted(logins, key=lambda l: (l.host, l.login))

    def active_login(self, host: str) -> str | None:
        """Active account for `host`, from gh's local config (offline, fast)."""
        result = self.runner.run(self._request(["config", "get", "-h", host, "user"]))
        login = result.stdout.strip()
        return login if result.succeeded and login else None

    def switch_account(self, host: str, user: str) -> None:
        run_checked(self.runner, self._request(["auth", "switch", "--hostname", host, "--user", user]))
