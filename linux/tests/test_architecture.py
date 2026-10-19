"""Guards the rules of docs/PLAN.md §4.1 for the Linux core."""

import ast
import re
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "multicred" / "core"

# Only these files may look at the real account or spawn processes.
REAL_ACCOUNT_FILES = {"real_env.py"}
PROCESS_FILES = {"runner.py", "pty.py"}

# Desktop system paths: Termux has no /usr/bin, its /bin/sh is a stub and /tmp is not
# writable, so only core/platform.py may name them (S-13, R-11). The exceptions below
# are reached only on the desktop; each lists the exact literals it may hold, so a new
# hard-coded path in those files still fails.
SYSTEM_PATH = re.compile(r"(?:^|[\s:=\"'(])(?:/usr/(?:local/)?s?bin|/s?bin|/tmp)(?=/|:|\s|$)")
PLATFORM_FILE = "platform.py"
DESKTOP_ONLY_PATHS = {
    # The desktop's test-mode sandbox; on Termux default_sandbox() puts it under $TMPDIR.
    # The passwd shell fallback: make_termux picks $SHELL or $PREFIX/bin/bash instead.
    "real_env.py": {"/tmp/multicred-sandbox", "/bin/bash"},
    # secret-tool reads the Secret Service; on Termux env.sh reads the 0600 files with
    # builtins (secret_reader_for, test_termux.SecretFileEnvTests).
    "rendering/envfile.py": {"/usr/bin/secret-tool"},
    # systemd's user manager: only in the graphical-session step, which Termux skips
    # (Platform.has_graphical_session; test_termux.TermuxSwitchTests).
    "adapters/session_env.py": {"/usr/bin/systemctl"},
    # XDG autostart of the desktop package; there is no graphical login on Android.
    "install/autostart.py": {"/usr/bin/multicred"},
}


def code_strings(path: Path) -> list[str]:
    """String literals of a module (f-string pieces included), docstrings left out."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


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

    def test_system_paths_come_from_the_platform_module(self):
        offenders = []
        for path in core_sources():
            name = path.relative_to(CORE).as_posix()
            if name == PLATFORM_FILE:
                continue
            allowed = DESKTOP_ONLY_PATHS.get(name, set())
            offenders += [f"{name}: {text!r}" for text in code_strings(path)
                          if SYSTEM_PATH.search(text) and text not in allowed]
        self.assertEqual(offenders, [])

    def test_the_path_guard_sees_paths_inside_code(self):
        for text in ("/usr/bin/true", "/bin/sh", "/usr/bin:/bin", "PATH=/usr/local/bin:x", "/tmp", "/tmp/x",
                     "/usr/sbin", "exec /bin/sh -c"):
            self.assertRegex(text, SYSTEM_PATH)
        for text in ("$PREFIX/bin", "/data/data/com.termux/files/usr/bin/sh", "~/.local/bin", "/proc",
                     "usr/bin", "/home/x/tmp", "/binary"):
            self.assertNotRegex(text, SYSTEM_PATH)
