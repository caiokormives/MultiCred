"""S-09: `multicred status` and `multicred trocar <perfil>` (desktop test mode and simulated Termux)."""

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from multicred import cli
from multicred.core import real_env
from multicred.core.models import APIKeyAccount, APIProvider, Config, Profile, ProfileColor
from multicred.core.platform import Platform, detect
from multicred.core.storage import ConfigRepository
from tests.support import SafeTestCase
from tests.test_termux import TERMUX_VERSION, TermuxWorld

LINUX = Path(__file__).resolve().parent.parent


def run(argv, environment, tools=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), environment=environment, tools=tools, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class DesktopSandboxCliTests(SafeTestCase):
    """The desktop app's test mode (dry-run runner, in-memory secrets): behaviour unchanged."""

    def setUp(self):
        super().setUp()
        self.sandbox = real_env.make_sandbox(self.home / "sandbox")
        ConfigRepository(self.sandbox).save(real_env.seed_sandbox(self.sandbox))

    def test_status_without_an_active_profile(self):
        code, out, err = run(["status"], self.sandbox)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Nenhum perfil ativo")
        self.assertIn("multicred trocar", out)

    def test_switch_by_name_ignoring_case_then_status(self):
        code, out, err = run(["trocar", "TRABALHO"], self.sandbox)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Agora em Trabalho")
        code, out, _ = run(["status"], self.sandbox)
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertRegex(lines[0], r"^Trabalho · Ativo desde \d\d:\d\d$")
        self.assertIn("git dev@empresa.example", lines)
        self.assertIn("Claude · ~/.claude · login da pasta", lines)
        self.assertIn("Chaves: nenhuma", lines)

    def test_switch_by_number_like_ctrl_n(self):
        code, out, _ = run(["trocar", "2"], self.sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Agora em Pessoal")

    def test_desktop_still_has_the_graphical_session_step(self):
        run(["trocar", "Trabalho"], self.sandbox)
        commands = [entry.command for entry in self.sandbox.runner.entries]
        self.assertTrue(any("systemctl --user" in command for command in commands), commands)

    def test_unknown_profile_exits_1_with_a_portuguese_message(self):
        for name in ("Inexistente", "9", "0"):
            code, out, err = run(["trocar", name], self.sandbox)
            self.assertEqual((code, out), (1, ""), name)
            self.assertIn(f"Nenhum perfil chamado “{name}”", err)
            self.assertIn("1  Trabalho", err)
            self.assertIn("2  Pessoal", err)

    def test_switch_failure_uses_the_app_message(self):
        config = ConfigRepository(self.sandbox).load()
        key = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        profile = config.profiles[0]
        profile.api_key_ids = [key.id]
        config.api_keys.append(key)
        ConfigRepository(self.sandbox).save(config)
        code, out, err = run(["trocar", "Trabalho"], self.sandbox)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Não consegui trocar para Trabalho\n"
                              "A chave Empresa (OPENAI_API_KEY) não está no chaveiro do sistema.\n")

    def test_not_configured(self):
        empty = real_env.make_sandbox(self.home / "empty")
        for argv in (["status"], ["trocar", "x"]):
            code, out, err = run(argv, empty)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("MultiCred ainda não foi configurado", err)


class TermuxCliTests(TermuxWorld):
    def setUp(self):
        super().setUp()
        key = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.termux.secrets.set_secret(b"sk", key.secret_account)
        self.config = Config(api_keys=[key], profiles=[
            Profile(name="Celular", color=ProfileColor.GREEN, api_key_ids=[key.id])])
        ConfigRepository(self.termux).save(self.config)

    def test_status_and_switch(self):
        tools = self.termux_tools()
        self.assertEqual(run(["status"], self.termux, tools)[1].splitlines()[0], "Nenhum perfil ativo")
        code, out, err = run(["trocar", "celular"], self.termux, tools)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Agora em Celular")
        code, out, _ = run(["status"], self.termux, tools)
        self.assertIn("Chaves: OPENAI_API_KEY", out.splitlines())
        self.assert_nothing_forbidden_ran()

    def test_unknown_profile(self):
        code, out, err = run(["trocar", "Mesa"], self.termux, self.termux_tools())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Nenhum perfil chamado “Mesa”", err)

    def test_python_m_multicred_status_runs_without_gtk(self):
        """The whole entry point on "Termux": real_env picks Termux from the environment,
        and nothing imports PyGObject (Termux has no GTK)."""
        blocker = self.home / "blocked"
        (blocker / "gi").mkdir(parents=True)
        (blocker / "gi" / "__init__.py").write_text("raise ImportError('PyGObject is not available on Termux')\n")
        environ = dict(self.termux_environ, PYTHONPATH=f"{blocker}:{LINUX}", PATH=f"{self.prefix}/bin",
                       LANG="C.UTF-8")
        result = subprocess.run([sys.executable, "-m", "multicred", "status"], capture_output=True, text=True,
                                env=environ, cwd=str(self.home), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "Nenhum perfil ativo")
        result = subprocess.run([sys.executable, "-m", "multicred"], capture_output=True, text=True,
                                env=environ, cwd=str(self.home), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trocar", result.stdout)
        self.assertIn("status", result.stdout)


class EntryPointTests(SafeTestCase):
    def test_version_keeps_working(self):
        from multicred import VERSION
        from multicred.__main__ import main
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"MultiCred {VERSION}")

    def test_gui_flags_still_parse(self):
        from multicred.__main__ import parse_args
        args = parse_args(["--test-mode", "--fresh", "--sandbox", "/x", "--background"])
        self.assertEqual((args.test_mode, args.fresh, args.sandbox, args.background, args.command),
                         (True, True, Path("/x"), True, None))
        self.assertTrue(parse_args(["--fresh"]).test_mode)

    def test_no_subcommand_on_termux_prints_help(self):
        from multicred.__main__ import main
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([], platform=detect({"TERMUX_VERSION": TERMUX_VERSION}))
        self.assertEqual(code, 0)
        self.assertIn("trocar", out.getvalue())

    def test_no_subcommand_on_desktop_opens_the_gui(self):
        import multicred.__main__ as entry
        opened = []
        original_build, original_run = entry.build_model, entry.run_gui
        entry.build_model = lambda args: "model"
        entry.run_gui = lambda model, background: opened.append((model, background)) or 0
        try:
            self.assertEqual(entry.main(["--background"], platform=Platform.desktop()), 0)
        finally:
            entry.build_model, entry.run_gui = original_build, original_run
        self.assertEqual(opened, [("model", True)])


if __name__ == "__main__":
    import unittest
    unittest.main()
