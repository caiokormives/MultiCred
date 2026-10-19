"""S-09: the Ajustes page (approved Linux design G) on a sandbox, driven programmatically.

Toggling the feedback setting persists in config.json, the autostart switch
creates and removes the file in the sandbox, the shells sub-page re-runs the
installer, "Copiar diagnóstico" carries no secret, and uninstalling from the
page leaves no MultiCred block in the sandbox rc files and quits the app. Also:
starting the app with an active profile re-applies it once.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop. With
MULTICRED_SCREENS_DIR set, S-09-ajustes.png is rendered from inside the private
display (never the user's screen).
"""

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

SECRET = "sk-valor-que-nunca-aparece-123"


class RealisticTools:
    """Paths like the design's; nothing is executed (the sandbox runner is a dry run)."""

    def __init__(self, home):
        self.paths = {"git": "/usr/bin/git", "gh": "/usr/bin/gh", "claude": f"{home}/.local/bin/claude"}

    def locate(self, tool):
        return self.paths[tool.value]

    def search_directories(self):
        return ["/usr/bin"]

    def version(self, tool):
        return f"{tool.value} 1.0"


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
class SettingsPageUITests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.install.autostart import Autostart
        from multicred.core.install.installer import Installer, InstallOptions
        from multicred.core.models import ShellKind
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-settings-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        (self.sandbox / ".bashrc").write_text("export ORIG=1\n")
        (self.sandbox / ".zshrc").write_text("# zsh do usuário\n")
        Installer(self.environment).apply(InstallOptions(shells=[ShellKind.BASH, ShellKind.ZSH]))
        config = real_env.seed_sandbox(self.environment)
        config.settings.enabled_shells = [ShellKind.BASH, ShellKind.ZSH]
        ConfigRepository(self.environment).save(config)
        self.environment.secrets.set_secret(SECRET.encode(), "A6F1D7C2-3B4E-4F50-9A61-7B8C9D0E1F23")
        command = self.sandbox / "usr/bin/multicred"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\n")
        command.chmod(0o755)
        self.autostart = Autostart(self.environment, command_path=str(command))
        self.model = AppModel(self.environment, RealisticTools(self.sandbox), dispatch=GLib.idle_add,
                              autostart=self.autostart)
        self.config_path = self.sandbox / ".config/multicred/config.json"

    def saved_settings(self):
        return json.loads(self.config_path.read_text())["settings"]

    def run_steps(self, steps, seconds=40):
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(self.model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        queue = list(steps)
        self.failure = None

        def tick():
            if not queue:
                app.quit()
                return False
            try:
                again = queue[0](app)
            except Exception:
                import traceback
                self.failure = traceback.format_exc()
                app.quit()
                return False
            if not again:
                queue.pop(0)
            return True

        app.connect("activate", lambda *_: GLib.timeout_add(60, tick))
        GLib.timeout_add_seconds(seconds, app.quit)
        app.run([])
        self.assertIsNone(self.failure, self.failure)
        self.assertEqual(queue, [], f"steps left: {[getattr(s, '__name__', s) for s in queue]}")
        return app

    def test_settings_page_end_to_end(self):
        from multicred.core.install import managed_block
        from multicred.core.install.installer import Integrations
        from multicred.ui.pages.settings import SettingsPage

        seen = {}
        shots = {"tries": 0}

        def open_settings(app):
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            window = app.window
            window.set_default_size(1225, 745)
            window.open_page("settings")
            page = window._section_pages["settings"]
            seen["is_settings_page"] = isinstance(page, SettingsPage)
            seen["section_title"] = window.content_page.get_title()
            self.page = page

        def feedback(_app):
            page = self.page
            seen["feedback_options"] = page.feedback_options()
            seen["feedback_initial"] = page.feedback_row.get_selected()
            page.feedback_row.set_selected(2)
            seen["after_none"] = self.saved_settings()["switchFeedback"]
            page.feedback_row.set_selected(1)
            seen["after_notification"] = self.saved_settings()["switchFeedback"]
            page.feedback_row.set_selected(0)
            seen["after_hud"] = self.saved_settings()["switchFeedback"]

        def autostart(_app):
            page = self.page
            seen["autostart_sensitive"] = page.autostart_row.get_sensitive()
            seen["autostart_initial"] = page.autostart_row.get_active()
            page.autostart_row.set_active(True)
            seen["file_after_on"] = self.autostart.file.is_file()
            seen["setting_after_on"] = self.saved_settings()["openAtLogin"]
            page.autostart_row.set_active(False)
            seen["file_after_off"] = self.autostart.file.exists()
            seen["setting_after_off"] = self.saved_settings()["openAtLogin"]
            page.autostart_row.set_active(True)  # as in the design

        def wait_for_tools(_app):
            return "claude ~/.local/bin/claude" not in (self.page.tools_row.get_subtitle() or "")

        def texts(_app):
            page = self.page
            seen["shells_subtitle"] = page.shells_row.get_subtitle()
            seen["tools_subtitle"] = page.tools_row.get_subtitle()
            seen["logs_subtitle"] = page.logs_row.get_subtitle()
            seen["groups"] = page.group_titles()

        def screenshot(app):
            directory = os.environ.get("MULTICRED_SCREENS_DIR")
            if not directory:
                return False
            app.window.dismiss_toast()
            shots["tries"] += 1
            if shots["tries"] < 8:  # let the transition and the tool lookup settle
                return True
            return not save_png(app.window, Path(directory) / "S-09-ajustes.png")

        def diagnostics(_app):
            if "diagnostics" not in seen:
                seen["diagnostics"] = None
                self.page.copy_diagnostics(lambda text: seen.__setitem__("diagnostics", text))
            return seen["diagnostics"] is None

        def shells(_app):
            sub = self.page.open_shells_page()
            seen["shell_rows"] = sorted(s.value for s in sub.switches)
            sub.switches[next(s for s in sub.switches if s.value == "fish")].set_active(True)
            seen["fish_file"] = Integrations.fish_conf_file(self.environment).is_file()
            seen["shells_after_fish"] = self.saved_settings()["enabledShells"]
            seen["shells_subtitle_after"] = self.page.shells_row.get_subtitle()
            self.page.window.nav.pop()

        def uninstall_dialog(_app):
            dialog = self.page.open_uninstall()
            seen["dialog_heading"] = dialog.get_heading()
            seen["dialog_text"] = self.page.uninstall_preview_text()
            seen["d12_active"] = self.page.claude_folders_check.get_active()

        def dialog_screenshot(app):
            directory = os.environ.get("MULTICRED_SCREENS_DIR")
            if not directory:
                return False
            app.window.dismiss_toast()
            shots["dialog"] = shots.get("dialog", 0) + 1
            if shots["dialog"] < 8:
                return True
            return not save_png(app.window, Path(directory) / "S-09-desinstalar.png")

        def confirm(_app):
            self.page.uninstall_dialog.emit("response", "confirm")

        def wait_result(_app):
            return self.page.result_dialog is None

        def quit_from_result(_app):
            seen["result_text"] = self.page.result_text
            self.page.result_dialog.emit("response", "quit")

        self.run_steps([open_settings, feedback, autostart, wait_for_tools, texts, screenshot, diagnostics, shells,
                        uninstall_dialog, dialog_screenshot, confirm, wait_result, quit_from_result])

        self.assertTrue(seen["is_settings_page"])
        self.assertEqual(seen["section_title"], "Ajustes")
        self.assertEqual(seen["feedback_options"], ["Notificação curta", "Notificação", "Nada"])
        self.assertEqual(seen["feedback_initial"], 0)
        self.assertEqual((seen["after_none"], seen["after_notification"], seen["after_hud"]),
                         ("none", "notification", "hud"))
        self.assertTrue(seen["autostart_sensitive"])
        self.assertFalse(seen["autostart_initial"], "the switch shows the file, not just the setting")
        self.assertTrue(seen["file_after_on"])
        self.assertTrue(seen["setting_after_on"])
        self.assertFalse(seen["file_after_off"])
        self.assertFalse(seen["setting_after_off"])
        self.assertEqual(seen["shells_subtitle"], "bash · zsh · fish não instalado")
        self.assertEqual(seen["tools_subtitle"], "git /usr/bin/git · gh /usr/bin/gh · claude ~/.local/bin/claude")
        self.assertEqual(seen["logs_subtitle"], "~/.local/state/multicred/")
        self.assertEqual(seen["groups"], ["Geral", "Avançado", "Zona de perigo"])
        diagnostics_text = seen["diagnostics"]
        self.assertIn("MultiCred 0.1.0", diagnostics_text)
        self.assertIn("git: /usr/bin/git", diagnostics_text)
        self.assertIn("claude: ~/.local/bin/claude", diagnostics_text)
        self.assertIn("Sistema:", diagnostics_text)
        self.assertNotIn(SECRET, diagnostics_text)
        self.assertEqual(seen["shell_rows"], ["bash", "fish", "zsh"])
        self.assertTrue(seen["fish_file"])
        self.assertEqual(sorted(seen["shells_after_fish"]), ["bash", "fish", "zsh"])
        self.assertEqual(seen["shells_subtitle_after"], "bash · zsh · fish")
        self.assertEqual(seen["dialog_heading"], "Desinstalar o MultiCred?")
        self.assertIn("~/.bashrc", seen["dialog_text"])
        self.assertIn("~/.zshrc", seen["dialog_text"])
        self.assertIn("app.multicred.MultiCred.desktop", seen["dialog_text"])
        self.assertFalse(seen["d12_active"], "D12 option off by default")
        self.assertIn("MultiCred desinstalado", seen["result_text"])

        # Nothing of MultiCred is left in the sandbox rc files; the user's lines are back.
        for name in (".bashrc", ".zshrc"):
            text = (self.sandbox / name).read_text()
            self.assertNotIn(managed_block.BEGIN, text, name)
        self.assertEqual((self.sandbox / ".bashrc").read_text(), "export ORIG=1\n")
        self.assertEqual((self.sandbox / ".zshrc").read_text(), "# zsh do usuário\n")
        self.assertFalse(Integrations.fish_conf_file(self.environment).exists())
        self.assertFalse(self.autostart.file.exists())
        self.assertFalse((self.sandbox / ".config/multicred").exists())
        self.assertEqual(self.environment.secrets.all_accounts(), [])

    def test_autostart_is_explained_when_not_installed_from_the_package(self):
        from multicred.core.install.autostart import Autostart

        self.model.autostart = Autostart(self.environment, command_path=str(self.sandbox / "nada/multicred"))
        seen = {}

        def look(app):
            app.window.open_page("settings")
            page = app.window._section_pages["settings"]
            seen["sensitive"] = page.autostart_row.get_sensitive()
            seen["subtitle"] = page.autostart_row.get_subtitle()

        self.run_steps([look])
        self.assertFalse(seen["sensitive"])
        self.assertIn("/usr/bin/multicred", seen["subtitle"])


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class ReapplyAtStartupUITests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.engine.switch_engine import SwitchEngine
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-reapply-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        environment = real_env.make_sandbox(self.sandbox)
        config = real_env.seed_sandbox(environment)
        ConfigRepository(environment).save(config)
        tools = RealisticTools(self.sandbox)
        SwitchEngine(environment, tools).switch_profile(config.profiles[1].id, config)
        self.model = AppModel(environment, tools, dispatch=GLib.idle_add)
        self.calls = []
        original = self.model.engine.reapply_active_profile
        self.model.engine.reapply_active_profile = lambda c: (self.calls.append(c), original(c))[1]

    def test_background_start_reapplies_the_active_profile_once(self):
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(self.model, background=True, flags=Gio.ApplicationFlags.NON_UNIQUE)
        seen = {}

        def drive():
            if self.model.switching or not self.calls:
                return True
            seen["generation"] = self.model.state.generation
            seen["inconsistent"] = self.model.inconsistent
            GLib.timeout_add(300, app.quit)
            return False

        app.connect("startup", lambda *_: GLib.timeout_add(50, drive))
        GLib.timeout_add_seconds(15, app.quit)
        app.run([])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(seen.get("generation"), 2)
        self.assertFalse(seen.get("inconsistent"))

    def test_failed_reapply_marks_inconsistent_and_notifies_once(self):
        from multicred.ui.app import MultiCredApplication

        def failing(_config):
            self.calls.append(_config)
            raise RuntimeError("systemctl indisponível")
        self.model.engine.reapply_active_profile = failing
        app = MultiCredApplication(self.model, background=True, flags=Gio.ApplicationFlags.NON_UNIQUE)
        errors = []

        def startup(*_):
            app.feedback.notify_error = lambda title, body: errors.append((title, body))

        def drive():
            if not errors:
                return True
            GLib.timeout_add(300, app.quit)  # a retry loop would add calls meanwhile
            return False

        app.connect("startup", startup)
        app.connect("startup", lambda *_: GLib.timeout_add(50, drive))
        GLib.timeout_add_seconds(15, app.quit)
        app.run([])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.model.inconsistent)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "Não consegui reaplicar Pessoal")
        self.assertIn("Abra Verificar", errors[0][1])


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class SwitchFeedbackWiringUITests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-feedback-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        environment = real_env.make_sandbox(self.sandbox)
        self.config = real_env.seed_sandbox(environment)
        ConfigRepository(environment).save(self.config)
        self.model = AppModel(environment, RealisticTools(self.sandbox), dispatch=GLib.idle_add)

    def test_window_and_tray_switches_reach_the_feedback_and_tray_errors_notify_while_hidden(self):
        from multicred.core.models import SwitchFeedback
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(self.model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        presented, errors, steps = [], [], []

        def activate(*_):
            app.feedback.present = lambda outcome, feedback: presented.append((outcome.profile_name, feedback))
            app.feedback.notify_error = lambda title, body: errors.append(title)
            GLib.timeout_add(50, drive)

        def drive():
            if self.model.switching:
                return True
            step = len(steps)
            steps.append(step)
            if step == 0:
                app.window.on_profile(None, self.config.profiles[1].id)  # from the window
                return True
            if step == 1:
                app.tray.handle(("profile", self.config.profiles[0].id))  # from the tray
                return True
            if step == 2:
                app.window.set_visible(False)
                app.show_error("Não consegui trocar para Pessoal", "gh: keyring locked")
                app.quit()
                return False
            return False

        app.connect("activate", activate)
        GLib.timeout_add_seconds(15, app.quit)
        app.run([])
        self.assertEqual(presented, [("Pessoal", SwitchFeedback.HUD), ("Trabalho", SwitchFeedback.HUD)])
        self.assertEqual(errors, ["Não consegui trocar para Pessoal"])
