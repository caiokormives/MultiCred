"""Counts running Claude Code sessions of the current user (D35), by scanning /proc.

Nothing is read from the processes' memory or environment: only `stat` (parent
pid), `status` (owner uid), the `exe` link and, for node and Android's linker,
`cmdline` (plus the `cwd` link, to place a relative script).
The notification only says "N sessões do Claude Code continuam com a conta
anterior".

Claude Code's native install runs `~/.local/bin/claude`, a link to
`~/.local/share/claude/versions/<version>`, which spawns helpers. One session =
one Claude Code process whose parent is not Claude Code (a shell, an editor,
systemd). The executable path decides, not the process name (D35).

npm installs (Termux, and desktops that use npm) run as node: `claude` is a link
to the package's cli.js, so the process is `node $PREFIX/bin/claude`. The script
node runs (the first argument that is not a node option) is resolved through its
links and must be `@anthropic-ai/claude-code/cli.js`; any other node program, an
eval (`node -e`), or a script merely named `claude` does not count. On Android,
termux-exec starts programs through the system linker, so `exe` is
/system/bin/linker64 and the real program comes next in `cmdline` (some launches
keep the program as argv[0] instead; both are read).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_NPM_ENTRY = "/@anthropic-ai/claude-code/cli.js"
# Android's dynamic linker, which termux-exec runs programs through (/system/bin/linker64).
_LINKERS = frozenset({"linker", "linker64"})
# node options followed by a separate value (`-r ./hook.js`), so the value is not the script.
_NODE_VALUE_OPTIONS = frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader", "-C",
                                 "--conditions", "--title", "--inspect-port", "--env-file", "--input-type"})
# node runs no script file with these (eval, print, REPL, syntax check).
_NODE_NO_SCRIPT = frozenset({"-e", "--eval", "-p", "--print", "-i", "--interactive", "-c", "--check"})


class ProcessScanning(Protocol):
    def claude_code_session_count(self) -> int: ...


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    parent: int
    path: str


def is_claude_code(path: str) -> bool:
    return os.path.basename(path) == "claude" or "/claude/versions/" in path or path.endswith(_NPM_ENTRY)


def node_script(arguments: list[str]) -> str | None:
    """The script `node <arguments>` runs, or None (no script: eval, REPL…)."""
    remaining = iter(arguments)
    for argument in remaining:
        if argument == "--":
            return next(remaining, None)
        if argument in _NODE_NO_SCRIPT:
            return None
        if argument.startswith("-"):
            if argument in _NODE_VALUE_OPTIONS:
                next(remaining, None)
            continue
        return argument or None
    return None


def count_sessions(processes: list[ProcessEntry]) -> int:
    claude = {p.pid for p in processes if is_claude_code(p.path)}
    return sum(1 for p in processes if p.pid in claude and p.parent not in claude)


class ProcScanner:
    """Reads a /proc tree. `proc_root`, `uid` and `own_pid` are injected
    (real_env.make_process_scanner builds the real one)."""

    def __init__(self, proc_root, uid: int, own_pid: int):
        self.proc_root = Path(proc_root)
        self.uid = uid
        self.own_pid = own_pid

    def claude_code_session_count(self) -> int:
        return count_sessions(self.list_own_processes())

    def list_own_processes(self) -> list[ProcessEntry]:
        try:
            names = os.listdir(self.proc_root)
        except OSError:
            return []
        entries = []
        for name in names:
            if not name.isdigit() or int(name) == self.own_pid:
                continue
            entry = self._entry(int(name))
            if entry is not None:
                entries.append(entry)
        return sorted(entries, key=lambda e: e.pid)

    def _entry(self, pid: int) -> ProcessEntry | None:
        directory = self.proc_root / str(pid)
        # Any of these can vanish or be unreadable (the process exited, another user).
        try:
            if self._owner(directory) != self.uid:
                return None
            parent = self._parent(directory)
            exe = os.readlink(directory / "exe")
        except (OSError, ValueError, IndexError):
            return None
        if exe.endswith(" (deleted)"):  # binary replaced by an update while running
            exe = exe[: -len(" (deleted)")]
        program, arguments = exe, None
        if os.path.basename(exe) in _LINKERS:
            command = self._cmdline(directory)
            if command and os.path.basename(command[0]) in _LINKERS:
                command = command[1:]  # linker64 <program> <arguments…>
            if not command:
                return ProcessEntry(pid, parent, exe)
            program, arguments = command[0], command[1:]
            if "/" in program:
                program = self._resolve(directory, program) or program
        if os.path.basename(program).startswith("node"):
            if arguments is None:
                arguments = (self._cmdline(directory) or [])[1:]
            script = node_script(arguments)
            resolved = self._resolve(directory, script) if script else None
            if resolved is not None and resolved.endswith(_NPM_ENTRY):
                program = resolved
        return ProcessEntry(pid, parent, program)

    @staticmethod
    def _owner(directory: Path) -> int:
        for line in (directory / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])  # real uid
        raise ValueError("no Uid line")

    @staticmethod
    def _parent(directory: Path) -> int:
        # The name in parentheses may itself contain spaces and parentheses.
        text = (directory / "stat").read_text()
        fields = text[text.rindex(")") + 1:].split()
        return int(fields[1])

    @staticmethod
    def _cmdline(directory: Path) -> list[str] | None:
        """argv as /proc shows it (NUL-separated, NUL-terminated), or None when unreadable."""
        try:
            raw = (directory / "cmdline").read_bytes()
        except OSError:
            return None
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [part.decode("utf-8", "replace") for part in parts]

    @staticmethod
    def _resolve(directory: Path, path: str) -> str | None:
        """`path` through its links; a relative one from the process's working directory
        (None when that cannot be read)."""
        if not os.path.isabs(path):
            try:
                path = os.path.join(os.readlink(directory / "cwd"), path)
            except OSError:
                return None
        return os.path.realpath(path)


class FixedProcessScanner:
    def __init__(self, sessions: int):
        self.sessions = sessions

    def claude_code_session_count(self) -> int:
        return self.sessions
