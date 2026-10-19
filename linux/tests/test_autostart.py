"""S-09: "Abrir ao iniciar a sessão" = an XDG autostart file (the Linux SMAppService).

Offered only when the app runs from the installed package (/usr/bin/multicred,
the D36 analogue): a dev checkout must not leave an autostart entry pointing
at a command that does not exist. Always in a temporary HOME.
"""

import configparser
from dataclasses import replace

from multicred.core.install.autostart import AUTOSTART_NAME, Autostart, AutostartUnavailable
from multicred.core.models import Config, GitIdentity, Profile, ProfileColor
from multicred.core.storage import ConfigRepository
from tests.support import SafeTestCase
from tests.test_engine import FakeTools


class AutostartTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.command = self.make_executable("usr/bin/multicred")
        self.autostart = Autostart(self.environment, command_path=str(self.command))

    def test_file_lives_in_the_users_autostart_folder(self):
        self.assertEqual(AUTOSTART_NAME, "app.multicred.MultiCred.desktop")
        self.assertEqual(self.autostart.file, self.home / ".config/autostart/app.multicred.MultiCred.desktop")

    def test_follows_xdg_config_home(self):
        environment = replace(self.environment, xdg_config_home=str(self.home / "xdg"))
        self.assertEqual(Autostart(environment).file, self.home / "xdg/autostart/app.multicred.MultiCred.desktop")

    def test_enable_writes_a_desktop_entry_that_starts_in_the_background(self):
        self.assertFalse(self.autostart.is_enabled())
        self.autostart.enable()
        self.assertTrue(self.autostart.is_enabled())
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(self.autostart.file.read_text())
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "MultiCred")
        self.assertEqual(entry["Exec"], "multicred --background")
        self.assertEqual(entry["X-GNOME-Autostart-enabled"], "true")
        self.assertEqual(entry["Terminal"], "false")
        self.assertEqual(self.mode_of(self.autostart.file), 0o644)

    def test_enable_twice_changes_nothing(self):
        self.autostart.enable()
        first = self.autostart.file.read_bytes()
        self.autostart.enable()
        self.assertEqual(self.autostart.file.read_bytes(), first)

    def test_disable_removes_the_file_and_is_harmless_when_missing(self):
        self.autostart.enable()
        self.autostart.disable()
        self.assertFalse(self.autostart.file.exists())
        self.assertFalse(self.autostart.is_enabled())
        self.autostart.disable()

    def test_an_entry_disabled_by_the_desktop_reads_as_off(self):
        self.autostart.enable()
        text = self.autostart.file.read_text().replace("X-GNOME-Autostart-enabled=true",
                                                       "X-GNOME-Autostart-enabled=false")
        self.autostart.file.write_text(text)
        self.assertFalse(self.autostart.is_enabled())
        self.autostart.file.write_text(self.autostart.file.read_text().replace("=false", "=true") + "Hidden=true\n")
        self.assertFalse(self.autostart.is_enabled())

    def test_available_only_with_the_installed_command(self):
        self.assertTrue(self.autostart.is_available())
        missing = Autostart(self.environment, command_path=str(self.home / "nowhere/multicred"))
        self.assertFalse(missing.is_available())
        with self.assertRaises(AutostartUnavailable):
            missing.enable()
        self.assertFalse(missing.file.exists())
        self.assertEqual(Autostart(self.environment).command_path, "/usr/bin/multicred")

    def test_disable_works_even_when_unavailable(self):
        self.autostart.enable()
        Autostart(self.environment, command_path=str(self.home / "nowhere")).disable()
        self.assertFalse(self.autostart.file.exists())


class AutostartModelTests(SafeTestCase):
    """The settings switch: file and `settings.openAtLogin` move together."""

    def setUp(self):
        super().setUp()
        from multicred.ui.model import AppModel

        git = GitIdentity(nickname="P", name="Dev", email="d@x.dev", color=ProfileColor.BLUE)
        ConfigRepository(self.environment).save(Config(
            git_identities=[git], profiles=[Profile(name="P", color=ProfileColor.BLUE, git_identity_id=git.id)]))
        self.autostart = Autostart(self.environment, command_path=str(self.make_executable("usr/bin/multicred")))
        self.model = AppModel(self.environment, FakeTools(), autostart=self.autostart)
        self.model.load()

    def saved(self):
        return ConfigRepository(self.environment).load().settings.open_at_login

    def test_switch_on_and_off(self):
        self.model.set_open_at_login(False)
        self.assertFalse(self.autostart.file.exists())
        self.assertFalse(self.saved())
        self.model.set_open_at_login(True)
        self.assertTrue(self.autostart.is_enabled())
        self.assertTrue(self.saved())

    def test_unavailable_raises_and_saves_nothing(self):
        from multicred.ui.model import AppModel

        self.model.set_open_at_login(False)
        model = AppModel(self.environment, FakeTools(),
                         autostart=Autostart(self.environment, command_path=str(self.home / "none")))
        model.load()
        with self.assertRaises(AutostartUnavailable):
            model.set_open_at_login(True)
        self.assertFalse(self.saved())
        self.assertFalse(self.autostart.file.exists())
