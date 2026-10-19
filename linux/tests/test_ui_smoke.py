"""S-06: the main window (sidebar, Perfis, profile editor, Contas) on a seeded sandbox.

Builds the window in test mode and walks every page, failing on any GTK/libadwaita
CRITICAL or WARNING. Saves an edited profile, adds an API key (value only in the
in-memory secret store), and checks Ctrl+2. With MULTICRED_SCREENSHOT_DIR set it
also writes PNGs of the main page, the editor and Contas.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import contextlib
import json
import os
import re
import shutil
import sys
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

LOG_DOMAINS = ("Gtk", "Adw", "Adwaita", "Gdk", "Gsk", "GLib-GObject", "GLib", "Gio")
LOG_LINE = re.compile(r"\b(Gtk|Adw|Adwaita|Gdk|Gsk|GLib-GObject|GLib|Gio)-(WARNING|CRITICAL)\b")
SECRET = "gm-valor-secreto-123"


@contextlib.contextmanager
def captured_stderr():
    """Redirects file descriptor 2 to a temporary file; yields a function returning its
    lines (echoed back to the real stderr so they stay visible in the test output)."""
    lines = []
    sys.stderr.flush()
    saved = os.dup(2)
    with tempfile.TemporaryFile() as sink:
        os.dup2(sink.fileno(), 2)
        try:
            yield lambda: lines
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)
            sink.seek(0)
            text = sink.read().decode("utf-8", "replace")
            sys.stderr.write(text)
            lines.extend(text.splitlines())


class SandboxTools:
    """git/gh/claude "found" at fake paths: the dry-run runner never executes them."""

    def locate(self, tool):
        return f"/fake/bin/{tool.value}"

    def search_directories(self):
        return ["/fake/bin"]


def seeded_config(environment):
    from multicred.core.models import (APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config,
                                       GitHubAccount, GitIdentity, Profile, ProfileColor)
    work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
    personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.dev", color=ProfileColor.ORANGE,
                           ssh_key_path="~/.ssh/id_pessoal")
    gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    gh_personal = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE,
                                claude_co_author=False)
    empresa = ClaudeAccount(nickname="Empresa", email="dev@empresa.com.br", color=ProfileColor.INDIGO,
                            kind=ClaudeAccountKind.LOGIN)
    cliente = ClaudeAccount(nickname="Cliente X", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
    openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
    gemini = APIKeyAccount(nickname="Empresa", provider=APIProvider.GEMINI, color=ProfileColor.INDIGO)
    folder = str(environment.home / ".claude-empresa")
    return Config(
        git_identities=[work, personal], github_accounts=[gh_work, gh_personal],
        claude_accounts=[empresa, cliente], api_keys=[openai, gemini],
        profiles=[
            Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work.id, github_account_id=gh_work.id,
                    claude_config_dir=folder, api_key_ids=[openai.id, gemini.id]),
            Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=personal.id,
                    github_account_id=gh_personal.id),
            Profile(name="Cliente X", color=ProfileColor.TEAL, git_identity_id=work.id, claude_config_dir=folder,
                    claude_account_id=cliente.id),
        ])


def pause(ticks=10):
    """A step that waits `ticks` main-loop ticks (page transitions, toasts going away)."""
    left = [ticks]

    def waiting(app):
        if left[0] == ticks:
            app.window.dismiss_toast()  # keep screenshots free of toasts
        left[0] -= 1
        return left[0] > 0

    return waiting


def save_png(window, path):
    """Renders the window with its own renderer (no screen grab needed)."""
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    if node is None:
        return False  # a redraw is pending; try again on the next tick
    texture = window.get_renderer().render_texture(node, None)
    if not texture.save_to_png(str(path)):
        raise AssertionError(f"could not write {path}")
    return True


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class MainWindowSmokeTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-smoke-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        (self.sandbox / ".claude-empresa").mkdir(mode=0o700)
        self.config = seeded_config(self.environment)
        for key in self.config.api_keys:
            self.environment.secrets.set_secret(b"sk-seed", key.secret_account)
        ConfigRepository(self.environment).save(self.config)
        self.config_path = self.sandbox / ".config/multicred/config.json"
        self.model = AppModel(self.environment, SandboxTools(), dispatch=GLib.idle_add)
        self.problems = []
        handlers = []
        for domain in LOG_DOMAINS:
            handlers.append((domain, GLib.log_set_handler(
                domain, GLib.LogLevelFlags.LEVEL_WARNING | GLib.LogLevelFlags.LEVEL_CRITICAL,
                self._log, None)))
        self.addCleanup(lambda: [GLib.log_remove_handler(d, h) for d, h in handlers])
        self.screens = os.environ.get("MULTICRED_SCREENSHOT_DIR")
        self.screenshot_tries = {}

    def _log(self, domain, level, message, _data=None):
        self.problems.append(f"{domain}: {message}")

    def screenshot(self, window, name):
        """Returns True while the window has nothing rendered yet (the step runs again)."""
        if not self.screens:
            return False
        Path(self.screens).mkdir(parents=True, exist_ok=True)
        tries = self.screenshot_tries.get(name, 0)
        self.screenshot_tries[name] = tries + 1
        if save_png(window, Path(self.screens) / f"S-06-{name}.png"):
            return False
        if tries >= 20:
            raise AssertionError(f"nothing rendered for {name}")
        return True

    def run_steps(self, steps, seconds=40):
        """Runs `steps` one after another on the main loop. A step returns True to be
        called again (waiting for something), False/None to move on."""
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
            except Exception as error:  # report after the loop ends
                import traceback
                self.failure = traceback.format_exc()
                app.quit()
                return False
            if not again:
                queue.pop(0)
            return True

        app.connect("activate", lambda *_: GLib.timeout_add(60, tick))
        GLib.timeout_add_seconds(seconds, app.quit)  # never hang the suite
        with captured_stderr() as output:
            app.run([])
        # GTK 4 and libadwaita log through g_log_structured, which bypasses
        # GLib.log_set_handler and goes straight to stderr: scan what was printed.
        self.problems += [line for line in output() if LOG_LINE.search(line)]
        self.assertIsNone(self.failure, self.failure)
        self.assertEqual(queue, [], f"steps left: {[s.__name__ for s in queue]}")

    def saved(self):
        return json.loads(self.config_path.read_text())

    def test_every_page_renders_and_edits_save(self):
        from multicred.ui.pages.claude import ClaudeFolderDetailPage

        model, seen = self.model, {}

        def not_switching(_app):
            return model.switching

        def check_shortcuts(app):
            window = app.window
            window.set_default_size(1100, 760)
            seen["section"] = window.current_section
            seen["accel2"] = app.get_actions_for_accel("<Control>2")
            seen["accel_r"] = app.get_actions_for_accel("<Control>r")
            seen["sidebar"] = window.sidebar_titles()
            seen["empty_rows"] = [row.get_title() for row in window.rows]
            _ok, name, target = Gio.Action.parse_detailed_name(seen["accel2"][0])
            seen["ctrl2"] = (name, target)
            window.activate_action(name, target)

        def after_ctrl2(app):
            seen["active_after_ctrl2"] = model.active_profile.name
            window = app.window
            window.activate_action("win.activate-profile", GLib.Variant("i", 1))

        def main_page(app):
            window = app.window
            page = window.profiles_page
            seen["hero"] = page.hero_title()
            seen["others"] = [row.get_title() for row in page.other_rows]
            seen["detail"] = page.detail_texts()
            seen["claude_only"] = page.claude_only_options()
            seen["footer"] = page.footer_label.get_label()
            seen["rows"] = [row.get_title() for row in window.rows]

        def shot_main(app):
            return self.screenshot(app.window, "perfis")

        def accounts(app):
            window = app.window
            window.open_page("accounts")
            seen["accounts_section"] = window.current_section
            # S-02: Git, GitHub and Chaves de API are pages of their own.
            seen["tab_rows"] = {}
            for name in ("git", "github", "keys"):
                window.open_page(name)
                seen["tab_rows"][name] = [row.info()["title"] for row in window.nav.get_visible_page().rows]
            window.open_page("accounts")
            page = window.nav.get_visible_page()
            seen["tab_rows"]["claude"] = [row.info()["title"] for row in page.account_rows]
            seen["folders"] = [row.info()["title"] for row in page.folder_rows]

        def shot_accounts(app):
            return self.screenshot(app.window, "contas")

        def git_tab(app):
            app.window.open_page("git")

        def shot_accounts_git(app):
            return self.screenshot(app.window, "contas-git")

        def editor(app):
            window = app.window
            window.open_page("main")
            editor = window.show_profile_editor(self.config.profiles[0].id)
            seen["editor_title"] = editor.get_title()
            seen["editor_keys"] = editor.key_providers()
            seen["editor_git"] = editor.git_row.get_selected()

        def shot_editor(app):
            return self.screenshot(app.window, "editor")

        def save_editor(app):
            editor = app.window.nav.get_visible_page()
            editor.name_entry.set_text("Empresa")
            editor.color_buttons["teal"].set_active(True)
            editor.save()
            seen["after_save_page"] = (app.window.nav.get_visible_page() is editor, editor.get_title())

        def wait_saved_switch(_app):
            return model.switching

        def new_profile(app):
            window = app.window
            window.open_page("new-profile")
            editor = window.nav.get_visible_page()
            seen["new_title"] = editor.get_title()
            editor.save()  # empty name
            seen["name_error"] = editor.error_text("name")
            editor.name_entry.set_text("Cliente Y")
            editor.save()
            seen["profiles_after_new"] = [p["name"] for p in self.saved()["profiles"]]

        def placeholders(app):
            window = app.window
            for name in ("verify", "settings"):
                window.open_page(name)
                seen[f"{name}_title"] = window.nav.get_visible_page().get_title()
                seen[f"{name}_section"] = window.current_section

        def add_key(app):
            window = app.window
            window.open_page("keys")
            page = window.nav.get_visible_page()
            page.add_button.emit("clicked")
            editor = window.nav.get_visible_page()
            seen["key_editor"] = editor.get_title()
            editor.nickname_row.set_text("Pessoal")
            editor.set_provider("gemini")
            seen["default_var"] = editor.env_var_row.get_text()
            editor.secret_row.set_text(SECRET)
            editor.save()
            seen["key_rows"] = [row.info()["title"] for row in page.rows]
            new_key = next(k for k in saved_keys(self.saved()) if k["nickname"] == "Pessoal")
            seen["new_key_var"] = new_key["envVar"]
            seen["new_key_secret"] = self.environment.secrets.stored_value(new_key["id"])
            seen["secret_on_disk"] = [str(p) for p in self.sandbox.rglob("*")
                                      if p.is_file() and SECRET.encode() in p.read_bytes()]

        def git_error(app):
            window = app.window
            window.open_page("git")
            window.nav.get_visible_page().add_button.emit("clicked")
            editor = window.nav.get_visible_page()
            editor.nickname_row.set_text("Novo")
            editor.name_row.set_text("Fulano")
            editor.email_row.set_text("sem-arroba")
            editor.save()
            seen["email_error"] = editor.error_text("email")
            window.nav.pop()

        def github_editor(app):
            window = app.window
            window.open_page("github")
            editor = window.nav.get_visible_page().open_editor(self.config.github_accounts[1].id)
            seen["coauthor"] = editor.coauthor_row.get_active()
            window.nav.pop()
            window.open_page("accounts")
            window.nav.get_visible_page().add_button.emit("clicked")
            # S-04: a page pushed on Claude, as on the Mac (no dialog).
            seen["login_form"] = window.nav.get_visible_page() is window.login_page is not None
            if seen["login_form"]:
                window.nav.pop()

        def other_actions(app):
            window = app.window
            folder = window.push(ClaudeFolderDetailPage(
                window, str(self.sandbox / ".claude-empresa")))
            seen["folder_page"] = folder.get_title()
            window.nav.pop()
            new_key = next(k for k in model.config.api_keys if k.nickname == "Pessoal")
            # S-02: like the Mac, the value is replaced and the key removed in its editor.
            window.open_page("keys")
            key_editor = window.nav.get_visible_page().open_editor(new_key.id)
            key_editor.remove_button.emit("clicked")  # the confirmation opens and is dismissed below
            seen["dialog"] = window.get_visible_dialog() is not None
            window.get_visible_dialog().force_close()
            key_editor._remove()
            seen["key_secret_after_remove"] = self.environment.secrets.stored_value(new_key.secret_account)
            editor = window.show_profile_editor(model.config.profiles[-1].id)
            editor.delete()
            seen["profiles_after_delete"] = [p["name"] for p in self.saved()["profiles"]]

        def onboarding_still_works(app):
            app.open_page("onboarding")
            seen["onboarding"] = app.window.onboarding is not None
            seen["windows"] = len(app.get_windows())

        self.run_steps([check_shortcuts, not_switching, after_ctrl2, not_switching, main_page, pause(), shot_main,
                        accounts, pause(), shot_accounts, git_tab, pause(), shot_accounts_git, editor, pause(),
                        shot_editor, save_editor, wait_saved_switch, new_profile, placeholders, add_key, git_error, github_editor,
                        other_actions, onboarding_still_works])

        self.assertEqual(seen["section"], "main")
        self.assertEqual(seen["sidebar"], ["Visão geral", "Trabalho", "Pessoal", "Cliente X", "Novo perfil", "Git",
                                           "GitHub", "Claude", "Chaves de API", "Verificar", "Ajustes"])
        self.assertEqual(seen["empty_rows"], ["Trabalho", "Pessoal", "Cliente X"], "no active profile yet")
        self.assertEqual(seen["accel2"], ["win.activate-profile(2)"])
        self.assertEqual(seen["accel_r"], ["win.verify"])
        self.assertEqual(seen["active_after_ctrl2"], "Pessoal", "Ctrl+2 activates the second profile")

        self.assertEqual(seen["hero"], "Trabalho")
        self.assertEqual(seen["others"], ["Pessoal", "Cliente X"])
        self.assertEqual(seen["rows"], ["Trabalho", "Pessoal", "Cliente X"])
        self.assertIn("Dev Trabalho · dev@empresa.com.br", seen["detail"]["git"])
        self.assertIn("dev-empresa", seen["detail"]["GitHub"])
        self.assertIn("~/.claude-empresa", seen["detail"]["Claude"])
        self.assertIn("OPENAI_API_KEY", seen["detail"]["Chaves"])
        self.assertEqual(seen["claude_only"], ["Conta do perfil (login da pasta)", "Empresa", "Cliente X"])
        self.assertEqual(seen["footer"], "Verificar")

        self.assertEqual(seen["accounts_section"], "claude", "Contas… opens the Claude entry, as on the Mac")
        self.assertEqual(seen["tab_rows"]["git"], ["Trabalho", "Pessoal"])
        self.assertEqual(seen["tab_rows"]["github"], ["Empresa", "Pessoal"])
        self.assertEqual(seen["tab_rows"]["claude"], ["Empresa", "Cliente X"])
        self.assertEqual(seen["tab_rows"]["keys"], ["Empresa", "Empresa"])
        self.assertEqual(seen["folders"], ["~/.claude", "~/.claude-empresa"])

        self.assertEqual(seen["editor_title"], "Editar perfil")
        self.assertEqual(seen["editor_keys"], ["OpenAI", "Gemini"])
        self.assertEqual(seen["editor_git"], 1, "Nenhuma, then the identities")
        self.assertEqual(seen["after_save_page"], (True, "Editar perfil"), "saving stays in the editor (Mac)")
        saved = self.saved()
        self.assertEqual(saved["profiles"][0]["name"], "Empresa")
        self.assertEqual(saved["profiles"][0]["color"], "teal")
        self.assertEqual(saved["profiles"][0]["icon"], {"letter": {"_0": "E"}})

        self.assertEqual(seen["new_title"], "Novo perfil")
        self.assertEqual(seen["name_error"], "Dê um nome ao perfil.")
        self.assertEqual(seen["profiles_after_new"], ["Empresa", "Pessoal", "Cliente X", "Cliente Y"])

        self.assertEqual((seen["verify_title"], seen["verify_section"]), ("Verificar", "verify"))
        self.assertEqual((seen["settings_title"], seen["settings_section"]), ("Ajustes", "settings"))

        self.assertEqual(seen["key_editor"], "Nova chave de API")
        self.assertEqual(seen["default_var"], "GEMINI_API_KEY")
        self.assertEqual(seen["key_rows"], ["Empresa", "Empresa", "Pessoal"])
        self.assertEqual(seen["new_key_var"], "GEMINI_API_KEY")
        self.assertEqual(seen["new_key_secret"], SECRET.encode())
        self.assertEqual(seen["secret_on_disk"], [], "the key value is only in the secret store")

        self.assertEqual(seen["email_error"], "E-mail inválido.")
        self.assertFalse(seen["coauthor"])
        self.assertTrue(seen["login_form"], "Adicionar conta Claude… asks for the new account")
        self.assertEqual(seen["folder_page"], "~/.claude-empresa")
        self.assertTrue(seen["dialog"], "Remover in the key editor asks first")
        self.assertIsNone(seen["key_secret_after_remove"], "removing a key deletes its secret")
        self.assertEqual(seen["profiles_after_delete"], ["Empresa", "Pessoal", "Cliente X"])
        self.assertTrue(seen["onboarding"])
        self.assertEqual(seen["windows"], 1)
        self.assertEqual(self.problems, [], "GTK/libadwaita criticals or warnings")


def saved_keys(saved):
    return saved["apiKeys"]


if __name__ == "__main__":
    unittest.main()
