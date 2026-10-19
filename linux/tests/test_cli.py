"""S-09: `multicred status` and `multicred trocar <perfil>`; S-10: `perfis`, `claude <conta|pasta>` and
`verificar [--corrigir]` (desktop test mode and simulated Termux). Texts are the Mac's (AppDelegate.swift
menu, VerifyPage.swift) and the app's messages (ui/messages.py, ui/feedback.py)."""

import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from multicred import cli
from multicred.core import real_env
from multicred.core.engine.types import Preflight, PreflightFailure
from multicred.core.models import (
    APIKeyAccount, APIProvider, ClaudeAccount, Config, GitHubAccount, GitIdentity, Profile, ProfileColor,
)
from multicred.core.platform import Platform, detect
from multicred.core.runner import CommandError
from multicred.core.storage import ConfigRepository
from tests.support import SafeTestCase
from tests.test_termux import TERMUX_VERSION, TermuxWorld

LINUX = Path(__file__).resolve().parent.parent


def run(argv, environment, tools=None, inp=""):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), environment=environment, tools=tools, out=out, err=err, inp=io.StringIO(inp))
    return code, out.getvalue(), err.getvalue()


CLAUDE_FOOTER = ["Agents, skills e conversas não mudam.", "Vale para novos terminais; sessões abertas continuam."]
HEADER_LINE = r"^\d+ de \d+ itens de acordo · verificado às \d\d:\d\d$"


def row_of(lines, mark, fragment):
    """Index of the first `verificar` row with `mark` whose title or details mention `fragment`."""
    for index, line in enumerate(lines):
        if line.startswith(mark + " ") and any(fragment in each for each in [line, *row_details(lines, index)]):
            return index
    return None


def row_details(lines, index):
    """The indented lines under the row at `index`."""
    details = []
    for line in lines[index + 1:]:
        if not line.startswith("  "):
            break
        details.append(line)
    return details


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


class CliTests(SafeTestCase):
    """S-10 on the desktop app's test mode: perfis, claude, verificar."""

    def setUp(self):
        super().setUp()
        self.sandbox = real_env.make_sandbox(self.home / "sandbox")
        config = real_env.seed_sandbox(self.sandbox)
        self.marco = ClaudeAccount(nickname="Marco", color=ProfileColor.GREEN, email="marco@example.com")
        self.ana = ClaudeAccount(nickname="Ana", color=ProfileColor.ORANGE)
        self.sandbox.secrets.set_secret(b"sk-ant-oat01-x", self.marco.secret_account)
        config.claude_accounts = [self.marco, self.ana]
        ConfigRepository(self.sandbox).save(config)
        # The default folder's own login, as Claude Code leaves it in ~/.claude.json.
        (self.sandbox.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "eu@example.com"}}))

    def edit(self, change):
        repository = ConfigRepository(self.sandbox)
        config = repository.load()
        change(config)
        repository.save(config)

    def commands(self):
        return [entry.command for entry in self.sandbox.runner.entries]

    def test_help_lists_every_command(self):
        code, out, _ = run([], self.sandbox)
        self.assertEqual(code, 0)
        for command in ("trocar", "status", "perfis", "claude", "verificar"):
            self.assertIn(command, out)

    # perfis: the menu's "Perfis" section (AppDelegate.swift:99-120)

    def test_perfis_numbers_names_and_the_active_check(self):
        code, out, err = run(["perfis"], self.sandbox)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["Perfis", "  1  Trabalho", "  2  Pessoal"])
        run(["trocar", "2"], self.sandbox)
        self.assertEqual(run(["perfis"], self.sandbox)[1].splitlines(), ["Perfis", "  1  Trabalho", "  2  Pessoal  ✓"])

    def test_perfis_warns_when_claude_needs_to_sign_in(self):
        folder = self.sandbox.home / ".claude-pessoal"
        folder.mkdir()
        (folder / ".claude.json").write_text("{}")  # Claude Code's state without a login

        def change(config):
            config.profiles[0].claude_account_id = self.ana.id  # token account without its token
            config.profiles[1].claude_config_dir = str(folder)  # folder without a login
        self.edit(change)
        code, out, _ = run(["perfis"], self.sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["Perfis", "  1  Trabalho  ⚠︎ Claude precisa entrar",
                                            "  2  Pessoal  ⚠︎ Claude precisa entrar"])
        self.edit(lambda config: setattr(config.profiles[0], "claude_account_id", self.marco.id))
        self.assertEqual(run(["perfis"], self.sandbox)[1].splitlines()[1], "  1  Trabalho")

    def test_perfis_without_profiles(self):
        self.edit(lambda config: setattr(config, "profiles", []))
        code, out, _ = run(["perfis"], self.sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Perfis")
        self.assertIn("Nenhum perfil", out)

    def test_not_configured(self):
        empty = real_env.make_sandbox(self.home / "empty")
        for argv in (["perfis"], ["claude"], ["claude", "pasta"], ["verificar"], ["verificar", "--corrigir"]):
            code, out, err = run(argv, empty)
            self.assertEqual((code, out), (1, ""), argv)
            self.assertIn("MultiCred ainda não foi configurado", err)

    # claude: the "Trocar só a conta Claude" submenu (AppDelegate.swift:142-194)

    def test_claude_without_an_active_profile(self):
        code, out, err = run(["claude"], self.sandbox)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["Conta Claude", "Ative um perfil primeiro"])
        code, out, err = run(["claude", "marco"], self.sandbox)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Não consegui trocar a conta Claude\n"
                              "Ative um perfil antes de trocar só a conta Claude para Marco.\n")

    def test_claude_lists_the_folder_login_and_the_accounts_like_the_menu(self):
        run(["trocar", "Trabalho"], self.sandbox)
        code, out, err = run(["claude"], self.sandbox)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "Conta Claude · pasta ~/.claude (Trabalho) fica",
            "  pasta  Login da pasta · eu@example.com · do perfil  ✓",
            "  1      Marco · marco@example.com",
            "  2      Ana · sem token · conta Claude",
            *CLAUDE_FOOTER,
        ])

    def test_claude_switches_only_the_claude_account(self):
        run(["trocar", "Trabalho"], self.sandbox)
        before = len(self.commands())
        code, out, err = run(["claude", "MARCO"], self.sandbox)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Conta Claude: Marco")
        self.assertTrue(out.splitlines()[1].startswith("Só o login do Claude mudou; agents, skills e conversas ficam."))
        later = self.commands()[before:]
        self.assertFalse([c for c in later if "git " in c or "gh " in c], later)  # git and gh stay
        status = run(["status"], self.sandbox)[1].splitlines()
        self.assertIn("Claude · ~/.claude · Marco (só Claude)", status)
        self.assertIn("git dev@empresa.example", status)
        listing = run(["claude"], self.sandbox)[1].splitlines()
        self.assertEqual(listing[1], "  pasta  Login da pasta · eu@example.com · do perfil")
        self.assertEqual(listing[2], "  1      Marco · marco@example.com  ✓")

        code, out, _ = run(["claude", "pasta"], self.sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Conta Claude: login da pasta")
        self.assertIn("Claude · ~/.claude · login da pasta", run(["status"], self.sandbox)[1].splitlines())

    def test_claude_by_number(self):
        run(["trocar", "Trabalho"], self.sandbox)
        code, out, _ = run(["claude", "1"], self.sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Conta Claude: Marco")

    def test_claude_account_without_a_token_uses_the_app_message(self):
        run(["trocar", "Trabalho"], self.sandbox)
        code, out, err = run(["claude", "Ana"], self.sandbox)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Não consegui trocar a conta Claude\n"
                              "O token da conta Claude Ana não está no chaveiro do sistema. "
                              "Gere outro em Contas › Claude.\n")

    def test_claude_unknown_account(self):
        run(["trocar", "Trabalho"], self.sandbox)
        for name in ("Zeca", "3", "0"):
            code, out, err = run(["claude", name], self.sandbox)
            self.assertEqual((code, out), (1, ""), name)
            self.assertIn(f"Nenhuma conta Claude chamada “{name}”", err)
            self.assertIn("pasta  Login da pasta", err)
            self.assertIn("1      Marco", err)

    def test_claude_folder_without_a_login(self):
        (self.sandbox.home / ".claude.json").write_text("{}")
        run(["trocar", "Trabalho"], self.sandbox)
        self.assertEqual(run(["claude"], self.sandbox)[1].splitlines()[1],
                         "  pasta  Login da pasta · Sem login · do perfil  ✓")

    # verificar (VerifyPage.swift). The rows' titles are the verifier's (S-06); these tests
    # look at the layout: marks, "Esperado …, encontrado …", "definido em", the fix and the top.

    def test_verificar_header_rows_and_exit_code(self):
        code, out, _ = run(["verificar"], self.sandbox)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Nenhum perfil ativo")
        self.assertRegex(lines[1], HEADER_LINE)
        self.assertIsNotNone(row_of(lines, "✗", "~/.bashrc"), lines)
        self.assertIn("Integração", lines)
        self.assertEqual(code, 1)  # problems left
        self.assertEqual(lines[-2:], ["Corrigir tudo: multicred verificar --corrigir",
                                      "“Corrigir” mostra o comando antes de executar."])
        run(["trocar", "Trabalho"], self.sandbox)
        lines = run(["verificar"], self.sandbox)[1].splitlines()
        self.assertEqual(lines[0], "Trabalho")
        self.assertIn("Ferramentas", lines)
        self.assertLess(lines.index("Ferramentas"), lines.index("Integração"))

    def test_verificar_rows_show_expected_found_and_the_fix(self):
        lines = run(["verificar"], self.sandbox)[1].splitlines()
        index = row_of(lines, "✗", "~/.bashrc")
        details = row_details(lines, index)
        self.assertTrue(any(line.startswith("  Corrigir: Reescrever o bloco do MultiCred") for line in details), details)
        self.assertTrue(any(line.startswith("  Esperado ") and ", encontrado " in line for line in lines), lines)

    def test_verificar_corrigir_asks_first_and_cancel_changes_nothing(self):
        for answer in ("", "n\n", "nao\n"):
            code, out, _ = run(["verificar", "--corrigir"], self.sandbox, inp=answer)
            self.assertEqual(code, 1, answer)
            lines = out.splitlines()
            question = lines.index("Corrigir tudo?")
            bullets = [line for line in lines[question:] if line.startswith("• ")]
            self.assertTrue(bullets and all(" (com backup)." in line for line in bullets), lines)
            self.assertEqual(lines[-2:], ["Executar? [s/N] ", "Nada foi alterado."])
            self.assertFalse((self.sandbox.home / ".bashrc").exists())
            self.assertFalse((self.sandbox.home / ".gitconfig").exists())

    def test_verificar_corrigir_runs_the_fixes_and_shows_the_new_report(self):
        code, out, _ = run(["verificar", "--corrigir"], self.sandbox, inp="sim\n")
        self.assertTrue((self.sandbox.home / ".bashrc").exists())
        after = out.split("Executar? [s/N] ", 1)[1].lstrip("\n").splitlines()
        self.assertEqual(after[0], "Nenhum perfil ativo")
        self.assertRegex(after[1], HEADER_LINE)
        self.assertIsNotNone(row_of(after, "✓", "~/.bashrc"), after)
        self.assertEqual(code, 0, out)


def _wording_failures():
    return {
        "apiKeyMissing": PreflightFailure("apiKeyMissing", "Empresa", "OPENAI_API_KEY"),
        "claudeTokenMissing": PreflightFailure("claudeTokenMissing", "Ana"),
        "claudeLoginMissing": PreflightFailure("claudeLoginMissing", "Ana"),
        "secretsUnavailable": PreflightFailure("secretsUnavailable", "Permission denied"),
    }


class SecretWordingTests(SafeTestCase):
    """Where secrets live, in the messages: the keyring on the desktop, the private files on Termux."""

    def test_desktop_wording_is_unchanged(self):
        from multicred.ui.messages import describe_preflight, secret_store_wording
        expected = {
            "apiKeyMissing": "A chave Empresa (OPENAI_API_KEY) não está no chaveiro do sistema.",
            "claudeTokenMissing": "O token da conta Claude Ana não está no chaveiro do sistema. "
                                  "Gere outro em Contas › Claude.",
            "claudeLoginMissing": "O login da conta Claude Ana não está no chaveiro do sistema. "
                                  "Em Contas › Claude › Ana, use \"Entrar de novo\".",
            "secretsUnavailable": "Não consegui consultar o chaveiro do sistema: Permission denied",
        }
        for kind, failure in _wording_failures().items():
            self.assertEqual(describe_preflight(failure), expected[kind])
            self.assertEqual(describe_preflight(failure, secret_store_wording(Platform.desktop())), expected[kind])

    def test_termux_names_the_secret_files(self):
        from multicred.ui.messages import describe_preflight, describe_switch_error, secret_store_wording
        termux = secret_store_wording(detect({"TERMUX_VERSION": TERMUX_VERSION}))
        failures = _wording_failures()
        self.assertEqual(describe_preflight(failures["apiKeyMissing"], termux),
                         "A chave Empresa (OPENAI_API_KEY) não está em ~/.config/multicred/secrets.")
        self.assertEqual(describe_preflight(failures["secretsUnavailable"], termux),
                         "Não consegui consultar a pasta ~/.config/multicred/secrets: Permission denied")
        self.assertEqual(describe_switch_error(Preflight(failures["claudeTokenMissing"]), termux),
                         "O token da conta Claude Ana não está em ~/.config/multicred/secrets. "
                         "Gere outro em Contas › Claude.")
        for failure in failures.values():
            self.assertNotIn("chaveiro", describe_preflight(failure, termux))


class _PrefixOnlyRunner:
    """Refuses anything outside $PREFIX before it runs, so a slip can never reach this
    machine's real systemctl, secret-tool or claude (here they live in /usr/bin)."""

    def __init__(self, inner, prefix):
        self.inner, self.prefix, self.refused = inner, str(prefix) + "/", []

    def run(self, request):
        if not request.executable.startswith(self.prefix):
            self.refused.append(request)
            raise CommandError(f"outside $PREFIX: {request.executable}")
        return self.inner.run(request)


class TermuxCommandTests(TermuxWorld):
    """S-10 in the simulated Termux: perfis, claude, verificar, and the Termux wording."""

    def setUp(self):
        super().setUp()
        self.guard = _PrefixOnlyRunner(self.recording, self.prefix)
        self.termux = dataclasses.replace(self.termux, runner=self.guard)
        git = shutil.which("git")
        if git:
            # Termux's git lives in $PREFIX/bin; this machine's git stands in for it
            # (it only reads the temporary HOME's files).
            (self.prefix / "bin" / "git").symlink_to(git)
        self.has_git = bool(git)
        identity = GitIdentity(nickname="A", name="Dev A", email="dev@a.example", color=ProfileColor.INDIGO)
        github = GitHubAccount(nickname="A", username="dev-a", color=ProfileColor.INDIGO)
        self.key = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.token = ClaudeAccount(nickname="Marco", color=ProfileColor.GREEN, email="marco@example.com")
        self.termux.secrets.set_secret(b"sk", self.key.secret_account)
        self.termux.secrets.set_secret(b"sk-ant-oat01-x", self.token.secret_account)
        self.config = Config(git_identities=[identity], github_accounts=[github], api_keys=[self.key],
                             claude_accounts=[self.token],
                             profiles=[Profile(name="Celular", color=ProfileColor.GREEN, git_identity_id=identity.id,
                                               github_account_id=github.id, api_key_ids=[self.key.id])])
        ConfigRepository(self.termux).save(self.config)

    def cli(self, *argv, inp=""):
        return run(argv, self.termux, self.termux_tools(), inp=inp)

    def assert_stayed_in_termux(self):
        self.assertEqual(self.guard.refused, [], [r.executable for r in self.guard.refused])
        self.assert_nothing_forbidden_ran()

    def test_perfis_and_claude(self):
        self.assertEqual(self.cli("perfis")[1].splitlines(), ["Perfis", "  1  Celular"])
        self.assertEqual(self.cli("trocar", "1")[0], 0)
        self.assertEqual(self.cli("perfis")[1].splitlines(), ["Perfis", "  1  Celular  ✓"])
        code, out, err = self.cli("claude")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Conta Claude · pasta ~/.claude (Celular) fica")
        self.assertEqual(out.splitlines()[2], "  1      Marco · marco@example.com")
        code, out, err = self.cli("claude", "marco")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Conta Claude: Marco")
        self.assertIn("Claude · ~/.claude · Marco (só Claude)", self.cli("status")[1].splitlines())
        self.assertIn(f'"$HOME/.config/multicred/secrets/{self.token.secret_account}"', self.paths.env_sh.read_text())
        self.assert_stayed_in_termux()

    def test_missing_secret_files_are_named_not_the_keyring(self):
        secrets = self.home / ".config/multicred/secrets"
        (secrets / self.key.secret_account).unlink()
        code, out, err = self.cli("trocar", "Celular")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Não consegui trocar para Celular\n"
                              "A chave O (OPENAI_API_KEY) não está em ~/.config/multicred/secrets.\n")
        (secrets / self.token.secret_account).unlink()
        self.config.profiles[0].api_key_ids = []
        ConfigRepository(self.termux).save(self.config)
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        code, out, err = self.cli("claude", "Marco")
        self.assertEqual((code, out), (1, ""))
        # S-11: Termux has no "Contas › Claude" screen; the hint names the terminal command.
        self.assertEqual(err, "Não consegui trocar a conta Claude\n"
                              "O token da conta Claude Marco não está em ~/.config/multicred/secrets. "
                              "Gere outro com: multicred entrar claude Marco\n")
        self.assert_stayed_in_termux()

    def test_verificar_has_no_graphical_session_row_and_runs_nothing_outside_prefix(self):
        if not self.has_git:
            self.skipTest("git is not installed on this machine")
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        code, out, _ = self.cli("verificar")
        lines = out.splitlines()
        self.assertEqual(lines[0], "Celular")
        self.assertRegex(lines[1], HEADER_LINE)
        # No row for the graphical session (systemctl --user, environment.d): Android has none.
        for absent in ("sessão gráfica", "pasta para apps", "systemd", "environment.d"):
            self.assertNotIn(absent, out)
        self.assertIsNotNone(row_of(lines, "✓", "dev-a em github.com"), lines)  # the fake gh, from $PREFIX/bin
        self.assertIsNotNone(row_of(lines, "✓", "CLAUDE_CONFIG_DIR não definido (~/.claude)"), lines)
        self.assertIsNotNone(row_of(lines, "✗", "~/.bashrc"), lines)
        self.assertEqual(code, 1)
        self.assert_stayed_in_termux()

    def test_verificar_corrigir_installs_and_ends_consistent(self):
        if not self.has_git:
            self.skipTest("git is not installed on this machine")
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        code, out, err = self.cli("verificar", "--corrigir", inp="s\n")
        self.assertEqual(err, "")
        before, after = out.split("Corrigir tudo?", 1)
        self.assertIsNotNone(row_of(before.splitlines(), "✗", "~/.bashrc"), before)
        bullets = [line for line in after.splitlines() if line.startswith("• ")]
        self.assertTrue(bullets, after)
        self.assertFalse([line for line in bullets if "systemctl" in line], bullets)
        report = after.split("Executar? [s/N] ", 1)[1].lstrip("\n").splitlines()
        self.assertEqual(report[0], "Celular")
        self.assertIsNotNone(row_of(report, "✓", "~/.bashrc"), report)
        self.assertIsNotNone(row_of(report, "✓", "Dev A <dev@a.example>"), report)  # git reads the new include
        self.assertFalse([line for line in report if line.startswith("✗ ")], report)
        self.assertEqual(code, 0, out)
        self.assertIn("multicred", (self.home / ".bashrc").read_text())
        self.assertEqual(self.cli("verificar")[0], 0)
        self.assert_stayed_in_termux()


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
        for command in ("trocar", "status", "perfis", "claude", "verificar"):
            self.assertIn(command, result.stdout)
        # S-10: the new commands through the real entry point, without PyGObject. No profile
        # is active, so `verificar` only reads files (no git, gh or systemctl runs).
        for argv, code, first in ((["perfis"], 0, "Perfis"), (["claude"], 0, "Conta Claude"),
                                  (["verificar"], 1, "Nenhum perfil ativo")):
            result = subprocess.run([sys.executable, "-m", "multicred", *argv], capture_output=True, text=True,
                                    env=environ, cwd=str(self.home), timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, code, (argv, result.stdout, result.stderr))
            self.assertEqual(result.stdout.splitlines()[0], first, argv)
            self.assertNotIn("Traceback", result.stderr, argv)


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
