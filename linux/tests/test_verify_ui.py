"""S-08: the standalone Verificar page on a temporary HOME, driven programmatically.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop. When
MULTICRED_SCREENS_DIR is set, screenshots are rendered from inside the private
display (never the user's screen) into that directory.
"""

import os
import unittest
from pathlib import Path

HAS_DISPLAY = False
if os.environ.get("MULTICRED_PRIVATE_BUS") == "1":
    if os.environ.get("MULTICRED_SCREENS_DIR"):
        # Screenshots compare against the stock Adwaita design: keep the desktop's own
        # ~/.config/gtk-4.0/gtk.css (COSMIC writes one) out of this process.
        import atexit
        import shutil
        import tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="multicred-ui-config-")
        atexit.register(shutil.rmtree, os.environ["XDG_CONFIG_HOME"], True)
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        gi.require_version("Graphene", "1.0")
        from gi.repository import Adw, GLib, Graphene, Gtk
        HAS_DISPLAY = Gtk.init_check()
    except (ImportError, ValueError):
        HAS_DISPLAY = False

from tests.test_engine import FakeTools  # noqa: E402
from tests.test_verifier import VerifierTestCase  # noqa: E402


def save_screenshot(window, name):
    directory = os.environ.get("MULTICRED_SCREENS_DIR")
    if not directory:
        return None
    width, height = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    rect = Graphene.Rect()
    rect.init(0, 0, width, height)
    texture = window.get_renderer().render_texture(node, rect)
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    texture.save_to_png(str(path))
    return path


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class VerifyPageUITests(VerifierTestCase):
    def make_model(self):
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        ConfigRepository(self.environment).save(self.config)
        model = AppModel(self.environment, FakeTools(), dispatch=GLib.idle_add)
        model.load()
        return model

    def run_page(self, model, drive, seconds=30):
        from multicred.core.adapters.process_scanner import FixedProcessScanner
        from multicred.ui.pages.verify import VerifyPage

        Adw.init()
        # Light, like the approved design (variant F), so the screenshot compares 1:1.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        loop = GLib.MainLoop()
        reports = []
        page = VerifyPage(model, processes=FixedProcessScanner(2))
        page.on_report = reports.append
        view = Adw.NavigationView()
        view.add(page)
        window = Adw.Window(default_width=1000, default_height=780, content=view)
        window.present()
        GLib.timeout_add(50, drive, page, window, reports, loop)
        GLib.timeout_add_seconds(seconds, loop.quit)  # never hang the suite
        loop.run()
        window.destroy()
        return page

    def test_page_shows_the_report_and_fix_all_leaves_every_row_ok(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[user]\n\temail = velho@exemplo.com\n")
        model = self.make_model()
        seen = {}

        def drive(page, window, reports, loop):
            if page.busy or not reports:
                return True
            if "before" not in seen:
                seen["before"] = page.last_report
                seen["headline"] = page.headline_label.get_label()
                seen["rows"] = {check_id: row.check.status.value for check_id, row in page.rows.items()}
                seen["fix_buttons"] = sorted(check_id for check_id, row in page.rows.items() if row.fix_button)
                seen["tooltip"] = page.rows["git"].fix_button.get_tooltip_text()
                seen["subtitle"] = page.rows["git"].get_subtitle()
                seen["groups"] = (page.system_group.get_title(), page.info_group.get_title())
                seen["footer_outside_scroll"] = page.footer.get_ancestor(Gtk.ScrolledWindow) is None
                seen["shot"] = save_screenshot(window, "S-08-verify.png")
                page.fix_all_button.popup()
                seen["fix_all_preview"] = page.fix_all_preview.get_label()
                page.fix_all_confirm.emit("clicked")
                return True
            seen["after"] = page.last_report
            seen["after_headline"] = page.headline_label.get_label()
            seen["after_rows"] = {check_id: row.check.status.value for check_id, row in page.rows.items()}
            seen["after_fix_buttons"] = [check_id for check_id, row in page.rows.items() if row.fix_button]
            seen["reports"] = len(reports)
            save_screenshot(window, "S-08-verify-ok.png")
            loop.quit()
            return False

        self.run_page(model, drive)

        self.assertIn("after", seen, "the page never finished fixing")
        self.assertEqual(seen["headline"], "6 de 7 ok para Trabalho")
        self.assertEqual(seen["rows"]["git"], "problem")
        self.assertEqual(seen["rows"]["claude.sessions"], "info")
        self.assertEqual(seen["fix_buttons"], ["git"])
        self.assertIn("~/.gitconfig", seen["tooltip"])
        self.assertIn("velho@exemplo.com", seen["subtitle"])
        self.assertIn("~/.gitconfig:", seen["subtitle"])
        self.assertEqual(seen["groups"], ("Sistema", "Informativo"))
        self.assertTrue(seen["footer_outside_scroll"], "footer is fixed outside the scrolling list")
        self.assertIn("~/.gitconfig", seen["fix_all_preview"])
        self.assertEqual(seen["after_headline"], "7 de 7 ok para Trabalho")
        self.assertTrue(all(status in ("ok", "info") for status in seen["after_rows"].values()), seen["after_rows"])
        self.assertEqual(seen["after_fix_buttons"], [])
        self.assertTrue(seen["after"].is_consistent)
        self.assertEqual(seen["reports"], 2, "on_report gets the first report and the one after the fixes")
        self.assertTrue(gitconfig.read_text().endswith("# <<< MultiCred <<<\n"))
        if seen["shot"] is not None:
            self.assertTrue(seen["shot"].stat().st_size > 10_000)


if __name__ == "__main__":
    unittest.main()
