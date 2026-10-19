"""S-05: the managed block and the transactional installer (port of InstallTests.swift, Linux files)."""

import dataclasses
import json
import os
import subprocess
import unittest
from pathlib import Path

from multicred.core.install import managed_block
from multicred.core.install.installer import (
    FileChange, InstallError, Installer, InstallOptions, Integrations, MarkerMismatch,
)
from multicred.core.install.legacy import LegacyMigration
from multicred.core.models import GitIdentity, ProfileColor, ShellKind
from multicred.core.paths import AppPaths
from multicred.core.rendering import gitconfig, hooks
from tests.support import SafeTestCase

Z, B, F = ShellKind.ZSH, ShellKind.BASH, ShellKind.FISH

LEGACY_ZSHRC = ('export PATH="$HOME/.local/bin:$PATH"\n\n# Perfis trabalho/pessoal\n'
                '[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"\n')
LEGACY_GITCONFIG = "[core]\n\teditor = vim\n\n[include]\n\tpath = ~/Perfis/git-atual.gitconfig\n"


class ManagedBlockTests(unittest.TestCase):
    def test_append_remove_round_trip(self):
        original = "export A=1\n"
        installed = managed_block.append_at_end("BODY", original)
        self.assertEqual(installed, "export A=1\n\n# >>> MultiCred >>>\nBODY\n# <<< MultiCred <<<\n")
        self.assertEqual(managed_block.append_at_end("BODY", installed), installed, "idempotent")
        self.assertEqual(managed_block.remove(installed), original)
        self.assertTrue(managed_block.contains(installed))
        self.assertEqual(managed_block.body(installed), "BODY")
        self.assertTrue(managed_block.is_last(installed))
        self.assertFalse(managed_block.is_last(installed + "[user]\n\temail = x\n"))

    def test_replaces_an_existing_block_and_moves_it_to_the_end(self):
        text = "a\n\n# >>> MultiCred >>>\nOLD\n# <<< MultiCred <<<\nb\n"
        self.assertEqual(managed_block.append_at_end("NEW", text), "a\nb\n\n# >>> MultiCred >>>\nNEW\n# <<< MultiCred <<<\n")

    def test_empty_file_and_unterminated_block(self):
        self.assertEqual(managed_block.append_at_end("B", ""), "# >>> MultiCred >>>\nB\n# <<< MultiCred <<<\n")
        with self.assertRaises(managed_block.Unterminated):
            managed_block.remove("a\n# >>> MultiCred >>>\nB\n")
        self.assertFalse(managed_block.contains("a\n# not >>> MultiCred >>>\n"))
        self.assertIsNone(managed_block.body("nothing here\n"))

    def test_text_without_trailing_newline(self):
        installed = managed_block.append_at_end("B", "export A=1")
        self.assertEqual(installed, "export A=1\n\n# >>> MultiCred >>>\nB\n# <<< MultiCred <<<\n")
        self.assertEqual(managed_block.remove(installed), "export A=1\n")


class IntegrationsTests(SafeTestCase):
    def test_rc_files_on_linux(self):
        self.assertEqual(Integrations.rc_files(B, self.environment), [self.home / ".bashrc"])
        (self.home / ".bash_profile").write_text("# mine\n")
        self.assertEqual(Integrations.rc_files(B, self.environment), [self.home / ".bashrc", self.home / ".bash_profile"])
        self.assertEqual(Integrations.rc_files(Z, self.environment), [self.home / ".zshrc"])
        self.assertEqual(Integrations.rc_files(F, self.environment), [])
        self.assertEqual(Integrations.fish_conf_file(self.environment), self.home / ".config/fish/conf.d/multicred.fish")

    def test_rc_blocks_source_the_hook_only_when_readable(self):
        self.assertEqual(Integrations.rc_block(B), '[ -r "$HOME/.config/multicred/shell/hook.bash" ] && '
                                                   '. "$HOME/.config/multicred/shell/hook.bash"')
        self.assertEqual(Integrations.rc_block(Z), '[ -r "$HOME/.config/multicred/shell/hook.zsh" ] && '
                                                   'source "$HOME/.config/multicred/shell/hook.zsh"')
        self.assertEqual(Integrations.rc_block(F), 'test -r "$HOME/.config/multicred/shell/hook.fish"; and '
                                                   'source "$HOME/.config/multicred/shell/hook.fish"')
        self.assertEqual(Integrations.GIT_INCLUDE_BLOCK, '[include]\n\tpath = "~/.config/multicred/gitconfig"')

    def test_global_git_config_resolution(self):
        primary, xdg = self.home / ".gitconfig", self.home / ".config/git/config"
        self.assertEqual(Integrations.global_git_config(self.environment), primary, "nothing exists: create ~/.gitconfig")
        xdg.parent.mkdir(parents=True)
        xdg.write_text("[core]\n")
        self.assertEqual(Integrations.global_git_config(self.environment), xdg)
        primary.write_text("[core]\n")
        self.assertEqual(Integrations.global_git_config(self.environment), primary, "~/.gitconfig wins when it exists")

    def test_xdg_config_home_comes_from_the_injected_environment(self):
        custom = self.home / "xdg"
        (custom / "git").mkdir(parents=True)
        (custom / "git/config").write_text("[core]\n")
        environment = dataclasses.replace(self.environment, xdg_config_home=str(custom))
        self.assertEqual(Integrations.global_git_config(environment), custom / "git/config")
        self.assertEqual(Integrations.global_git_config(self.environment), self.home / ".gitconfig",
                         "without the injected variable ~/.config is used, never the app's own environment")


class RealToolTestCase(SafeTestCase):
    def run_tool(self, *arguments, extra=None):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
        env.update(extra or {})
        return subprocess.run(list(arguments), env=env, cwd=self.home, capture_output=True, text=True, timeout=20)

    def write(self, text, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class InstallerTests(RealToolTestCase):
    def setUp(self):
        super().setUp()
        self.installer = Installer(self.environment)
        self.paths = AppPaths(self.environment)
        self.bashrc = self.home / ".bashrc"
        self.zshrc = self.home / ".zshrc"
        self.gitconfig = self.home / ".gitconfig"

    def options(self, *shells, remove_legacy=False):
        return InstallOptions(shells=list(shells), remove_legacy=remove_legacy)

    def snapshot(self, *files):
        return [Path(f).read_bytes() if os.path.lexists(f) else None for f in files]

    def test_fish_and_environment_d_follow_xdg_config_home(self):
        """S-28: fish reads conf.d and systemd reads environment.d under $XDG_CONFIG_HOME;
        MultiCred's own folder stays ~/.config/multicred (the hooks name it)."""
        from multicred.core.adapters.session_env import environment_d_file
        from multicred.core.install.autostart import Autostart
        xdg = self.home / "xdg"
        object.__setattr__(self.environment, "xdg_config_home", str(xdg))
        self.assertEqual(self.environment.config_home, xdg)
        self.assertEqual(Integrations.fish_conf_file(self.environment), xdg / "fish/conf.d/multicred.fish")
        self.assertEqual(environment_d_file(self.environment), xdg / "environment.d/90-multicred.conf")
        self.assertEqual(Autostart(self.environment).file.parent, xdg / "autostart")
        self.assertEqual(self.paths.root, self.home / ".config/multicred")

        Installer(self.environment).apply(self.options(F))
        self.assertTrue((xdg / "fish/conf.d/multicred.fish").is_file())
        self.assertFalse((self.home / ".config/fish").exists())

        object.__setattr__(self.environment, "xdg_config_home", None)
        self.assertEqual(self.environment.config_home, self.home / ".config")
        self.assertEqual(Integrations.fish_conf_file(self.environment), self.home / ".config/fish/conf.d/multicred.fish")
        self.assertEqual(environment_d_file(self.environment), self.home / ".config/environment.d/90-multicred.conf")

    def test_fresh_install_writes_blocks_backups_and_manifest(self):
        self.write("export A=1\n", self.bashrc)
        preview = self.installer.preview(self.options(B))
        self.assertEqual([c.path for c in preview], ["~/.bashrc", "~/.gitconfig"])
        self.assertEqual([c.kind for c in preview], ["modify", "create"])
        self.assertIn(Integrations.rc_block(B), preview[0].added_lines)

        manifest = self.installer.apply(self.options(B))

        self.assertTrue(self.read(self.bashrc).startswith("export A=1\n\n# >>> MultiCred >>>\n"))
        self.assertTrue(managed_block.is_last(self.read(self.gitconfig)))
        self.assertEqual(self.read(self.paths.hook(B)), hooks.render(B))
        self.assertFalse(self.paths.hook(Z).exists())
        self.assertFalse((self.home / ".bash_profile").exists(), "never created on Linux")
        self.assertEqual(self.read(self.paths.gitconfig), gitconfig.HEADER)

        bash_entry = next(f for f in manifest.files if f.path == str(self.bashrc))
        self.assertFalse(bash_entry.created_by_multicred)
        self.assertEqual(self.read(bash_entry.backup), "export A=1\n")
        self.assertTrue(bash_entry.backup.startswith(str(self.paths.backups) + "/"))
        self.assertEqual(next(f for f in manifest.files if f.path == str(self.gitconfig)).created_by_multicred, True)
        self.assertEqual(self.installer.manifest().shells, [B])

    def test_manifest_has_the_mac_shape(self):
        self.installer.apply(self.options(B))
        data = json.loads(self.paths.manifest.read_text())
        self.assertEqual(set(data), {"version", "installedAt", "shells", "files"})
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["shells"], ["bash"])
        self.assertTrue(data["installedAt"].endswith("Z"))
        self.assertEqual(data["files"][0], {"path": str(self.bashrc), "createdByMultiCred": True})
        self.assertEqual(self.mode_of(self.paths.manifest), 0o600)
        self.assertEqual(self.mode_of(self.paths.root), 0o700)

    def test_install_twice_is_byte_for_byte_identical(self):
        self.write("export A=1\n", self.zshrc)
        self.write("# mine\n", self.home / ".bash_profile")
        self.write("[core]\n\teditor = vim\n", self.gitconfig)
        files = [self.zshrc, self.bashrc, self.home / ".bash_profile", self.gitconfig,
                 Integrations.fish_conf_file(self.environment), self.paths.hook(Z), self.paths.hook(B),
                 self.paths.hook(F), self.paths.gitconfig]
        self.installer.apply(self.options(Z, B, F))
        first = self.snapshot(*files)
        backups = sorted(p for p in self.paths.backups.rglob("*") if p.is_file())
        self.assertEqual(self.installer.preview(self.options(Z, B, F)), [])
        self.installer.apply(self.options(Z, B, F))
        self.assertEqual(self.snapshot(*files), first)
        self.assertEqual(sorted(p for p in self.paths.backups.rglob("*") if p.is_file()), backups, "no new backups")

    def test_backup_happens_once_before_the_first_change(self):
        self.write("export A=1\n", self.bashrc)
        manifest = self.installer.apply(self.options(B))
        backup = next(f for f in manifest.files if f.path == str(self.bashrc)).backup
        # A later change (zsh added, bash removed) must not overwrite the original copy.
        self.write(self.read(self.bashrc) + "alias ll='ls -la'\n", self.bashrc)
        manifest = self.installer.apply(self.options(Z))
        entries = [f for f in manifest.files if f.path == str(self.bashrc)]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].backup, backup)
        self.assertEqual(self.read(backup), "export A=1\n")
        self.assertEqual(self.read(self.bashrc), "export A=1\nalias ll='ls -la'\n")

    def test_rc_without_trailing_newline(self):
        self.write("export A=1", self.bashrc)
        self.installer.apply(self.options(B))
        self.assertEqual(self.read(self.bashrc),
                         "export A=1\n\n# >>> MultiCred >>>\n" + Integrations.rc_block(B) + "\n# <<< MultiCred <<<\n")

    def test_bash_profile_only_when_it_already_exists(self):
        self.installer.apply(self.options(B))
        self.assertFalse((self.home / ".bash_profile").exists())
        self.write("[ -f ~/.bashrc ] && . ~/.bashrc\n", self.home / ".bash_profile")
        preview = self.installer.preview(self.options(B))
        self.assertEqual([c.path for c in preview], ["~/.bash_profile"])
        self.installer.apply(self.options(B))
        self.assertTrue(managed_block.contains(self.read(self.home / ".bash_profile")))
        self.installer.apply(self.options(Z))
        self.assertEqual(self.read(self.home / ".bash_profile"), "[ -f ~/.bashrc ] && . ~/.bashrc\n")

    def test_fish_gets_a_whole_conf_d_file(self):
        self.installer.apply(self.options(F))
        fish = Integrations.fish_conf_file(self.environment)
        self.assertEqual(self.read(fish), Integrations.fish_conf_contents())
        self.assertIn('source "$HOME/.config/multicred/shell/hook.fish"', self.read(fish))
        self.assertEqual(self.read(self.paths.hook(F)), hooks.render(F))
        self.assertFalse(self.bashrc.exists(), "a shell left out gets no rc file")

    def test_disabling_a_shell_removes_its_block_and_files(self):
        self.write("# mine\n", self.home / ".bash_profile")
        self.installer.apply(self.options(Z, B, F))
        self.assertTrue(managed_block.contains(self.read(self.home / ".bash_profile")))
        self.assertTrue(Integrations.fish_conf_file(self.environment).exists())

        self.installer.apply(self.options(Z))
        self.assertFalse(managed_block.contains(self.read(self.home / ".bash_profile")))
        self.assertFalse(managed_block.contains(self.read(self.bashrc)))
        self.assertFalse(Integrations.fish_conf_file(self.environment).exists())
        self.assertFalse(self.paths.hook(F).exists())
        self.assertFalse(self.paths.hook(B).exists())

    def test_a_fish_file_that_is_not_ours_is_left_alone(self):
        fish = Integrations.fish_conf_file(self.environment)
        self.write("set -g mine 1\n", fish)
        self.installer.apply(self.options(B))
        self.assertEqual(self.read(fish), "set -g mine 1\n")

    def test_include_is_the_last_thing_in_the_global_git_config(self):
        self.write("[include]\n\tpath = ~/.other\n\n# >>> MultiCred >>>\n[include]\n\tpath = old\n# <<< MultiCred <<<\n"
                   "[user]\n\temail = after@example.com\n", self.gitconfig)
        self.installer.apply(self.options(B))
        text = self.read(self.gitconfig)
        self.assertTrue(text.endswith("# >>> MultiCred >>>\n" + Integrations.GIT_INCLUDE_BLOCK + "\n# <<< MultiCred <<<\n"))
        self.assertTrue(text.startswith("[include]\n\tpath = ~/.other\n[user]\n\temail = after@example.com\n"))
        self.assertTrue(managed_block.is_last(text))

    def test_xdg_git_config_gets_the_include(self):
        xdg = self.home / ".config/git/config"
        self.write("[core]\n\teditor = nano\n", xdg)
        preview = self.installer.preview(self.options(B))
        self.assertIn("~/.config/git/config", [c.path for c in preview])
        self.installer.apply(self.options(B))
        self.assertFalse(self.gitconfig.exists())
        self.assertTrue(self.read(xdg).startswith("[core]\n\teditor = nano\n\n# >>> MultiCred >>>\n"))

    def test_installed_include_is_read_by_the_real_git(self):
        self.write("[user]\n\tname = Old\n\temail = old@example.com\n", self.gitconfig)
        self.installer.apply(self.options(B))
        identity = GitIdentity(nickname="P", name="Pessoal", email="p@example.com", color=ProfileColor.ORANGE)
        self.write(gitconfig.render(identity, self.environment), self.paths.gitconfig)
        self.assertEqual(self.run_tool("git", "config", "--get", "user.email").stdout, "p@example.com\n")
        self.assertEqual(self.run_tool("git", "config", "--get", "user.name").stdout, "Pessoal\n")

    def test_symlinked_rc_is_edited_at_its_destination(self):
        real = self.home / "dotfiles/bashrc"
        self.write("export B=2\n", real)
        self.bashrc.symlink_to(real)
        preview = self.installer.preview(self.options(B))
        self.assertIn("~/dotfiles/bashrc", [c.path for c in preview])
        self.installer.apply(self.options(B))
        self.assertTrue(managed_block.contains(self.read(real)))
        self.assertTrue(self.bashrc.is_symlink())
        self.assertEqual(os.readlink(self.bashrc), str(real))

    def test_broken_symlink_is_refused_before_writing_anything(self):
        self.bashrc.symlink_to(self.home / "dotfiles/missing")
        with self.assertRaises(InstallError) as caught:
            self.installer.apply(self.options(B))
        self.assertEqual(caught.exception.path, "~/.bashrc")
        self.assertFalse(self.gitconfig.exists())
        self.assertFalse(self.paths.root.exists())

    def test_hand_edited_marker_stops_before_writing_anything(self):
        self.write("x\n" + managed_block.BEGIN + "\nbroken\n", self.bashrc)
        with self.assertRaises(MarkerMismatch) as caught:
            self.installer.apply(self.options(B))
        self.assertEqual(caught.exception.path, "~/.bashrc")
        self.assertFalse(self.gitconfig.exists())
        self.assertFalse(self.paths.root.exists())

    def test_failure_midway_restores_everything_written(self):
        self.write("export A=1\n", self.bashrc)
        # conf.d is a file, so writing the fish file fails after ~/.bashrc was written.
        self.write("not a directory", self.home / ".config/fish/conf.d")
        with self.assertRaises(InstallError) as caught:
            self.installer.apply(self.options(B, F))
        self.assertEqual(caught.exception.path, "~/.config/fish/conf.d/multicred.fish", "names the file that failed")
        self.assertEqual(self.read(self.bashrc), "export A=1\n")
        self.assertFalse(self.gitconfig.exists())
        self.assertFalse(self.paths.hook(B).exists())
        self.assertFalse(self.paths.gitconfig.exists())
        self.assertFalse(self.paths.manifest.exists())

    def test_preview_lists_added_and_removed_lines(self):
        self.write(LEGACY_ZSHRC, self.zshrc)
        preview = self.installer.preview(self.options(Z, remove_legacy=True))
        zsh = next(c for c in preview if c.path == "~/.zshrc")
        self.assertEqual(zsh.kind, "modify")
        self.assertEqual(zsh.added_lines, [managed_block.BEGIN, Integrations.rc_block(Z), managed_block.END])
        self.assertEqual(zsh.removed_lines, ["# Perfis trabalho/pessoal",
                                             '[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"'])
        self.installer.apply(self.options(Z))
        removal = self.installer.preview(self.options(B))
        zsh = next(c for c in removal if c.path == "~/.zshrc")
        self.assertEqual(zsh.removed_lines, [managed_block.BEGIN, Integrations.rc_block(Z), managed_block.END])
        self.assertEqual(zsh.added_lines, [])
        self.assertIsInstance(zsh, FileChange)

    def test_preview_marks_deleted_files(self):
        self.installer.apply(self.options(B, F))
        changes = self.installer.preview(self.options(B))
        self.assertEqual([(c.path, c.kind) for c in changes], [("~/.config/fish/conf.d/multicred.fish", "delete")])

    def test_legacy_removal_is_optional(self):
        self.write(LEGACY_ZSHRC, self.zshrc)
        self.write(LEGACY_GITCONFIG, self.gitconfig)
        self.installer.apply(self.options(Z))
        self.assertIn("Perfis/claude-env.sh", self.read(self.zshrc))

        preview = self.installer.preview(self.options(Z, remove_legacy=True))
        self.assertTrue(any("Perfis/claude-env.sh" in line for c in preview for line in c.removed_lines))
        self.installer.apply(self.options(Z, remove_legacy=True))
        self.assertNotIn("Perfis/claude-env.sh", self.read(self.zshrc))
        self.assertNotIn("Perfis/git-atual", self.read(self.gitconfig))
        self.assertTrue(self.read(self.gitconfig).startswith("[core]\n\teditor = vim\n"))
        self.assertEqual(LegacyMigration.findings(self.environment), [])

    def test_bytes_outside_the_block_are_kept_even_when_not_utf8(self):
        original = b"export A='\xe9t\xe9'\n"
        self.bashrc.write_bytes(original)
        self.installer.apply(self.options(B))
        self.assertTrue(self.bashrc.read_bytes().startswith(original + b"\n# >>> MultiCred >>>\n"))

    def test_rc_file_mode_is_kept(self):
        self.write("export A=1\n", self.bashrc)
        self.bashrc.chmod(0o600)
        self.installer.apply(self.options(B))
        self.assertEqual(self.mode_of(self.bashrc), 0o600)

    def test_installed_hook_drives_a_real_interactive_bash(self):
        """Install, then an interactive bash reading the installed ~/.bashrc follows the generation file."""
        self.write("export KEEP=yes\n", self.bashrc)
        self.installer.apply(self.options(B))
        self.write("export MULTICRED_TEST_VALUE='from-env'\n", self.paths.env_sh)
        self.write("1\n", self.paths.generation)
        result = self.run_tool("/bin/bash", "-i", "-c", 'printf "%s|%s" "$KEEP" "$MULTICRED_TEST_VALUE"',
                               extra={"TERM": "dumb"})
        self.assertTrue(result.stdout.endswith("yes|from-env"), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
