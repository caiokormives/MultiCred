"""S-12: Claude Code on Termux "no que der", with guidance (R-12), and the Verificar
checks that only make sense on a desktop (R-11).

Claude Code stopped running on plain Termux at 2.1.113 (anthropics/claude-code issue
#50270): the npm package became a stub that looks for a platform-native binary and
Android is not one of its platforms, so `claude --version` prints "Error: claude native
binary not installed." and exits. The workarounds are pinning 2.1.112 (the last
JavaScript build) with the auto-updater off, or running Claude Code inside a
proot-distro Ubuntu.

There is no device here: the Termux world is tests.test_termux.TermuxWorld (temporary
HOME and PREFIX, fake tools under $PREFIX/bin) and Claude Code is a fake npm install
under $PREFIX/lib/node_modules, linked from $PREFIX/bin/claude like npm does.
"""

import dataclasses
import json
import os
import shutil
from pathlib import Path

from multicred import cli
from multicred.core.adapters.process_scanner import FixedProcessScanner
from multicred.core.claude import termux_support
from multicred.core.claude.termux_support import ClaudeCodeState, diagnose, guidance_lines, parse_version
from multicred.core.engine.verifier import CheckSection, CheckStatus, Verifier
from multicred.core.models import (
    ActiveState, APIKeyAccount, APIProvider, ClaudeAccount, Config, GitHubAccount, GitIdentity, Profile, ProfileColor,
)
from multicred.core.paths import AppPaths
from multicred.core.platform import Platform
from multicred.core.storage import ConfigRepository, StateStore
from tests.test_cli import _PrefixOnlyRunner, row_details, row_of, run
from tests.test_termux import TermuxWorld

STUB_ERROR = ("Error: claude native binary not installed.\n\n"
              "Either postinstall did not run (--ignore-scripts, some pnpm configs)\n"
              "or the platform-native optional dependency was not downloaded\n"
              "(--omit=optional).\n")
PIN = "DISABLE_AUTOUPDATER=1 npm install -g @anthropic-ai/claude-code@2.1.112"
LOCK = "chmod -R a-w $PREFIX/lib/node_modules/@anthropic-ai/claude-code"
SETTINGS_ENV = '"env": {"DISABLE_AUTOUPDATER": "1"}'
PROOT = "pkg install proot-distro && proot-distro install ubuntu && proot-distro login ubuntu"

# Only shell builtins: the child PATH is $PREFIX/bin, where nothing else lives.
FAKE_CLAUDE = """#!/bin/sh
if [ "$1" = "--version" ]; then
{version_branch}
fi
exit 1
"""


class TermuxClaudeWorld(TermuxWorld):
    def setUp(self):
        super().setUp()
        self.package = self.prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
        self.guard = _PrefixOnlyRunner(self.recording, self.prefix)
        self.termux = dataclasses.replace(self.termux, runner=self.guard)

    def install_claude(self, version, stub=False, output=None, exit_code=0):
        """npm's layout: the package in lib/node_modules, `bin/claude` a relative link to cli.js."""
        self.package.mkdir(parents=True, exist_ok=True)
        (self.package / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-code",
                                                               "version": version}))
        if stub:
            branch = f"  printf '%s' '{STUB_ERROR}' >&2\n  exit 1"
        else:
            text = output if output is not None else f"{version} (Claude Code)"
            branch = f"  printf '%s\\n' '{text}'\n  exit {exit_code}"
        script = self.package / "cli.js"
        script.write_text(FAKE_CLAUDE.format(version_branch=branch))
        script.chmod(0o755)
        link = self.prefix / "bin" / "claude"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to("../lib/node_modules/@anthropic-ai/claude-code/cli.js")

    def disable_autoupdater(self, folder=None):
        directory = Path(folder) if folder else self.home / ".claude"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))

    def diagnose(self, config_dir=None):
        return diagnose(self.termux, self.termux_tools(), config_dir)

    def assert_stayed_in_termux(self):
        self.assertEqual(self.guard.refused, [], [r.executable for r in self.guard.refused])
        self.assert_nothing_forbidden_ran()


class TermuxClaudeTests(TermuxClaudeWorld):
    """The detector: stub, version ≥ 2.1.113, good version, missing, unreadable."""

    def test_stub_is_broken_and_names_the_installed_version(self):
        self.install_claude("2.1.120", stub=True)
        found = self.diagnose()
        self.assertEqual(found.state, ClaudeCodeState.BROKEN_STUB)
        self.assertEqual(found.version, "2.1.120")  # from the package's package.json
        self.assertEqual(found.path, str(self.prefix / "bin" / "claude"))
        self.assertTrue(found.needs_attention)
        self.assertFalse(found.works)
        guidance = "\n".join(guidance_lines(found))
        for expected in (PIN, LOCK, SETTINGS_ENV, "~/.claude/settings.json", "export DISABLE_AUTOUPDATER=1",
                         PROOT, "#50270", "2.1.113"):
            self.assertIn(expected, guidance)
        self.assertIn("2.1.120", termux_support.summary(found))
        self.assertIn("não roda no Android", termux_support.summary(found))
        self.assert_stayed_in_termux()

    def test_version_from_2_1_113_on_is_broken_on_android(self):
        for version in ("2.1.113", "2.1.200", "2.2.0", "3.0.1"):
            self.install_claude(version)
            found = self.diagnose()
            self.assertEqual((found.state, found.version), (ClaudeCodeState.BROKEN_VERSION, version), version)
            self.assertTrue(found.needs_attention)
            self.assertIn(PIN, guidance_lines(found))
        self.assert_stayed_in_termux()

    def test_good_version_with_the_autoupdater_off_works(self):
        self.install_claude("2.1.112")
        self.disable_autoupdater()
        found = self.diagnose()
        self.assertEqual((found.state, found.version), (ClaudeCodeState.WORKING, "2.1.112"))
        self.assertTrue(found.works)
        self.assertIs(found.autoupdater_disabled, True)
        self.assertFalse(found.needs_attention)
        self.assertEqual(guidance_lines(found), [])
        self.assertEqual(termux_support.summary(found), "Claude Code 2.1.112 · atualização automática desligada")
        # Older versions that run on Android count as working too.
        self.install_claude("2.0.77")
        self.assertEqual(self.diagnose().state, ClaudeCodeState.WORKING)

    def test_autoupdater_off_through_the_global_config_counts(self):
        self.install_claude("2.1.112")
        (self.home / ".claude.json").write_text(json.dumps({"autoUpdates": False, "numStartups": 2}))
        self.assertIs(self.diagnose().autoupdater_disabled, True)

    def test_good_version_with_the_autoupdater_on_needs_attention(self):
        """Left on, the updater installs the next version, which does not run on Android."""
        self.install_claude("2.1.112")
        folder = str(self.home / ".claude-celular")
        self.disable_autoupdater()  # ~/.claude only: the profile's folder is another one
        found = self.diagnose(folder)
        self.assertEqual(found.state, ClaudeCodeState.WORKING)
        self.assertIs(found.autoupdater_disabled, False)
        self.assertTrue(found.needs_attention)
        guidance = "\n".join(guidance_lines(found))
        self.assertIn("~/.claude-celular/settings.json", guidance)
        self.assertIn(SETTINGS_ENV, guidance)
        self.assertIn(LOCK, guidance)
        self.assertNotIn(PIN, guidance)  # already on 2.1.112
        self.disable_autoupdater(folder)
        self.assertFalse(self.diagnose(folder).needs_attention)

    def test_missing_claude(self):
        found = self.diagnose()
        self.assertEqual((found.state, found.path, found.version), (ClaudeCodeState.MISSING, None, None))
        self.assertFalse(found.works)
        guidance = "\n".join(guidance_lines(found))
        self.assertIn("pkg install nodejs-lts", guidance)
        self.assertIn(PIN, guidance)
        self.assertIn(PROOT, guidance)

    def test_unreadable_version_is_unknown(self):
        self.install_claude("2.1.112", output="Segmentation fault", exit_code=139)
        found = self.diagnose()
        self.assertEqual(found.state, ClaudeCodeState.UNKNOWN)
        self.assertEqual(found.version, "2.1.112")
        self.assertIn("Segmentation fault", found.output)
        self.assertTrue(found.needs_attention)

    def test_parse_version(self):
        self.assertEqual(parse_version("2.1.112 (Claude Code)"), (2, 1, 112))
        self.assertEqual(parse_version("claude 2.1.113\n"), (2, 1, 113))
        self.assertEqual(parse_version("v10.0.3-beta.1"), (10, 0, 3))
        self.assertIsNone(parse_version("Error: claude native binary not installed."))
        self.assertIsNone(parse_version(""))
        self.assertTrue(termux_support.is_broken_on_android("2.1.113"))
        self.assertFalse(termux_support.is_broken_on_android("2.1.112"))
        self.assertFalse(termux_support.is_broken_on_android("1.0.0"))
        self.assertFalse(termux_support.is_broken_on_android(None))


class TermuxVerifierTests(TermuxClaudeWorld):
    """Verificar on Termux: no graphical-session row, a Claude Code row with the guidance,
    and the texts name the secret files and `pkg`, never the keyring, apt or systemd."""

    def setUp(self):
        super().setUp()
        self.has_git = bool(shutil.which("git"))
        if self.has_git:
            (self.prefix / "bin" / "git").symlink_to(shutil.which("git"))  # stands in for Termux's git
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

    def verify(self, environment=None):
        environment = environment or self.termux
        state = StateStore(AppPaths(environment), environment.clock).load()
        verifier = Verifier(environment, self.termux_tools(environment), FixedProcessScanner(0))
        return verifier.run(self.config, state)

    def check(self, report, check_id):
        return next(c for c in report.checks if c.id == check_id)

    def all_text(self, report):
        parts = []
        for check in report.checks:
            parts += [check.title, check.found or "", check.expected or "", check.hint or "",
                      check.fix.command if check.fix else ""]
        return "\n".join(parts)

    def test_no_graphical_session_row_and_a_claude_code_row(self):
        self.install_claude("2.1.120", stub=True)
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        report = self.verify()
        ids = [c.id for c in report.checks]
        self.assertNotIn("claude.session", ids)
        self.assertIn("claude.code", ids)
        self.assertLess(ids.index("claude.code"), ids.index("claude.login"))  # Ferramentas: git, gh, Claude, keys
        row = self.check(report, "claude.code")
        self.assertEqual((row.title, row.status, row.section), ("Claude Code", CheckStatus.ERROR, CheckSection.TOOLS))
        self.assertIn("2.1.120", row.found)
        self.assertIn("native binary not installed", row.found)
        self.assertIsNone(row.fix)  # fixing Claude Code on Android is out of scope: the commands are shown
        for expected in (PIN, LOCK, SETTINGS_ENV, PROOT):
            self.assertIn(expected, row.hint)
        text = self.all_text(report)
        for absent in ("systemd", "systemctl", "environment.d", "sessão gráfica", "pasta para apps"):
            self.assertNotIn(absent, text)
        self.assert_stayed_in_termux()

    def version_runs(self):
        claude = str(self.prefix / "bin" / "claude")
        return sum(1 for r in self.recording.requests if r.executable == claude and r.arguments == ["--version"])

    def test_a_fix_and_corrigir_tudo_run_claude_version_once(self):
        """S-16 (review r1): `claude --version` takes up to 10 s; fix() and fix_all() re-check
        before and after every repair, so the diagnosis is kept like the folders' statuses."""
        self.install_claude("2.1.112")
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        state = StateStore(AppPaths(self.termux), self.termux.clock).load()
        verifier = Verifier(self.termux, self.termux_tools(), FixedProcessScanner(0))
        report = verifier.run(self.config, state)
        self.assertTrue(report.fixable, [c.id for c in report.problems])  # the shells' block is missing
        before = self.version_runs()
        verifier.fix(report.fixable[0], self.config)
        self.assertEqual(self.version_runs() - before, 1)

        (self.home / ".bashrc").unlink()  # the block is missing again
        report = verifier.run(self.config, state)
        before = self.version_runs()
        after = verifier.fix_all(report, self.config)
        self.assertEqual(self.version_runs() - before, 1)
        self.assertEqual(self.check(after, "claude.code").found, "Claude Code 2.1.112 · atualização automática ligada")
        # Outside a fix every run asks again (a Claude Code just installed shows).
        before = self.version_runs()
        verifier.run(self.config, state)
        verifier.run(self.config, state)
        self.assertEqual(self.version_runs() - before, 2)
        self.assert_stayed_in_termux()

    def auth_status_runs(self):
        claude = str(self.prefix / "bin" / "claude")
        return sum(1 for r in self.recording.requests
                   if r.executable == claude and r.arguments[:2] == ["auth", "status"])

    def test_verificar_corrigir_asks_claude_once_for_the_report_and_once_for_the_repairs(self):
        """S-18 (review r2, achado 3): `verificar --corrigir` = the deep report, then
        "Corrigir tudo" whose last re-check is deep inside the verifier's memo; no extra run
        repeats `claude --version` (up to 10 s) or `claude auth status`."""
        if not self.has_git:
            self.skipTest("git is not installed on this machine")
        self.install_claude("2.1.112")
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        versions, statuses = self.version_runs(), self.auth_status_runs()
        code, out, err = self.cli("verificar", "--corrigir", inp="s\n")
        self.assertIn("Executar? [s/N] ", out)
        self.assertNotIn("Não consegui", err)
        self.assertEqual(self.version_runs() - versions, 2, "the report, then Corrigir tudo")
        self.assertEqual(self.auth_status_runs() - statuses, 2, "the report, then Corrigir tudo")
        self.assert_stayed_in_termux()

    def test_claude_code_row_states(self):
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        report = self.verify()
        missing = self.check(report, "claude.code")
        self.assertEqual((missing.status, missing.found), (CheckStatus.INFO, "Claude Code não está instalado"))
        self.assertIn(PIN, missing.hint)
        self.assertNotIn(missing, report.system)  # informative: never counted

        self.install_claude("2.1.113")
        new = self.check(self.verify(), "claude.code")
        self.assertEqual(new.status, CheckStatus.ERROR)
        self.assertIn("2.1.113", new.found)
        self.assertIn(PIN, new.hint)

        self.install_claude("2.1.112")
        on = self.check(self.verify(), "claude.code")
        self.assertEqual(on.status, CheckStatus.WARNING)
        self.assertEqual(on.found, "Claude Code 2.1.112 · atualização automática ligada")
        self.assertIn(SETTINGS_ENV, on.hint)

        self.disable_autoupdater()
        good = self.check(self.verify(), "claude.code")
        self.assertEqual((good.status, good.found, good.hint),
                         (CheckStatus.OK, "Claude Code 2.1.112 · atualização automática desligada", None))
        self.assert_stayed_in_termux()

    def test_secret_texts_name_the_files_not_the_keyring(self):
        self.install_claude("2.1.112")
        self.disable_autoupdater()
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        self.assertEqual(self.cli("claude", "Marco")[0], 0)
        report = self.verify()
        key_row = next(c for c in report.checks if c.id.startswith("key."))
        self.assertEqual(key_row.found, "O em ~/.config/multicred/secrets")
        self.assertEqual(self.check(report, "claude.login").found,
                         "Marco · marco@example.com (token em ~/.config/multicred/secrets), pasta ~/.claude (padrão)")
        self.assertNotIn("chaveiro", self.all_text(report))

        secrets = self.home / ".config/multicred/secrets"
        (secrets / self.key.secret_account).unlink()
        (secrets / self.token.secret_account).unlink()
        report = self.verify()
        key_row = next(c for c in report.checks if c.id.startswith("key."))
        self.assertEqual(key_row.found, "chave não está em ~/.config/multicred/secrets")
        self.assertEqual(self.check(report, "claude.login").found,
                         "Marco: token não está em ~/.config/multicred/secrets")
        self.assertNotIn("chaveiro", self.all_text(report))
        self.assert_stayed_in_termux()

    def test_unavailable_secret_folder_is_named(self):
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)

        class Unavailable:
            directory = self.home / ".config/multicred/secrets"

            def contains_item(self, account):
                raise OSError("permissão negada")

        report = self.verify(dataclasses.replace(self.termux, secrets=Unavailable()))
        row = next(c for c in report.checks if c.id.startswith("key."))
        self.assertEqual((row.status, row.found),
                         (CheckStatus.WARNING, "pasta de segredos indisponível: permissão negada"))
        self.assertEqual(row.hint, "Confira as permissões de ~/.config/multicred/secrets (pasta 0700, arquivos 0600) "
                                   "e verifique de novo.")
        self.assertNotIn("chaveiro", self.all_text(report))

    def test_missing_tools_point_to_pkg(self):
        for tool in ("git", "gh"):
            path = self.prefix / "bin" / tool
            if path.exists() or path.is_symlink():
                path.unlink()
        verifier = Verifier(self.termux, self.termux_tools(), FixedProcessScanner(0))
        report = verifier.run(self.config, ActiveState(profile_id=self.config.profiles[0].id))
        self.assertEqual(self.check(report, "git").hint, "Instale o git (pkg install git) e verifique de novo.")
        self.assertEqual(self.check(report, "gh").hint, "Instale o GitHub CLI (pkg install gh) e verifique de novo.")
        self.assertNotIn("apt", self.all_text(report))

    def test_reapply_text_does_not_mention_the_graphical_session(self):
        if not self.has_git:
            self.skipTest("git is not installed on this machine")
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        self.paths.env_sh.unlink()
        row = self.check(self.verify(), "claude.shells")
        self.assertEqual(row.status, CheckStatus.ERROR)
        self.assertEqual(row.fix.command, "Reaplicar o perfil Celular: reescreve o gitconfig incluído, a conta do gh "
                                          "e CLAUDE_CONFIG_DIR nos arquivos de ambiente do shell.")
        self.assert_stayed_in_termux()

    def test_cli_uses_the_plain_verifier(self):
        self.assertIs(type(cli.make_verifier(self.termux, self.termux_tools())), Verifier)

    def test_verificar_shows_the_pin_command(self):
        """`multicred verificar` on Termux with the broken npm Claude Code."""
        if not self.has_git:
            self.skipTest("git is not installed on this machine")
        self.install_claude("2.1.120", stub=True)
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        code, out, err = self.cli("verificar")
        sample = os.environ.get("MULTICRED_VERIFY_SAMPLE")
        if sample:
            Path(sample).write_text(out)
        lines = out.splitlines()
        index = row_of(lines, "✗", "Claude Code")
        self.assertIsNotNone(index, out)
        self.assertIn(PIN, out)
        self.assertIn(PROOT, out)
        self.assertNotIn("chaveiro", out)
        self.assertNotIn("systemd", out)
        self.assertEqual(code, 1)
        self.assertTrue(row_details(lines, index))
        self.assert_stayed_in_termux()


class DesktopUnchangedTests(TermuxClaudeWorld):
    def test_desktop_has_no_claude_code_row(self):
        desktop = dataclasses.replace(self.termux, platform=Platform.desktop())
        self.assertEqual([p.__name__ for p in Verifier(desktop, self.termux_tools(desktop)).producers][2],
                         "_claude_session_checks")
        self.assertNotIn("_claude_code_checks",
                         [p.__name__ for p in Verifier(desktop, self.termux_tools(desktop)).producers])
        self.assertNotIn("_claude_session_checks",
                         [p.__name__ for p in Verifier(self.termux, self.termux_tools()).producers])
