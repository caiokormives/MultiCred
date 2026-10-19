"""S-07: the standalone login dialog, driven in test mode with the simulators.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

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
    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class LoginDialogTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.models import GitHubAccount, ProfileColor
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-login-ui-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        config = real_env.seed_sandbox(self.environment)
        self.github = GitHubAccount(nickname="Trabalho", username="dev-trabalho", color=ProfileColor.INDIGO)
        config.github_accounts.append(self.github)
        config.profiles[0].github_account_id = self.github.id
        ConfigRepository(self.environment).save(config)
        self.model = AppModel(self.environment, SandboxTools(), dispatch=GLib.idle_add)
        self.model.load()
        self.results = []

    def run_dialog(self, make_dialog, drive, seconds=25):
        app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
        seen = {}

        def activate(_app):
            window = Adw.ApplicationWindow(application=app)
            window.set_default_size(700, 600)
            window.present()
            dialog = make_dialog()
            dialog.present(window)
            seen["dialog"] = dialog
            GLib.idle_add(drive, app, dialog, seen)

        app.connect("activate", activate)
        GLib.timeout_add_seconds(seconds, app.quit)  # never hang the suite
        app.run([])
        return seen

    def poll(self, condition, then):
        """Calls `then()` once `condition()` holds, checking every 50 ms on the main loop."""
        def tick():
            if not condition():
                return True
            then()
            return False
        GLib.timeout_add(50, tick)

    def test_claude_login_with_the_simulator_reaches_connected_and_stores_the_account(self):
        from multicred.core.claude.login_flow import LoginPhase, NewClaudeAccount
        from multicred.core.models import ClaudeAccountKind, ProfileColor, uuid_text
        from multicred.ui.pages.login import LoginDialog, LoginKind

        code = "abcdefghijklmnopqrstuvwxyz012345#ABCDEFGHIJKLMNOP"

        def make():
            return LoginDialog(self.model, LoginKind.CLAUDE_LOGIN, self.results.append,
                               target=NewClaudeAccount("Cliente X", None, ProfileColor.TEAL))

        def drive(app, dialog, seen):
            seen["title"] = dialog.title_label.get_label()
            seen["test_notice"] = dialog.test_notice.get_visible()

            def type_code():
                seen["steps_waiting"] = [s.state for s in dialog.steps]
                dialog.code_row.set_text(code)
                dialog.connect_button.emit("clicked")
                self.poll(lambda: bool(self.results), finish)

            def finish():
                seen["phase"] = dialog.session.phase
                seen["steps_done"] = [s.state for s in dialog.steps]
                seen["terminal"] = dialog.terminal_text()
                seen["log"] = dialog.log_text()
                seen["done_visible"] = dialog.done_button.get_visible()
                dialog.terminal_toggle.emit("clicked")
                seen["terminal_hidden"] = not dialog.terminal_revealer.get_reveal_child()
                dialog.done_button.emit("clicked")
                GLib.timeout_add(200, app.quit)

            dialog.terminal_toggle.emit("clicked")  # show the terminal while it runs
            self.poll(lambda: dialog.session is not None and dialog.session.phase == LoginPhase.WAITING_FOR_CODE,
                      type_code)
            return False

        seen = self.run_dialog(make, drive)
        self.assertEqual(seen.get("title"), "Entrar com Cliente X")
        self.assertTrue(seen.get("test_notice"))
        self.assertEqual(seen.get("steps_waiting"), ["done", "current", "todo"])
        self.assertEqual(seen.get("phase"), LoginPhase.CONNECTED)
        self.assertEqual(seen.get("steps_done"), ["done", "done", "done"])
        self.assertTrue(seen.get("done_visible"))
        self.assertTrue(seen.get("terminal_hidden"))
        self.assertIn("$ claude auth login --claudeai", seen.get("terminal", ""))
        self.assertIn("Paste code here if prompted", seen.get("terminal", ""))
        self.assertNotIn(code, seen.get("terminal", ""))
        self.assertNotIn(code, seen.get("log", ""))

        self.assertEqual(len(self.results), 1, "the callback runs once")
        result = self.results[0]
        self.assertTrue(result.success)
        self.assertEqual(result.kind, LoginKind.CLAUDE_LOGIN)
        self.assertEqual(result.account.kind, ClaudeAccountKind.LOGIN)
        self.assertIn(uuid_text(result.account.id), self.environment.claude_credentials.vault)
        self.assertIn(result.account.id, [a.id for a in self.model.config.claude_accounts])
        scratch_root = self.sandbox / ".config/multicred/scratch"
        self.assertEqual(list(scratch_root.iterdir()), [], "the throwaway folder is gone")

    def test_rejected_code_shows_the_error_and_reports_failure_when_closed(self):
        from multicred.core.claude.login_flow import NewClaudeAccount
        from multicred.core.models import ProfileColor
        from multicred.ui.pages.login import LoginDialog, LoginKind

        def make():
            return LoginDialog(self.model, LoginKind.CLAUDE_LOGIN, self.results.append,
                               target=NewClaudeAccount("Cliente X", None, ProfileColor.TEAL))

        def drive(app, dialog, seen):
            def type_code():
                dialog.code_row.set_text("erro")
                dialog.connect_button.emit("clicked")
                self.poll(lambda: dialog.session.phase.is_finished, failed)

            def failed():
                seen["error_visible"] = dialog.error_label.get_visible()
                seen["error_text"] = dialog.error_label.get_label()
                seen["retry_visible"] = dialog.retry_button.get_visible()
                seen["terminal_shown"] = dialog.terminal_revealer.get_reveal_child()
                seen["results_before_close"] = len(self.results)
                dialog.close()
                GLib.timeout_add(300, app.quit)

            self.poll(lambda: dialog.session is not None and dialog.session.phase.accepts_code
                      and "Paste code" in dialog.session.output, type_code)
            return False

        seen = self.run_dialog(make, drive)
        self.assertTrue(seen.get("error_visible"))
        self.assertIn("Invalid code or expired", seen.get("error_text", ""))
        self.assertTrue(seen.get("retry_visible"))
        self.assertTrue(seen.get("terminal_shown"), "the output opens on failure")
        self.assertEqual(seen.get("results_before_close"), 0)
        self.assertEqual(len(self.results), 1)
        self.assertFalse(self.results[0].success)
        self.assertEqual(self.environment.claude_credentials.vault, {})

    def test_setup_token_stores_the_token(self):
        from multicred.core.claude.login_flow import NewClaudeAccount
        from multicred.core.claude.login_simulator import SetupTokenSimulator
        from multicred.core.models import ClaudeAccountKind, ProfileColor
        from multicred.ui.pages.login import LoginDialog, LoginKind

        def make():
            return LoginDialog(self.model, LoginKind.CLAUDE_TOKEN, self.results.append,
                               target=NewClaudeAccount("Token X", None, ProfileColor.RED))

        def drive(app, dialog, seen):
            seen["subtitle"] = dialog.subtitle_label.get_label()

            def type_code():
                dialog.code_row.set_text("CODE-1")
                dialog.code_row.emit("entry-activated")
                self.poll(lambda: bool(self.results), lambda: (seen.update(log=dialog.log_text()), app.quit()))

            self.poll(lambda: dialog.session is not None and dialog.session.phase.accepts_code
                      and "Paste code" in dialog.session.output, type_code)
            return False

        seen = self.run_dialog(make, drive)
        self.assertEqual(seen.get("subtitle"), "claude setup-token numa pasta temporária")
        self.assertEqual(len(self.results), 1)
        account = self.results[0].account
        self.assertEqual(account.kind, ClaudeAccountKind.TOKEN)
        self.assertEqual(self.environment.secrets.stored_value(account.secret_account),
                         SetupTokenSimulator.FAKE_TOKEN.encode())
        self.assertNotIn(SetupTokenSimulator.FAKE_TOKEN, seen.get("log", ""))

    def test_github_login_with_the_simulator_switches_back_to_the_profile_account(self):
        from multicred.core.adapters.github_login_flow import GitHubLoginSimulator
        from multicred.ui.pages.login import LoginDialog, LoginKind

        self.model.state.profile_id = self.model.config.profiles[0].id

        def make():
            return LoginDialog(self.model, LoginKind.GITHUB, self.results.append, host="github.com")

        def drive(app, dialog, seen):
            def code_shown():
                seen["code"] = dialog.github_code_label.get_label()
                dialog.connect_button.emit("clicked")  # "Abrir o GitHub": copies the code, sends Enter
                self.poll(lambda: bool(self.results), app.quit)

            self.poll(lambda: dialog.github_session is not None and dialog.github_session.code is not None, code_shown)
            return False

        self.run_dialog(make, drive)
        self.assertEqual(len(self.results), 1)
        result = self.results[0]
        self.assertTrue(result.success)
        self.assertEqual(result.login, GitHubLoginSimulator.SIMULATED_LOGIN)
        self.assertTrue(result.switched_back)
        commands = [entry.command for entry in self.environment.runner.entries]
        self.assertTrue(any(c.endswith("auth switch --hostname github.com --user dev-trabalho") for c in commands),
                        commands)


if __name__ == "__main__":
    unittest.main()
