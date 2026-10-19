"""Verificar e Corrigir (port of VerifierTests in Phase4CoreTests.swift, Linux checks).

S-08 ported the checks; S-06 gave each one the Mac's section (Ferramentas /
Integração / Sessões) and severity (error ✗ / warning !), and the fixes that lead to
a login or to a key instead of running a command (`Engine/Verifier.swift`).

States are real files in a temporary HOME: the installer and the switch engine
write them, git is the real git (GIT_CONFIG_NOSYSTEM, temporary HOME), gh and
`systemctl --user` are the stateful fakes of tests/test_engine.FakeSystem.
"""

import dataclasses
import json
import os
import subprocess
from pathlib import Path

from multicred.core.adapters.process_scanner import FixedProcessScanner
from multicred.core.claude.status import ClaudeLoginStatus
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.engine.verifier import Check, CheckSection, CheckStatus, FixKind, Verifier
from multicred.core.install.installer import Installer, InstallOptions
from multicred.core.models import (
    ActiveState, APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config, GitHubAccount, GitIdentity,
    Profile, ProfileColor, Settings, ShellKind, uuid_text,
)
from multicred.core.paths import AppPaths
from multicred.core.runner import CommandResult
from multicred.core.storage import StateStore
from tests.support import SafeTestCase
from tests.test_engine import FakeSystem, FakeTools

GIT = "/usr/bin/git"
LEGACY_LINE = '[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"\n'


def real_git(home):
    """Answers git requests with the real git, isolated from the system config."""

    def respond(request):
        environment = dict(request.environment, GIT_CONFIG_NOSYSTEM="1")
        assert environment["HOME"] == str(home), "git must only ever see the temporary HOME"
        done = subprocess.run([GIT, *request.arguments], env=environment, cwd=request.cwd or str(home),
                              capture_output=True, timeout=20)
        return CommandResult(done.returncode, done.stdout, done.stderr)

    return respond


class VerifierTestCase(SafeTestCase):
    """A machine where MultiCred was installed for bash and the profile Trabalho activated;
    the profile's Claude folder has its own login (the folder's `.claude.json`)."""

    def setUp(self):
        super().setUp()
        self.system = FakeSystem(self.runner)
        self.system.gh_active = "dev-pessoal"  # the switch must really move it
        self.runner.on("git", respond=real_git(self.home))
        self.paths = AppPaths(self.environment)
        self.claude_dir = f"{self.home}/.claude-empresa"
        (self.home / ".claude-empresa").mkdir()
        self.set_folder_login("dev@empresa.com.br")
        (self.home / ".bashrc").write_text("export KEEP=yes\n")
        (self.home / ".gitconfig").write_text("[core]\n\teditor = vim\n")
        git = GitIdentity(nickname="Empresa", name="Dev Trabalho", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
        gh = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
        self.openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.gemini = APIKeyAccount(nickname="Pessoal", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
        self.secrets.set_secret(b"sk", self.openai.secret_account)
        self.secrets.set_secret(b"g", self.gemini.secret_account)
        self.profile = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git.id,
                               github_account_id=gh.id, claude_config_dir=self.claude_dir,
                               api_key_ids=[self.openai.id, self.gemini.id])
        self.config = Config(git_identities=[git], github_accounts=[gh], api_keys=[self.openai, self.gemini],
                             profiles=[self.profile], settings=Settings(enabled_shells=[ShellKind.BASH]))
        Installer(self.environment).apply(InstallOptions(shells=[ShellKind.BASH]))
        self.state = SwitchEngine(self.environment, FakeTools()).switch_profile(self.profile.id, self.config).state
        self.verifier = Verifier(self.environment, FakeTools(), FixedProcessScanner(2))
        self.key_ids = [f"key.{uuid_text(self.openai.id)}", f"key.{uuid_text(self.gemini.id)}"]

    def set_folder_login(self, email):
        """The folder's `.claude.json`: logged in as `email`, or logged out with None."""
        data = {"oauthAccount": {"emailAddress": email}} if email else {"numStartups": 3}
        (self.home / ".claude-empresa" / ".claude.json").write_text(json.dumps(data))

    def verify(self, deep=False):
        state = StateStore(self.paths, self.environment.clock).load()
        return self.verifier.run(self.config, state, deep=deep)

    def check(self, report, check_id):
        return next(c for c in report.checks if c.id == check_id)

    def mutating_commands(self):
        out = []
        for request in self.runner.requests:
            args = request.arguments
            if args[:2] == ["auth", "switch"] or (len(args) > 1 and args[1] in ("set-environment", "unset-environment")):
                out.append(" ".join(args))
        return out

    def problems(self, report):
        return {c.id: (c.found, c.expected) for c in report.problems}

    def asked_gh_token_health(self):
        """`gh auth status` (the Claude check asks `claude auth status` on every run)."""
        return any(Path(r.executable).name == "gh" and r.arguments[:2] == ["auth", "status"]
                   for r in self.runner.requests)


class VerifierTests(VerifierTestCase):
    def system_ids(self):
        return ["git", "gh", "claude.session", "claude.login", *self.key_ids, "shell.bash", "claude.shells"]

    def test_consistent_system_is_eight_of_eight(self):
        report = self.verify()
        self.assertEqual(self.problems(report), {})
        self.assertEqual([c.id for c in report.system], self.system_ids())
        self.assertEqual((report.ok_count, report.total), (8, 8))
        self.assertTrue(report.is_consistent)
        self.assertEqual(report.profile_name, "Trabalho")
        self.assertTrue(all(c.fix is None for c in report.checks), "nothing to fix on a consistent system")
        # Titles and details of the Mac (Verifier.swift:96, 124, 137, 164, 185, 202).
        self.assertEqual(self.check(report, "gh").title, "GitHub CLI · conta ativa")
        self.assertEqual(self.check(report, "gh").found, "dev-empresa em github.com")
        self.assertEqual(self.check(report, "claude.session").title, "Claude Code · pasta para apps")
        login = self.check(report, "claude.login")
        self.assertEqual((login.title, login.found),
                         ("Claude Code · conta", "login da pasta ~/.claude-empresa · dev@empresa.com.br"))
        openai = self.check(report, self.key_ids[0])
        self.assertEqual((openai.title, openai.found), ("OpenAI · OPENAI_API_KEY", "Empresa no chaveiro do sistema"))
        self.assertEqual(self.check(report, "shell.bash").title, "~/.bashrc carrega o MultiCred")
        self.assertEqual(self.check(report, "claude.shells").title, "Arquivos de ambiente do shell")
        self.assertEqual(self.check(report, "claude.shells").found, "CLAUDE_CONFIG_DIR=~/.claude-empresa")
        self.assertNotIn("legacy", [c.id for c in report.checks], "the Mac lists the old version only when found")
        self.assertEqual([(c.id, c.status, c.title) for c in report.informative],
                         [("claude.sessions", CheckStatus.INFO, "2 sessões do Claude Code abertas")])

    def test_every_check_has_the_section_of_the_mac(self):
        """VerifyPage.swift:21-23 groups by `CheckItem.section`."""
        bashrc = self.home / ".bashrc"
        bashrc.write_text(bashrc.read_text() + LEGACY_LINE)
        report = self.verify(deep=True)
        sections = {c.id: c.section for c in report.checks}
        self.assertEqual(sections, {
            "git": CheckSection.TOOLS, "gh": CheckSection.TOOLS, "gh.token": CheckSection.TOOLS,
            "claude.session": CheckSection.TOOLS, "claude.login": CheckSection.TOOLS,
            self.key_ids[0]: CheckSection.TOOLS, self.key_ids[1]: CheckSection.TOOLS,
            "shell.bash": CheckSection.INTEGRATION, "claude.shells": CheckSection.INTEGRATION,
            "legacy": CheckSection.INTEGRATION, "claude.sessions": CheckSection.SESSIONS,
        })
        self.assertEqual([s.title for s in CheckSection], ["Ferramentas", "Integração", "Sessões"])
        self.assertEqual([c.id for c in report.in_section(CheckSection.SESSIONS)], ["claude.sessions"])
        self.assertEqual([c.id for c in report.in_section(CheckSection.INTEGRATION)],
                         ["shell.bash", "claude.shells", "legacy"])

    def test_an_include_problem_alone_belongs_to_integration(self):
        """Mac `git.include` is an Integração item; the Linux git row moves there when
        the identity itself is fine and only the include's place is wrong."""
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[alias]\n\tco = checkout\n")
        item = self.check(self.verify(), "git")
        self.assertEqual((item.status, item.section), (CheckStatus.ERROR, CheckSection.INTEGRATION))
        without_identity = self.verifier.run(self.config, ActiveState())
        self.assertEqual(self.check(without_identity, "git").section, CheckSection.INTEGRATION)

    def test_email_overridden_after_the_include_points_at_file_and_line_and_fix_moves_the_include(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[user]\n\temail = velho@exemplo.com\n")
        line = gitconfig.read_text().split("\n").index("\temail = velho@exemplo.com") + 1
        report = self.verify()
        item = self.check(report, "git")
        self.assertEqual((item.status, item.section), (CheckStatus.ERROR, CheckSection.TOOLS))
        self.assertEqual(item.title, "E-mail do git sobrescrito")
        self.assertEqual((item.expected, item.found), ("dev@empresa.com.br", "velho@exemplo.com"))
        self.assertEqual(item.origin, f"~/.gitconfig:{line}")
        self.assertIn("depois do include do MultiCred", item.hint)
        self.assertIn("~/.gitconfig", item.fix.command)
        self.assertEqual((item.fix.kind, item.fix.button_title), (FixKind.RUN, "Corrigir"))
        self.assertEqual((report.ok_count, report.total), (7, 8))

        fixed = self.verifier.fix(item)
        self.assertEqual(self.check(fixed, "git").status, CheckStatus.OK, self.problems(fixed))
        text = gitconfig.read_text()
        self.assertIn("\temail = velho@exemplo.com\n", text, "the user's own line is kept")
        self.assertTrue(text.endswith('[include]\n\tpath = "~/.config/multicred/gitconfig"\n# <<< MultiCred <<<\n'))
        self.assertTrue(fixed.is_consistent, self.problems(fixed))

    def test_stale_included_file_is_fixed_by_reapplying_the_profile(self):
        self.paths.gitconfig.write_text("[user]\n\tname = Dev Trabalho\n\temail = antigo@empresa.com.br\n")
        item = self.check(self.verify(), "git")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual(item.origin, "~/.config/multicred/gitconfig:3")
        self.assertIn("Reaplicar o perfil Trabalho", item.fix.command)
        self.assertTrue(self.verifier.fix(item).is_consistent)

    def test_include_that_is_not_last_is_a_problem_even_without_an_override(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[alias]\n\tco = checkout\n")
        item = self.check(self.verify(), "git")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual(item.title, "Include do MultiCred não é o último")
        self.assertEqual(item.origin, f"~/.gitconfig:{gitconfig.read_text().split(chr(10)).index('[alias]') + 1}")
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertIn("\tco = checkout\n", gitconfig.read_text())

    def test_gh_switched_outside_the_app_is_fixed_with_gh_auth_switch(self):
        self.system.gh_active = "dev-pessoal"
        item = self.check(self.verify(), "gh")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual((item.expected, item.found), ("dev-empresa", "dev-pessoal em github.com"))
        self.assertEqual(item.fix.command, "gh auth switch --hostname github.com --user dev-empresa")
        fixed = self.verifier.fix(item)
        self.assertEqual(self.check(fixed, "gh").status, CheckStatus.OK)
        self.assertEqual(self.system.gh_active, "dev-empresa")
        self.assertTrue(fixed.is_consistent, self.problems(fixed))

    def test_hook_block_removed_from_bashrc_is_reinstalled(self):
        (self.home / ".bashrc").write_text("export KEEP=yes\n")
        item = self.check(self.verify(), "shell.bash")
        self.assertEqual((item.status, item.section), (CheckStatus.ERROR, CheckSection.INTEGRATION))
        self.assertEqual(item.found, "a linha do MultiCred foi removida")  # Verifier.swift:187
        self.assertIn("~/.bashrc", item.fix.command)
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        bashrc = (self.home / ".bashrc").read_text()
        self.assertTrue(bashrc.startswith("export KEEP=yes\n\n# >>> MultiCred >>>\n"), bashrc)

    def test_missing_hook_file_is_a_problem_too(self):
        self.paths.hook(ShellKind.BASH).unlink()
        item = self.check(self.verify(), "shell.bash")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual(item.found, "o arquivo do hook está diferente")
        self.assertTrue(self.verifier.fix(item).is_consistent)

    def test_missing_key_is_an_error_whose_fix_opens_the_key(self):
        """Verifier.swift:159-170 and VerifyPage.swift:137, 158: "Abrir chave…" opens the
        key's editor; nothing runs."""
        self.secrets.delete_item(self.gemini.secret_account)
        report = self.verify()
        item = self.check(report, self.key_ids[1])
        self.assertEqual((item.status, item.section), (CheckStatus.ERROR, CheckSection.TOOLS))
        self.assertEqual(item.title, "Gemini · GEMINI_API_KEY")
        self.assertEqual(item.found, "chave não está no chaveiro do sistema")
        self.assertEqual(self.check(report, self.key_ids[0]).status, CheckStatus.OK)
        self.assertEqual((item.fix.kind, item.fix.target), (FixKind.OPEN_API_KEY, self.gemini.id))
        self.assertEqual(item.fix.button_title, "Abrir chave…")
        self.assertEqual(item.fix.command, "Abrir a chave Pessoal para colar o valor de novo.")
        self.assertTrue(item.fix.opens_page)
        self.assertEqual(report.fixable, [], "Corrigir tudo has nothing it can run by itself")
        self.assertFalse(report.is_consistent)

    def test_folder_without_login_is_an_error_whose_fix_is_the_folder_login(self):
        self.set_folder_login(None)
        item = self.check(self.verify(), "claude.login")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual(item.found, "a pasta ~/.claude-empresa está sem login")
        self.assertEqual((item.fix.kind, item.fix.target), (FixKind.LOGIN_CLAUDE_FOLDER, self.claude_dir))
        self.assertEqual(item.fix.button_title, "Entrar…")
        self.assertEqual(item.fix.command, "Entrar no Claude Code na pasta ~/.claude-empresa (claude auth login).")

    def test_unknown_folder_login_is_a_warning_without_fix(self):
        (self.home / ".claude-empresa" / ".claude.json").unlink()
        report = self.verify()
        item = self.check(report, "claude.login")
        self.assertEqual((item.status, item.found, item.fix), (CheckStatus.WARNING, "status indisponível", None))
        self.assertTrue(report.is_consistent, "a warning is not a problem (VerificationReport.problems)")
        self.assertEqual(report.warnings, [item])
        self.assertEqual((report.ok_count, report.total), (7, 8))

    def test_the_login_status_comes_from_the_injected_service(self):
        """Verifier.swift:146: the app passes its cached statuses."""
        asked = []

        def status(account):
            asked.append(account.config_dir)
            return ClaudeLoginStatus.logged_in("outra@empresa.com.br")

        verifier = Verifier(self.environment, FakeTools(), FixedProcessScanner(0), claude_status=status)
        item = self.check(verifier.run(self.config, self.state), "claude.login")
        self.assertEqual(asked, [self.claude_dir])
        self.assertEqual(item.found, "login da pasta ~/.claude-empresa · outra@empresa.com.br")

    def test_fix_all_asks_the_claude_status_once_while_it_rechecks(self):
        """Each `claude auth status` costs about a second; "Corrigir tudo" re-checks after every repair."""
        asked = []

        def status(account):
            asked.append(account.config_dir)
            return ClaudeLoginStatus.logged_in(None)

        verifier = Verifier(self.environment, FakeTools(), FixedProcessScanner(0), claude_status=status)
        (self.home / ".bashrc").write_text("export KEEP=yes\n")
        self.system.gh_active = "dev-pessoal"
        fixed = verifier.fix_all(config=self.config)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertEqual(asked, [self.claude_dir])
        verifier.run(self.config, self.state)
        self.assertEqual(len(asked), 2, "a plain run asks again")

    def test_claude_account_in_use_is_checked_in_the_keyring_and_its_fix_signs_in_again(self):
        """Verifier.swift:130-144: with an account applied, the folder's own login does not
        matter; a missing token or login leads to "Entrar…" for that account."""
        marco = ClaudeAccount(nickname="Marco", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
        ana = ClaudeAccount(nickname="Ana", color=ProfileColor.ORANGE, email="ana@empresa.com.br",
                            kind=ClaudeAccountKind.LOGIN)
        self.config.claude_accounts = [marco, ana]
        self.set_folder_login(None)
        self.secrets.set_secret(b"sk-ant-oat01-marco", marco.secret_account)
        token_state = dataclasses.replace(self.state, applied_claude_account_id=marco.id)
        item = self.check(self.verifier.run(self.config, token_state), "claude.login")
        self.assertEqual(item.status, CheckStatus.OK)
        self.assertEqual(item.found, "Marco (token no chaveiro do sistema), pasta ~/.claude-empresa")

        self.secrets.delete_item(marco.secret_account)
        item = self.check(self.verifier.run(self.config, token_state), "claude.login")
        self.assertEqual((item.status, item.found), (CheckStatus.ERROR, "Marco: token não está no chaveiro do sistema"))
        self.assertEqual((item.fix.kind, item.fix.target, item.fix.button_title),
                         (FixKind.LOGIN_CLAUDE, marco.id, "Entrar…"))
        self.assertEqual(item.fix.command, "Gerar um novo token para a conta Claude Marco (claude setup-token).")
        before = len(self.mutating_commands())
        self.verifier.fix(item, self.config)
        self.assertEqual(len(self.mutating_commands()), before, "a fix that leads to a page runs nothing")
        self.assertFalse(any(r.arguments[:1] == ["setup-token"] for r in self.runner.requests))

        login_state = dataclasses.replace(self.state, applied_claude_account_id=ana.id)
        item = self.check(self.verifier.run(self.config, login_state), "claude.login")
        self.assertEqual((item.status, item.found), (CheckStatus.ERROR, "Ana: login não está no chaveiro do sistema"))
        self.assertEqual(item.fix.command, "Entrar de novo na conta Claude Ana (claude auth login).")
        self.claude_store.write_vault(uuid_text(ana.id), b"{}")
        item = self.check(self.verifier.run(self.config, login_state), "claude.login")
        self.assertEqual(item.status, CheckStatus.OK)
        self.assertEqual(item.found, "Ana · ana@empresa.com.br (login completo no chaveiro do sistema), pasta ~/.claude-empresa")

    def test_session_variable_missing_is_fixed_in_systemd_and_environment_d(self):
        self.system.session.pop("CLAUDE_CONFIG_DIR")
        item = self.check(self.verify(), "claude.session")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual(item.expected, "~/.claude-empresa")
        self.assertIn("systemd --user: não definido", item.found)
        self.assertIn("systemctl --user set-environment CLAUDE_CONFIG_DIR=", item.fix.command)
        fixed = self.verifier.fix(item)
        self.assertEqual(self.system.claude, self.claude_dir)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))

        (self.home / ".config/environment.d/90-multicred.conf").unlink()
        item = self.check(self.verify(), "claude.session")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertIn("environment.d: sem o arquivo", item.found)
        self.assertTrue(self.verifier.fix(item).is_consistent)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir}",
                      (self.home / ".config/environment.d/90-multicred.conf").read_text())

    def test_environment_d_is_checked_and_fixed_under_xdg_config_home(self):
        """S-28: the check reads the file where the switch writes it."""
        xdg = self.home / "xdg"
        object.__setattr__(self.environment, "xdg_config_home", str(xdg))
        SwitchEngine(self.environment, FakeTools()).switch_profile(self.profile.id, self.config)
        env_d = xdg / "environment.d/90-multicred.conf"
        self.assertTrue(env_d.is_file())
        (self.home / ".config/environment.d/90-multicred.conf").unlink()  # the old place is not used
        self.assertEqual(self.check(self.verify(), "claude.session").status, CheckStatus.OK)

        env_d.unlink()
        item = self.check(self.verify(), "claude.session")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertIn("environment.d: sem o arquivo", item.found)
        self.assertTrue(self.verifier.fix(item).is_consistent)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir}", env_d.read_text())

    def test_a_folder_environment_d_cannot_hold_is_a_problem_without_automatic_fix(self):
        """S-19: offering "Corrigir" would never help: systemd drops the variable at the
        next login whatever MultiCred does. The user must rename the folder."""
        folder = f"{self.home}/.claude-$cliente"
        os.makedirs(folder)
        self.profile.claude_config_dir = folder
        SwitchEngine(self.environment, FakeTools()).switch_profile(self.profile.id, self.config)
        report = self.verify()
        item = self.check(report, "claude.session")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertIsNone(item.fix)
        self.assertIn("Renomeie a pasta", item.hint)
        self.assertIn("environment.d", item.hint)
        self.assertNotIn("claude.session", [c.id for c in report.fixable])

    def test_stale_env_file_is_fixed_by_reapplying(self):
        text = self.paths.env_sh.read_text().replace(f"generation={self.state.generation}", "generation=0")
        self.paths.env_sh.write_text(text)
        item = self.check(self.verify(), "claude.shells")
        self.assertEqual((item.status, item.section), (CheckStatus.ERROR, CheckSection.INTEGRATION))
        self.assertIn("geração 0", item.found)
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertEqual(fixed.state.generation, self.state.generation + 1)

    def test_legacy_line_is_removed(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text(bashrc.read_text() + LEGACY_LINE)
        item = self.check(self.verify(), "legacy")
        self.assertEqual(item.status, CheckStatus.ERROR)
        self.assertEqual(item.title, "Versão antiga (~/Perfis) ainda ativa")
        self.assertTrue(item.origin.startswith("~/.bashrc:"))
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertNotIn("legacy", [c.id for c in fixed.checks])
        self.assertNotIn("Perfis", bashrc.read_text())
        self.assertIn("export KEEP=yes", bashrc.read_text())

    def test_fix_all_leaves_everything_ok(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[user]\n\temail = velho@exemplo.com\n")
        (self.home / ".bashrc").write_text("export KEEP=yes\n" + LEGACY_LINE)
        self.system.gh_active = "dev-pessoal"
        self.system.session.pop("CLAUDE_CONFIG_DIR")
        broken = self.verify()
        self.assertEqual({c.id for c in broken.problems}, {"git", "gh", "claude.session", "shell.bash", "legacy"})
        self.assertEqual(len(broken.fixable), 5)

        fixed = self.verifier.fix_all(broken)
        self.assertEqual(fixed.errors, [])
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertEqual((fixed.ok_count, fixed.total), (8, 8))
        self.assertEqual(self.mutating_commands().count("auth switch --hostname github.com --user dev-empresa"), 2,
                         "one from the switch in setUp, one from the fix")

    def test_fix_all_reports_failures_and_keeps_going(self):
        self.system.gh_active = "dev-pessoal"
        self.system.fail_gh_switch_to = "dev-empresa"
        (self.home / ".bashrc").write_text("export KEEP=yes\n")
        fixed = self.verifier.fix_all(self.verify())
        self.assertEqual(len(fixed.errors), 1)
        self.assertIn("keyring locked", fixed.errors[0])
        self.assertEqual({c.id for c in fixed.problems}, {"gh"})

    def test_a_fix_never_touches_anything_when_its_check_is_ok(self):
        self.system.gh_active = "dev-pessoal"
        stale = self.check(self.verify(), "gh")
        self.system.gh_active = "dev-empresa"  # fixed elsewhere meanwhile
        before = len(self.mutating_commands())
        report = self.verifier.fix(stale)
        self.assertEqual(len(self.mutating_commands()), before, "the stale fix was not run")
        self.assertTrue(report.is_consistent)

        files = [self.home / ".bashrc", self.home / ".gitconfig", self.paths.env_sh, self.paths.state,
                 self.home / ".config/environment.d/90-multicred.conf"]
        snapshot = [f.read_bytes() for f in files]
        for item in report.checks:
            self.verifier.fix(item)
        self.verifier.fix_all(report)
        self.assertEqual([f.read_bytes() for f in files], snapshot)
        self.assertEqual(len(self.mutating_commands()), before)

    def test_default_run_never_asks_gh_for_token_health(self):
        self.system.gh_logins = [("dev-empresa", "failed"), ("dev-pessoal", "success")]
        report = self.verify()
        self.assertFalse(self.asked_gh_token_health())
        self.assertEqual(self.check(report, "gh").status, CheckStatus.OK)
        self.assertNotIn("gh.token", [c.id for c in report.checks])
        self.verifier.fix_all(report)
        self.assertFalse(self.asked_gh_token_health())

    def test_deep_run_checks_the_gh_token_as_a_warning_whose_fix_is_the_github_login(self):
        """Verifier.swift:102-111: its own row, a warning (not a problem), "Entrar…"."""
        self.system.gh_logins = [("dev-empresa", "failed"), ("dev-pessoal", "success")]
        report = self.verify(deep=True)
        self.assertTrue(self.asked_gh_token_health())
        self.assertEqual(self.check(report, "gh").status, CheckStatus.OK)
        item = self.check(report, "gh.token")
        self.assertEqual((item.title, item.status, item.found),
                         ("GitHub CLI · login de dev-empresa", CheckStatus.WARNING, "o gh precisa de um novo login"))
        self.assertEqual((item.fix.kind, item.fix.target, item.fix.button_title),
                         (FixKind.LOGIN_GITHUB, "github.com", "Entrar…"))
        self.assertEqual(item.fix.command, "gh auth login --hostname github.com --git-protocol https --web")
        self.assertTrue(report.is_consistent, "a warning keeps the sidebar dot green, as on the Mac")
        self.assertEqual((report.ok_count, report.total), (8, 9))
        self.assertEqual(report.fixable, [])

        self.system.gh_logins = [("dev-pessoal", "success")]
        self.assertEqual(self.check(self.verify(deep=True), "gh.token").found, "conta não está no gh")
        self.system.gh_logins = [("dev-empresa", "success")]
        token = self.check(self.verify(deep=True), "gh.token")
        self.assertEqual((token.status, token.found, token.fix), (CheckStatus.OK, "token válido", None))

    def test_without_active_profile_only_the_installation_is_checked(self):
        report = self.verifier.run(self.config, ActiveState())
        self.assertIsNone(report.profile_name)
        self.assertEqual([c.id for c in report.system], ["git", "shell.bash"])
        self.assertTrue(report.is_consistent, self.problems(report))

    def test_sessions_are_listed_only_when_open_with_the_mac_text(self):
        """Verifier.swift:218-228."""
        none = Verifier(self.environment, FakeTools(), FixedProcessScanner(0)).run(self.config, self.state)
        self.assertEqual(none.informative, [])
        one = Verifier(self.environment, FakeTools(), FixedProcessScanner(1)).run(self.config, self.state)
        self.assertEqual(one.informative[0].title, "1 sessão do Claude Code aberta")
        self.assertEqual(one.informative[0].found, "Sessões abertas antes da última troca continuam na conta em que "
                                                   "foram abertas até serem reiniciadas.")

    def test_platform_checks_can_be_added_to_the_list(self):
        """Termux (S-12) adds its own checks: producers are a plain, ordered list."""
        extra = Check("termux.example", "Exemplo", CheckStatus.OK, section=CheckSection.INTEGRATION)
        self.verifier.producers.append(lambda context: [extra])
        report = self.verify()
        self.assertIn(extra, report.in_section(CheckSection.INTEGRATION))
