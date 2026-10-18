"""S-08: Verificar e Corrigir (port of VerifierTests in Phase4CoreTests.swift, Linux checks).

States are real files in a temporary HOME: the installer and the switch engine
write them, git is the real git (GIT_CONFIG_NOSYSTEM, temporary HOME), gh and
`systemctl --user` are the stateful fakes of tests/test_engine.FakeSystem.
"""

import subprocess

from multicred.core.adapters.process_scanner import FixedProcessScanner
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.engine.verifier import CheckStatus, Verifier
from multicred.core.install.installer import Installer, InstallOptions
from multicred.core.models import (
    ActiveState, APIKeyAccount, APIProvider, Config, GitHubAccount, GitIdentity, Profile, ProfileColor, Settings, ShellKind,
)
from multicred.core.paths import AppPaths
from multicred.core.runner import CommandResult
from multicred.core.storage import StateStore
from tests.support import SafeTestCase
from tests.test_engine import FakeSystem, FakeTools

GIT = "/usr/bin/git"
LEGACY_LINE = '[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"\n'
SYSTEM_IDS = ["git", "gh", "claude.shells", "claude.session", "shell.bash", "keys", "legacy"]


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
    """A machine where MultiCred was installed for bash and the profile Trabalho activated."""

    def setUp(self):
        super().setUp()
        self.system = FakeSystem(self.runner)
        self.system.gh_active = "dev-pessoal"  # the switch must really move it
        self.runner.on("git", respond=real_git(self.home))
        self.paths = AppPaths(self.environment)
        self.claude_dir = f"{self.home}/.claude-empresa"
        (self.home / ".claude-empresa").mkdir()
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


class VerifierTests(VerifierTestCase):
    def test_consistent_system_is_seven_of_seven(self):
        report = self.verify()
        self.assertEqual(self.problems(report), {})
        self.assertEqual([c.id for c in report.system], SYSTEM_IDS)
        self.assertEqual((report.ok_count, report.total), (7, 7))
        self.assertTrue(report.is_consistent)
        self.assertEqual(report.profile_name, "Trabalho")
        self.assertTrue(all(c.fix is None for c in report.checks), "nothing to fix on a consistent system")
        self.assertEqual(self.check(report, "gh").found, "dev-empresa em github.com")
        self.assertEqual(self.check(report, "claude.shells").found, "CLAUDE_CONFIG_DIR=~/.claude-empresa")
        self.assertEqual(self.check(report, "shell.bash").title, "bash integrado")
        self.assertEqual(self.check(report, "shell.bash").found, "~/.bashrc")
        self.assertEqual(self.check(report, "keys").found, "OPENAI_API_KEY, GEMINI_API_KEY")
        self.assertEqual(self.check(report, "legacy").title, "Sem versão antiga (~/Perfis)")
        info = report.informative
        self.assertEqual([(c.id, c.status, c.title) for c in info],
                         [("claude.sessions", CheckStatus.INFO, "2 sessões do Claude Code abertas")])

    def test_email_overridden_after_the_include_points_at_file_and_line_and_fix_moves_the_include(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[user]\n\temail = velho@exemplo.com\n")
        line = gitconfig.read_text().split("\n").index("\temail = velho@exemplo.com") + 1
        report = self.verify()
        item = self.check(report, "git")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertEqual(item.title, "E-mail do git sobrescrito")
        self.assertEqual((item.expected, item.found), ("dev@empresa.com.br", "velho@exemplo.com"))
        self.assertEqual(item.origin, f"~/.gitconfig:{line}")
        self.assertIn("depois do include do MultiCred", item.hint)
        self.assertIn("~/.gitconfig", item.fix.command)
        self.assertEqual((report.ok_count, report.total), (6, 7))

        fixed = self.verifier.fix(item)
        self.assertEqual(self.check(fixed, "git").status, CheckStatus.OK, self.problems(fixed))
        text = gitconfig.read_text()
        self.assertIn("\temail = velho@exemplo.com\n", text, "the user's own line is kept")
        self.assertTrue(text.endswith('[include]\n\tpath = "~/.config/multicred/gitconfig"\n# <<< MultiCred <<<\n'))
        self.assertTrue(fixed.is_consistent, self.problems(fixed))

    def test_stale_included_file_is_fixed_by_reapplying_the_profile(self):
        self.paths.gitconfig.write_text("[user]\n\tname = Dev Trabalho\n\temail = antigo@empresa.com.br\n")
        item = self.check(self.verify(), "git")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertEqual(item.origin, "~/.config/multicred/gitconfig:3")
        self.assertIn("Reaplicar o perfil Trabalho", item.fix.command)
        self.assertTrue(self.verifier.fix(item).is_consistent)

    def test_include_that_is_not_last_is_a_problem_even_without_an_override(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[alias]\n\tco = checkout\n")
        item = self.check(self.verify(), "git")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertEqual(item.title, "Include do MultiCred não é o último")
        self.assertEqual(item.origin, f"~/.gitconfig:{gitconfig.read_text().split(chr(10)).index('[alias]') + 1}")
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertIn("\tco = checkout\n", gitconfig.read_text())

    def test_gh_switched_outside_the_app_is_fixed_with_gh_auth_switch(self):
        self.system.gh_active = "dev-pessoal"
        item = self.check(self.verify(), "gh")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertEqual((item.expected, item.found), ("dev-empresa", "dev-pessoal em github.com"))
        self.assertEqual(item.fix.command, "gh auth switch --hostname github.com --user dev-empresa")
        fixed = self.verifier.fix(item)
        self.assertEqual(self.check(fixed, "gh").status, CheckStatus.OK)
        self.assertEqual(self.system.gh_active, "dev-empresa")
        self.assertTrue(fixed.is_consistent, self.problems(fixed))

    def test_hook_block_removed_from_bashrc_is_reinstalled(self):
        (self.home / ".bashrc").write_text("export KEEP=yes\n")
        item = self.check(self.verify(), "shell.bash")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertIn("~/.bashrc", item.found)
        self.assertIn("~/.bashrc", item.fix.command)
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        bashrc = (self.home / ".bashrc").read_text()
        self.assertTrue(bashrc.startswith("export KEEP=yes\n\n# >>> MultiCred >>>\n"), bashrc)

    def test_missing_hook_file_is_a_problem_too(self):
        self.paths.hook(ShellKind.BASH).unlink()
        item = self.check(self.verify(), "shell.bash")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertIn("hook", item.found)
        self.assertTrue(self.verifier.fix(item).is_consistent)

    def test_missing_key_is_a_problem_without_automatic_fix(self):
        self.secrets.delete_item(self.gemini.secret_account)
        report = self.verify()
        item = self.check(report, "keys")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertIn("GEMINI_API_KEY", item.found)
        self.assertNotIn("OPENAI_API_KEY", item.found)
        self.assertIsNone(item.fix)
        self.assertIn("Edite a conta", item.hint)
        self.assertEqual(report.fixable, [])

    def test_session_variable_missing_is_fixed_in_systemd_and_environment_d(self):
        self.system.session.pop("CLAUDE_CONFIG_DIR")
        item = self.check(self.verify(), "claude.session")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertEqual(item.expected, "~/.claude-empresa")
        self.assertIn("systemd --user: não definido", item.found)
        self.assertIn("systemctl --user set-environment CLAUDE_CONFIG_DIR=", item.fix.command)
        fixed = self.verifier.fix(item)
        self.assertEqual(self.system.claude, self.claude_dir)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))

        (self.home / ".config/environment.d/90-multicred.conf").unlink()
        item = self.check(self.verify(), "claude.session")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertIn("environment.d: sem o arquivo", item.found)
        self.assertTrue(self.verifier.fix(item).is_consistent)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir}",
                      (self.home / ".config/environment.d/90-multicred.conf").read_text())

    def test_stale_env_file_is_fixed_by_reapplying(self):
        text = self.paths.env_sh.read_text().replace(f"generation={self.state.generation}", "generation=0")
        self.paths.env_sh.write_text(text)
        item = self.check(self.verify(), "claude.shells")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertIn("geração 0", item.found)
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
        self.assertEqual(fixed.state.generation, self.state.generation + 1)

    def test_legacy_line_is_removed(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text(bashrc.read_text() + LEGACY_LINE)
        item = self.check(self.verify(), "legacy")
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertEqual(item.title, "Versão antiga (~/Perfis) ainda ativa")
        self.assertTrue(item.origin.startswith("~/.bashrc:"))
        fixed = self.verifier.fix(item)
        self.assertTrue(fixed.is_consistent, self.problems(fixed))
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
        self.assertEqual((fixed.ok_count, fixed.total), (7, 7))
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
        self.assertFalse(any(r.arguments[:2] == ["auth", "status"] for r in self.runner.requests))
        self.assertEqual(self.check(report, "gh").status, CheckStatus.OK)
        self.verifier.fix_all(report)
        self.assertFalse(any(r.arguments[:2] == ["auth", "status"] for r in self.runner.requests))

    def test_deep_run_checks_the_gh_token(self):
        self.system.gh_logins = [("dev-empresa", "failed"), ("dev-pessoal", "success")]
        item = self.check(self.verify(deep=True), "gh")
        self.assertTrue(any(r.arguments[:2] == ["auth", "status"] for r in self.runner.requests))
        self.assertEqual(item.status, CheckStatus.PROBLEM)
        self.assertIn("novo login", item.found)
        self.assertIsNone(item.fix)
        self.system.gh_logins = [("dev-empresa", "success")]
        self.assertEqual(self.check(self.verify(deep=True), "gh").status, CheckStatus.OK)

    def test_without_active_profile_only_the_installation_is_checked(self):
        report = self.verifier.run(self.config, ActiveState())
        self.assertIsNone(report.profile_name)
        self.assertEqual([c.id for c in report.system], ["git", "shell.bash", "legacy"])
        self.assertTrue(report.is_consistent, self.problems(report))

    def test_no_open_session_is_still_informative(self):
        report = Verifier(self.environment, FakeTools(), FixedProcessScanner(0)).run(self.config, self.state)
        self.assertEqual([(c.title, c.status) for c in report.informative],
                         [("Nenhuma sessão do Claude Code aberta", CheckStatus.INFO)])
        one = Verifier(self.environment, FakeTools(), FixedProcessScanner(1)).run(self.config, self.state)
        self.assertEqual(one.informative[0].title, "1 sessão do Claude Code aberta")
