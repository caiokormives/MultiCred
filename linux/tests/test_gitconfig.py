"""The generated git file is read back with the real git, inside the temporary HOME."""

import subprocess

from multicred.core.models import GitIdentity, ProfileColor
from multicred.core.paths import AppPaths
from multicred.core.rendering import gitconfig
from tests.support import SafeTestCase


class RealToolTestCase(SafeTestCase):
    def run_tool(self, *arguments, extra=None):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
        env.update(extra or {})
        return subprocess.run(list(arguments), env=env, cwd=self.home, capture_output=True, text=True, timeout=20)

    def write(self, text, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class GitConfigRendererTests(RealToolTestCase):
    def git_get(self, key, path):
        return self.run_tool("git", "config", "--file", str(path), "--get", key).stdout.rstrip("\n")

    def test_values_survive_git_parsing(self):
        identity = GitIdentity(nickname="x", name='Rafael "Rafa" Leivas \\ ç # not a comment ; neither',
                               email="rafael+tag@npu.com.br", ssh_key_path="~/.ssh/id work's key", color=ProfileColor.INDIGO)
        path = self.home / "gitconfig"
        self.write(gitconfig.render(identity, self.environment), path)
        self.assertEqual(self.git_get("user.name", path), identity.name)
        self.assertEqual(self.git_get("user.email", path), identity.email)
        self.assertEqual(self.git_get("core.sshCommand", path),
                         f"ssh -i '{self.home}/.ssh/id work'\\''s key' -o IdentitiesOnly=yes")

    def test_control_characters_cannot_inject_config(self):
        identity = GitIdentity(nickname="x", name="Evil\n[core]\n\tsshCommand = touch /tmp/pwned", email="e@x.com",
                               color=ProfileColor.RED)
        path = self.home / "gitconfig"
        self.write(gitconfig.render(identity, self.environment), path)
        self.assertEqual(self.run_tool("git", "config", "--file", str(path), "--get", "core.sshCommand").returncode, 1)
        self.assertEqual(self.git_get("user.name", path), "Evil [core]  sshCommand = touch /tmp/pwned")

    def test_empty_identity_renders_only_the_header(self):
        path = self.home / "gitconfig"
        self.write(gitconfig.render(None, self.environment), path)
        self.assertEqual(self.run_tool("git", "config", "--file", str(path), "--list").stdout, "")

    def test_include_at_end_of_global_config_wins(self):
        identity = GitIdentity(nickname="p", name="Pessoal", email="p@example.com", color=ProfileColor.ORANGE)
        paths = AppPaths(self.environment)
        self.write(gitconfig.render(identity, self.environment), paths.gitconfig)
        self.write('[user]\n\tname = Old Name\n\temail = old@example.com\n'
                   '[include]\n\tpath = "~/.config/multicred/gitconfig"\n', self.home / ".gitconfig")
        self.assertEqual(self.run_tool("git", "config", "--get", "user.email").stdout, "p@example.com\n")
        self.assertEqual(self.run_tool("git", "config", "--global", "--get", "user.email").stdout, "old@example.com\n")
        self.assertEqual(self.run_tool("git", "config", "--global", "--includes", "--get", "user.email").stdout,
                         "p@example.com\n")

    def test_user_setting_after_the_include_overrides_it(self):
        identity = GitIdentity(nickname="p", name="Pessoal", email="p@example.com", color=ProfileColor.ORANGE)
        paths = AppPaths(self.environment)
        self.write(gitconfig.render(identity, self.environment), paths.gitconfig)
        self.write('[include]\n\tpath = "~/.config/multicred/gitconfig"\n[user]\n\temail = later@example.com\n',
                   self.home / ".gitconfig")
        result = self.run_tool("git", "config", "--show-origin", "--get", "user.email").stdout
        self.assertTrue(result.endswith("\tlater@example.com\n"), result)
        self.assertTrue(result.startswith(f"file:{self.home}/.gitconfig"), result)
