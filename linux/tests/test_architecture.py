"""Guards the rules of docs/PLAN.md §4.1 for the Linux core."""

import re
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "multicred" / "core"

# Only these files may look at the real account or spawn processes.
REAL_ACCOUNT_FILES = {"real_env.py"}
PROCESS_FILES = {"runner.py", "pty.py"}


def core_sources():
    return sorted(p for p in CORE.rglob("*.py"))


class ArchitectureGuardTests(unittest.TestCase):
    def test_core_exists(self):
        self.assertTrue(core_sources(), "multicred/core has no sources")

    def test_core_never_imports_the_ui_toolkit(self):
        pattern = re.compile(r"\b(Gtk|Adw|Gdk|Gsk)\b|require_version\(\s*['\"](Gtk|Adw|Gdk)")
        offenders = [p.relative_to(CORE).as_posix() for p in core_sources() if pattern.search(p.read_text())]
        self.assertEqual(offenders, [])

    def test_only_real_env_reads_the_real_account(self):
        pattern = re.compile(r"os\.environ|os\.getenv|Path\.home\(|expanduser\(|\bpwd\.|getpass\.")
        offenders = [p.relative_to(CORE).as_posix() for p in core_sources()
                     if p.name not in REAL_ACCOUNT_FILES and pattern.search(p.read_text())]
        self.assertEqual(offenders, [])

    def test_only_the_runners_spawn_processes(self):
        pattern = re.compile(r"\bimport subprocess\b|\bfrom subprocess\b|os\.system\(|os\.popen\(|os\.exec|os\.spawn")
        offenders = [p.relative_to(CORE).as_posix() for p in core_sources()
                     if p.name not in PROCESS_FILES and pattern.search(p.read_text())]
        self.assertEqual(offenders, [])

    def test_no_shell_strings(self):
        offenders = [p.relative_to(CORE).as_posix() for p in core_sources() if "shell=True" in p.read_text()]
        self.assertEqual(offenders, [])
