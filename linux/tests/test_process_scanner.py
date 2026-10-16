"""S-04: counting open Claude Code sessions by root process executable (D35), via /proc.

The tests build a fake /proc tree inside the temporary HOME; the real /proc is
only read by the smoke test, which prints nothing.
"""

import os
import unittest

from multicred.core.adapters.process_scanner import (
    FixedProcessScanner, ProcessEntry, ProcScanner, count_sessions, is_claude_code,
)
from tests.support import SafeTestCase

ME = 1000
OTHER = 1001


class CountSessionsTests(unittest.TestCase):
    def test_counts_only_session_roots_in_a_real_tree(self):
        """Native install tree observed on macOS, with Linux paths: three sessions
        with version binaries and helpers below them."""
        home = "/home/u"
        tree = [
            (28412, 1, f"{home}/.local/share/claude/versions/2.1.288"),
            (28563, 28412, f"{home}/.local/share/claude/versions/2.1.288"),
            (28566, 28412, f"{home}/.local/bin/claude"),
            (28597, 28563, f"{home}/.local/share/claude/versions/2.1.288"),
            (42763, 1, f"{home}/.local/share/claude/versions/2.1.288"),
            (80858, 2000, f"{home}/.local/share/claude/versions/2.1.285"),
            (80881, 80858, f"{home}/.local/share/claude/versions/2.1.285"),
            (90000, 1, "/usr/bin/bash"),
            (90001, 90000, "/usr/bin/node"),
        ]
        self.assertEqual(count_sessions([ProcessEntry(*row) for row in tree]), 3)

    def test_session_started_from_a_shell_counts(self):
        entries = [ProcessEntry(10, 1, "/usr/bin/bash"), ProcessEntry(11, 10, "/home/u/.local/share/claude/versions/2.1.288")]
        self.assertEqual(count_sessions(entries), 1)
        self.assertEqual(count_sessions([]), 0)

    def test_is_claude_code(self):
        self.assertTrue(is_claude_code("/home/u/.local/bin/claude"))
        self.assertTrue(is_claude_code("/home/u/.local/share/claude/versions/2.1.288"))
        self.assertFalse(is_claude_code("/usr/bin/claude-desktop"))
        self.assertFalse(is_claude_code("/usr/bin/node"))

    def test_fixed_scanner(self):
        self.assertEqual(FixedProcessScanner(4).claude_code_session_count(), 4)


class ProcScannerTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.proc = self.home / "proc"
        self.proc.mkdir()
        (self.proc / "self").mkdir()  # not a pid
        (self.proc / "meminfo").write_text("MemTotal: 1 kB\n")

    def add(self, pid, parent, exe, uid=ME, name="claude", cmdline=None):
        directory = self.proc / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} ({name}) S {parent} {pid} {pid} 0 -1 4194304 0 0\n")
        (directory / "status").write_text(f"Name:\t{name}\nUmask:\t0002\nState:\tS (sleeping)\nPid:\t{pid}\n"
                                          f"PPid:\t{parent}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        if exe is not None:
            os.symlink(exe, directory / "exe")
        if cmdline is not None:
            (directory / "cmdline").write_bytes(b"\0".join(part.encode() for part in cmdline) + b"\0")

    def scanner(self, own_pid=999_999):
        return ProcScanner(self.proc, uid=ME, own_pid=own_pid)

    def test_counts_the_users_sessions_from_proc(self):
        versions = "/home/u/.local/share/claude/versions/2.1.288"
        self.add(100, 1, "/usr/bin/bash", name="bash")
        self.add(101, 100, versions)
        self.add(102, 101, versions)          # helper of 101
        self.add(200, 1, versions + " (deleted)")  # binary replaced by an update
        self.add(300, 1, versions, uid=OTHER)  # another user's session
        self.add(400, 1, None, name="kworker/0:1")  # kernel thread: no exe
        self.add(500, 100, "/usr/bin/node", name="claude",
                 cmdline=["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])  # npm install
        self.add(600, 1, "/usr/bin/node", name="node", cmdline=["node", "server.js"])
        self.add(700, 1, versions, name="weird) (name")  # parentheses in the name
        self.assertEqual(self.scanner().claude_code_session_count(), 4)

    def test_own_process_is_skipped(self):
        self.add(100, 1, "/home/u/.local/bin/claude")
        self.assertEqual(self.scanner(own_pid=100).claude_code_session_count(), 0)
        entries = self.scanner().list_own_processes()
        self.assertEqual(entries, [ProcessEntry(100, 1, "/home/u/.local/bin/claude")])

    def test_unreadable_or_vanishing_entries_are_skipped(self):
        (self.proc / "123").mkdir()  # process exited between listdir and read
        self.add(124, 1, "/home/u/.local/bin/claude")
        (self.proc / "124" / "stat").write_text("garbage")
        self.assertEqual(self.scanner().claude_code_session_count(), 0)
        self.assertEqual(ProcScanner(self.home / "missing", uid=ME, own_pid=1).claude_code_session_count(), 0)

    def test_real_proc_scan_does_not_crash(self):
        from multicred.core import real_env
        self.assertGreaterEqual(real_env.make_process_scanner().claude_code_session_count(), 0)


if __name__ == "__main__":
    unittest.main()
