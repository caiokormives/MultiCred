"""Logging a Claude account in through the pseudo-terminal (R-05, D26, D29, D39).

Port of LoginFlow.swift (parser and state machine) plus the orchestration the
macOS app keeps in SetupTokenSession / AppModel:

- `LoginOutputParser` turns raw PTY bytes into `LoginEvent`s. Every pattern
  `claude auth login` / `claude setup-token` is recognized by lives in this file
  (`_classify`). Escape sequences are removed first.
- `LoginStateMachine`: starting → waitingBrowser → waitingCode → connecting →
  connected / failed. The CLI may finish without a code when the browser was
  already logged in (D26).
- `ClaudeLoginSession` runs one attempt in a throwaway folder
  (`~/.config/multicred/scratch/<uuid>`, `CLAUDE_CONFIG_DIR` pointing to it), so no
  profile folder is touched. A full login is captured from that folder into the
  vault; a setup-token goes to the Secret Service. The folder and its
  `.credentials.json` are deleted at the end, whatever happened. Test mode runs
  the LoginSimulator instead of the real `claude` (D30).

The code typed by the user is forwarded to the CLI and never stored; the
visible output and the copied log show it (and any token) masked.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from ..log import redact_credential_like
from ..models import ClaudeAccount, Config, ProfileColor, uuid_text
from ..pty import PseudoTerminalProcess, PTYError
from ..tools import Tool
from . import account_editor
from .credentials import FileClaudeCredentialStore
from .login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher, NoLoginInFolder

# The login window gives up after ten minutes.
LOGIN_TIMEOUT = 600.0
# Value for the CLI's BROWSER variable: a no-op opener. The dialog opens the
# link itself, in the browser the user picks.
SUPPRESS_CLI_BROWSER = "/usr/bin/true"

CLAUDE_NOT_INSTALLED = "Claude Code não foi encontrado. Instale com: curl -fsSL https://claude.ai/install.sh | bash"
NO_LOGIN_FOUND = "O login terminou, mas não encontrei a credencial. Tente de novo."
NO_TOKEN_SHOWN = "O Claude Code terminou sem mostrar um token. Tente de novo ou cole o token manualmente."


# MARK: escape sequences

class EscapeStripper:
    """Removes terminal escape sequences from text that arrives in pieces.

    CSI (`ESC [ … final`), OSC/DCS/APC/PM (`ESC ] … BEL` or `… ESC \\`), charset
    designations (`ESC ( B`) and other two-byte escapes are dropped, and so are
    BEL and the other C0 controls except tab, CR and LF. Ink draws spaces with
    cursor moves ("Paste\\e[8Gcode"): cursor forward (C) and column (G) become a
    space so words stay apart. A sequence cut at the end of a read is kept and
    completed by the next one (the macOS parser stripped each read on its own).
    """

    _STRING_INTRODUCERS = "]PX^_"
    _CHARSET_INTRODUCERS = "()*+-./"
    _MAX_PENDING = 4096

    def __init__(self):
        self._pending = ""

    def feed(self, text: str) -> str:
        text = self._pending + text
        self._pending = ""
        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch != "\x1b":
                if ch in "\t\r\n" or ch >= " " and ch != "\x7f":
                    out.append(ch)
                i += 1
                continue
            end = self._sequence_end(text, i, out)
            if end is None:
                rest = text[i:]
                self._pending = rest if len(rest) <= self._MAX_PENDING else ""
                break
            i = end
        return "".join(out)

    def _sequence_end(self, text: str, start: int, out: list[str]) -> int | None:
        """Index after the escape sequence starting at `start`, or None when it is incomplete."""
        n = len(text)
        if start + 1 >= n:
            return None
        kind = text[start + 1]
        if kind == "[":
            j = start + 2
            while j < n:
                if 0x40 <= ord(text[j]) <= 0x7E:
                    if text[j] in "CG":
                        out.append(" ")
                    return j + 1
                j += 1
            return None
        if kind in self._STRING_INTRODUCERS:
            j = start + 2
            while j < n:
                if text[j] == "\x07":
                    return j + 1
                if text[j] == "\x1b":
                    if j + 1 >= n:
                        return None
                    if text[j + 1] == "\\":
                        return j + 2
                j += 1
            return None
        if kind in self._CHARSET_INTRODUCERS:
            return start + 3 if start + 2 < n else None
        return start + 2


def strip_escapes(text: str) -> str:
    """Escape sequences removed from a complete text (an unfinished one at the end is dropped)."""
    return EscapeStripper().feed(text)


# MARK: events and parser

@dataclass(frozen=True)
class LoginEvent:
    # "openingBrowser" | "authorizationURL" | "promptingForCode" | "succeeded" | "failed"
    kind: str
    value: str | None = None

    @staticmethod
    def opening_browser() -> "LoginEvent": return LoginEvent("openingBrowser")

    @staticmethod
    def authorization_url(url: str) -> "LoginEvent": return LoginEvent("authorizationURL", url)

    @staticmethod
    def prompting_for_code() -> "LoginEvent": return LoginEvent("promptingForCode")

    @staticmethod
    def succeeded() -> "LoginEvent": return LoginEvent("succeeded")

    @staticmethod
    def failed(message: str) -> "LoginEvent": return LoginEvent("failed", message)


_URL = re.compile(r"https://[^\s<>\"']+")
_ANY_URL = re.compile(r"https?://\S+")
_FAILURE_WORDS = ("error", "invalid", "expired", "failed", "denied")
_NEWLINES = re.compile(r"[\r\n]")


class LoginOutputParser:
    """Turns raw PTY bytes into LoginEvents. Each event fires once.

    Patterns come from real transcripts (docs/spikes/claude-config-dir.md item d,
    and `claude setup-token` 2.1.296):

        Opening browser to sign in…
        If the browser didn't open, visit: <OSC 8 link>https://claude.com/cai/oauth/authorize?…
        Paste code here if prompted > Login successful.
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stripper = EscapeStripper()
        self._pending = ""
        self._emitted: set[str] = set()
        # Visible text so far, without escape sequences ("Copiar log", after masking).
        self.transcript = ""

    def consume(self, data: bytes) -> list[LoginEvent]:
        text = self._stripper.feed(self._decoder.decode(bytes(data)))
        self.transcript += text
        self._pending += text
        events: list[LoginEvent] = []
        # Complete lines first; a PTY ends lines with "\r\n" and Ink redraws with "\r".
        while True:
            match = _NEWLINES.search(self._pending)
            if match is None:
                break
            line, self._pending = self._pending[:match.start()], self._pending[match.end():]
            events += self._classify(line, partial=False)
        # The code prompt has no newline after it.
        events += self._classify(self._pending, partial=True)
        return events

    def _classify(self, raw: str, partial: bool) -> list[LoginEvent]:
        line = raw.strip(" \t")
        if not line:
            return []
        lower = line.lower()
        events: list[LoginEvent] = []

        def once(key, event):
            if key not in self._emitted:
                self._emitted.add(key)
                events.append(event)

        if "opening browser" in lower:
            once("open", LoginEvent.opening_browser())
        url = _URL.search(raw)
        # On an unfinished line the URL may still be growing: take it only once
        # something follows it (the macOS parser could take half a URL).
        if url and (not partial or ("visit" in lower and url.end() < len(raw))):
            once("url", LoginEvent.authorization_url(url.group(0)))
        if "paste code" in lower:
            once("prompt", LoginEvent.prompting_for_code())
        # Failure words are looked for outside URLs: an authorization URL can
        # contain "error", "invalid" or "denied" in its host or parameters.
        without_urls = _ANY_URL.sub("", lower)
        if "login successful" in lower or "logged in as" in lower:
            once("success", LoginEvent.succeeded())
        elif not partial and any(word in without_urls for word in _FAILURE_WORDS):
            once(f"failed:{line}", LoginEvent.failed(line))
        return events


_AUTHORIZATION_CODE = re.compile(r"[A-Za-z0-9_\-]{20,}#[A-Za-z0-9_\-]{10,}")


def looks_like_authorization_code(text: str) -> bool:
    """The code the Claude authorization page shows for the manual flow ("<code>#<state>")."""
    trimmed = text.strip()
    return len(trimmed) <= 512 and _AUTHORIZATION_CODE.fullmatch(trimmed) is not None


# MARK: state machine

@dataclass(frozen=True)
class LoginPhase:
    # "starting" | "waitingForBrowser" | "waitingForCode" | "connecting" | "connected" | "failed"
    kind: str
    message: str | None = None

    @staticmethod
    def failed(message: str) -> "LoginPhase":
        return LoginPhase("failed", message)

    @property
    def is_finished(self) -> bool:
        return self.kind in ("connected", "failed")

    @property
    def is_failure(self) -> bool:
        return self.kind == "failed"

    @property
    def accepts_code(self) -> bool:
        return self.kind in ("waitingForCode", "waitingForBrowser")


LoginPhase.STARTING = LoginPhase("starting")
LoginPhase.WAITING_FOR_BROWSER = LoginPhase("waitingForBrowser")
LoginPhase.WAITING_FOR_CODE = LoginPhase("waitingForCode")
LoginPhase.CONNECTING = LoginPhase("connecting")
LoginPhase.CONNECTED = LoginPhase("connected")


class LoginStateMachine:
    """Pure state machine for one attempt; `connected` and `failed` are final.

        starting ─opening/URL─► waitingForBrowser ─prompt─► waitingForCode ─code sent─► connecting
            │                          │                                                 │
            │                          └── browser callback completed on its own ───────►├─ success ─► connected
            └── failure text / exit ≠ 0 / timeout / cancel ───────────────────────────────┴───────────► failed
    """

    def __init__(self):
        self.phase = LoginPhase.STARTING
        self.authorization_url: str | None = None

    def handle(self, event: LoginEvent) -> None:
        if event.kind == "authorizationURL":
            self.authorization_url = event.value
        if self.phase.is_finished:
            return
        if event.kind in ("openingBrowser", "authorizationURL"):
            if self.phase == LoginPhase.STARTING:
                self.phase = LoginPhase.WAITING_FOR_BROWSER
        elif event.kind == "promptingForCode":
            if self.phase in (LoginPhase.STARTING, LoginPhase.WAITING_FOR_BROWSER):
                self.phase = LoginPhase.WAITING_FOR_CODE
        elif event.kind == "succeeded":
            self.phase = LoginPhase.CONNECTED
        elif event.kind == "failed":
            self.phase = LoginPhase.failed(event.value or "")

    def code_submitted(self) -> None:
        if self.phase.accepts_code:
            self.phase = LoginPhase.CONNECTING

    def process_exited(self, status: int) -> None:
        if not self.phase.is_finished:
            self.phase = LoginPhase.CONNECTED if status == 0 else LoginPhase.failed(f"processo encerrado (código {status})")

    def timed_out(self) -> None:
        if not self.phase.is_finished:
            self.phase = LoginPhase.failed("tempo esgotado")

    def cancelled(self) -> None:
        if not self.phase.is_finished:
            self.phase = LoginPhase.failed("cancelado")


# MARK: launching a login tool

class LoginLaunchError(Exception):
    pass


def outside_test_home_message(tool_name: str) -> str:
    return f"No modo de teste, só rodo um {tool_name} que esteja dentro do HOME de teste."


def resolve_login_tool(environment, tools, tool: Tool, simulated: bool) -> str | None:
    """The tool to run in the pseudo-terminal, or None to run the simulator.

    Safety: the simulator never runs against the real system (its fake login would
    land in the real vault), and outside the real system a tool runs only when it
    lives inside the test HOME (tests use fake scripts there; the real claude or
    gh would touch the user's real accounts)."""
    if simulated:
        if environment.mode == "real":
            raise LoginLaunchError("A simulação de login só existe no modo de teste.")
        return None
    path = tools.locate(tool)
    if path is None:
        if tool == Tool.CLAUDE:
            raise LoginLaunchError(CLAUDE_NOT_INSTALLED)
        raise LoginLaunchError(f"{tool.display_name} não foi encontrado.")
    if environment.mode != "real":
        home = os.path.realpath(environment.home)
        if not os.path.realpath(path).startswith(home + os.sep):
            raise LoginLaunchError(outside_test_home_message(tool.value))
    return path


def terminal_environment(environment, tools) -> dict[str, str]:
    """Explicit environment for a CLI in the pseudo-terminal (never the app's own)."""
    env = environment.base_process_environment(tools.search_directories())
    env["TERM"] = "xterm-256color"
    return env


# MARK: saving what a login produced

@dataclass(frozen=True)
class NewClaudeAccount:
    """The account being added (the dialog's "new account" form)."""

    nickname: str
    email: str | None
    color: ProfileColor


class NotConfigured(Exception):
    def __init__(self):
        super().__init__("MultiCred ainda não foi configurado")


class ClaudeAccountSaver:
    """Stores a captured login or token for a new or an existing account.

    `load`/`save` read and write the configuration (the app's model in the UI,
    the repository in tests). Secrets go only to the vault / Secret Service.
    """

    def __init__(self, environment, load: Callable[[], Config | None], save: Callable[[Config], None],
                 switcher: ClaudeLoginSwitcher | None = None):
        self.environment = environment
        self._load = load
        self._save = save
        self.switcher = switcher or ClaudeLoginSwitcher(environment)

    def _scratch_store(self) -> FileClaudeCredentialStore:
        # The throwaway folder is a real folder Claude Code (or the simulator) wrote
        # into, also in test mode, where the environment's store lives in memory.
        return FileClaudeCredentialStore(self.environment.home)

    def capture(self, scratch: str) -> ClaudeLoginRecord:
        """The login `claude auth login` left in the throwaway folder."""
        return ClaudeLoginSwitcher(self.environment, store=self._scratch_store()).capture(scratch)

    def discard_scratch(self, scratch: str) -> None:
        """Deletes the throwaway folder and the `.credentials.json` Claude Code made in it."""
        try:
            self._scratch_store().delete_folder_item(scratch)
        except Exception:
            pass  # removing the folder below takes the file with it
        shutil.rmtree(scratch, ignore_errors=True)

    def _config(self) -> Config:
        config = self._load()
        if config is None:
            raise NotConfigured()
        return config

    def save_login(self, target, record: ClaudeLoginRecord) -> ClaudeAccount:
        config = self._config()
        if isinstance(target, ClaudeAccount):
            updated = account_editor.replace_login(target, record, config, self.switcher, self.environment.secrets)
            if updated is not config:
                self._save(updated)
                account_editor.discard_replaced_token(target, self.environment.secrets)
            return updated.claude_account(target.id) or target
        updated, account = account_editor.add_login_account(target.nickname, target.email, target.color, record,
                                                            config, self.switcher)
        try:
            self._save(updated)
        except Exception:
            try:
                self.environment.claude_credentials.delete_vault(uuid_text(account.id))
            except Exception:
                pass
            raise
        return account

    def save_token(self, target, token: str) -> ClaudeAccount:
        if isinstance(target, ClaudeAccount):
            account_editor.replace_token(target, token, self.environment.secrets)
            return target
        updated, account = account_editor.add_token_account(target.nickname, target.email, target.color, token,
                                                            self._config(), self.environment.secrets)
        try:
            self._save(updated)
        except Exception:
            try:
                self.environment.secrets.delete_item(account.secret_account)
            except Exception:
                pass
            raise
        return account


# MARK: one login attempt

class LoginMode(str, Enum):
    # `claude auth login --claudeai`: a full login, switched live (D39).
    LOGIN = "login"
    # `claude setup-token`: a long-lived token exported as CLAUDE_CODE_OAUTH_TOKEN.
    SETUP_TOKEN = "setupToken"


@dataclass(frozen=True)
class LoginCommand:
    executable: str
    arguments: list[str]
    environment: dict[str, str]
    cwd: str
    scratch: str | None
    # What the terminal panel shows after "$ ".
    display: str = ""


def _direct(fn, *args):
    fn(*args)


@dataclass
class _Timer:
    timer: threading.Timer | None = field(default=None)

    def start(self, seconds: float, action: Callable[[], None]) -> None:
        self.timer = threading.Timer(seconds, action)
        self.timer.daemon = True
        self.timer.start()

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class ClaudeLoginSession:
    """One `claude auth login --claudeai` (full login) or `claude setup-token` run.

    Pseudo-terminal callbacks arrive on background threads and are handed to
    `dispatch` (GLib.idle_add in the app), so the parser, the state machine and
    the saving run in order on one thread. `on_change(session)` follows every
    update; `on_finished(session)` runs once, when the phase becomes final.

    "Login successful" comes before the CLI exits; the attempt counts as
    connected only once the login (or token) is stored, so success text is held
    back until then (the macOS SetupTokenSession did this for logins only and
    showed a setup-token as connected before the token had arrived).
    """

    def __init__(self, environment, tools, mode: LoginMode, target, saver: ClaudeAccountSaver, *,
                 dispatch: Callable = _direct, on_change: Callable | None = None, on_finished: Callable | None = None,
                 simulated: bool | None = None, timeout: float = LOGIN_TIMEOUT, log=None):
        from .setup_token import SetupTokenParser  # setup_token imports this module

        self.environment = environment
        self.tools = tools
        self.mode = mode
        self.target = target
        self.saver = saver
        self.simulated = environment.mode != "real" if simulated is None else simulated
        self.timeout = timeout
        self._dispatch = dispatch
        self._on_change = on_change
        self._on_finished = on_finished
        self._log = log
        self._lock = threading.RLock()
        self._scratch_lock = threading.Lock()
        self._pty = PseudoTerminalProcess()
        self._parser = SetupTokenParser()
        self._machine = LoginStateMachine()
        self._sent_codes: list[str] = []
        self._timer = _Timer()
        self._scratch: str | None = None
        self._stored = False
        self._logged = False
        self.command: LoginCommand | None = None
        self.launch_error: str | None = None
        # What was stored: the ClaudeAccount (new or updated).
        self.result: ClaudeAccount | None = None

    # MARK: state

    @property
    def phase(self) -> LoginPhase:
        with self._lock:
            return self._machine.phase

    @property
    def authorization_url(self) -> str | None:
        with self._lock:
            return self._machine.authorization_url

    @property
    def is_running(self) -> bool:
        return self._pty.is_running

    @property
    def redacted_log(self) -> str:
        """Visible output with the typed codes and anything credential-like masked ("Copiar log")."""
        with self._lock:
            return redact_credential_like(self._parser.masked_transcript, self._sent_codes)

    @property
    def output(self) -> str:
        """What the terminal panel shows: the same masked text."""
        return self.redacted_log

    # MARK: actions

    def start(self) -> None:
        try:
            command = self._prepare()
        except LoginLaunchError as error:
            self._fail_to_launch(str(error))
            return
        except OSError as error:
            self._fail_to_launch(f"Não consegui criar a pasta temporária: {error.strerror or error}")
            return
        self.command = command
        try:
            self._pty.start(command.executable, command.arguments, command.environment, cwd=command.cwd,
                            # Wide, so the ~108-character token is printed on one line.
                            columns=500,
                            on_output=lambda data: self._dispatch(self._received, data),
                            on_exit=lambda status: self._dispatch(self._exited, status))
        except PTYError as error:
            self._fail_to_launch(f"Não consegui iniciar o {command.display}: {error}")
            self._remove_scratch()
            return
        self._timer.start(self.timeout, lambda: self._dispatch(self._timed_out))
        self._log_info(f"{self.mode.value} started ({'simulated' if self.simulated else 'claude'})")

    def submit_code(self, code: str) -> None:
        """Sends the pasted code to the CLI, then Enter separately."""
        trimmed = code.strip()
        with self._lock:
            if not trimmed or not self._machine.phase.accepts_code:
                return
            self._sent_codes.append(trimmed)
            self._pty.submit_line(trimmed)
            self._update(self._machine.code_submitted)

    def cancel(self) -> None:
        with self._lock:
            self._update(self._machine.cancelled)
        self.stop()

    def stop(self) -> None:
        """Ends the attempt (dialog closed): stops the CLI and deletes the throwaway folder."""
        self._timer.cancel()
        self._pty.terminate()
        with self._lock:
            if not self._stored:
                self._write_log("stopped")
        self._remove_scratch()

    # MARK: internals

    def _prepare(self) -> LoginCommand:
        path = resolve_login_tool(self.environment, self.tools, Tool.CLAUDE, self.simulated)
        from .login_simulator import LoginSimulator, SetupTokenSimulator

        env = terminal_environment(self.environment, self.tools)
        env["BROWSER"] = SUPPRESS_CLI_BROWSER
        if path is None:
            simulator = LoginSimulator if self.mode == LoginMode.LOGIN else SetupTokenSimulator
            executable, arguments = simulator.EXECUTABLE, simulator.arguments()
            env["PATH"] = "/usr/bin:/bin"
        else:
            executable = path
            arguments = ["auth", "login", "--claudeai"] if self.mode == LoginMode.LOGIN else ["setup-token"]
            env["PATH"] = ":".join([os.path.dirname(path), env["PATH"]])
        display = "claude auth login --claudeai" if self.mode == LoginMode.LOGIN else "claude setup-token"
        if path is None:
            display += " (simulado)"
        scratch = self._make_scratch()
        env["CLAUDE_CONFIG_DIR"] = scratch
        return LoginCommand(executable, arguments, env, str(self.environment.home), scratch, display)

    def _make_scratch(self) -> str:
        root = Path(self.environment.app_support_directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch_root = root / "scratch"
        scratch_root.mkdir(mode=0o700, exist_ok=True)
        scratch = scratch_root / uuid_text(uuid.uuid4())
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o700)
        with self._lock:
            self._scratch = str(scratch)
        return str(scratch)

    def _remove_scratch(self) -> None:
        # Held during the deletion: when stop() and the exit callback race, the one
        # that finds nothing left to do returns only once the folder is gone.
        with self._scratch_lock:
            with self._lock:
                scratch, self._scratch = self._scratch, None
            if scratch is not None:
                self.saver.discard_scratch(scratch)

    def _fail_to_launch(self, message: str) -> None:
        with self._lock:
            self.launch_error = message
            self._update(lambda: self._machine.handle(LoginEvent.failed(message)))

    def _received(self, data: bytes) -> None:
        with self._lock:
            events = [e for e in self._parser.consume(data) if e.kind != "succeeded"]

            def apply():
                for event in events:
                    self._machine.handle(event)
            self._update(apply)
            if self._machine.phase.is_failure:
                self._write_log("failed")
            self._deliver_token_if_ready()

    def _deliver_token_if_ready(self) -> None:
        if self.mode != LoginMode.SETUP_TOKEN or self._stored or self._parser.token is None:
            return
        if self._machine.phase.is_finished:
            return
        self._stored = True
        try:
            self.result = self.saver.save_token(self.target, self._parser.token)
        except Exception as error:
            self.launch_error = f"Não consegui guardar o token: {error}"
            self._update(lambda: self._machine.handle(LoginEvent.failed(self.launch_error)))
            self._pty.terminate()
            return
        self._log_info("setup-token: token received (kept only in the Secret Service)")
        self._update(lambda: self._machine.handle(LoginEvent.succeeded()))

    def _exited(self, status: int) -> None:
        with self._lock:
            self._timer.cancel()
            if self.mode == LoginMode.LOGIN:
                self._finish_login(status)
            else:
                self._deliver_token_if_ready()
                if not self._machine.phase.is_finished:
                    if status == 0:
                        self.launch_error = NO_TOKEN_SHOWN
                    self._update(lambda: self._machine.process_exited(status or 1))
                    self._write_log(f"exited {status} without a token")
        self._remove_scratch()

    def _finish_login(self, status: int) -> None:
        if self._machine.phase.is_finished:
            return
        if status != 0:
            self._update(lambda: self._machine.process_exited(status))
            self._write_log(f"auth login exited {status}")
            return
        try:
            record = self.saver.capture(self._scratch) if self._scratch else None
            if record is None:
                raise NoLoginInFolder()
            self.result = self.saver.save_login(self.target, record)
        except NoLoginInFolder:
            self.launch_error = NO_LOGIN_FOUND
        except Exception as error:
            self.launch_error = f"Não consegui guardar o login: {error}"
        if self.result is None:
            self._update(lambda: self._machine.process_exited(1))
            self._write_log("auth login exited 0 without a saved login")
            return
        self._stored = True
        self._log_info("auth login: login saved (kept only in the vault)")
        self._update(lambda: self._machine.handle(LoginEvent.succeeded()))

    def _timed_out(self) -> None:
        with self._lock:
            self._update(self._machine.timed_out)
        self._pty.terminate()

    def _update(self, change: Callable[[], None]) -> None:
        with self._lock:
            was_finished = self._machine.phase.is_finished
            change()
            finished_now = not was_finished and self._machine.phase.is_finished
        if finished_now:
            self._timer.cancel()
        if self._on_change is not None:
            self._on_change(self)
        if finished_now and self._on_finished is not None:
            self._on_finished(self)

    def _log_info(self, message: str) -> None:
        if self._log is not None:
            self._log.log("info", message)

    def _write_log(self, reason: str) -> None:
        """Masked CLI output, for diagnosing an attempt that did not end with a saved login."""
        if self._logged or self._log is None:
            return
        self._logged = True
        masked = self.redacted_log[-3000:]
        self._log.log("info", f"claude {self.mode.value} {reason}; phase={self._machine.phase.kind}; "
                              f"output (masked, last 3000):\n{masked}")
