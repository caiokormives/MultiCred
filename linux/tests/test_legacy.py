"""S-05: the shell-script version in ~/Perfis is detected, removed on request and imported without running it."""

import tempfile
from pathlib import Path

from multicred.core.install.legacy import LegacyMigration, LegacyProfile
from tests.support import SafeTestCase

LEGACY_ZSHRC = ('export PATH="$HOME/.local/bin:$PATH"\n\n# Perfis trabalho/pessoal\n'
                '[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"\n')
LEGACY_GITCONFIG = "[core]\n\teditor = vim\n\n[include]\n\tpath = ~/Perfis/git-atual.gitconfig\n"


class LegacyMigrationTests(SafeTestCase):
    def test_removes_only_the_legacy_lines(self):
        self.assertEqual(LegacyMigration.remove_legacy_lines(LEGACY_ZSHRC), 'export PATH="$HOME/.local/bin:$PATH"\n')
        self.assertEqual(LegacyMigration.remove_legacy_lines(LEGACY_GITCONFIG), "[core]\n\teditor = vim\n")
        untouched = "export A=1\n# Perfis is a word\n"
        self.assertEqual(LegacyMigration.remove_legacy_lines(untouched), untouched)

    def test_keeps_include_sections_with_other_paths(self):
        text = "[include]\n\tpath = ~/Perfis/git-atual.gitconfig\n\tpath = ~/.gitconfig.local\n"
        self.assertEqual(LegacyMigration.remove_legacy_lines(text), "[include]\n\tpath = ~/.gitconfig.local\n")

    def test_findings_in_bashrc_and_gitconfig(self):
        (self.home / ".bashrc").write_text(LEGACY_ZSHRC)
        (self.home / ".gitconfig").write_text(LEGACY_GITCONFIG)
        findings = LegacyMigration.findings(self.environment)
        self.assertEqual(sorted(f.file.name for f in findings), [".bashrc", ".gitconfig"])
        bashrc = next(f for f in findings if f.file.name == ".bashrc")
        self.assertEqual(bashrc.lines, ['[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"'])

    def test_import_of_perfis_conf_never_executes_it(self):
        (self.home / ".zshrc").write_text(LEGACY_ZSHRC)
        (self.home / ".gitconfig").write_text(LEGACY_GITCONFIG)
        folder = LegacyMigration.folder(self.environment)
        folder.mkdir()
        canary = Path(tempfile.gettempdir()) / f"multicred-never-{self.home.name}"
        self.addCleanup(lambda: canary.unlink(missing_ok=True))
        (folder / "perfis.conf").write_text(
            "# comment\n"
            'TRABALHO_GIT_NOME="NPU-Rafael"\n'
            'TRABALHO_GIT_EMAIL="r@npu.com.br"\n'
            'TRABALHO_GH_USUARIO="NPU-Rafael"\n'
            'TRABALHO_SSH_CHAVE=""          # opcional\n'
            "PESSOAL_GIT_NOME='Rafael'\n"
            'PESSOAL_GIT_EMAIL="r@gmail.com"\n'
            "PESSOAL_GH_USUARIO=Rafael-L   # sem aspas\n"
            'PESSOAL_SSH_CHAVE="~/.ssh/id_pessoal"\n'
            'PESSOAL_CLAUDE_DIR="$HOME/.claude-pessoal"\n'
            f"EVIL=$(touch {canary})\n"
            f'TRABALHO_CLAUDE_DIR="`touch {canary}`"\n'
        )
        (folder / "atual").write_text("pessoal\n")

        self.assertEqual(sorted(f.file.name for f in LegacyMigration.findings(self.environment)), [".gitconfig", ".zshrc"])
        profiles = LegacyMigration.import_profiles(self.environment)
        self.assertEqual([p.key for p in profiles], ["TRABALHO", "PESSOAL"])
        self.assertEqual(profiles[0].display_name, "Trabalho")
        self.assertEqual(profiles[0].git_name, "NPU-Rafael")
        self.assertIsNone(profiles[0].ssh_key)
        self.assertIsNone(profiles[0].claude_dir, "a backquoted value is not a path: ignored")
        self.assertEqual(profiles[1].git_name, "Rafael")
        self.assertEqual(profiles[1].github_user, "Rafael-L")
        self.assertEqual(profiles[1].ssh_key, f"{self.home}/.ssh/id_pessoal")
        self.assertEqual(profiles[1].claude_dir, f"{self.home}/.claude-pessoal")
        self.assertEqual(LegacyMigration.active_profile_key(self.environment), "PESSOAL")
        self.assertFalse(canary.exists(), "perfis.conf must never be executed")

    def test_default_claude_folder_means_no_claude_dir(self):
        folder = LegacyMigration.folder(self.environment)
        folder.mkdir()
        (folder / "perfis.conf").write_text('A_GIT_EMAIL="a@x.com"\nA_CLAUDE_DIR="${HOME}/.claude/"\n')
        self.assertEqual(LegacyMigration.import_profiles(self.environment),
                         [LegacyProfile(key="A", git_email="a@x.com")])

    def test_nothing_legacy(self):
        self.assertEqual(LegacyMigration.findings(self.environment), [])
        self.assertEqual(LegacyMigration.import_profiles(self.environment), [])
        self.assertIsNone(LegacyMigration.active_profile_key(self.environment))

    def test_parse_conf_ignores_anything_that_is_not_an_assignment(self):
        parsed = LegacyMigration.parse_conf('export X="1"\nlower="2"\nGOOD="3"\nUNTERMINATED="4\n  SPACED = 5\n')
        self.assertEqual(parsed, [("GOOD", "3")])
