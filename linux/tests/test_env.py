"""The explicit environment given to child processes."""

from dataclasses import replace

from tests.support import SafeTestCase


class BaseProcessEnvironmentTests(SafeTestCase):
    def test_minimal_environment_without_xdg(self):
        env = self.environment.base_process_environment(["/usr/bin", "/bin"])
        self.assertEqual(set(env), {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG"})
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_xdg_config_home_reaches_children_so_git_gh_and_claude_read_the_same_config(self):
        custom = f"{self.home}/dotfiles/config"
        env = replace(self.environment, xdg_config_home=custom).base_process_environment(["/usr/bin"])
        self.assertEqual(env["XDG_CONFIG_HOME"], custom)
