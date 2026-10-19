"""The login pages plugged into the window (window.on_login_requested), as on the Mac:
sub-steps are pages pushed in the content of the section (MainNavigator.swift:18-125).

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

    def test_adding_a_claude_account_pushes_the_page_on_claude_and_saves_it(self):
        from multicred.core.claude.login_flow import LoginPhase
        from multicred.core.models import ClaudeAccountKind
        from multicred.ui.pages.add_claude import AddClaudeAccountPage
        seen = {}

        def drive(app):
            window = app.window
            window.open_page("accounts")
            root = window.nav.get_visible_page()
            window.on_login_requested("add", None)
            page = window.nav.get_visible_page()
            seen["page"] = isinstance(page, AddClaudeAccountPage) and page is window.login_page
            seen["pushed_over_root"] = window.nav.get_previous_page(page) is root
            seen["no_dialog"] = window.get_visible_dialog() is None
            page.nickname_entry.set_text("Cliente X")
            page.email_entry.set_text("x@cliente.example")
            page.continue_button.emit("clicked")

            def type_code():
                page.code_entry.set_text("abcdefghijklmnopqrstuvwxyz012345#ABCDEFGHIJKLMNOP")
                page.send_button.emit("clicked")
                self.poll(lambda: window.last_login_result is not None, finish)

            def finish():
                seen["result"] = window.last_login_result
                page.done_button.emit("clicked")
                GLib.timeout_add(400, lambda: (seen.update(back=window.nav.get_visible_page() is root), app.quit()))

            self.poll(lambda: page.session is not None and page.session.phase == LoginPhase.WAITING_FOR_CODE,
                      type_code)
            return True

        self.run_app(drive)
        self.assertTrue(seen.get("page"))
        self.assertTrue(seen.get("pushed_over_root"))
        self.assertTrue(seen.get("no_dialog"))
        self.assertTrue(seen["result"].success)
        self.assertTrue(seen.get("back"), "Concluir empties the stack")
        accounts = [a for a in self.model.config.claude_accounts if a.nickname == "Cliente X"]
        self.assertEqual(len(accounts), 1)
        # The e-mail of the account that actually logged in wins over the one typed.
        self.assertEqual((accounts[0].kind, accounts[0].email), (ClaudeAccountKind.LOGIN, "simulado@example.com"))

    def test_github_login_pushes_the_page_on_github_and_imports_new_logins_on_success(self):
        from multicred.ui.pages.login import GitHubLoginPage, LoginKind, LoginResult
        seen, imported = {}, []
        self.model.import_github_accounts = lambda on_done: imported.append(on_done)

        def drive(app):
            window = app.window
            window.open_page("claude")
            window.on_login_requested("github", None)
            page = window.nav.get_visible_page()
            seen["page"] = isinstance(page, GitHubLoginPage) and page is window.login_page
            seen["section"] = (window.current_section, window.selected_sidebar_key())
            window.login_finished(LoginResult(LoginKind.GITHUB, True, login="dev-novo", switched_back=True))
            window.nav.pop()
            GLib.timeout_add(400, lambda: (seen.update(stopped=not page.session.is_running), app.quit()))
            return True

        self.run_app(drive)
        self.assertTrue(seen.get("page"))
        self.assertEqual(seen.get("section"), ("github", "github"), "the GitHub login lives in GitHub (Mac)")
        self.assertEqual(len(imported), 1, "a successful gh login adds the new accounts to the config")
        self.assertTrue(seen.get("stopped"), "leaving the page stops gh")

    def test_folder_login_is_a_page_on_claude_that_runs_the_login_in_the_folder(self):
        from multicred.ui.pages.claude import ClaudeFolderLoginPage
        seen = {}
        folder = f"{self.sandbox}/.claude-x y"

        def drive(app):
            window = app.window
            window.open_page("main")
            window.on_login_requested("folder", folder)
            page = window.nav.get_visible_page()
            seen["page"] = isinstance(page, ClaudeFolderLoginPage) and page is window.login_page
            seen["section"] = (window.current_section, window.selected_sidebar_key())

            def started():
                seen["environment"] = page.session.command.environment.get("CLAUDE_CONFIG_DIR")
                seen["display"] = page.session.command.display
                app.quit()
            self.poll(lambda: page.session is not None and page.session.command is not None, started)
            return True

        self.run_app(drive)
        self.assertTrue(seen.get("page"), "S-03: the Mac's \"Entrar no Claude\" page, not a command to copy")
        self.assertEqual(seen.get("section"), ("claude", "claude"))
        self.assertEqual(seen.get("environment"), folder)
        self.assertEqual(seen.get("display"), "claude auth login (simulado)")
