"""One process attached to a pseudo-terminal, for `claude auth login`,
`claude setup-token` and `gh auth login --web`, which need a TTY for their prompts.

Port of PseudoTerminalProcess.swift (D29). Linux specifics:
- `os.openpty()` + subprocess running `/usr/bin/setsid --ctty <program>`: the
  program gets its own session with the terminal as its controlling terminal, so
  /dev/tty, job control and SIGWINCH work like in a real terminal emulator, without
  a preexec_fn (not safe in a process with threads).
- The environment is always the explicit one given; nothing is inherited.
- The master side is read by a thread with `select` + `read(2)`: once the child
  side closes, Linux answers EIO, which ends the output.
- `on_exit` fires once, after the output was drained or one second after the
  process ended (a grandchild may keep the terminal open), whichever comes first.
  The status is the exit code, or minus the signal number when it was killed.
- `terminate()` sends SIGTERM to the whole process group, then SIGKILL two
  seconds later if the process is still running.

Callbacks run on the reader/waiter threads; the UI hands them to its main loop.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
from typing import Callable

# A process that ended without closing the terminal (grandchild) is reported after this.
EXIT_GRACE_SECONDS = 1.0
KILL_AFTER_SECONDS = 2.0
_READ_SIZE = 8192


class PTYError(Exception):
    """kinds: openFailed, launchFailed, alreadyStarted."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


# util-linux setsid(1): `--ctty` makes stdin (the terminal) the controlling terminal
# of the new session. The child Popen starts is not a process group leader, so
# setsid does not fork: it becomes the session (and group) leader and execs the
# program in place, so the pid stays the program's.
SETSID = "/usr/bin/setsid"


def _launch_failure(executable: str, environment: dict[str, str]) -> str | None:
    """Why `executable` cannot run, checked here because a missing program would
    otherwise only make setsid exit 1 instead of failing the launch."""
    if "/" not in executable:
        found = shutil.which(executable, path=environment.get("PATH", os.defpath))
        if found is None:
            return f"{executable}: No such file or directory"
        executable = found
    if not os.path.isfile(executable):
        return f"{executable}: No such file or directory"
    if not os.access(executable, os.X_OK):
        return f"{executable}: Permission denied"
    return None


class PseudoTerminalProcess:
    def __init__(self):
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._master = -1
        self._exit_status: int | None = None
        self._output_closed = False
        self._exit_reported = False
        self._stop_reading = threading.Event()
        self._on_exit: Callable[[int], None] | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._exit_status is None

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process is not None else None

    def start(self, executable: str, arguments: list[str], environment: dict[str, str], cwd: str | None = None,
              columns: int = 100, rows: int = 24, on_output: Callable[[bytes], None] = lambda _data: None,
              on_exit: Callable[[int], None] = lambda _status: None) -> None:
        with self._lock:
            if self._process is not None:
                raise PTYError("alreadyStarted")
        failure = _launch_failure(executable, environment)
        if failure is not None:
            raise PTYError("launchFailed", failure)
        try:
            master, slave = os.openpty()
        except OSError as error:
            raise PTYError("openFailed", error.strerror or str(error)) from error
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            # No preexec_fn (unsafe with threads): setsid does setsid() + TIOCSCTTY.
            process = subprocess.Popen(
                [SETSID, "--ctty", executable, *arguments], env=dict(environment), cwd=cwd,
                stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            os.close(slave)
            os.close(master)
            reason = getattr(error, "strerror", None) or str(error)
            raise PTYError("launchFailed", reason) from error
        # The child has its own copy; ours would keep the output from ever ending.
        os.close(slave)
        with self._lock:
            self._process, self._master, self._on_exit = process, master, on_exit
        threading.Thread(target=self._read_loop, args=(master, on_output), name="multicred-pty-read",
                         daemon=True).start()
        threading.Thread(target=self._wait, args=(process,), name="multicred-pty-wait", daemon=True).start()

    def submit_line(self, text: str, enter_delay: float = 0.3) -> None:
        """Types a code into an interactive CLI and presses Enter. The two are sent
        apart: Claude Code (Ink) treats a long text arriving in one read as a paste
        and swallows a trailing Enter in it (seen with setup-token 2.1.301)."""
        self.write(text.encode("utf-8"))
        timer = threading.Timer(enter_delay, self.write, [b"\r"])
        timer.daemon = True
        timer.start()

    def write(self, data: bytes) -> None:
        """Writes to the process's terminal input, as if typed. Ignored once the
        terminal is closed (never writes to a descriptor number reused elsewhere)."""
        with self._lock:
            fd = self._master
            if fd < 0:
                return
            view = memoryview(data)
            while view:
                try:
                    written = os.write(fd, view)
                except InterruptedError:
                    continue
                except OSError:
                    return
                view = view[written:]

    def terminate(self) -> None:
        """SIGTERM to the process group, then SIGKILL two seconds later if still running."""
        with self._lock:
            process = self._process
            running = process is not None and self._exit_status is None
        if not running:
            return
        self._signal_group(process.pid, signal.SIGTERM)

        def kill_if_running():
            if self.is_running:
                self._signal_group(process.pid, signal.SIGKILL)

        timer = threading.Timer(KILL_AFTER_SECONDS, kill_if_running)
        timer.daemon = True
        timer.start()

    @staticmethod
    def _signal_group(pid: int, signum: int) -> None:
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    # MARK: threads

    def _read_loop(self, master: int, on_output: Callable[[bytes], None]) -> None:
        while not self._stop_reading.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.1)
            except InterruptedError:
                continue
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(master, _READ_SIZE)
            except InterruptedError:
                continue
            except OSError as error:
                if error.errno == errno.EAGAIN:
                    continue
                break  # EIO: the terminal closed
            if not chunk:
                break
            on_output(chunk)
        with self._lock:
            self._output_closed = True
            if self._master >= 0:
                os.close(self._master)
                self._master = -1
        self._report_exit_if_ready(force=False)

    def _wait(self, process: subprocess.Popen) -> None:
        status = process.wait()
        with self._lock:
            self._exit_status = status
        self._report_exit_if_ready(force=False)
        # A grandchild may keep the terminal open; do not wait for it forever.
        timer = threading.Timer(EXIT_GRACE_SECONDS, self._report_exit_if_ready, kwargs={"force": True})
        timer.daemon = True
        timer.start()

    def _report_exit_if_ready(self, force: bool) -> None:
        with self._lock:
            if self._exit_reported or self._exit_status is None or self._on_exit is None:
                return
            if not (self._output_closed or force):
                return
            self._exit_reported = True
            handler, status = self._on_exit, self._exit_status
        if force:
            self._stop_reading.set()
        handler(status)
