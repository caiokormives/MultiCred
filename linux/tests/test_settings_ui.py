"""S-07 (R-07): the Ajustes page like the Mac's SettingsPage.swift, on a sandbox, driven programmatically.

One form, no sub-pages (SettingsPage.swift:14-73): "Geral" (autostart switch and
"Notificação breve / Só notificação / Nada"), "Shells" with a switch per shell and
the Mac's footer, "Ferramentas encontradas", "Diagnóstico" ("Abrir pasta de logs",
"Copiar diagnóstico") and, only once installed, "Desinstalar e restaurar arquivos
originais", whose "Desinstalar…" pushes a page with the diff of each file and
"Cancelar / Desinstalar" (UninstallPage, SettingsPage.swift:114-174). Uninstalling
from that page leaves no MultiCred block in the sandbox rc files and quits the app.
Also: starting the app with an active profile re-applies it once.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop. With
MULTICRED_SCREENS_DIR set, S-07-ajustes.png, S-07-desinstalar.png and
S-07-desinstalado.png are rendered from inside the private display (never the
user's screen).
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


def screenshot_step(name, shots):
    """A run_steps step that waits a few ticks (transitions, lookups) and saves `name`."""
    def step(app):
        directory = os.environ.get("MULTICRED_SCREENS_DIR")
        if not directory:
            return False
        app.window.dismiss_toast()
        shots[name] = shots.get(name, 0) + 1
        if shots[name] < 8:
            return True
        return not save_png(app.window, Path(directory) / name)
    step.__name__ = f"screenshot {name}"
    return step


class SandboxSettingsCase(unittest.TestCase):
    installed = True

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
        if self.installed:
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

    def open_settings(self, app):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        window = app.window
        window.set_default_size(1225, 745)
        window.open_page("settings")
        self.page = window._section_pages["settings"]
        return False


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class SettingsPageTests(SandboxSettingsCase):
    def test_settings_page_end_to_end(self):
        from multicred.core.install import managed_block
        from multicred.core.install.installer import Integrations
        from multicred.core.models import ShellKind
        from multicred.core.tools import Tool
        from multicred.ui.pages.settings import SettingsPage
        from multicred.ui.pages.uninstall import UninstallPage

        seen = {}
        shots = {}

        def opened(app):
            seen["is_settings_page"] = isinstance(self.page, SettingsPage)
            seen["section_title"] = app.window.content_page.get_title()

        def feedback(_app):
            page = self.page
            seen["feedback_options"] = page.feedback_options()
            seen["feedback_title"] = page.feedback_row.get_title()
            seen["feedback_initial"] = page.feedback_row.get_selected()
            page.feedback_row.set_selected(2)
            seen["after_none"] = self.saved_settings()["switchFeedback"]
            page.feedback_row.set_selected(1)
            seen["after_notification"] = self.saved_settings()["switchFeedback"]
            page.feedback_row.set_selected(0)
            seen["after_hud"] = self.saved_settings()["switchFeedback"]

        def autostart(_app):
            page = self.page
            seen["autostart_title"] = page.autostart_row.get_title()
            seen["autostart_sensitive"] = page.autostart_row.get_sensitive()
            seen["autostart_initial"] = page.autostart_row.get_active()
            page.autostart_row.set_active(True)
            seen["file_after_on"] = self.autostart.file.is_file()
            seen["setting_after_on"] = self.saved_settings()["openAtLogin"]
            page.autostart_row.set_active(False)
            seen["file_after_off"] = self.autostart.file.exists()
            seen["setting_after_off"] = self.saved_settings()["openAtLogin"]
            page.autostart_row.set_active(True)  # the uninstall page then lists it

        def wait_for_tools(_app):
            return self.page.tool_value(Tool.CLAUDE) == "procurando…"

        def texts(_app):
            page = self.page
            seen["groups"] = page.group_titles()
            seen["shell_rows"] = [(shell.value, row.get_active(), row.get_subtitle())
                                  for shell, row in page.shell_switches.items()]
            seen["shells_footer"] = page.shells_footer.get_label()
            seen["tools"] = {tool.value: (page.tool_rows[tool].get_title(), page.tool_value(tool)) for tool in Tool}
            seen["diagnostics_buttons"] = [page.logs_button.get_label(), page.diagnostics_button.get_label()]
            seen["danger_visible"] = page.danger_group.get_visible()
            seen["danger_title"] = page.uninstall_row.get_title()
            seen["danger_subtitle"] = page.uninstall_row.get_subtitle()
            seen["danger_button"] = page.uninstall_button.get_label()
            seen["sub_pages"] = [name for name in ("open_shells_page", "open_tools_page") if hasattr(page, name)]

        def scroll_to_bottom(_app):
            adjustment = self.page.scroller.get_vadjustment()
            adjustment.set_value(adjustment.get_upper())

        def diagnostics(_app):
            if "diagnostics" not in seen:
                seen["diagnostics"] = None
                self.page.copy_diagnostics(lambda text: seen.__setitem__("diagnostics", text))
            return seen["diagnostics"] is None

        def shells(_app):
            self.page.shell_switches[ShellKind.FISH].set_active(True)
            seen["fish_file"] = Integrations.fish_conf_file(self.environment).is_file()
            seen["shells_after_fish"] = self.saved_settings()["enabledShells"]

        def open_uninstall_and_cancel(app):
            uninstall = self.page.open_uninstall()
            seen["pushed"] = isinstance(uninstall, UninstallPage) and app.window.nav.get_visible_page() is uninstall
            seen["cancel_label"] = uninstall.cancel_button.get_label()
            uninstall.cancel_button.emit("clicked")
            seen["back_on_settings"] = app.window.nav.get_visible_page() is self.page
            seen["bashrc_after_cancel"] = managed_block.BEGIN in (self.sandbox / ".bashrc").read_text()

        def open_uninstall(app):
            self.uninstall = self.page.open_uninstall()
            page = self.uninstall
            seen["uninstall_title"] = page.get_title()
            seen["heading"] = page.heading_label.get_label()
            seen["intro"] = page.intro_label.get_label()
            seen["changes"] = {path: (summary, lines) for path, summary, lines in page.file_changes()}
            seen["also"] = page.also_label.get_label()
            seen["d12"] = (page.claude_folders_check.get_active(), page.claude_folders_check.get_sensitive())
            seen["buttons"] = [page.cancel_button.get_label(), page.uninstall_button.get_label()]
            seen["destructive"] = page.uninstall_button.has_css_class("destructive-action")

        def scroll_uninstall(_app):
            adjustment = self.uninstall.stack.get_child_by_name("preview").get_vadjustment()
            adjustment.set_value(adjustment.get_upper())

        def confirm(_app):
            self.uninstall.uninstall_button.emit("clicked")

        def wait_result(_app):
            return self.uninstall.report is None

        def result(_app):
            page = self.uninstall
            seen["result_texts"] = page.result_texts()
            seen["quit_label"] = page.quit_button.get_label()

        def quit_from_result(_app):
            self.uninstall.quit_button.emit("clicked")
            return False

        self.run_steps([self.open_settings, opened, feedback, autostart, wait_for_tools, texts,
                        screenshot_step("S-07-ajustes.png", shots), scroll_to_bottom,
                        screenshot_step("S-07-ajustes-fim.png", shots), diagnostics, shells,
                        open_uninstall_and_cancel, open_uninstall, screenshot_step("S-07-desinstalar.png", shots),
                        scroll_uninstall, screenshot_step("S-07-desinstalar-fim.png", shots),
                        confirm, wait_result, result, screenshot_step("S-07-desinstalado.png", shots),
                        quit_from_result])

        self.assertTrue(seen["is_settings_page"])
        self.assertEqual(seen["section_title"], "Ajustes")
        # Geral (SettingsPage.swift:15-22; HUD → notificação breve).
        self.assertEqual(seen["autostart_title"], "Abrir ao iniciar a sessão")
        self.assertEqual(seen["feedback_title"], "Ao trocar de perfil, mostrar")
        self.assertEqual(seen["feedback_options"], ["Notificação breve", "Só notificação", "Nada"])
        self.assertEqual(seen["feedback_initial"], 0)
        self.assertEqual((seen["after_none"], seen["after_notification"], seen["after_hud"]),
                         ("none", "notification", "hud"))
        self.assertTrue(seen["autostart_sensitive"])
        self.assertFalse(seen["autostart_initial"], "the switch shows the file, not just the setting")
        self.assertTrue(seen["file_after_on"])
        self.assertTrue(seen["setting_after_on"])
        self.assertFalse(seen["file_after_off"])
        self.assertFalse(seen["setting_after_off"])
        # One form, in the Mac's order, no "Avançado" sub-pages.
        self.assertEqual(seen["groups"], ["Geral", "Shells", "Ferramentas encontradas", "Diagnóstico"])
        self.assertEqual(seen["sub_pages"], [])
        self.assertEqual(seen["shell_rows"], [("bash", True, "~/.bashrc"),
                                              ("zsh", True, "~/.zshrc"),
                                              ("fish", False, "~/.config/fish/conf.d/multicred.fish")])
        self.assertEqual(seen["shells_footer"],
                         "Nos shells marcados, terminais já abertos também trocam de conta a cada prompt. Mudar aqui "
                         "reescreve só o bloco do MultiCred nesses arquivos, com backup.")
        self.assertEqual(seen["tools"], {"git": ("git", "/usr/bin/git"), "gh": ("GitHub CLI", "/usr/bin/gh"),
                                         "claude": ("Claude Code", "~/.local/bin/claude")})
        self.assertEqual(seen["diagnostics_buttons"], ["Abrir pasta de logs", "Copiar diagnóstico"])
        # The red zone (SettingsPage.swift:55-71; launchd and Keychain in Linux terms).
        self.assertTrue(seen["danger_visible"])
        self.assertEqual(seen["danger_title"], "Desinstalar e restaurar arquivos originais")
        self.assertEqual(seen["danger_subtitle"],
                         "Remove o bloco do MultiCred dos arquivos de shell e do gitconfig, a variável do Claude Code "
                         "na sessão gráfica e as chaves de API do chaveiro do sistema. As contas continuam logadas "
                         "nas próprias ferramentas.")
        self.assertEqual(seen["danger_button"], "Desinstalar…")
        diagnostics_text = seen["diagnostics"]
        self.assertIn("MultiCred 0.1.0", diagnostics_text)
        self.assertIn("git: /usr/bin/git", diagnostics_text)
        self.assertIn("claude: ~/.local/bin/claude", diagnostics_text)
        self.assertNotIn(SECRET, diagnostics_text)
        self.assertTrue(seen["fish_file"])
        self.assertEqual(sorted(seen["shells_after_fish"]), ["bash", "fish", "zsh"])
        # The uninstall page: pushed, "Cancelar" goes back and changes nothing.
        self.assertTrue(seen["pushed"])
        self.assertEqual(seen["cancel_label"], "Cancelar")
        self.assertTrue(seen["back_on_settings"])
        self.assertTrue(seen["bashrc_after_cancel"])
        self.assertEqual(seen["uninstall_title"], "Desinstalar")
        self.assertEqual(seen["heading"], "Desinstalar o MultiCred")
        self.assertEqual(seen["intro"], "Estas são as mudanças. Só o bloco do MultiCred é removido de cada arquivo; "
                                        "o resto fica como você deixou.")
        changes = seen["changes"]
        for path in ("~/.bashrc", "~/.zshrc", "~/.gitconfig"):
            summary, lines = changes[path]
            self.assertEqual(lines[0], f"− {managed_block.BEGIN}", path)
            self.assertEqual(lines[-1], f"− {managed_block.END}", path)
            self.assertTrue(summary.startswith("−") and summary.endswith("linhas"), summary)
        self.assertTrue(any(line.startswith("− ") and '. "$HOME/.config/multicred/shell/hook.bash"' in line
                            for line in changes["~/.bashrc"][1]), changes["~/.bashrc"])
        self.assertEqual(changes["~/.config/fish/conf.d/multicred.fish"], ("será apagado", []))
        self.assertEqual(changes["~/.config/multicred"], ("será apagado", []))
        also = seen["also"]
        self.assertTrue(also.startswith("Também: "), also)
        self.assertIn("apagar do chaveiro do sistema as chaves de API e os tokens das contas Claude "
                      "(pastas e conversas do Claude ficam)", also)
        self.assertIn("(pastas e conversas do Claude ficam), desligar a abertura ao iniciar a sessão. "
                      "Os backups vão para ~/.config/multicred-backups-", also)
        self.assertEqual(seen["d12"], (False, False), "D12 option off, and nothing to delete in this sandbox")
        self.assertEqual(seen["buttons"], ["Cancelar", "Desinstalar"])
        self.assertTrue(seen["destructive"])
        # Result in the same page (SettingsPage.swift:156-173).
        texts = seen["result_texts"]
        self.assertEqual(texts[0], "MultiCred desinstalado")
        self.assertTrue(any(t.startswith("Arquivos restaurados: ") and "~/.bashrc" in t for t in texts), texts)
        self.assertEqual(texts[-1], "Abra um novo terminal para que os shells deixem de carregar o MultiCred. "
                                    "Para tirar o app: sudo apt remove multicred.")
        self.assertEqual(seen["quit_label"], "Sair do MultiCred")

        # Nothing of MultiCred is left in the sandbox rc files; the user's lines are back.
        for name in (".bashrc", ".zshrc"):
            self.assertNotIn(managed_block.BEGIN, (self.sandbox / name).read_text(), name)
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

        def look(_app):
            seen["sensitive"] = self.page.autostart_row.get_sensitive()
            seen["subtitle"] = self.page.autostart_row.get_subtitle()

        self.run_steps([self.open_settings, look])
        self.assertFalse(seen["sensitive"])
        self.assertIn("/usr/bin/multicred", seen["subtitle"])


    def test_abrir_verificar_on_an_error_notification_opens_verificar(self):
        """The error notification's button (AppDelegate.swift:242) reaches the real app."""
        seen = {}

        def press(app):
            app.window.set_visible(False)
            seen["registered"] = app.lookup_action("open-verify") is not None
            app.activate_action("open-verify", None)
            seen["section"] = app.window.current_section
            seen["visible"] = app.window.get_visible()

        self.run_steps([press])
        self.assertTrue(seen["registered"])
        self.assertEqual(seen["section"], "verify")
        self.assertTrue(seen["visible"])


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class SettingsBeforeInstallTests(SandboxSettingsCase):
    installed = False

    def test_the_red_zone_only_shows_once_installed(self):
        """SettingsPage.swift:55: `if model.isInstalled`."""
        seen = {}

        def look(_app):
            seen["groups"] = self.page.group_titles()
            seen["danger_visible"] = self.page.danger_group.get_visible()

        self.run_steps([self.open_settings, look])
        self.assertEqual(seen["groups"], ["Geral", "Shells", "Ferramentas encontradas", "Diagnóstico"])
        self.assertFalse(seen["danger_visible"])


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
