"""S-05: the 3-step onboarding in the main window, walked programmatically in test mode.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import dataclasses
import json
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


class SandboxTools:
    """No git/gh/claude found; the shells come from the real /usr/bin (read-only check)."""

    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class OnboardingUITests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-onboarding-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = dataclasses.replace(real_env.make_sandbox(self.sandbox), login_shell="/bin/bash")
        (self.sandbox / ".bashrc").write_text("export KEEP=yes\n")

    def make_model(self):
        from multicred.ui.model import AppModel
        return AppModel(self.environment, SandboxTools(), dispatch=GLib.idle_add)

    def run_app(self, model, drive, seconds=20):
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.connect("activate", lambda *_: GLib.idle_add(drive, app))
        GLib.timeout_add_seconds(seconds, app.quit)  # never hang the suite
        app.run([])
        return app

    def test_unconfigured_app_walks_the_three_steps_and_activates_the_profile(self):
        model = self.make_model()
        seen = {}

        def drive(app):
            page = app.window.onboarding
            seen["shown"] = page is not None and app.window.view.get_content() is page
            GLib.timeout_add(50, wait_for_detection, app, page)
            return False

        def wait_for_detection(app, page):
            if page.setup is None:
                return True
            seen["step1"] = page.step
            seen["claude_command"] = page.install_command_labels.get("claude")
            page.next_button.emit("clicked")
            seen["step2"] = page.step
            card = page.profile_cards[0]
            card.name_row.set_text("Trabalho")
            card.git_name_row.set_text("Dev Trabalho")
            card.git_email_row.set_text("dev@empresa.example")
            page.next_button.emit("clicked")
            seen["step3"] = page.step
            seen["checked"] = {shell.value: check.get_active() for shell, check in page.shell_checks.items()}
            seen["sensitive"] = {shell.value: check.get_sensitive() for shell, check in page.shell_checks.items()}
            seen["diff_files"] = [change.path for change in page.preview_changes]
            seen["initial"] = page.initial_profile_label.get_label()
            page.apply_button.emit("clicked")
            GLib.timeout_add(50, wait_for_apply, app)
            return False

        def wait_for_apply(app):
            if app.window.onboarding is not None or model.switching:
                if app.window.onboarding is not None and app.window.onboarding.error_label.get_visible():
                    seen["error"] = app.window.onboarding.error_label.get_label()
                    app.quit()
                    return False
                return True
            seen["main_shown"] = app.window.view.get_content() is app.window.page
            seen["rows"] = [row.get_title() for row in app.window.rows]
            seen["toast"] = app.window.last_toast
            app.quit()
            return False

        self.run_app(model, drive)

        self.assertNotIn("error", seen, seen.get("error"))
        self.assertTrue(seen.get("shown"), "an unconfigured app opens the onboarding")
        self.assertEqual((seen.get("step1"), seen.get("step2"), seen.get("step3")), (0, 1, 2))
        self.assertEqual(seen.get("claude_command"), "curl -fsSL https://claude.ai/install.sh | bash")
        self.assertTrue(seen["checked"]["bash"], "the login shell is pre-checked")
        self.assertTrue(seen["sensitive"]["bash"])
        self.assertIn("~/.bashrc", seen["diff_files"])
        self.assertIn("~/.gitconfig", seen["diff_files"])
        self.assertEqual(seen.get("initial"), "Trabalho")
        self.assertTrue(seen.get("main_shown"), "the normal window content comes back")
        self.assertEqual(seen.get("rows"), ["Trabalho"])
        self.assertEqual(seen.get("toast"), "Perfil Trabalho ativo")

        bashrc = (self.sandbox / ".bashrc").read_text()
        self.assertTrue(bashrc.startswith("export KEEP=yes\n\n# >>> MultiCred >>>\n"), bashrc)
        self.assertIn('. "$HOME/.config/multicred/shell/hook.bash"', bashrc)
        self.assertIn('[include]\n\tpath = "~/.config/multicred/gitconfig"', (self.sandbox / ".gitconfig").read_text())
        config = json.loads((self.sandbox / ".config/multicred/config.json").read_text())
        self.assertEqual([p["name"] for p in config["profiles"]], ["Trabalho"])
        self.assertEqual(config["settings"]["enabledShells"], ["bash"])
        state = json.loads((self.sandbox / ".config/multicred/state.json").read_text())
        self.assertEqual(state["profileId"], config["profiles"][0]["id"])
        self.assertEqual(str(model.state.profile_id).upper(), config["profiles"][0]["id"])
        self.assertIn("dev@empresa.example", (self.sandbox / ".config/multicred/gitconfig").read_text())
        self.assertTrue((self.sandbox / ".config/multicred/shell/hook.bash").exists())

    def test_tray_action_opens_the_onboarding_in_the_main_window(self):
        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository

        ConfigRepository(self.environment).save(real_env.seed_sandbox(self.environment))
        model = self.make_model()
        seen = {}

        def drive(app):
            seen["before"] = app.window.onboarding is None
            app.open_page("onboarding")
            page = app.window.onboarding
            seen["after"] = page is not None and app.window.view.get_content() is page
            seen["windows"] = len(app.get_windows())
            app.quit()
            return False

        self.run_app(model, drive)
        self.assertTrue(seen.get("before"), "a configured app opens on its profiles")
        self.assertTrue(seen.get("after"))
        self.assertEqual(seen.get("windows"), 1, "single window (D31)")


if __name__ == "__main__":
    unittest.main()
