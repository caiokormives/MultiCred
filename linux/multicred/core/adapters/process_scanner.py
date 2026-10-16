"""Counts running Claude Code sessions of the current user (D35), by scanning /proc.

Nothing is read from the processes' memory or environment: only `stat` (parent
pid), `status` (owner uid), the `exe` link and, for npm installs, `cmdline`.
The notification only says "N sessões do Claude Code continuam com a conta
anterior".

Claude Code's native install runs `~/.local/bin/claude`, a link to
`~/.local/share/claude/versions/<version>`, which spawns helpers. One session =
one Claude Code process whose parent is not Claude Code (a shell, an editor,
systemd). The executable path decides, not the process name (D35).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_NPM_ENTRY = "/@anthropic-ai/claude-code/cli.js"


class ProcessScanning(Protocol):
    def claude_code_session_count(self) -> int: ...


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    parent: int
    path: str


def is_claude_code(path: str) -> bool:
    return os.path.basename(path) == "claude" or "/claude/versions/" in path or path.endswith(_NPM_ENTRY)


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
        if os.path.basename(exe).startswith("node"):
            script = self._script(directory)
            if script is not None and script.endswith(_NPM_ENTRY):
                exe = script
        return ProcessEntry(pid, parent, exe)

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
    def _script(directory: Path) -> str | None:
        try:
            parts = (directory / "cmdline").read_bytes().split(b"\0")
        except OSError:
            return None
        return parts[1].decode("utf-8", "replace") if len(parts) > 1 and parts[1] else None


class FixedProcessScanner:
    def __init__(self, sessions: int):
        self.sessions = sessions

    def claude_code_session_count(self) -> int:
        return self.sessions
