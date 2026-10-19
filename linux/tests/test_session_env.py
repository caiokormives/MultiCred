"""The graphical session environment: `systemctl --user` plus ~/.config/environment.d."""

import glob
import os
import shutil
import subprocess

from multicred.core.adapters.session_env import (
    ENVIRONMENT_D_FILE, SessionEnvironment, environment_d_line, parse_show_environment,
)
from multicred.core.adapters import session_env
from multicred.core.runner import CommandResult
from tests.support import SafeTestCase

GENERATOR = next(iter(glob.glob("/usr/lib/systemd/user-environment-generators/*environment-d-generator")), None)


class FakeSystemd:
    """Stateful `systemctl --user` behind FakeCommandRunner."""

    def __init__(self, runner, values=None):
        self.values = dict(values or {})
        self.fail_set = False
        self.fail_unset = False
        runner.on("systemctl", ["--user", "show-environment"], respond=self.show)
        runner.on("systemctl", ["--user", "set-environment"], respond=self.set)
        runner.on("systemctl", ["--user", "unset-environment"], respond=self.unset)

    def show(self, _request):
        lines = [f"{k}={v}" if " " not in v else f"{k}=$'{v}'" for k, v in sorted(self.values.items())]
        return CommandResult(0, ("\n".join(lines) + "\n").encode())

    def set(self, request):
        if self.fail_set:
            return CommandResult(1, b"", b"Failed to set environment: Access denied")
        key, value = request.arguments[2].split("=", 1)
        self.values[key] = value
        return CommandResult(0)

    def unset(self, request):
        if self.fail_unset:
            return CommandResult(1, b"", b"Failed to unset environment: Access denied")
        self.values.pop(request.arguments[2], None)
        return CommandResult(0)


class ParsingTests(SafeTestCase):
    def test_parses_plain_and_quoted_values(self):
        text = "HOME=/home/u\nA=$'/x y/it\\'s'\nB=\"/q r\"\nLANG=C.UTF-8\n"
        values = parse_show_environment(text)
        self.assertEqual(values["A"], "/x y/it's")
        self.assertEqual(values["B"], "/q r")
        self.assertEqual(values["HOME"], "/home/u")

    def test_environment_d_line_only_for_values_systemd_reads_back_exactly(self):
        self.assertEqual(environment_d_line("CLAUDE_CONFIG_DIR", "/h/.claude-x y's"),
                         "CLAUDE_CONFIG_DIR=/h/.claude-x y's")
        for unsafe in ["/h/$HOME", "/h/back\\slash", '/h/"q"', "/h/`x`", " /lead", "/trail "]:
            self.assertIsNone(environment_d_line("CLAUDE_CONFIG_DIR", unsafe), unsafe)

    def test_representable_is_the_same_rule_as_environment_d_line(self):
        """S-19: the engine and Verificar ask the adapter instead of copying its regex."""
        self.assertTrue(session_env.representable("/h/.claude-x y's"))
        for unsafe in ["/h/$HOME", "/h/back\\slash", '/h/"q"', "/h/`x`", " /lead", "/trail ", ""]:
            self.assertFalse(session_env.representable(unsafe), unsafe)


class UnrepresentableFolderLogTests(SafeTestCase):
    def test_the_switch_logs_a_warning_when_environment_d_cannot_hold_the_folder(self):
        """S-19: the graphical session would go back to ~/.claude at the next login."""
        from multicred.core.engine.switch_engine import SwitchEngine
        from multicred.core.models import Config, Profile, ProfileColor
        from tests.test_engine import FakeSystem, FakeTools

        class Log:
            def __init__(self):
                self.lines = []

            def log(self, level, message):
                self.lines.append((level, message))

        system = FakeSystem(self.runner)
        folder = f"{self.home}/.claude-$cliente"
        os.makedirs(folder)
        profile = Profile(name="Cliente", color=ProfileColor.TEAL, claude_config_dir=folder)
        log = Log()
        SwitchEngine(self.environment, FakeTools(), log).switch_profile(profile.id, Config(profiles=[profile]))
        self.assertEqual(system.claude, folder, "the running session still gets it")
        warnings = [m for level, m in log.lines if level == "warning"]
        self.assertTrue(any("environment.d" in m and folder in m for m in warnings), log.lines)


class SessionEnvironmentTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.systemd = FakeSystemd(self.runner, {"HOME": str(self.home)})
        self.session = SessionEnvironment(self.environment, base_environment={"HOME": str(self.home)})
        self.file = self.home / ENVIRONMENT_D_FILE

    def test_environment_d_follows_xdg_config_home(self):
        """S-28: systemd reads $XDG_CONFIG_HOME/environment.d when it is set."""
        xdg = self.home / "xdg"
        object.__setattr__(self.environment, "xdg_config_home", str(xdg))
        session = SessionEnvironment(self.environment, base_environment={"HOME": str(self.home)})
        session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-w")
        self.assertIn("CLAUDE_CONFIG_DIR=/h/.claude-w", self.read(xdg / "environment.d/90-multicred.conf"))
        self.assertFalse(self.file.exists())

    def test_get_reads_the_user_manager(self):
        self.assertIsNone(self.session.get("CLAUDE_CONFIG_DIR"))
        self.systemd.values["CLAUDE_CONFIG_DIR"] = "/h/a b"
        self.assertEqual(self.session.get("CLAUDE_CONFIG_DIR"), "/h/a b")

    def test_apply_sets_the_manager_and_writes_environment_d(self):
        undo = self.session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-w")
        self.assertEqual(self.systemd.values["CLAUDE_CONFIG_DIR"], "/h/.claude-w")
        self.assertEqual(self.read(self.file), "# Generated by MultiCred. Do not edit.\nCLAUDE_CONFIG_DIR=/h/.claude-w\n")
        undo()
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.systemd.values)
        self.assertFalse(self.file.exists())

    def test_apply_none_unsets_and_removes_the_file(self):
        self.session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-w")
        undo = self.session.apply("CLAUDE_CONFIG_DIR", None)
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.systemd.values)
        self.assertFalse(self.file.exists())
        undo()
        self.assertEqual(self.systemd.values["CLAUDE_CONFIG_DIR"], "/h/.claude-w")
        self.assertIn("CLAUDE_CONFIG_DIR=/h/.claude-w", self.read(self.file))

    def test_already_applied_changes_nothing(self):
        self.session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-w")
        before = len(self.runner.requests)
        self.session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-w")
        mutating = [r for r in self.runner.requests[before:] if r.arguments[1] != "show-environment"]
        self.assertEqual(mutating, [])

    def test_unsafe_value_still_reaches_the_manager_but_not_environment_d(self):
        self.session.apply("CLAUDE_CONFIG_DIR", "/h/$weird")
        self.assertEqual(self.systemd.values["CLAUDE_CONFIG_DIR"], "/h/$weird")
        self.assertFalse(self.file.exists())

    def test_manager_failure_restores_the_file(self):
        self.session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-w")
        before = self.read(self.file)
        self.systemd.fail_set = True
        with self.assertRaises(Exception):
            self.session.apply("CLAUDE_CONFIG_DIR", "/h/.claude-other")
        self.assertEqual(self.read(self.file), before)

    def test_file_written_is_read_back_by_the_real_systemd_generator(self):
        if GENERATOR is None:
            self.skipTest("systemd environment.d generator not installed")
        value = f"{self.home}/.claude-x y's"
        self.session.apply("CLAUDE_CONFIG_DIR", value)
        result = subprocess.run([GENERATOR], env={"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                                                  "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        line = next(l for l in result.stdout.splitlines() if l.startswith("CLAUDE_CONFIG_DIR="))
        self.assertEqual(parse_show_environment(line)["CLAUDE_CONFIG_DIR"], value)
