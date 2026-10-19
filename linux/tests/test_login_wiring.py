"""The login dialog plugged into Contas (window.on_login_requested).

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus).
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
        from gi.repository import Gio, GLib, Gtk
        HAS_DISPLAY = Gtk.init_check()
    except (ImportError, ValueError):
        HAS_DISPLAY = False


class SandboxTools:
    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class LoginWiringTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-login-wiring-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        environment = real_env.make_sandbox(self.sandbox)
        ConfigRepository(environment).save(real_env.seed_sandbox(environment))
        self.model = AppModel(environment, SandboxTools(), dispatch=GLib.idle_add)

    def run_app(self, drive, seconds=25):
        from multicred.ui.app import MultiCredApplication
        app = MultiCredApplication(self.model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.connect("activate", lambda *_: GLib.idle_add(lambda: drive(app) and False))
        GLib.timeout_add_seconds(seconds, app.quit)
        app.run([])

    @staticmethod
    def poll(condition, then):
        def tick():
            if not condition():
                return True
            then()
            return False
        GLib.timeout_add(50, tick)

    def test_adding_a_claude_account_asks_for_it_then_logs_in_and_saves_it(self):
        from multicred.core.claude.login_flow import LoginPhase
        from multicred.core.models import ClaudeAccountKind
        from multicred.ui.pages.login import LoginDialog, LoginKind
        seen = {}

        def drive(app):
            window = app.window
            window.open_page("accounts")
            window.on_login_requested("add", None)
            seen["form"] = window.new_account_form is not None
            window.confirm_new_account("Cliente X", "x@cliente.example", "login")
            dialog = window.login_dialog
            seen["dialog"] = isinstance(dialog, LoginDialog) and dialog.kind == LoginKind.CLAUDE_LOGIN

            def type_code():
                dialog.code_row.set_text("abcdefghijklmnopqrstuvwxyz012345#ABCDEFGHIJKLMNOP")
                dialog.connect_button.emit("clicked")
                self.poll(lambda: window.last_login_result is not None, finish)

            def finish():
                seen["toast"] = window.last_toast
                dialog.done_button.emit("clicked")
                GLib.timeout_add(200, app.quit)

            self.poll(lambda: dialog.session is not None and dialog.session.phase == LoginPhase.WAITING_FOR_CODE,
                      type_code)
            return True

        self.run_app(drive)
        self.assertTrue(seen.get("form"))
        self.assertTrue(seen.get("dialog"))
        accounts = [a for a in self.model.config.claude_accounts if a.nickname == "Cliente X"]
        self.assertEqual(len(accounts), 1)
        # The e-mail of the account that actually logged in wins over the one typed.
        self.assertEqual((accounts[0].kind, accounts[0].email), (ClaudeAccountKind.LOGIN, "simulado@example.com"))
        self.assertEqual(seen.get("toast"), "Conta Claude Cliente X conectada")

    def test_github_login_opens_the_dialog_and_imports_new_logins_on_success(self):
        from multicred.ui.pages.login import LoginDialog, LoginKind, LoginResult
        seen, imported = {}, []
        self.model.import_github_accounts = lambda on_done: imported.append(on_done)

        def drive(app):
            window = app.window
            window.on_login_requested("github", None)
            dialog = window.login_dialog
            seen["dialog"] = isinstance(dialog, LoginDialog) and dialog.kind == LoginKind.GITHUB
            window.login_finished(LoginResult(LoginKind.GITHUB, True, login="dev-novo", switched_back=True))
            dialog.close()
            GLib.timeout_add(200, app.quit)
            return True

        self.run_app(drive)
        self.assertTrue(seen.get("dialog"))
        self.assertEqual(len(imported), 1, "a successful gh login adds the new accounts to the config")

    def test_folder_login_shows_the_command_to_run(self):
        seen = {}
        folder = f"{self.sandbox}/.claude-x y"

        def drive(app):
            window = app.window
            window.on_login_requested("folder", folder)
            seen["command"] = window.folder_login_command
            GLib.timeout_add(200, app.quit)
            return True

        self.run_app(drive)
        self.assertEqual(seen.get("command"), f"CLAUDE_CONFIG_DIR='{folder}' claude auth login")
