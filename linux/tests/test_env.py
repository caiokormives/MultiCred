"""The explicit environment given to child processes."""

from dataclasses import replace

from tests.support import SafeTestCase


class BaseProcessEnvironmentTests(SafeTestCase):
    def test_minimal_environment_without_xdg(self):
        env = self.environment.base_process_environment(["/usr/bin", "/bin"])
        self.assertEqual(set(env), {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG"})
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_session_bus_reaches_children_so_systemctl_user_works(self):
        # Without these, `systemctl --user` fails with "Failed to connect to bus: No medium found".
        env = replace(self.environment, xdg_runtime_dir="/run/user/1000",
                      dbus_session_bus_address="unix:path=/run/user/1000/bus").base_process_environment(["/usr/bin"])
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")

    def test_real_environment_carries_the_session_bus(self):
        from unittest import mock
        from multicred.core import real_env
        with mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/run/user/4242",
                                            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus"}):
            environment = real_env.make_real()  # builds only: nothing is touched
        self.assertEqual(environment.xdg_runtime_dir, "/run/user/4242")
        self.assertEqual(environment.dbus_session_bus_address, "unix:path=/run/user/4242/bus")

    def test_xdg_config_home_reaches_children_so_git_gh_and_claude_read_the_same_config(self):
        custom = f"{self.home}/dotfiles/config"
        env = replace(self.environment, xdg_config_home=custom).base_process_environment(["/usr/bin"])
        self.assertEqual(env["XDG_CONFIG_HOME"], custom)
