"""S-05/S-07: the 3-step onboarding in the main window, walked programmatically in test mode.

S-07 (R-07) brings it to the Mac's OnboardingView.swift: the cards' status
"Completo" / "Falta N item" (:326, 363-365), step 3's group with the backup
folder, "Perfil inicial" and the "Abrir ao iniciar a sessão" switch (:249-262),
which is applied (the sandbox autostart entry written or removed, the setting
saved, AppModel.swift:536-548), and the final "Aplicar e começar" (:101).

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop. With
MULTICRED_SCREENS_DIR set, S-07-onboarding-{1,2,3}-*.png are rendered from
inside the private display (never the user's screen).
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
    if os.environ.get("MULTICRED_SCREENS_DIR"):
        # Compare against stock Adwaita: keep the desktop's ~/.config/gtk-4.0/gtk.css out.
        import atexit
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="multicred-ui-config-")
        atexit.register(shutil.rmtree, os.environ["XDG_CONFIG_HOME"], True)
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


def save_png(window, path):
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    if node is None:
        return False
    texture = window.get_renderer().render_texture(node, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    return texture.save_to_png(str(path))


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class OnboardingUITests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.install.autostart import Autostart

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-onboarding-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = dataclasses.replace(real_env.make_sandbox(self.sandbox), login_shell="/bin/bash")
        (self.sandbox / ".bashrc").write_text("export KEEP=yes\n")
        # A stand-in for /usr/bin/multicred inside the sandbox: autostart is available.
        command = self.sandbox / "usr/bin/multicred"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\n")
        command.chmod(0o755)
        self.autostart = Autostart(self.environment, command_path=str(command))

    def make_model(self, autostart=None):
        from multicred.ui.model import AppModel
        return AppModel(self.environment, SandboxTools(), dispatch=GLib.idle_add,
                        autostart=autostart or self.autostart)

    def run_app(self, model, drive, seconds=20):
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.connect("activate", lambda *_: GLib.idle_add(drive, app))
        GLib.timeout_add_seconds(seconds, app.quit)  # never hang the suite
        app.run([])
        return app

    def saved_config(self):
        return json.loads((self.sandbox / ".config/multicred/config.json").read_text())

    def walk_to_apply(self, page, name="Trabalho"):
        page.next_button.emit("clicked")
        card = page.profile_cards[0]
        card.name_row.set_text(name)
        card.git_name_row.set_text(f"Dev {name}")
        card.git_email_row.set_text("dev@empresa.example")
        page.next_button.emit("clicked")

    def test_unconfigured_app_walks_the_three_steps_and_activates_the_profile(self):
        model = self.make_model()
        seen = {}
        shots = {}

        def drive(app):
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            app.window.set_default_size(1225, 745)
            page = app.window.onboarding
            seen["shown"] = page is not None and app.window.view.get_content() is page
            GLib.timeout_add(50, wait_for_detection, app, page)
            return False

        def shot(app, name):
            """True while the frame is not saved yet (a few ticks for the transition)."""
            directory = os.environ.get("MULTICRED_SCREENS_DIR")
            if not directory:
                return False
            shots[name] = shots.get(name, 0) + 1
            if shots[name] < 6:
                return True
            return not save_png(app.window, Path(directory) / name)

        def wait_for_detection(app, page):
            if page.setup is None:
                return True
            if shot(app, "S-07-onboarding-1-preparar.png"):
                return True
            seen["step1"] = page.step
            seen["tools_title"] = page.tools_group.get_title()
            seen["claude_command"] = page.install_command_labels.get("claude")
            page.next_button.emit("clicked")
            seen["step2"] = page.step
            card = page.profile_cards[0]
            seen["status_empty"] = card.status_pill.get_label()
            card.name_row.set_text("Trabalho")
            card.git_name_row.set_text("Dev Trabalho")
            card.git_email_row.set_text("dev@empresa.example")
            seen["status_git"] = card.status_pill.get_label()
            seen["remove_tooltip"] = card.remove_button.get_tooltip_text() if card.remove_button else None
            GLib.timeout_add(50, on_profiles, app, page)
            return False

        def on_profiles(app, page):
            if shot(app, "S-07-onboarding-2-perfis.png"):
                return True
            page.next_button.emit("clicked")
            seen["step3"] = page.step
            seen["apply_subtitle"] = page.apply_subtitle.get_label()
            seen["shells_title"] = page.shells_group.get_title()
            seen["checked"] = {shell.value: check.get_active() for shell, check in page.shell_checks.items()}
            seen["sensitive"] = {shell.value: check.get_sensitive() for shell, check in page.shell_checks.items()}
            seen["diff_files"] = [change.path for change in page.preview_changes]
            seen["backup"] = page.backup_row.get_title()
            seen["initial"] = (page.initial_row.get_title(), page.initial_profile_name())
            seen["autostart"] = (page.autostart_row.get_title(), page.autostart_row.get_active(),
                                 page.autostart_row.get_sensitive())
            seen["apply_label"] = page.apply_button.get_label()
            seen["initial_in_footer"] = hasattr(page, "initial_box")
            GLib.timeout_add(50, on_apply, app, page)
            return False

        def on_apply(app, page):
            if shot(app, "S-07-onboarding-3-aplicar.png"):
                return True
            adjustment = page.stack.get_visible_child().get_vadjustment()
            adjustment.set_value(adjustment.get_upper())
            if shot(app, "S-07-onboarding-3-aplicar-fim.png"):
                return True
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
            visible = app.window.nav.get_visible_page()
            seen["opened"] = (visible.get_title(), getattr(visible, "draft", None) and visible.draft.name,
                              app.window.selected_sidebar_key() == f"profile:{model.state.profile_id}")
            app.quit()
            return False

        self.run_app(model, drive, seconds=30)

        self.assertNotIn("error", seen, seen.get("error"))
        self.assertTrue(seen.get("shown"), "an unconfigured app opens the onboarding")
        self.assertEqual((seen.get("step1"), seen.get("step2"), seen.get("step3")), (0, 1, 2))
        self.assertEqual(seen.get("tools_title"), "Ferramentas nesta máquina")
        self.assertEqual(seen.get("claude_command"), "curl -fsSL https://claude.ai/install.sh | bash")
        # OnboardingView.swift:326, 363-365: git identity and GitHub account.
        self.assertEqual(seen.get("status_empty"), "Faltam 2 itens")
        self.assertEqual(seen.get("status_git"), "Falta 1 item")
        self.assertEqual(seen["apply_subtitle"], "Estas são as únicas mudanças fora da pasta ~/.config/multicred/.")
        self.assertEqual(seen["shells_title"], "Shells que vão carregar o MultiCred")
        self.assertTrue(seen["checked"]["bash"], "the login shell is pre-checked")
        self.assertTrue(seen["sensitive"]["bash"])
        self.assertIn("~/.bashrc", seen["diff_files"])
        self.assertIn("~/.gitconfig", seen["diff_files"])
        self.assertEqual(seen["backup"], "Backup dos originais em ~/.config/multicred/backups/")
        self.assertEqual(seen["initial"], ("Perfil inicial", "Trabalho"))
        self.assertFalse(seen["initial_in_footer"], "Perfil inicial moved into the step's group, like the Mac")
        self.assertEqual(seen["autostart"], ("Abrir ao iniciar a sessão", True, True), "on by default, like the Mac")
        self.assertEqual(seen["apply_label"], "Aplicar e começar")
        self.assertTrue(seen.get("main_shown"), "the normal window content comes back")
        self.assertEqual(seen.get("rows"), ["Trabalho"])
        self.assertEqual(seen.get("toast"), "Perfil Trabalho ativo")
        self.assertEqual(seen.get("opened"), ("Editar perfil", "Trabalho", True),
                         "like the Mac, setup ends on the active profile's editor")

        bashrc = (self.sandbox / ".bashrc").read_text()
        self.assertTrue(bashrc.startswith("export KEEP=yes\n\n# >>> MultiCred >>>\n"), bashrc)
        self.assertIn('. "$HOME/.config/multicred/shell/hook.bash"', bashrc)
        self.assertIn('[include]\n\tpath = "~/.config/multicred/gitconfig"', (self.sandbox / ".gitconfig").read_text())
        config = self.saved_config()
        self.assertEqual([p["name"] for p in config["profiles"]], ["Trabalho"])
        self.assertEqual(config["settings"]["enabledShells"], ["bash"])
        self.assertTrue(config["settings"]["openAtLogin"])
        self.assertTrue(self.autostart.is_enabled(), "the autostart switch was applied (sandbox entry written)")
        state = json.loads((self.sandbox / ".config/multicred/state.json").read_text())
        self.assertEqual(state["profileId"], config["profiles"][0]["id"])
        self.assertEqual(str(model.state.profile_id).upper(), config["profiles"][0]["id"])
        self.assertIn("dev@empresa.example", (self.sandbox / ".config/multicred/gitconfig").read_text())
        self.assertTrue((self.sandbox / ".config/multicred/shell/hook.bash").exists())

    def test_autostart_switched_off_is_applied(self):
        """AppModel.swift:539, 548: the setting is saved and the login item unregistered."""
        self.autostart.enable()  # left over from an earlier setup
        model = self.make_model()
        seen = {}

        def drive(app):
            GLib.timeout_add(50, wait_for_detection, app, app.window.onboarding)
            return False

        def wait_for_detection(app, page):
            if page.setup is None:
                return True
            self.walk_to_apply(page)
            page.autostart_row.set_active(False)
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
            app.quit()
            return False

        self.run_app(model, drive)
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertFalse(self.saved_config()["settings"]["openAtLogin"])
        self.assertFalse(self.autostart.file.exists())

    def test_autostart_is_explained_when_not_installed_from_the_package(self):
        from multicred.core.install.autostart import Autostart

        model = self.make_model(Autostart(self.environment, command_path=str(self.sandbox / "nada/multicred")))
        seen = {}

        def drive(app):
            GLib.timeout_add(50, wait_for_detection, app, app.window.onboarding)
            return False

        def wait_for_detection(app, page):
            if page.setup is None:
                return True
            self.walk_to_apply(page)
            seen["autostart"] = (page.autostart_row.get_active(), page.autostart_row.get_sensitive(),
                                 page.autostart_row.get_subtitle())
            app.quit()
            return False

        self.run_app(model, drive)
        active, sensitive, subtitle = seen["autostart"]
        self.assertFalse(active)
        self.assertFalse(sensitive)
        self.assertIn("/usr/bin/multicred", subtitle)

    def test_a_card_with_git_and_github_is_complete(self):
        from multicred.core.models import GitHubAccount, ProfileColor

        model = self.make_model()
        seen = {}

        def drive(app):
            GLib.timeout_add(50, wait_for_detection, app, app.window.onboarding)
            return False

        def wait_for_detection(app, page):
            if page.setup is None:
                return True
            account = GitHubAccount(nickname="E", username="dev-empresa", color=ProfileColor.INDIGO)
            page.draft.github_accounts.append(account)
            page.next_button.emit("clicked")
            card = page.profile_cards[0]
            card.git_name_row.set_text("Dev")
            card.git_email_row.set_text("dev@empresa.example")
            card.github_row.set_selected(1)
            seen["status"] = card.status_pill.get_label()
            seen["tone"] = card.status_pill.has_css_class("ok")
            app.quit()
            return False

        self.run_app(model, drive)
        self.assertEqual(seen.get("status"), "Completo")
        self.assertTrue(seen.get("tone"))

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
