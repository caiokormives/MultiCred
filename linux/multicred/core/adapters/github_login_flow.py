"""Logging in to GitHub with gh's own `gh auth login --web` in the pseudo-terminal (D34).

Port of GitHubLoginFlow.swift plus the macOS GitHubLoginSession and
AppModel.didLogInToGitHub. What gh 2.x says:

    ! First copy your one-time code: 1A2B-3C4D
    Press Enter to open https://github.com/login/device in your browser...
    ✓ Authentication complete.
    ✓ Logged in as octocat

Yes/no questions (for example whether to set up git credentials) are answered
with the default by pressing Enter. gh stores the token itself; MultiCred never
sees it. gh makes the new login the active account, so once gh has exited the
active account goes back to the profile's (`gh auth switch`).
"""

from __future__ import annotations

import codecs
import re
import threading
from dataclasses import dataclass
from typing import Callable

from ..claude.login_flow import EscapeStripper, LoginLaunchError, resolve_login_tool, terminal_environment
from ..pty import PseudoTerminalProcess, PTYError
from ..tools import Tool
from .github_cli import GitHubCLI


def login_arguments(host: str) -> list[str]:
    """A browser login that adds (and activates) an account."""
    return ["auth", "login", "--hostname", host, "--git-protocol", "https", "--web"]


@dataclass(frozen=True)
class GitHubLoginEvent:
    # "oneTimeCode" | "pressEnterToOpenBrowser" | "question" | "succeeded" | "failed"
    kind: str
    value: str | None = None

    @staticmethod
    def one_time_code(code: str) -> "GitHubLoginEvent": return GitHubLoginEvent("oneTimeCode", code)

    @staticmethod
    def press_enter_to_open_browser() -> "GitHubLoginEvent": return GitHubLoginEvent("pressEnterToOpenBrowser")

    @staticmethod
    def question(line: str) -> "GitHubLoginEvent": return GitHubLoginEvent("question", line)

    @staticmethod
    def succeeded(login: str | None) -> "GitHubLoginEvent": return GitHubLoginEvent("succeeded", login)

    @staticmethod
    def failed(message: str) -> "GitHubLoginEvent": return GitHubLoginEvent("failed", message)


_CODE = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{4}")
_LOGGED_IN = re.compile(r"(?i)logged in as\s+([A-Za-z0-9-]+)")
_URL = re.compile(r"https://[^\s<>\"']+")
_ANY_URL = re.compile(r"https?://\S+")
_NEWLINES = re.compile(r"[\r\n]")


class GitHubLoginParser:
    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stripper = EscapeStripper()
        self._pending = ""
        self._emitted: set[str] = set()
        self._completed = False
        self.transcript = ""
        # The login reported once the process ends successfully.
        self.logged_in_as: str | None = None
        # Where gh sends the browser ("Press Enter to open <url>").
        self.device_url: str | None = None

    def consume(self, data: bytes) -> list[GitHubLoginEvent]:
        text = self._stripper.feed(self._decoder.decode(bytes(data)))
        self.transcript += text
        self._pending += text
        events: list[GitHubLoginEvent] = []
        while True:
            match = _NEWLINES.search(self._pending)
            if match is None:
                break
            line, self._pending = self._pending[:match.start()], self._pending[match.end():]
            events += self._classify(line, partial=False)
        events += self._classify(self._pending, partial=True)
        return events

    def _classify(self, raw: str, partial: bool) -> list[GitHubLoginEvent]:
        line = raw.strip(" \t")
        if not line:
            return []
        lower = line.lower()
        events: list[GitHubLoginEvent] = []

        def once(key, event):
            if key not in self._emitted:
                self._emitted.add(key)
                events.append(event)

        code = _CODE.search(line)
        if "one-time code" in lower and code:
            once("code", GitHubLoginEvent.one_time_code(code.group(0)))
        if "press enter" in lower:
            url = _URL.search(raw)
            if url and url.end() < len(raw):
                self.device_url = url.group(0).rstrip(".")
            once("enter", GitHubLoginEvent.press_enter_to_open_browser())
        if "(Y/n)" in line or "(y/N)" in line:
            once(f"q:{line}", GitHubLoginEvent.question(line))
        logged = _LOGGED_IN.search(line)
        if logged and not partial:
            self.logged_in_as = logged.group(1)
            once("logged", GitHubLoginEvent.succeeded(self.logged_in_as))
        if "authentication complete" in lower:
            self._completed = True
        without_urls = _ANY_URL.sub("", lower)
        if not partial and not self._completed and any(w in without_urls for w in ("error", "failed", "expired")):
            once(f"failed:{line}", GitHubLoginEvent.failed(line))
        return events


@dataclass(frozen=True)
class GitHubLoginPhase:
    # "starting" | "showingCode" (the user presses "Abrir o GitHub", which sends Enter)
    # | "waitingForBrowser" | "connected" | "failed"
    kind: str
    value: str | None = None

    @staticmethod
    def showing_code(code: str) -> "GitHubLoginPhase": return GitHubLoginPhase("showingCode", code)

    @staticmethod
    def waiting_for_browser(code: str) -> "GitHubLoginPhase": return GitHubLoginPhase("waitingForBrowser", code)

    @staticmethod
    def connected(login: str | None) -> "GitHubLoginPhase": return GitHubLoginPhase("connected", login)

    @staticmethod
    def failed(message: str) -> "GitHubLoginPhase": return GitHubLoginPhase("failed", message)

    @property
    def is_finished(self) -> bool:
        return self.kind in ("connected", "failed")

    @property
    def is_failure(self) -> bool:
        return self.kind == "failed"


GitHubLoginPhase.STARTING = GitHubLoginPhase("starting")


class GitHubLoginStateMachine:
    def __init__(self):
        self.phase = GitHubLoginPhase.STARTING
        self.code: str | None = None

    def handle(self, event: GitHubLoginEvent) -> None:
        if self.phase.is_finished:
            return
        if event.kind == "oneTimeCode":
            self.code = event.value
            self.phase = GitHubLoginPhase.showing_code(event.value)
        elif event.kind == "succeeded":
            self.phase = GitHubLoginPhase.connected(event.value)
        elif event.kind == "failed":
            self.phase = GitHubLoginPhase.failed(event.value or "")

    def browser_opened(self) -> None:
        if self.phase.kind == "showingCode":
            self.phase = GitHubLoginPhase.waiting_for_browser(self.phase.value)

    def process_exited(self, status: int, login: str | None) -> None:
        if not self.phase.is_finished:
            self.phase = (GitHubLoginPhase.connected(login) if status == 0
                          else GitHubLoginPhase.failed(f"processo encerrado (código {status})"))

    def cancelled(self) -> None:
        if not self.phase.is_finished:
            self.phase = GitHubLoginPhase.failed("cancelado")


class GitHubLoginSimulator:
    """Test-mode stand-in for `gh auth login --web` (gh in a sandbox would still store
    a real token in the user's keyring)."""

    EXECUTABLE = "/bin/sh"
    SIMULATED_LOGIN = "usuario-teste"

    SCRIPT = r"""
printf '[MODO DE TESTE: simulação do gh auth login]\n'
printf '! First copy your one-time code: TEST-1234\n'
printf 'Press Enter to open https://github.com/login/device in your browser... '
IFS= read -r answer
sleep 1
printf '\342\234\223 Authentication complete.\n'
printf '\342\234\223 Logged in as usuario-teste\n'
"""

    @classmethod
    def arguments(cls) -> list[str]:
        return ["-c", cls.SCRIPT]


def _direct(fn, *args):
    fn(*args)


class GitHubLoginSession:
    """One `gh auth login --web` attempt in a pseudo-terminal.

    `switch_back_to` is the gh user of the active profile on `host` (None: the
    profile does not pick a GitHub account, so nothing is switched). Callbacks
    from the pseudo-terminal go through `dispatch` (GLib.idle_add in the app).
    `on_finished(session)` runs once: on failure right away; on success after gh
    exited and the active account went back to the profile's (the switch runs
    off the dispatch thread, as gh may take a moment).
    """

    def __init__(self, environment, tools, host: str = "github.com", switch_back_to: str | None = None, *,
                 dispatch: Callable = _direct, on_change: Callable | None = None, on_finished: Callable | None = None,
                 simulated: bool | None = None, log=None):
        self.environment = environment
        self.tools = tools
        self.host = host
        self.switch_back_to = switch_back_to
        self.simulated = environment.mode != "real" if simulated is None else simulated
        self.extra_environment: dict[str, str] = {}
        self._dispatch = dispatch
        self._on_change = on_change
        self._on_finished = on_finished
        self._log = log
        self._lock = threading.RLock()
        self._pty = PseudoTerminalProcess()
        self._parser = GitHubLoginParser()
        self._machine = GitHubLoginStateMachine()
        self._finished_reported = False
        self._gh: str | None = None
        self.command = None
        self.launch_error: str | None = None
        self.switched_back = False
        self.switch_back_error: str | None = None

    @property
    def phase(self) -> GitHubLoginPhase:
        with self._lock:
            return self._machine.phase

    @property
    def code(self) -> str | None:
        with self._lock:
            return self._machine.code

    @property
    def device_url(self) -> str | None:
        with self._lock:
            return self._parser.device_url

    @property
    def login(self) -> str | None:
        with self._lock:
            phase = self._machine.phase
            return phase.value if phase.kind == "connected" else None

    @property
    def output(self) -> str:
        with self._lock:
            return self._parser.transcript

    @property
    def is_running(self) -> bool:
        return self._pty.is_running

    def start(self) -> None:
        from ..claude.login_flow import LoginCommand

        try:
            path = resolve_login_tool(self.environment, self.tools, Tool.GH, self.simulated)
        except LoginLaunchError as error:
            self._fail_to_launch(str(error))
            return
        env = terminal_environment(self.environment, self.tools)
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        env.update(self.extra_environment)
        if path is None:
            executable, arguments, display = (GitHubLoginSimulator.EXECUTABLE, GitHubLoginSimulator.arguments(),
                                              "gh auth login --web (simulado)")
            env["PATH"] = "/usr/bin:/bin"
            # Switching back in test mode is recorded by the dry-run runner.
            self._gh = self.tools.locate(Tool.GH) or "/usr/bin/gh"
        else:
            executable, arguments, display = path, login_arguments(self.host), "gh auth login --web"
            self._gh = path
        self.command = LoginCommand(executable, arguments, env, str(self.environment.home), None, display)
        try:
            self._pty.start(executable, arguments, env, cwd=str(self.environment.home),
                            on_output=lambda data: self._dispatch(self._received, data),
                            on_exit=lambda status: self._dispatch(self._exited, status))
        except PTYError as error:
            self._fail_to_launch(f"Não consegui iniciar o login do GitHub: {error}")

    def open_browser(self) -> None:
        """gh waits for Enter to open the browser (the dialog copies the code first)."""
        self._pty.write(b"\r")
        with self._lock:
            self._machine.browser_opened()
        self._changed()

    def cancel(self) -> None:
        with self._lock:
            self._machine.cancelled()
        self._changed()
        self.stop()

    def stop(self) -> None:
        self._pty.terminate()

    # MARK: internals

    def _fail_to_launch(self, message: str) -> None:
        with self._lock:
            self.launch_error = message
            self._machine.handle(GitHubLoginEvent.failed(message))
        self._changed()

    def _received(self, data: bytes) -> None:
        with self._lock:
            events = self._parser.consume(data)
            for event in events:
                if event.kind == "question":
                    self._pty.write(b"\r")
                self._machine.handle(event)
        self._changed()

    def _exited(self, status: int) -> None:
        with self._lock:
            self._machine.process_exited(status, self._parser.logged_in_as)
            connected = self._machine.phase.kind == "connected"
        if connected and self.switch_back_to:
            threading.Thread(target=self._switch_back, name="multicred-gh-switch", daemon=True).start()
            return
        self._changed()

    def _switch_back(self) -> None:
        """Gives gh back to the profile's account; runs after gh wrote its config."""
        try:
            cli = GitHubCLI(self.environment.runner, self._gh,
                            self.environment.base_process_environment(self.tools.search_directories()))
            cli.switch_account(self.host, self.switch_back_to)
            switched, error = True, None
        except Exception as failure:
            switched, error = False, str(failure)
        self._dispatch(self._switched_back, switched, error)

    def _switched_back(self, switched: bool, error: str | None) -> None:
        with self._lock:
            self.switched_back, self.switch_back_error = switched, error
        if self._log is not None:
            self._log.log("info" if switched else "warning",
                          f"gh login: active account back to {self.switch_back_to}" if switched
                          else f"gh login: could not switch back to {self.switch_back_to}: {error}")
        self._changed(final=True)

    def _changed(self, final: bool = False) -> None:
        """on_change always; on_finished once, when the phase is final and nothing is left to do."""
        with self._lock:
            phase = self._machine.phase
            waiting_for_exit = phase.kind == "connected" and self._pty.is_running
            waiting_for_switch = (phase.kind == "connected" and self.switch_back_to is not None and not final
                                  and self.launch_error is None)
            report = phase.is_finished and not self._finished_reported and (
                phase.is_failure or (not waiting_for_exit and not waiting_for_switch))
            if report:
                self._finished_reported = True
        if self._on_change is not None:
            self._on_change(self)
        if report and self._on_finished is not None:
            self._on_finished(self)
