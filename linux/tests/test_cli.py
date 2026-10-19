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
        for command in ("trocar", "status", "perfis", "claude", "verificar", "configurar", "contas", "entrar",
                        "desinstalar"):
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


class CliReviewR1Tests(SafeTestCase):
    """S-15 (review r1): Unicode digits in the choices, "Usar agora" without a profile, the report
    after "Corrigir tudo" at the depth of the first one, and "Corrigir tudo" from `report.fixable`."""

    # Digits Python's str.isdigit() accepts besides 0-9: superscript two, Arabic-Indic one,
    # full-width one (the last two even convert with int()).
    UNICODE_DIGITS = ("²", "١", "１")

    def setUp(self):
        super().setUp()
        from tests.login_support import ToolsAt
        from tests.test_cli_menus import responder
        self.sandbox = real_env.make_sandbox(self.home / "sandbox", responder=responder)
        config = real_env.seed_sandbox(self.sandbox)
        self.ana = ClaudeAccount(nickname="Ana", color=ProfileColor.ORANGE)
        self.sandbox.secrets.set_secret(b"sk-ant-oat01-x", self.ana.secret_account)
        config.claude_accounts = [self.ana]
        ConfigRepository(self.sandbox).save(config)
        self.tools = ToolsAt(git="/usr/bin/git", gh="/usr/bin/gh")

    def cli(self, *argv, inp=""):
        return run(argv, self.sandbox, self.tools, inp=inp)

    def edit(self, change):
        repository = ConfigRepository(self.sandbox)
        config = repository.load()
        change(config)
        repository.save(config)

    # achado 1: str.isdigit() + int()

    def test_unicode_digits_are_not_profile_numbers(self):
        for text in self.UNICODE_DIGITS:
            code, out, err = self.cli("trocar", text)
            self.assertEqual((code, out), (1, ""), text)
            self.assertIn(f"Nenhum perfil chamado “{text}”", err)

    def test_unicode_digits_are_not_claude_account_numbers(self):
        self.assertEqual(self.cli("trocar", "Trabalho")[0], 0)
        for text in self.UNICODE_DIGITS:
            code, out, err = self.cli("claude", text)
            self.assertEqual((code, out), (1, ""), text)
            self.assertIn(f"Nenhuma conta Claude chamada “{text}”", err)

    def test_ascii_numbers_still_choose(self):
        config = ConfigRepository(self.sandbox).load()
        self.assertEqual(cli.resolve_profile(config, " 2 ").name, "Pessoal")
        self.assertIs(cli.resolve_claude_account(config, "1"), config.claude_accounts[0])
        self.assertIsNone(cli.resolve_profile(config, "02"))

    def test_menu_lists_and_colors_say_invalid_option_for_unicode_digits(self):
        from multicred.cli_menus import Terminal
        for text in self.UNICODE_DIGITS:
            out = io.StringIO()
            terminal = Terminal(out, io.StringIO(f"{text}\n2\n"), interactive=True)
            self.assertEqual(terminal.pick("Git", [("a", "A"), ("b", "B")], None), "b", text)
            self.assertIn("Opção inválida.", out.getvalue())
            out = io.StringIO()
            terminal = Terminal(out, io.StringIO(f"{text}\n1\n"), interactive=True)
            self.assertEqual(terminal.color(ProfileColor.ORANGE), list(ProfileColor)[0], text)
            self.assertIn("Opção inválida.", out.getvalue())

    def test_choice_index_takes_only_ascii_numbers_in_range(self):
        self.assertEqual(cli.choice_index("3", 3), 2)
        self.assertEqual(cli.choice_index("8", 2, first=8), 0)
        for text in ("0", "4", "", "-1", "1.0", " 1", *self.UNICODE_DIGITS):
            self.assertIsNone(cli.choice_index(text, 3), text)
        self.assertIsNone(cli.choice_index("7", 2, first=8))

    # achado 5 (not applied, see the S-15 report): removing the applied Claude account with no
    # active profile still goes back to the folder's login, like the Mac (AppModel.swift:372-375,
    # "Shells must stop exporting a token that no longer exists") and ui/model.py.

    def test_removing_the_applied_account_without_a_profile_goes_back_to_the_folder_login(self):
        from multicred.core.models import ActiveState
        from multicred.core.paths import AppPaths
        from multicred.core.storage import StateStore
        store = StateStore(AppPaths(self.sandbox), self.sandbox.clock)
        # The active profile was deleted ("o sistema fica como está"): the token stays applied.
        store.commit(ActiveState(applied_claude_account_id=self.ana.id, generation=1))
        code, out, err = self.cli("contas", "--menu", inp="3\n1\nr\ns\n0\n0\n")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Conta Ana removida", out)
        self.assertIn("Conta Claude: login da pasta", out)
        self.assertNotIn("Não consegui trocar a conta Claude", out)
        self.assertEqual(ConfigRepository(self.sandbox).load().claude_accounts, [])
        state = store.load()
        self.assertEqual((state.profile_id, state.applied_claude_account_id, state.generation), (None, None, 2))

    # achado 8: the report after "Corrigir tudo" has the depth of the first

    def _github_profile_with_a_stale_login(self):
        """Trabalho uses dev-b, whose gh login is no longer valid (the deep run's warning)."""
        from tests import test_cli_menus
        logins = {"hosts": {"github.com": [
            {"host": "github.com", "login": "dev-a", "active": True, "state": "success"},
            {"host": "github.com", "login": "dev-b", "active": False, "state": "error"},
        ]}}
        original = test_cli_menus.GH_LOGINS
        test_cli_menus.GH_LOGINS = logins
        self.addCleanup(setattr, test_cli_menus, "GH_LOGINS", original)
        account = GitHubAccount(nickname="Dev B", username="dev-b", color=ProfileColor.BLUE)
        self.edit(lambda config: (config.github_accounts.append(account),
                                  setattr(config.profiles[0], "github_account_id", account.id)))

    def test_verificar_corrigir_shows_the_new_report_as_deep_as_the_first(self):
        self._github_profile_with_a_stale_login()
        self.assertEqual(self.cli("trocar", "Trabalho")[0], 0)
        code, out, _ = self.cli("verificar", "--corrigir", inp="s\n")
        before, after = out.split("Executar? [s/N] ", 1)
        before, after = before.splitlines(), after.lstrip("\n").splitlines()
        self.assertIsNotNone(row_of(before, "!", "GitHub CLI · login de dev-b"), before)
        self.assertRegex(after[1], HEADER_LINE)
        self.assertIsNotNone(row_of(after, "!", "GitHub CLI · login de dev-b"), after)
        self.assertIn("  No terminal: multicred entrar github", after)

    def test_verificar_corrigir_keeps_the_errors_of_the_repairs(self):
        from datetime import datetime, timezone
        from unittest import mock

        from multicred.core.engine.verifier import Check, CheckStatus, Fix, VerifyReport
        broken = Check("shell", "Bash carrega o MultiCred", CheckStatus.ERROR, fix=Fix("install", "Reescrever"))
        token = Check("gh.token", "GitHub CLI · login de dev-b", CheckStatus.WARNING)
        when = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)

        class FakeVerifier:
            runs = []

            def run(self, config, state, deep=False):
                self.runs.append(deep)
                return VerifyReport([broken, token] if len(self.runs) == 1 else [token], when)

            def fix_all(self, report, config):
                return VerifyReport([], when, errors=["Bash carrega o MultiCred: sem permissão"])

        fake = FakeVerifier()
        with mock.patch.object(cli, "make_verifier", lambda *args, **kwargs: fake):
            code, out, err = self.cli("verificar", "--corrigir", inp="s\n")
        self.assertEqual(fake.runs, [True, True])
        after = out.split("Executar? [s/N] ", 1)[1].splitlines()
        self.assertIsNotNone(row_of(after, "!", "GitHub CLI · login de dev-b"), after)
        self.assertIn("Não consegui corrigir: Bash carrega o MultiCred: sem permissão", err)
        self.assertEqual(code, 1)

    # achado 7: "Corrigir tudo" = report.fixable, one per key

    def test_runnable_fixes_are_report_fixable_deduplicated_by_key(self):
        from datetime import datetime, timezone

        from multicred.core.engine.verifier import Check, CheckStatus, Fix, FixKind, VerifyReport
        install = Fix("install", "Reescrever o bloco", lambda: None)
        bare = Fix("gh", "gh auth switch")  # runs (kind RUN) even without `apply` in this report
        checks = [
            Check("bash", "Bash", CheckStatus.ERROR, fix=install),
            Check("zsh", "Zsh", CheckStatus.ERROR, fix=install),
            Check("gh", "GitHub CLI · conta ativa", CheckStatus.ERROR, fix=bare),
            Check("claude.login", "Claude Code · conta", CheckStatus.ERROR,
                  fix=Fix("login-claude:x", "Entrar", kind=FixKind.LOGIN_CLAUDE)),
            Check("legacy", "Legado", CheckStatus.WARNING, fix=Fix("install-legacy", "Remover")),
        ]
        report = VerifyReport(checks, datetime(2026, 10, 19, tzinfo=timezone.utc))
        expected = []
        for check in report.fixable:
            if check.fix.key not in [fix.key for fix in expected]:
                expected.append(check.fix)
        self.assertEqual(cli.runnable_fixes(report), expected)
        self.assertEqual([fix.key for fix in cli.runnable_fixes(report)], ["install", "gh"])
        self.assertFalse(hasattr(cli, "_opens_page"), "one rule: Fix.opens_page / report.fixable")


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


# MARK: S-14 · `multicred desinstalar` (UninstallPage, SettingsPage.swift:114-174)

UNINSTALL_INTRO = ("Estas são as mudanças. Só o bloco do MultiCred é removido de cada arquivo; o resto fica como "
                   "você deixou.")


def install_bash_block(environment):
    from multicred.core.install.installer import Installer, InstallOptions
    from multicred.core.models import ShellKind
    (Path(environment.home) / ".bashrc").write_text("alias ll='ls -l'\n")
    Installer(environment).apply(InstallOptions(shells=[ShellKind.BASH]))


class DesktopUninstallCliTests(SafeTestCase):
    """The desktop app's test mode (dry-run runner, in-memory secrets)."""

    def setUp(self):
        super().setUp()
        self.sandbox = real_env.make_sandbox(self.home / "sandbox")
        config = real_env.seed_sandbox(self.sandbox)
        self.key = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.sandbox.secrets.set_secret(b"sk-x", self.key.secret_account)
        config.api_keys.append(self.key)
        ConfigRepository(self.sandbox).save(config)
        install_bash_block(self.sandbox)
        self.assertEqual(run(["trocar", "Trabalho"], self.sandbox)[0], 0)
        self.bashrc = self.sandbox.home / ".bashrc"
        self.app_dir = self.sandbox.home / ".config/multicred"

    def commands(self):
        return [entry.command for entry in self.sandbox.runner.entries]

    def test_preview_lists_every_change_and_cancel_changes_nothing(self):
        before = self.bashrc.read_text()
        code, out, err = run(["desinstalar"], self.sandbox, inp="n\n")
        self.assertEqual((code, err), (1, ""))
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["Desinstalar o MultiCred", UNINSTALL_INTRO])
        self.assertIn("~/.bashrc · −3 linhas", lines)
        self.assertIn("  − # >>> MultiCred >>>", lines)
        self.assertIn("  − # <<< MultiCred <<<", lines)
        self.assertIn("~/.config/multicred · será apagado", lines)
        also = next(line for line in lines if line.startswith("Também: "))
        self.assertIn("systemctl --user unset-environment CLAUDE_CONFIG_DIR", also)
        self.assertIn("apagar do chaveiro do sistema as chaves de API e os tokens das contas Claude (pastas e "
                      "conversas do Claude ficam)", also)
        self.assertEqual(lines[-2:], ["Desinstalar? [s/N] ", "Nada foi alterado."])
        self.assertEqual(self.bashrc.read_text(), before)
        self.assertTrue((self.app_dir / "config.json").exists())
        self.assertTrue(self.sandbox.secrets.contains_item(self.key.secret_account))
        self.assertFalse([c for c in self.commands() if "unset-environment" in c])

    def test_no_answer_is_no(self):
        code, out, _ = run(["desinstalar"], self.sandbox, inp="")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], "Nada foi alterado.")
        self.assertTrue((self.app_dir / "config.json").exists())

    def test_confirming_restores_the_files_and_shows_the_result(self):
        before = len(self.commands())
        code, out, err = run(["desinstalar"], self.sandbox, inp="s\n")
        self.assertEqual((code, err), (0, ""), out)
        result = out.split("Desinstalar? [s/N] ", 1)[1].lstrip("\n").splitlines()
        self.assertEqual(result[0], "MultiCred desinstalado")
        restored = next(line for line in result if line.startswith("Arquivos restaurados: "))
        self.assertIn("~/.bashrc", restored)
        self.assertIn("~/.gitconfig", restored)
        self.assertIn("Abra um novo terminal para que os shells deixem de carregar o MultiCred. "
                      "Para tirar o app: sudo apt remove multicred.", result)
        self.assertEqual(self.bashrc.read_text(), "alias ll='ls -l'\n")
        self.assertFalse(self.app_dir.exists())
        self.assertFalse(self.sandbox.secrets.contains_item(self.key.secret_account))
        # The graphical-session step ran through the dry-run runner (recorded, never executed): it
        # asks systemctl what is set and unsets CLAUDE_CONFIG_DIR only if it is there.
        self.assertTrue([c for c in self.commands()[before:] if "systemctl --user" in c], self.commands())

    def test_created_claude_folders_are_deleted_only_when_asked(self):
        from multicred.core.install.uninstaller import remember_created_claude_folder
        from tests.login_support import ToolsAt
        folder = self.sandbox.home / ".claude-celular"
        folder.mkdir()
        remember_created_claude_folder(self.sandbox, str(folder))
        tools = ToolsAt(claude="/usr/bin/claude", git="/usr/bin/git", gh="/usr/bin/gh")
        code, out, _ = run(["desinstalar"], self.sandbox, tools, inp="n\nn\n")
        self.assertIn("Também sair e apagar as pastas do Claude criadas pelo MultiCred (~/.claude-celular)? [s/N] ",
                      out)
        self.assertEqual(code, 1)
        self.assertTrue(folder.exists())
        code, out, _ = run(["desinstalar"], self.sandbox, tools, inp="s\ns\n")
        self.assertEqual(code, 0, out)
        self.assertIn("sair (claude auth logout) e apagar ~/.claude-celular", out)
        self.assertIn("Pastas do Claude apagadas: ~/.claude-celular.", out)
        self.assertFalse(folder.exists())
        self.assertTrue([c for c in self.commands() if "auth logout" in c], "claude auth logout recorded (dry run)")

    def test_nothing_to_uninstall(self):
        empty = real_env.make_sandbox(self.home / "vazio")
        code, out, err = run(["desinstalar"], empty, inp="s\n")
        self.assertEqual((code, out, err),
                         (0, "Nada para desinstalar: o MultiCred não deixou nada nesta máquina.\n", ""))

    def test_a_block_without_its_end_marker_is_a_manual_step(self):
        from multicred.core.install import managed_block
        zshrc = self.sandbox.home / ".zshrc"
        zshrc.write_text(f"{managed_block.BEGIN}\nsource algo\n")
        code, out, _ = run(["desinstalar"], self.sandbox, inp="s\n")
        self.assertIn("~/.zshrc · bloco sem o marcador de fim; fica como está e você remove à mão", out)
        self.assertIn(f"! Remova à mão, em ~/.zshrc, as linhas a partir de \"{managed_block.BEGIN}\".", out)
        self.assertEqual(code, 1, "something is left for the user to do")
        self.assertEqual(zshrc.read_text(), f"{managed_block.BEGIN}\nsource algo\n")


class TermuxUninstallCliTests(TermuxWorld):
    """`multicred desinstalar` in the simulated Termux: no graphical session, the secret files go,
    nothing outside $PREFIX runs."""

    def setUp(self):
        super().setUp()
        self.guard = _PrefixOnlyRunner(self.recording, self.prefix)
        self.termux = dataclasses.replace(self.termux, runner=self.guard)
        self.key = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.termux.secrets.set_secret(b"sk", self.key.secret_account)
        ConfigRepository(self.termux).save(Config(api_keys=[self.key], profiles=[
            Profile(name="Celular", color=ProfileColor.GREEN, api_key_ids=[self.key.id])]))
        install_bash_block(self.termux)
        self.secrets_dir = self.home / ".config/multicred/secrets"
        self.assertTrue((self.secrets_dir / self.key.secret_account).exists())

    def cli(self, *argv, inp=""):
        return run(argv, self.termux, self.termux_tools(), inp=inp)

    def test_preview_names_the_secret_files_and_no_graphical_session(self):
        code, out, err = self.cli("desinstalar", inp="n\n")
        self.assertEqual((code, err), (1, ""))
        self.assertIn("~/.bashrc · −3 linhas", out)
        also = next(line for line in out.splitlines() if line.startswith("Também: "))
        self.assertIn("apagar da pasta ~/.config/multicred/secrets as chaves de API e os tokens das contas Claude",
                      also)
        for absent in ("systemctl", "chaveiro", "sessão"):
            self.assertNotIn(absent, out)
        self.assertEqual(out.splitlines()[-1], "Nada foi alterado.")
        self.assertTrue((self.secrets_dir / self.key.secret_account).exists())

    def test_confirming_removes_the_block_and_the_secret_files(self):
        code, out, err = self.cli("desinstalar", inp="s\n")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("MultiCred desinstalado", out)
        self.assertIn("Abra um novo terminal para que os shells deixem de carregar o MultiCred. "
                      "Para tirar o app: apt remove multicred.", out)
        self.assertNotIn("sudo", out)
        self.assertNotIn("bandeja", out)
        self.assertEqual((self.home / ".bashrc").read_text(), "alias ll='ls -l'\n")
        self.assertFalse((self.home / ".config/multicred").exists())
        self.assertEqual(self.guard.refused, [], [r.executable for r in self.guard.refused])
        self.assert_nothing_forbidden_ran()


class TestModeSandboxTests(TermuxWorld):
    """S-14 · D(3): test mode on Termux keeps its HOME under $TMPDIR (never /tmp) and the
    Termux platform; the desktop's is unchanged."""

    def test_termux_test_mode_lives_under_tmpdir_with_the_termux_platform(self):
        platform = detect(self.termux_environ)
        environment = cli.build_environment(cli.parse_args(["--test-mode", "status"]), platform=platform)
        self.assertEqual(environment.home, (self.prefix / "tmp" / "multicred-sandbox").resolve())
        self.assertTrue(environment.platform.is_termux)
        self.assertEqual(environment.mode, "dryRun")
        self.assertEqual(environment.platform.temp_directory, str(self.prefix / "tmp"))

    def test_termux_fresh_test_mode_makes_its_home_under_tmpdir(self):
        from contextlib import redirect_stderr
        platform = detect(self.termux_environ)
        said = io.StringIO()
        with redirect_stderr(said):
            environment = cli.build_environment(cli.parse_args(["--fresh", "status"]), platform=platform)
        self.assertTrue(str(environment.home).startswith(str((self.prefix / "tmp").resolve()) + "/"), environment.home)
        self.assertIn("HOME de teste vazio em", said.getvalue())
        self.assertTrue(environment.platform.is_termux)

    def test_desktop_test_mode_keeps_the_desktop_platform(self):
        sandbox = self.home / "mesa"
        environment = cli.build_environment(cli.parse_args(["--test-mode", "--sandbox", str(sandbox), "status"]),
                                            platform=Platform.desktop())
        self.assertEqual(environment.home, sandbox.resolve())
        self.assertFalse(environment.platform.is_termux)


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
