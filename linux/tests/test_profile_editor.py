"""S-05 (R-05): the profile editor is laid out and behaves like the Mac's (ProfileEditorPage.swift).

Header with the 64 px badge, "No painel:" + "Ctrl+N no menu" and the preview (badge +
HH:MM); Nome, Cor and Ícone (the letter or one of the 8 symbols of Components.swift:65);
"Composição" with Git, GitHub, the Claude folder as a list of the known folders with
their conversation counts + "Nova pasta…", the Claude account as "apelido · e-mail"
with a color dot, the "A pasta X não tem login." warning + "Entrar…", and OpenAI and
Gemini always; footer "Apagar perfil / Ativar este perfil / Cancelar / Salvar". Saving
stays in the editor, deleting goes to the first profile. With MULTICRED_SCREENS_DIR set,
the editor is rendered (stock light Adwaita) into S-05-*.png.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
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

SYMBOLS = ["briefcase", "house", "building.2", "laptopcomputer", "star", "person", "flask", "globe"]


class NoTools:
    """Nothing found: no background gh/claude lookups change the crafted states."""

    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


class SandboxTools:
    """git/gh/claude "found" at fake paths: the dry-run runner never executes them."""

    def locate(self, tool):
        return f"/fake/bin/{tool.value}"

    def search_directories(self):
        return ["/fake/bin"]


def crafted_config(environment, keys=True):
    from multicred.core.models import (APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config,
                                       GitHubAccount, GitIdentity, Profile, ProfileColor, ProfileIcon)
    work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.example", color=ProfileColor.INDIGO)
    personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.example",
                           color=ProfileColor.ORANGE)
    gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    empresa = ClaudeAccount(nickname="Empresa", email="dev@empresa.example", color=ProfileColor.INDIGO,
                            kind=ClaudeAccountKind.LOGIN)
    cliente = ClaudeAccount(nickname="Cliente X", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
    openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
    anthropic = APIKeyAccount(nickname="Pessoal", provider=APIProvider.ANTHROPIC, color=ProfileColor.ORANGE)
    folder = str(environment.home / ".claude-empresa")
    return Config(
        git_identities=[work, personal], github_accounts=[gh_work],
        claude_accounts=[empresa, cliente], api_keys=[openai, anthropic] if keys else [],
        profiles=[
            Profile(name="Trabalho", color=ProfileColor.INDIGO, icon=ProfileIcon("symbol", "briefcase"),
                    git_identity_id=work.id, github_account_id=gh_work.id, claude_config_dir=folder,
                    claude_account_id=empresa.id, api_key_ids=[openai.id] if keys else []),
            Profile(name="Pessoal", color=ProfileColor.ORANGE, icon=ProfileIcon("letter", "P"),
                    git_identity_id=personal.id),
            Profile(name="Cliente X", color=ProfileColor.TEAL, icon=ProfileIcon("letter", "C"),
                    git_identity_id=work.id, claude_config_dir=folder, claude_account_id=cliente.id),
        ])


def conversation(folder: Path, project: str, name: str) -> None:
    directory = folder / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.jsonl").write_text('{"type":"ai-title","aiTitle":"Uma conversa"}\n')


def save_png(window, path):
    """Renders the window with its own renderer (never a screen grab)."""
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    if node is None:
        return False
    texture = window.get_renderer().render_texture(node, None)
    if not texture.save_to_png(str(path)):
        raise AssertionError(f"could not write {path}")
    return True


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class ProfileEditorTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-editor-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        (self.sandbox / ".claude").mkdir(mode=0o700, exist_ok=True)
        (self.sandbox / ".claude-empresa").mkdir(mode=0o700)
        (self.sandbox / ".claude-antiga").mkdir(mode=0o700)  # discovered, used by no profile
        conversation(self.sandbox / ".claude", "-home-dev-app", "a")
        conversation(self.sandbox / ".claude", "-home-dev-app", "b")
        conversation(self.sandbox / ".claude-empresa", "-home-dev-api", "c")

    def make_model(self, config, tools=None):
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        ConfigRepository(self.environment).save(config)
        model = AppModel(self.environment, tools or NoTools(), dispatch=GLib.idle_add)
        # Folder login statuses are crafted by the tests, never looked up.
        model.refresh_claude_statuses = lambda on_done=None: None
        return model

    def saved(self):
        return json.loads((self.environment.app_support_directory / "config.json").read_text())

    def run_steps(self, model, steps, seconds=30):
        """Runs `steps` one after another on the main loop; a step returns True to run again."""
        from multicred.ui.app import MultiCredApplication

        app = MultiCredApplication(model, flags=Gio.ApplicationFlags.NON_UNIQUE)
        queue = list(steps)
        self.failure = None

        def tick():
            if not queue:
                app.quit()
                return False
            try:
                again = queue[0](app)
            except Exception:  # reported after the loop ends
                import traceback
                self.failure = traceback.format_exc()
                app.quit()
                return False
            if not again:
                queue.pop(0)
            return True

        app.connect("activate", lambda *_: GLib.timeout_add(40, tick))
        GLib.timeout_add_seconds(seconds, app.quit)  # never hang the suite
        app.run([])
        self.assertIsNone(self.failure, self.failure)
        self.assertEqual(queue, [], f"steps left: {[getattr(s, '__name__', s) for s in queue]}")
        return app

    @staticmethod
    def mark_active(model, profile_id):
        """A step: the app loaded state.json at startup, so the crafted state goes in after it."""
        def step(_app):
            model.state.profile_id = profile_id
            model.state.activated_at = datetime.now(timezone.utc)
            model.notify()
        return step

    @staticmethod
    def until(condition):
        """A step that waits (up to the run's timeout) until `condition(app)` holds."""
        def step(app):
            return not condition(app)
        return step

    # MARK: layout

    def test_layout_header_footer_and_composition_like_the_mac(self):
        config = crafted_config(self.environment)
        model = self.make_model(config)
        work, personal, client = config.profiles
        seen = {}

        def existing(app):
            window = app.window
            editor = window.show_profile_editor(work.id)
            seen["title"] = editor.get_title()
            seen["header"] = editor.header_texts()
            seen["now"] = datetime.now().strftime("%H:%M")
            seen["footer"] = editor.footer_labels()
            seen["header_bar_has_save"] = editor.save_in_header_bar()
            seen["groups"] = editor.group_titles()
            seen["first_rows"] = editor.form_titles()
            seen["composition"] = editor.composition_titles()
            seen["icons"] = editor.icon_choices()
            seen["icon_selected"] = editor.selected_icon()
            seen["git"] = editor.options("git")
            seen["github"] = editor.options("github")
            seen["claude"] = editor.options("claude")
            seen["openai"] = editor.options("openai")
            seen["gemini"] = editor.options("gemini")
            seen["selected"] = {name: editor.selected_option(name) for name in ("git", "github", "claude", "openai")}
            seen["legend"] = editor.claude_legend()
            seen["note"] = editor.footer_note()
            seen["subtitles"] = editor.composition_subtitles()

        def active(app):
            editor = app.window.show_profile_editor(personal.id)
            seen["active_footer"] = editor.footer_labels()
            seen["active_shortcut"] = editor.header_texts()["shortcut"]

        def new(app):
            window = app.window
            window.open_page("new-profile")
            editor = window.nav.get_visible_page()
            seen["new_title"] = editor.get_title()
            seen["new_header"] = editor.header_texts()
            seen["new_footer"] = editor.footer_labels()
            seen["new_save_enabled"] = editor.save_button.get_sensitive()
            seen["new_icons"] = editor.icon_choices()
            editor.name_entry.set_text("  ")
            seen["blank_save_enabled"] = editor.save_button.get_sensitive()
            editor.name_entry.set_text("freela")
            seen["typed_save_enabled"] = editor.save_button.get_sensitive()
            seen["typed_icons"] = editor.icon_choices()[0]
            seen["typed_header"] = editor.header_texts()["title"]

        self.run_steps(model, [self.mark_active(model, personal.id), existing, active, new])
        self.assertEqual(seen["title"], "Editar perfil")
        self.assertEqual(seen["header"], {"title": "Trabalho", "panel": "No painel:", "shortcut": "Ctrl+1 no menu",
                                          "preview_time": seen["now"], "preview_label": "Prévia no painel"})
        self.assertEqual(seen["footer"], ["Apagar perfil", "Ativar este perfil", "Cancelar", "Salvar"])
        self.assertFalse(seen["header_bar_has_save"], "Salvar is in the page's footer, as on the Mac")
        self.assertEqual(seen["groups"], ["", "Composição"])
        self.assertEqual(seen["first_rows"], ["Nome", "Cor", "Ícone"])
        self.assertEqual(seen["composition"], ["Git", "GitHub", "Pasta do Claude", "Conta Claude", "OpenAI",
                                               "Gemini", "Anthropic API"])
        self.assertEqual(seen["icons"], ["T"] + SYMBOLS)
        self.assertEqual(seen["icon_selected"], "briefcase")
        self.assertEqual(seen["git"], [("Nenhuma", None), ("Trabalho · dev@empresa.example", "indigo"),
                                       ("Pessoal · dev@pessoal.example", "orange")])
        self.assertEqual(seen["github"], [("Nenhuma", None), ("dev-empresa · github.com", "indigo")])
        self.assertEqual(seen["claude"], [("Login da própria pasta", None), ("Empresa · dev@empresa.example", "indigo"),
                                          ("Cliente X", "teal")])
        self.assertEqual(seen["openai"], [("Nenhuma", None), ("Empresa · OPENAI_API_KEY", "indigo")])
        self.assertEqual(seen["gemini"], [("Nenhuma", None)], "Gemini is always there, even without keys")
        self.assertEqual(seen["selected"], {"git": 1, "github": 1, "claude": 1, "openai": 1})
        self.assertEqual(seen["legend"], "A pasta guarda agents, skills e conversas. A conta é só o login: trocar a "
                                         "conta não muda a pasta.")
        self.assertIn("“Nenhuma” deixa aquele item como está", seen["note"])
        self.assertEqual(seen["subtitles"], [], "no \"vazio = …\" subtitles: the Mac has none")

        self.assertEqual(seen["active_footer"], ["Apagar perfil", "Cancelar", "Salvar"], "no Ativar on the active one")
        self.assertEqual(seen["active_shortcut"], "Ctrl+2 no menu")

        self.assertEqual(seen["new_title"], "Novo perfil")
        self.assertEqual(seen["new_header"]["title"], "Novo perfil")
        self.assertIsNone(seen["new_header"]["shortcut"], "a new profile has no Ctrl+N yet")
        self.assertEqual(seen["new_header"]["panel"], "No painel:")
        self.assertEqual(seen["new_footer"], ["Cancelar", "Salvar"])
        self.assertFalse(seen["new_save_enabled"], "Salvar is off while the name is empty")
        self.assertEqual(seen["new_icons"][0], "?")
        self.assertFalse(seen["blank_save_enabled"], "spaces only count as empty")
        self.assertTrue(seen["typed_save_enabled"])
        self.assertEqual(seen["typed_icons"], "F", "the letter choice follows the name")
        self.assertEqual(seen["typed_header"], "freela")

    def test_openai_and_gemini_always_and_no_shortcut_after_nine(self):
        from multicred.core.models import Profile, ProfileColor

        config = crafted_config(self.environment, keys=False)
        config.profiles += [Profile(name=f"Extra {n}", color=ProfileColor.GREEN) for n in range(7)]
        model = self.make_model(config)
        seen = {}

        def inspect(app):
            window = app.window
            editor = window.show_profile_editor(config.profiles[0].id)
            seen["composition"] = editor.composition_titles()
            seen["ninth"] = window.show_profile_editor(config.profiles[8].id).header_texts()["shortcut"]
            seen["tenth"] = window.show_profile_editor(config.profiles[9].id).header_texts()["shortcut"]

        self.run_steps(model, [inspect])
        self.assertEqual(seen["composition"], ["Git", "GitHub", "Pasta do Claude", "Conta Claude", "OpenAI", "Gemini"])
        self.assertEqual(seen["ninth"], "Ctrl+9 no menu")
        self.assertIsNone(seen["tenth"], "only the first nine profiles have a shortcut")

    # MARK: Claude folder and account

    def test_folder_list_with_conversations_and_new_folder(self):
        config = crafted_config(self.environment)
        model = self.make_model(config)
        work = config.profiles[0]
        seen = {}

        def open_editor(app):
            app.window.show_profile_editor(work.id)

        def counted(app):
            labels = [text for text, _color in app.window.nav.get_visible_page().options("folder")]
            return all("conversas" in text for text in labels[:-1])

        def inspect(app):
            editor = app.window.nav.get_visible_page()
            seen["folders"] = [text for text, _color in editor.options("folder")]
            seen["folder_selected"] = editor.selected_option("folder")
            editor.select_option("folder", len(seen["folders"]) - 1)  # Nova pasta…
            seen["path_shown"] = editor.new_folder_row.get_visible()
            seen["path_title"] = editor.new_folder_row.get_title()
            seen["path"] = editor.new_folder_row.get_text()
            seen["path_buttons"] = editor.new_folder_buttons()
            seen["snapped_back"] = editor.selected_option("folder")
            editor.new_folder_row.set_text("relativo")
            seen["created_bad"] = editor.create_folder()
            seen["bad_error"] = editor.error_text("new_folder")
            editor.new_folder_row.set_text("~/.claude-nova")
            seen["created"] = editor.create_folder()
            seen["path_hidden"] = not editor.new_folder_row.get_visible()
            seen["error_after"] = editor.error_text("new_folder")
            seen["draft_dir"] = editor.draft.claude_config_dir
            options = [text for text, _color in editor.options("folder")]
            seen["selected_after"] = options[editor.selected_option("folder")]
            editor.select_option("folder", len(options) - 1)
            editor.cancel_new_folder()
            seen["cancelled_hidden"] = not editor.new_folder_row.get_visible()
            editor.save()
            seen["saved_dir"] = self.saved()["profiles"][0]["claudeConfigDir"]

        self.run_steps(model, [open_editor, self.until(counted), inspect])
        self.assertEqual(seen["folders"], ["~/.claude (padrão) · 2 conversas", "~/.claude-antiga · 0 conversas",
                                           "~/.claude-empresa · 1 conversas", "Nova pasta…"])
        self.assertEqual(seen["folder_selected"], 2)
        self.assertTrue(seen["path_shown"])
        self.assertEqual(seen["path_title"], "Caminho")
        self.assertEqual(seen["path"], "~/.claude-trabalho")
        self.assertEqual(seen["path_buttons"], ["Criar", "Cancelar"])
        self.assertEqual(seen["snapped_back"], 2, "the list keeps showing the current folder")
        self.assertFalse(seen["created_bad"])
        self.assertEqual(seen["bad_error"], "Caminho inválido: relativo. Use um caminho absoluto ou começando com ~/.")
        self.assertTrue(seen["created"])
        self.assertTrue(seen["path_hidden"])
        self.assertIsNone(seen["error_after"])
        self.assertEqual(seen["draft_dir"], str(self.sandbox / ".claude-nova"))
        self.assertTrue((self.sandbox / ".claude-nova").is_dir())
        self.assertEqual((self.sandbox / ".claude-nova").stat().st_mode & 0o777, 0o700)
        self.assertEqual(seen["selected_after"], "~/.claude-nova · 0 conversas")
        self.assertTrue(seen["cancelled_hidden"])
        self.assertEqual(seen["saved_dir"], str(self.sandbox / ".claude-nova"))

    def test_folder_without_login_warning_and_entrar(self):
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.models import ClaudeFolder

        config = crafted_config(self.environment)
        model = self.make_model(config)
        personal = config.profiles[1]
        requested = []
        seen = {}

        def logged_out(app):
            app.window.on_login_requested = lambda kind, account: requested.append((kind, account))
            model.claude_statuses[ClaudeFolder.own_login(None).id] = ClaudeLoginStatus.logged_out()
            editor = app.window.show_profile_editor(personal.id)
            seen["warning"] = editor.folder_warning()
            editor.folder_login_button.emit("clicked")
            editor.select_option("claude", 1)
            seen["with_account"] = editor.folder_warning()
            editor.select_option("claude", 0)
            seen["back"] = editor.folder_warning()

        def logged_in(app):
            model.claude_statuses[ClaudeFolder.own_login(None).id] = ClaudeLoginStatus.logged_in("eu@casa.example")
            model.notify()

        def after_login(app):
            editor = app.window.nav.get_visible_page()
            seen["after_login"] = editor.folder_warning()
            seen["own_login"] = editor.options("claude")[0]

        self.run_steps(model, [logged_out, logged_in, after_login])
        self.assertEqual(seen["warning"], ("A pasta ~/.claude não tem login.", "Entrar…"))
        self.assertEqual(requested, [("folder", None)], "Entrar… opens the folder's login")
        self.assertIsNone(seen["with_account"], "an account was chosen: the folder's login does not matter")
        self.assertEqual(seen["back"], ("A pasta ~/.claude não tem login.", "Entrar…"))
        self.assertIsNone(seen["after_login"])
        self.assertEqual(seen["own_login"], ("Login da própria pasta · eu@casa.example", None))

    # MARK: save, activate, cancel, delete

    def test_save_stays_activate_switches_and_delete_goes_to_the_first_profile(self):
        from multicred.ui.pages.profile_editor import ProfileEditorPage

        config = crafted_config(self.environment)
        for key in config.api_keys:
            self.environment.secrets.set_secret(b"sk-seed", key.secret_account)
        self.environment.secrets.set_secret(b"tok", config.claude_accounts[1].secret_account)
        model = self.make_model(config, SandboxTools())  # the switch needs gh "found"
        work, personal, client = config.profiles
        seen = {}

        def edit_and_save(app):
            window = app.window
            editor = window.show_profile_editor(personal.id)
            editor.name_entry.set_text("casa")
            editor.color_buttons["green"].set_active(True)
            editor.select_icon("house")
            seen["preview_icon"] = editor.badge_glyph()
            seen["saved_ok"] = editor.save()
            visible = window.nav.get_visible_page()
            seen["after_save"] = (visible is editor, visible.get_title(), visible.draft.id == personal.id)
            seen["selected_after_save"] = window.selected_sidebar_key()
            seen["saved"] = self.saved()["profiles"][1]
            seen["switching_after_save"] = model.switching

        def letter_from_name(app):
            editor = app.window.show_profile_editor(client.id)
            editor.name_entry.set_text("empresa X")
            editor.select_icon("letter")
            editor.save()
            seen["letter_icon"] = self.saved()["profiles"][2]["icon"]
            seen["letter_name"] = self.saved()["profiles"][2]["name"]

        def activate(app):
            editor = app.window.show_profile_editor(personal.id)
            seen["activate_shown"] = editor.activate_button.get_visible()
            editor.activate_button.emit("clicked")
            seen["activate_disabled_while_switching"] = not editor.activate_button.get_sensitive()

        def wait(_app):
            return model.switching

        def activated(app):
            editor = app.window.nav.get_visible_page()
            seen["active"] = model.state.profile_id
            seen["activate_after"] = editor.activate_button.get_visible()
            seen["toast"] = app.window.last_toast

        def cancel_existing(app):
            editor = app.window.show_profile_editor(work.id)
            editor.name_entry.set_text("Outro nome")
            editor.select_icon("star")
            editor.cancel_button.emit("clicked")
            visible = app.window.nav.get_visible_page()
            seen["cancel_existing"] = (visible.name_entry.get_text(), visible.selected_icon(),
                                       visible.draft.id == work.id)

        def cancel_new(app):
            window = app.window
            window.open_page("new-profile")
            window.nav.get_visible_page().cancel_button.emit("clicked")
            visible = window.nav.get_visible_page()
            seen["cancel_new"] = (isinstance(visible, ProfileEditorPage) and not visible.is_new
                                  and visible.draft.id == work.id, window.selected_sidebar_key())

        def save_new(app):
            window = app.window
            window.open_page("new-profile")
            editor = window.nav.get_visible_page()
            editor.name_entry.set_text("Freela")
            editor.select_icon("globe")
            editor.save()
            visible = window.nav.get_visible_page()
            new_id = model.config.profiles[-1].id
            seen["save_new"] = (isinstance(visible, ProfileEditorPage), visible.is_new, visible.get_title(),
                                visible.draft.id == new_id, window.selected_sidebar_key() == f"profile:{new_id}")
            seen["new_saved"] = self.saved()["profiles"][-1]["icon"]

        def delete(app):
            window = app.window
            editor = window.show_profile_editor(client.id)
            editor.delete()
            visible = window.nav.get_visible_page()
            seen["after_delete"] = (isinstance(visible, ProfileEditorPage) and visible.draft.id == work.id,
                                    window.selected_sidebar_key())
            seen["names_after_delete"] = [p["name"] for p in self.saved()["profiles"]]

        self.run_steps(model, [self.mark_active(model, work.id), edit_and_save, letter_from_name, activate, wait,
                               activated, cancel_existing, cancel_new, save_new, delete])
        self.assertEqual(seen["preview_icon"], "house")
        self.assertTrue(seen["saved_ok"])
        self.assertEqual(seen["after_save"], (True, "Editar perfil", True), "saving stays in the editor")
        self.assertEqual(seen["selected_after_save"], f"profile:{personal.id}")
        self.assertEqual(seen["saved"]["name"], "casa")
        self.assertEqual(seen["saved"]["color"], "green")
        self.assertEqual(seen["saved"]["icon"], {"symbol": {"_0": "house"}})
        self.assertFalse(seen["switching_after_save"], "saving a profile that is not active does not switch")
        self.assertEqual(seen["letter_icon"], {"letter": {"_0": "E"}}, "the letter comes from the name")
        self.assertEqual(seen["letter_name"], "empresa X")
        self.assertTrue(seen["activate_shown"])
        self.assertTrue(seen["activate_disabled_while_switching"])
        self.assertEqual(seen["active"], personal.id, f"Ativar este perfil switches ({seen['toast']})")
        self.assertFalse(seen["activate_after"], "the active profile has no Ativar button")
        self.assertEqual(seen["cancel_existing"], ("Trabalho", "briefcase", True), "Cancelar reloads the profile")
        self.assertEqual(seen["cancel_new"], (True, f"profile:{work.id}"), "Cancelar on a new profile: first profile")
        self.assertEqual(seen["save_new"], (True, False, "Editar perfil", True, True))
        self.assertEqual(seen["new_saved"], {"symbol": {"_0": "globe"}})
        self.assertEqual(seen["after_delete"], (True, f"profile:{work.id}"), "deleting goes to the first profile")
        self.assertEqual(seen["names_after_delete"], ["Trabalho", "casa", "Freela"])

    def test_deleting_the_only_profile_goes_to_claude(self):
        config = crafted_config(self.environment)
        config.profiles = config.profiles[:1]
        model = self.make_model(config)
        seen = {}

        def delete(app):
            window = app.window
            window.show_profile_editor(config.profiles[0].id).delete()
            seen["after"] = (window.current_section, window.selected_sidebar_key())

        self.run_steps(model, [delete])
        self.assertEqual(seen["after"], ("claude", "claude"))

    # MARK: screenshots

    def test_screenshots(self):
        directory = os.environ.get("MULTICRED_SCREENS_DIR")
        if not directory:
            self.skipTest("MULTICRED_SCREENS_DIR not set")
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.models import ClaudeFolder

        Path(directory).mkdir(parents=True, exist_ok=True)
        config = crafted_config(self.environment)
        model = self.make_model(config)
        tries = {}

        def setup(app):
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            app.window.set_default_size(980, 660)
            model.state.profile_id = config.profiles[1].id
            model.claude_statuses[ClaudeFolder.own_login(None).id] = ClaudeLoginStatus.logged_out()
            model.notify()
            app.window.show_profile_editor(config.profiles[0].id)

        def wait(ticks):
            left = [ticks]

            def step(app):
                if left[0] == ticks:
                    app.window.dismiss_toast()
                left[0] -= 1
                return left[0] > 0
            return step

        def shot(name):
            def step(app):
                tries[name] = tries.get(name, 0) + 1
                if save_png(app.window, Path(directory) / f"S-05-{name}.png"):
                    return False
                if tries[name] > 20:
                    raise AssertionError(f"nothing rendered for {name}")
                return True
            return step

        def new_profile(app):
            app.window.open_page("new-profile")

        def validation(app):
            editor = app.window.nav.get_visible_page()
            options = editor.options("folder")
            editor.select_option("folder", len(options) - 1)
            editor.new_folder_row.set_text("pasta-relativa")
            editor.create_folder()

        def folder_without_login(app):
            app.window.show_profile_editor(config.profiles[1].id)

        def scroll_to(row_name):
            def step(app):
                gi.require_version("Graphene", "1.0")
                from gi.repository import Graphene
                editor = app.window.nav.get_visible_page()
                viewport = editor.scroller.get_child()
                found, point = getattr(editor, row_name).compute_point(viewport, Graphene.Point())
                self.assertTrue(found)
                editor.scroller.get_vadjustment().set_value(point.y - 140)
            return step

        self.run_steps(model, [setup, wait(25), shot("editor"), new_profile, wait(20), shot("novo"),
                               validation, wait(10), scroll_to("new_folder_row"), wait(10), shot("validacao"),
                               folder_without_login, wait(15), scroll_to("warning_row"), wait(10),
                               shot("pasta-sem-login")])
        for name in ("editor", "novo", "validacao", "pasta-sem-login"):
            self.assertTrue((Path(directory) / f"S-05-{name}.png").exists())


if __name__ == "__main__":
    unittest.main()
