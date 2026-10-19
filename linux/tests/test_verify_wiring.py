"""Verificar plugged into the window: the real page, the sidebar badge and the Perfis footer.

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


class FixedTools:
    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class VerifyWiringTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-verify-wiring-"))
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        environment = real_env.make_sandbox(self.sandbox)
        ConfigRepository(environment).save(real_env.seed_sandbox(environment))
        self.model = AppModel(environment, FixedTools(), dispatch=GLib.idle_add)

    def run_app(self, drive, timeout=15):
        from multicred.ui.app import MultiCredApplication
        app = MultiCredApplication(self.model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.connect("activate", lambda *_: GLib.idle_add(lambda: drive(app) and False))
        GLib.timeout_add_seconds(timeout, app.quit)
        app.run([])
        return app

    def test_verify_page_feeds_the_badge_and_the_footer_without_looping(self):
        from multicred.ui.pages.verify import VerifyPage
        seen = {}

        def drive(app):
            window = app.window
            window.open_page("verify")
            page = window._section_pages["verify"]
            seen["is_verify_page"] = isinstance(page, VerifyPage)
            runs = {"n": 0}
            original = page.verifier.run

            def counting_run(*args, **kwargs):
                runs["n"] += 1
                return original(*args, **kwargs)
            page.verifier.run = counting_run

            def wait():
                if self.model.verification is None:
                    return True
                report = self.model.verification
                seen["badge"] = window.verify_badge.get_label()
                seen["badge_visible"] = window.verify_badge.get_visible()
                seen["expected"] = f"{report.ok_count}/{report.total}"
                window.show_section("main")
                profiles = window._section_pages["main"]
                seen["footer"] = profiles.footer_label.get_label()
                seen["footer_expected"] = f"Verificar: {report.ok_count} de {report.total} ok"
                # More model changes must not start new verifier runs.
                before = runs["n"]
                for _ in range(5):
                    self.model.notify()
                GLib.timeout_add(500, lambda: finish(before))
                return False

            def finish(before):
                seen["extra_runs"] = runs["n"] - before
                app.quit()
                return False

            GLib.timeout_add(50, wait)
            return True

        self.run_app(drive)
        self.assertTrue(seen.get("is_verify_page"), "the Verificar section is the real VerifyPage")
        self.assertTrue(seen.get("badge_visible"))
        self.assertEqual(seen.get("badge"), seen.get("expected"))
        self.assertEqual(seen.get("footer"), seen.get("footer_expected"))
        self.assertEqual(seen.get("extra_runs"), 0)

    def test_model_verify_fills_the_report_without_the_page(self):
        seen = {}

        def drive(app):
            def done(report, error):
                seen["report"], seen["error"] = report, error
                app.quit()
            self.model.verify(done)
            return True

        self.run_app(drive)
        self.assertIsNone(seen.get("error"))
        self.assertIs(self.model.verification, seen.get("report"))
        self.assertGreater(seen["report"].total, 0)
