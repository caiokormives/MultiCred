"""The standalone Verificar page on a temporary HOME, driven programmatically (S-08, S-06).

S-06 checks it against the Mac (`MultiCred/Views/VerifyPage.swift`): the active
profile as the title with "N de M itens de acordo · verificado às HH:MM", "Corrigir
tudo" at the top, the sections Ferramentas / Integração / Sessões, ✗ separate from !,
the "Corrigir X?" / "Corrigir tudo?" alerts and the fixes that open a login or a key.

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

FOOTER_NOTE = "“Corrigir” mostra o comando antes de executar."


def save_screenshot(window, name):
    directory = os.environ.get("MULTICRED_SCREENS_DIR")
    if not directory:
        return None
    width, height = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        return None
    rect = Graphene.Rect()
    rect.init(0, 0, width, height)
    texture = window.get_renderer().render_texture(node, rect)
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    texture.save_to_png(str(path))
    return path


def settling(seen):
    """Skips a few 50 ms ticks (seen["wait"]) so the next screenshot shows what was drawn."""
    if seen.get("wait", 0) > 0:
        seen["wait"] -= 1
        return True
    return False


def answer(page, response):
    """What a click on the alert's button does: the response, then the alert closes."""
    page.dialog.emit("response", response)
    page.dialog.force_close()
    return response


def hhmm(report):
    return report.checked_at.astimezone().strftime("%H:%M")


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class VerifyPageUITests(VerifierTestCase):
    def make_model(self, save=True):
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        if save:
            ConfigRepository(self.environment).save(self.config)
        model = AppModel(self.environment, FakeTools(), dispatch=GLib.idle_add)
        model.load()
        return model

    def run_page(self, model, drive, seconds=30, opened=None):
        from multicred.core.adapters.process_scanner import FixedProcessScanner
        from multicred.ui.pages.verify import VerifyPage

        Adw.init()
        # No animations: an alert is fully drawn on the next frame, gone right after closing.
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        # Light, like the approved design (variant F), so the screenshot compares 1:1.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        loop = GLib.MainLoop()
        reports = []
        page = VerifyPage(model, processes=FixedProcessScanner(2),
                          open_fix=(opened.append if opened is not None else None))
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

    def test_problems_show_like_the_mac_and_fix_all_runs_after_the_alert(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(gitconfig.read_text() + "[user]\n\temail = velho@exemplo.com\n")
        self.secrets.delete_item(self.gemini.secret_account)
        self.system.gh_logins = [("dev-empresa", "failed"), ("dev-pessoal", "success")]
        gemini_id = self.key_ids[1]
        model = self.make_model()
        opened, seen = [], {}

        def drive(page, window, reports, loop):
            if page.busy or not reports or settling(seen):
                return True
            if "before" not in seen:
                report = seen["before"] = page.last_report
                seen["title"] = page.title_label.get_label()
                seen["subtitle"] = page.subtitle_label.get_label()
                seen["expected_subtitle"] = f"6 de 9 itens de acordo · verificado às {hhmm(report)}"
                seen["groups"] = [(g.get_title(), g.get_visible()) for g in page.groups.values()]
                seen["glyphs"] = {i: (r.status_label.get_label(), r.status_name) for i, r in page.rows.items()}
                seen["error_css"] = page.rows["git"].status_label.has_css_class("error")
                seen["warning_css"] = page.rows["gh.token"].status_label.has_css_class("warning")
                seen["buttons"] = {i: r.fix_button.get_label() for i, r in page.rows.items() if r.fix_button}
                seen["git_subtitle"] = page.rows["git"].subtitle_markup
                seen["section_of"] = {i: r.get_ancestor(Adw.PreferencesGroup).get_title()
                                      for i, r in page.rows.items()}
                seen["fix_all_at_top"] = page.fix_all_button.is_ancestor(page.header)
                seen["fix_all_sensitive"] = page.fix_all_button.get_sensitive()
                seen["footer_note"] = page.footer_note.get_label()
                seen["footer_outside_scroll"] = page.footer.get_ancestor(Gtk.ScrolledWindow) is None
                seen["wait"] = 3  # let the rows be drawn
                return True
            if "shot" not in seen:
                seen["shot"] = save_screenshot(window, "S-06-verificar-problemas.png")
                # A fix that opens a key: alert first, then the page, nothing runs.
                page.rows[gemini_id].fix_button.emit("clicked")
                dialog = page.dialog
                seen["key_alert"] = (dialog.get_heading(), dialog.get_body(),
                                     [dialog.get_response_label(r) for r in ("cancel", "run")])
                seen["wait"] = 2
                return True
            if "key_answered" not in seen:
                seen["key_answered"] = answer(page, "run")
                # Cancel on the GitHub login: nothing opens.
                page.rows["gh.token"].fix_button.emit("clicked")
                seen["wait"] = 2
                return True
            if "gh_answered" not in seen:
                seen["gh_answered"] = answer(page, "cancel")
                seen["opened"] = list(opened)
                page.fix_all_button.emit("clicked")
                seen["fix_all_alert"] = (page.dialog.get_heading(), page.dialog.get_body())
                seen["wait"] = 3
                return True
            if "fixing" not in seen:
                seen["fixing"] = save_screenshot(window, "S-06-verificar-corrigir-tudo.png")
                answer(page, "run")
                return True
            if "settled" not in seen:
                seen["settled"], seen["wait"] = True, 3
                return True
            report = seen["after"] = page.last_report
            seen["after_subtitle"] = page.subtitle_label.get_label()
            seen["after_expected"] = f"7 de 8 itens de acordo · verificado às {hhmm(report)}"
            seen["after_buttons"] = {i: r.fix_button.get_label() for i, r in page.rows.items() if r.fix_button}
            seen["reports"] = len(reports)
            save_screenshot(window, "S-06-verificar-depois.png")
            loop.quit()
            return False

        self.run_page(model, drive, opened=opened)

        self.assertIn("after", seen, "the page never finished fixing")
        self.assertEqual(seen["title"], "Trabalho")
        self.assertEqual(seen["subtitle"], seen["expected_subtitle"])
        self.assertEqual(seen["groups"], [("Ferramentas", True), ("Integração", True), ("Sessões", True)])
        self.assertEqual(seen["glyphs"]["git"], ("✗", "Problema"))
        self.assertEqual(seen["glyphs"]["gh.token"], ("!", "Atenção"))
        self.assertEqual(seen["glyphs"]["gh"], ("✓", "De acordo"))
        self.assertEqual(seen["glyphs"]["claude.sessions"], ("i", "Informação"))
        self.assertTrue(seen["error_css"] and seen["warning_css"], "✗ is red, ! is orange")
        self.assertEqual(seen["buttons"], {"git": "Corrigir", "gh.token": "Entrar…", gemini_id: "Abrir chave…"})
        self.assertIn("Esperado <b>dev@empresa.com.br</b>, encontrado velho@exemplo.com", seen["git_subtitle"])
        self.assertIn("definido em", seen["git_subtitle"])
        self.assertEqual(seen["section_of"]["git"], "Ferramentas")
        self.assertEqual(seen["section_of"]["shell.bash"], "Integração")
        self.assertEqual(seen["section_of"]["claude.sessions"], "Sessões")
        self.assertTrue(seen["fix_all_at_top"], "Corrigir tudo is in the header, as on the Mac")
        self.assertTrue(seen["fix_all_sensitive"])
        self.assertEqual(seen["footer_note"], FOOTER_NOTE)
        self.assertTrue(seen["footer_outside_scroll"], "footer is fixed outside the scrolling list")
        self.assertEqual(seen["key_alert"], ("Corrigir Gemini · GEMINI_API_KEY?",
                                             "Abrir a chave Pessoal para colar o valor de novo.",
                                             ["Cancelar", "Executar"]))
        self.assertEqual([(f.kind.value, f.target) for f in seen["opened"]], [("open-api-key", self.gemini.id)])
        heading, body = seen["fix_all_alert"]
        self.assertEqual(heading, "Corrigir tudo?")
        self.assertTrue(body.startswith("• Mover o include do MultiCred para o fim de ~/.gitconfig"), body)
        self.assertNotIn("Abrir a chave", body, "Corrigir tudo lists only what it runs")
        self.assertEqual(seen["after_subtitle"], seen["after_expected"])
        self.assertEqual(seen["after_buttons"], {gemini_id: "Abrir chave…"})
        self.assertEqual(seen["reports"], 2, "on_report gets the first report and the one after the fixes")
        self.assertTrue(gitconfig.read_text().endswith("# <<< MultiCred <<<\n"))
        if seen["shot"] is not None:
            self.assertTrue(seen["shot"].stat().st_size > 10_000)

    def test_a_consistent_system_and_no_active_profile(self):
        from multicred.core.engine.verifier import VerifyReport

        model = self.make_model()
        seen = {}

        def drive(page, window, reports, loop):
            if page.busy or not reports or settling(seen):
                return True
            if "settled" not in seen:
                seen["settled"], seen["wait"] = True, 3
                return True
            report = page.last_report
            seen["subtitle"] = page.subtitle_label.get_label()
            seen["expected"] = f"9 de 9 itens de acordo · verificado às {hhmm(report)}"
            seen["fix_all_sensitive"] = page.fix_all_button.get_sensitive()
            seen["buttons"] = [i for i, r in page.rows.items() if r.fix_button]
            seen["avatar"] = (page.avatar.get_visible(), page.avatar.get_label())
            save_screenshot(window, "S-06-verificar-ok.png")
            page._show(VerifyReport([], report.checked_at))
            seen["no_profile"] = (page.title_label.get_label(), page.avatar.get_visible())
            loop.quit()
            return False

        self.run_page(model, drive)
        self.assertEqual(seen["subtitle"], seen["expected"])
        self.assertFalse(seen["fix_all_sensitive"])
        self.assertEqual(seen["buttons"], [])
        self.assertEqual(seen["avatar"], (True, "T"))
        self.assertEqual(seen["no_profile"], ("Nenhum perfil ativo", False))

    def test_before_setup_the_page_says_so(self):
        model = self.make_model(save=False)
        seen = {}

        def drive(page, window, reports, loop):
            seen["state"] = page.stack.get_visible_child_name()
            seen["status"] = page.unconfigured.get_title()
            seen["title"] = page.title_label.get_label()
            loop.quit()
            return False

        self.run_page(model, drive, seconds=5)
        self.assertEqual(seen, {"state": "unconfigured", "status": "MultiCred ainda não foi configurado",
                                "title": "Nenhum perfil ativo"})

    def test_in_the_main_window_entrar_opens_the_login_and_abrir_chave_the_key(self):
        """The real window: "Entrar…" reaches its login entry point (recorded here, so no
        login flow starts), "Abrir chave…" shows Chaves de API with the key's editor."""
        from gi.repository import Gio
        from multicred.ui.app import MultiCredApplication

        self.set_folder_login(None)
        self.secrets.delete_item(self.gemini.secret_account)
        model = self.make_model()
        app = MultiCredApplication(model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        seen, logins = {}, []

        def step(window):
            page = window._section_pages.get("verify")
            if page is None or page.busy or page.last_report is None or settling(seen):
                return True
            if "login" not in seen:
                seen["login"] = page.rows["claude.login"].fix_button.get_label()
                page.rows["claude.login"].fix_button.emit("clicked")
                seen["wait"] = 2
                return True
            if "login_answered" not in seen:
                seen["login_answered"] = answer(page, "run")
                seen["logins"] = list(logins)
                page.rows[self.key_ids[1]].fix_button.emit("clicked")
                seen["wait"] = 2
                return True
            answer(page, "run")
            seen["section"] = window.current_section
            seen["top"] = type(window.nav.get_visible_page()).__name__
            app.quit()
            return False

        def activate(*_):
            window = app.window
            window.on_login_requested = lambda kind, account, host="github.com": logins.append((kind, account))
            window.open_page("verify")
            GLib.timeout_add(50, step, window)

        app.connect("activate", lambda *_: GLib.idle_add(lambda: activate() and False))
        GLib.timeout_add_seconds(20, app.quit)
        app.run([])
        self.assertEqual(seen.get("login"), "Entrar…")
        self.assertEqual(seen.get("logins"), [("folder", self.claude_dir)])
        self.assertEqual((seen.get("section"), seen.get("top")), ("keys", "APIKeyEditorPage"))

    def test_page_fixes_open_the_login_or_the_key_in_the_window(self):
        """VerifyPage.swift:155-158 through the Linux window's entry points."""
        from multicred.core.engine.verifier import Fix, FixKind
        from multicred.core.models import ClaudeAccount, ProfileColor
        from multicred.ui.pages.verify import open_fix_in_window

        marco = ClaudeAccount(nickname="Marco", color=ProfileColor.TEAL)
        self.config.claude_accounts = [marco]
        calls = []

        class FakeKeysPage:
            def open_editor(self, item_id):
                calls.append(("editor", item_id))

        class FakeWindow:
            model = type("M", (), {"config": self.config})()

            def on_login_requested(self, kind, account, host="github.com"):
                calls.append(("login", kind, account if kind != "github" else host))

            def show_section(self, name):
                calls.append(("section", name))
                return FakeKeysPage()

        window = FakeWindow()
        for kind, target in ((FixKind.LOGIN_CLAUDE, marco.id), (FixKind.LOGIN_CLAUDE_FOLDER, self.claude_dir),
                             (FixKind.LOGIN_GITHUB, "github.com"), (FixKind.OPEN_API_KEY, self.gemini.id)):
            self.assertTrue(open_fix_in_window(window, Fix(kind.value, "", kind=kind, target=target)))
        self.assertEqual(calls, [("login", "login", marco), ("login", "folder", self.claude_dir),
                                 ("login", "github", "github.com"), ("section", "keys"),
                                 ("editor", self.gemini.id)])
        self.assertFalse(open_fix_in_window(object(), Fix("x", "", kind=FixKind.LOGIN_GITHUB, target="github.com")))


if __name__ == "__main__":
    unittest.main()
