"""External command execution.

There is deliberately no shell involved: the executable is an absolute path and
arguments are a list, so user-provided text can never be parsed as shell syntax.
The environment is explicit and never inherited from the app.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .quoting import quote_for_display


@dataclass
class CommandRequest:
    executable: str
    arguments: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    cwd: str | None = None
    timeout: float = 15.0

    @property
    def name(self) -> str:
        return Path(self.executable).name

    @property
    def display_string(self) -> str:
        """For logs and "command that would run" previews. Never includes stdin (it may carry a secret)."""
        return " ".join(quote_for_display(part) for part in [str(self.executable), *self.arguments])


@dataclass
class CommandResult:
    exit_code: int
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""

    @property
    def stdout(self) -> str:
        return self.stdout_bytes.decode("utf-8", "replace")

    @property
    def stderr(self) -> str:
        return self.stderr_bytes.decode("utf-8", "replace")

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class CommandError(Exception):
    pass


class LaunchFailed(CommandError):
    def __init__(self, executable: str, reason: str):
        super().__init__(f"Could not start {executable}: {reason}")
        self.executable, self.reason = executable, reason


class TimedOut(CommandError):
    def __init__(self, executable: str, seconds: float):
        super().__init__(f"{executable} did not finish within {seconds:g}s")
        self.executable, self.seconds = executable, seconds


class NonZeroExit(CommandError):
    def __init__(self, executable: str, code: int, stderr: str):
        super().__init__(f"{executable} exited with code {code}: {stderr.strip()}")
        self.executable, self.code, self.stderr = executable, code, stderr


class CommandRunner(Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...


def run_checked(runner: CommandRunner, request: CommandRequest) -> CommandResult:
    """Runs the command and raises NonZeroExit when it fails."""
    result = runner.run(request)
    if not result.succeeded:
        raise NonZeroExit(request.name, result.exit_code, result.stderr)
    return result


class ProcessRunner:
    """Runs commands with subprocess, in their own process group.

    On timeout the whole group gets SIGTERM, then SIGKILL two seconds later, so a
    grandchild that inherited the pipes cannot keep us waiting.
    """

    def run(self, request: CommandRequest) -> CommandResult:
        try:
            process = subprocess.Popen(
                [request.executable, *request.arguments],
                env=request.environment,
                cwd=request.cwd,
                stdin=subprocess.PIPE if request.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise LaunchFailed(request.name, error.strerror or str(error)) from error
        try:
            out, err = process.communicate(request.stdin, timeout=request.timeout)
        except subprocess.TimeoutExpired:
            _stop_group(process)
            raise TimedOut(request.name, request.timeout) from None
        except BrokenPipeError:
            # The child exited without reading stdin.
            out, err = process.communicate()
        return CommandResult(process.returncode, out, err)


def _stop_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for stream in (process.stdout, process.stderr, process.stdin):
        if stream:
            stream.close()
    try:
        process.wait(1)
    except subprocess.TimeoutExpired:
        pass


@dataclass
class DryRunEntry:
    date: datetime
    command: str


class DryRunCommandRunner:
    """Records commands instead of running them (app test mode).

    `responder` fakes specific outputs; anything it does not handle succeeds
    with empty output.
    """

    def __init__(self, clock: Callable[[], datetime], responder: Callable[[CommandRequest], CommandResult | None] | None = None):
        self._clock = clock
        self._responder = responder
        self._lock = threading.Lock()
        self._entries: list[DryRunEntry] = []

    @property
    def entries(self) -> list[DryRunEntry]:
        with self._lock:
            return list(self._entries)

    def run(self, request: CommandRequest) -> CommandResult:
        with self._lock:
            self._entries.append(DryRunEntry(self._clock(), request.display_string))
        answer = self._responder(request) if self._responder else None
        return answer if answer is not None else CommandResult(0)
