"""S-01: the minimal window lists the profiles and a row switches the profile.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

HAS_DISPLAY = False
if os.environ.get("MULTICRED_PRIVATE_BUS") == "1":
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, GLib, Gtk
        HAS_DISPLAY = Gtk.init_check()
    except (ImportError, ValueError):
        HAS_DISPLAY = False


class FixedTools:
    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class MainWindowSkeletonTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-ui-"))
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        environment = real_env.make_sandbox(self.sandbox)
        self.config = real_env.seed_sandbox(environment)
        ConfigRepository(environment).save(self.config)
        self.model = AppModel(environment, FixedTools(), dispatch=GLib.idle_add)

    def test_rows_list_profiles_and_activating_one_switches(self):
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(self.model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        seen = {}

        def drive():
            window = app.window
            seen["titles"] = [row.get_title() for row in window.rows]
            window.rows[0].emit("activated")
            GLib.timeout_add(50, wait_for_switch)
            return False

        def wait_for_switch():
            if self.model.switching:
                return True
            seen["active"] = self.model.active_profile.name
            seen["toast_shown"] = app.window.last_toast
            app.quit()
            return False

        app.connect("activate", lambda *_: GLib.idle_add(drive))
        GLib.timeout_add_seconds(15, app.quit)  # never hang the suite
        app.run([])

        self.assertEqual(seen.get("titles"), ["Trabalho", "Pessoal"])
        self.assertEqual(seen.get("active"), "Trabalho")
        self.assertEqual(seen.get("toast_shown"), "Perfil Trabalho ativo")
        gitconfig = (self.sandbox / ".config/multicred/gitconfig").read_text()
        self.assertIn("dev@empresa.example", gitconfig)


if __name__ == "__main__":
    unittest.main()
