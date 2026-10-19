"""S-01 (R-01): the main window's sidebar is structured like the Mac's (MainWindowView.swift:35-100).

Sections "Perfis" (Linux extra "Visão geral", one row per profile with ✓ on the active
one, "Novo perfil"), "Contas" (Git, GitHub, Claude, Chaves de API with a count and an
orange ⚠ by the Mac rule) and "Sistema" (Verificar with a status dot, Ajustes).
Clicking a profile opens its editor and never switches; the dashboard rows no longer
switch either (the "Ativar" button does). With MULTICRED_SCREENS_DIR set, the window
is rendered (stock light Adwaita) into S-01-*.png.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import os
import shutil
import tempfile
import unittest
import uuid
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
        from gi.repository import Adw, Gdk, Gio, GLib, Gtk
        HAS_DISPLAY = Gtk.init_check()
    except (ImportError, ValueError):
        HAS_DISPLAY = False


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


def crafted_config(environment, profiles=True):
    from multicred.core.models import (APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config,
                                       GitHubAccount, GitIdentity, Profile, ProfileColor)
    work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.example", color=ProfileColor.INDIGO)
    personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.example",
                           color=ProfileColor.ORANGE)
    gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    gh_personal = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE)
    empresa = ClaudeAccount(nickname="Empresa", email="dev@empresa.example", color=ProfileColor.INDIGO,
                            kind=ClaudeAccountKind.LOGIN)
    cliente = ClaudeAccount(nickname="Cliente X", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
    openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
    gemini = APIKeyAccount(nickname="Pessoal", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
    folder = str(environment.home / ".claude-empresa")
    return Config(
        git_identities=[work, personal], github_accounts=[gh_work, gh_personal],
        claude_accounts=[empresa, cliente], api_keys=[openai, gemini, ],
        profiles=[
            Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work.id,
                    github_account_id=gh_work.id, claude_config_dir=folder, claude_account_id=empresa.id,
                    api_key_ids=[openai.id]),
            Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=personal.id,
                    github_account_id=gh_personal.id, api_key_ids=[gemini.id]),
            Profile(name="Cliente X", color=ProfileColor.TEAL, git_identity_id=work.id, claude_config_dir=folder,
                    claude_account_id=cliente.id),
        ] if profiles else [])


def report(*statuses):
    from multicred.core.engine.verifier import Check, CheckStatus, VerifyReport
    return VerifyReport(checks=[Check(id=f"c{i}", title=f"Item {i}", status=CheckStatus(s))
                                for i, s in enumerate(statuses)],
                        checked_at=datetime.now(timezone.utc))


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
class SidebarTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-sidebar-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        (self.sandbox / ".claude-empresa").mkdir(mode=0o700)

    def make_model(self, config, tools=None):
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        if config is not None:
            ConfigRepository(self.environment).save(config)
        model = AppModel(self.environment, tools or NoTools(), dispatch=GLib.idle_add)
        # Folder statuses and summaries are crafted by the tests, never looked up.
        model.refresh_claude_statuses = lambda on_done=None: None
        model.load_folder_summaries = lambda on_done=None: None
        return model

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
    def mark_active(model, profile_id, verification=None):
        """A step: the app loaded state.json at startup, so the crafted state goes in after it."""
        def step(_app):
            model.state.profile_id = profile_id
            model.state.activated_at = datetime.now(timezone.utc)
            if verification is not None:
                model.verification = verification
            model.notify()
        return step

    @staticmethod
    def row_state(window, key):
        row = window.sidebar_row(key)
        return {
            "count": row.count_label.get_label() if row.count_label is not None else None,
            "warning": row.warning_icon is not None,
            "check": row.check_icon is not None,
            "dot": row.dot_state,
        }

    # MARK: structure

    def test_sections_items_order_and_active_check(self):
        config = crafted_config(self.environment)
        for key in config.api_keys:
            self.environment.secrets.set_secret(b"sk-seed", key.secret_account)
        self.environment.secrets.set_secret(b"tok", config.claude_accounts[1].secret_account)
        model = self.make_model(config)
        work, personal, client = config.profiles
        seen = {}

        def inspect(app):
            window = app.window
            seen["sections"] = window.sidebar_sections()
            seen["checks_before"] = [self.row_state(window, f"profile:{p.id}")["check"] for p in config.profiles]
            model.state.profile_id = personal.id
            model.notify()

        def after_activation(app):
            window = app.window
            seen["checks_after"] = [self.row_state(window, f"profile:{p.id}")["check"] for p in config.profiles]
            row = window.sidebar_row(f"profile:{personal.id}")
            seen["check_tooltip"] = row.check_icon.get_tooltip_text()
            seen["avatar"] = (row.avatar.get_label(), row.avatar.has_css_class("mc-avatar-20"),
                              row.avatar.has_css_class("mc-color-orange"))
            seen["icons"] = {key: window.sidebar_row(key).item.icon
                             for key in ("overview", "new-profile", "git", "github", "claude", "keys",
                                         "verify", "settings")}
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            seen["missing_icons"] = [name for name in seen["icons"].values() if not theme.has_icon(name)]

        self.run_steps(model, [inspect, after_activation])
        self.assertEqual(seen["sections"], [
            ("Perfis", ["Visão geral", "Trabalho", "Pessoal", "Cliente X", "Novo perfil"]),
            ("Contas", ["Git", "GitHub", "Claude", "Chaves de API"]),
            ("Sistema", ["Verificar", "Ajustes"]),
        ])
        self.assertEqual(seen["checks_before"], [False, False, False], "no active profile: no ✓")
        self.assertEqual(seen["checks_after"], [False, True, False], "✓ only on the active profile")
        self.assertEqual(seen["check_tooltip"], "ativo")
        self.assertEqual(seen["avatar"], ("P", True, True))
        self.assertEqual(seen["icons"], {
            "overview": "view-grid-symbolic", "new-profile": "list-add-symbolic",
            "git": "multicred-branch-symbolic", "github": "multicred-code-symbolic",
            "claude": "multicred-sparkle-symbolic", "keys": "dialog-password-symbolic",
            "verify": "security-high-symbolic", "settings": "emblem-system-symbolic"})
        self.assertEqual(seen["missing_icons"], [], "the app's own symbolic icons are registered")

    def test_counts_and_warnings_follow_the_mac_rules(self):
        from multicred.core.adapters.github_cli import GitHubLogin
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.models import ClaudeFolder

        config = crafted_config(self.environment)
        secrets = self.environment.secrets
        for key in config.api_keys:
            secrets.set_secret(b"sk-seed", key.secret_account)
        token_account = config.claude_accounts[1]
        secrets.set_secret(b"tok", token_account.secret_account)
        model = self.make_model(config)
        seen = {}
        kinds = ("git", "github", "claude", "keys")

        def snapshot(label):
            def step(app):
                seen[label] = {k: self.row_state(app.window, k) for k in kinds}
            step.__name__ = f"snapshot_{label}"
            return step

        def key_without_secret(_app):
            secrets.delete_item(config.api_keys[1].secret_account)
            model.notify()

        def gh_login_not_usable(_app):
            model.github_logins = [GitHubLogin("github.com", "dev-pessoal", False, "failure")]
            model.notify()

        def gh_login_usable(_app):
            model.github_logins = [GitHubLogin("github.com", "dev-pessoal", False, "success")]
            model.notify()

        def token_missing(_app):
            secrets.delete_item(token_account.secret_account)
            model.notify()

        def token_back_folder_logged_out(_app):
            secrets.set_secret(b"tok", token_account.secret_account)
            # Pessoal uses the default folder's own login: logged out → it needs to sign in.
            model.claude_statuses[ClaudeFolder.own_login(None).id] = ClaudeLoginStatus("loggedOut")
            model.notify()

        self.run_steps(model, [snapshot("clear"), key_without_secret, snapshot("key"), gh_login_not_usable,
                               snapshot("gh_bad"), gh_login_usable, snapshot("gh_ok"), token_missing,
                               snapshot("token"), token_back_folder_logged_out, snapshot("folder")])

        def expect(git=False, github=False, claude=False, keys=False):
            return {"git": {"count": "2", "warning": git, "check": False, "dot": None},
                    "github": {"count": "2", "warning": github, "check": False, "dot": None},
                    "claude": {"count": "2", "warning": claude, "check": False, "dot": None},
                    "keys": {"count": "2", "warning": keys, "check": False, "dot": None}}

        self.assertEqual(seen["clear"], expect(), "nothing pending: counts only, no ⚠ (gh logins unknown)")
        self.assertEqual(seen["key"], expect(keys=True), "a key without value")
        self.assertEqual(seen["gh_bad"], expect(github=True, keys=True), "a gh login that is not usable")
        self.assertEqual(seen["gh_ok"], expect(keys=True))
        self.assertEqual(seen["token"], expect(claude=True, keys=True), "Cliente X's token account has no token")
        self.assertEqual(seen["folder"], expect(claude=True, keys=True), "Pessoal's folder is logged out")

    def test_claude_warning_ignores_unused_accounts_without_token(self):
        """Mac: only profiles count (`profiles.contains { needsClaudeLogin }`), not every account."""
        from multicred.core.models import ClaudeAccount, ClaudeAccountKind, ProfileColor

        config = crafted_config(self.environment)
        for key in config.api_keys:
            self.environment.secrets.set_secret(b"sk-seed", key.secret_account)
        self.environment.secrets.set_secret(b"tok", config.claude_accounts[1].secret_account)
        config.claude_accounts.append(ClaudeAccount(nickname="Sobra", color=ProfileColor.PINK,
                                                    kind=ClaudeAccountKind.TOKEN))
        model = self.make_model(config)
        seen = {}

        def inspect(app):
            seen["claude"] = self.row_state(app.window, "claude")

        self.run_steps(model, [inspect])
        self.assertEqual(seen["claude"], {"count": "3", "warning": False, "check": False, "dot": None})

    def test_verify_dot_absent_then_green_then_orange(self):
        model = self.make_model(crafted_config(self.environment))
        seen = {}

        def before(app):
            seen["before"] = self.row_state(app.window, "verify")
            seen["no_badge"] = not hasattr(app.window, "verify_badge")
            model.verification = report("ok", "ok", "info")
            model.notify()

        def ok(app):
            row = app.window.sidebar_row("verify")
            seen["ok"] = self.row_state(app.window, "verify")
            seen["ok_tooltip"] = row.dot.get_tooltip_text()
            model.verification = report("ok", "error")
            model.notify()

        def problems(app):
            row = app.window.sidebar_row("verify")
            seen["problems"] = self.row_state(app.window, "verify")
            seen["problems_tooltip"] = row.dot.get_tooltip_text()

        self.run_steps(model, [before, ok, problems])
        self.assertEqual(seen["before"], {"count": None, "warning": False, "check": False, "dot": None})
        self.assertTrue(seen["no_badge"], "the old ok/total badge is gone")
        self.assertEqual(seen["ok"]["dot"], "ok")
        self.assertEqual(seen["ok_tooltip"], "tudo certo")
        self.assertEqual(seen["problems"]["dot"], "warning")
        self.assertEqual(seen["problems_tooltip"], "inconsistente")

    # MARK: navigation

    def test_clicking_a_profile_opens_its_editor_without_switching(self):
        from multicred.ui.pages.profile_editor import ProfileEditorPage
        from multicred.ui.pages.profiles import ProfilesPage

        config = crafted_config(self.environment)
        model = self.make_model(config)
        work, personal, _client = config.profiles
        seen = {}

        def click_profile(app):
            window = app.window
            seen["start"] = (window.current_section, window.selected_sidebar_key())
            window.sidebar.select_row(window.sidebar_row(f"profile:{personal.id}"))

        def editor_shown(app):
            window = app.window
            page = window.nav.get_visible_page()
            seen["editor"] = (isinstance(page, ProfileEditorPage), getattr(page, "draft", None) and page.draft.id,
                              page.get_title())
            seen["selected"] = window.selected_sidebar_key()
            seen["active"] = model.state.profile_id
            seen["switching"] = model.switching

        def click_new(app):
            window = app.window
            window.sidebar.select_row(window.sidebar_row("new-profile"))
            page = window.nav.get_visible_page()
            seen["new"] = (isinstance(page, ProfileEditorPage) and page.is_new, page.get_title(),
                           page.name_entry.get_text() if isinstance(page, ProfileEditorPage) else None)
            seen["new_selected"] = window.selected_sidebar_key()

        def click_overview(app):
            window = app.window
            window.sidebar.select_row(window.sidebar_row("overview"))
            page = window.nav.get_visible_page()
            seen["overview"] = (isinstance(page, ProfilesPage), page.get_title(), window.content_page.get_title())

        def editor_is_the_root_of_its_row(app):
            # S-03: like the Mac, the editor is the page of the profile's row (no back button).
            window = app.window
            window.sidebar.select_row(window.sidebar_row(f"profile:{personal.id}"))
            window.nav.pop()
            seen["after_pop"] = (window.nav.get_navigation_stack().get_n_items(), window.selected_sidebar_key())

        def keys(app):
            window = app.window
            window.sidebar.select_row(window.sidebar_row("keys"))
            keys_page = window.nav.get_visible_page()  # S-02: Chaves de API is a page of its own
            seen["keys"] = (window.current_section, keys_page.get_tag(), window.content_page.get_title())
            keys_page.add_button.emit("clicked")  # an editor pushed on Chaves de API
            seen["pushed"] = window.nav.get_visible_page().get_title()
            window.sidebar.emit("row-activated", window.sidebar_row("keys"))
            seen["reclicked"] = (window.nav.get_visible_page() is keys_page, window.selected_sidebar_key())

        self.run_steps(model, [self.mark_active(model, work.id), click_profile, editor_shown, click_new, click_overview, editor_is_the_root_of_its_row,
                               keys])
        self.assertEqual(seen["start"], ("main", "overview"), "with profiles the start page is Visão geral")
        self.assertEqual(seen["editor"], (True, personal.id, "Editar perfil"))
        self.assertEqual(seen["selected"], f"profile:{personal.id}")
        self.assertEqual(seen["active"], work.id, "opening a profile never switches")
        self.assertFalse(seen["switching"])
        self.assertEqual(seen["new"], (True, "Novo perfil", ""))
        self.assertEqual(seen["new_selected"], "new-profile")
        self.assertEqual(seen["overview"], (True, "Visão geral", "Visão geral"))
        self.assertEqual(seen["after_pop"], (1, f"profile:{personal.id}"), "nothing to go back to")
        self.assertEqual(seen["keys"], ("keys", "keys", "Chaves de API"))
        self.assertEqual(seen["pushed"], "Nova chave de API")
        self.assertEqual(seen["reclicked"], (True, "keys"), "clicking the selected entry goes back to its page")

    def test_dashboard_rows_open_the_editor_and_only_ativar_switches(self):
        from multicred.ui.pages.profile_editor import ProfileEditorPage

        config = crafted_config(self.environment)
        for key in config.api_keys:
            self.environment.secrets.set_secret(b"sk-seed", key.secret_account)
        model = self.make_model(config, SandboxTools())  # the switch needs gh "found"
        work, personal, _client = config.profiles
        seen = {}

        def activate_row(app):
            window = app.window
            page = window.profiles_page
            row = page.other_rows[0]
            seen["row"] = row.get_title()
            row.emit("activated")
            visible = window.nav.get_visible_page()
            seen["opened"] = isinstance(visible, ProfileEditorPage) and visible.draft.id == personal.id
            seen["switching_after_row"] = model.switching
            seen["active_after_row"] = model.state.profile_id
            window.nav.pop()
            page.activate_buttons[personal.id].emit("clicked")

        def wait(_app):
            return model.switching

        def after_ativar(app):
            seen["active_after_button"] = model.state.profile_id
            seen["toast"] = app.window.last_toast

        self.run_steps(model, [self.mark_active(model, work.id), activate_row, wait, after_ativar])
        self.assertEqual(seen["row"], "Pessoal")
        self.assertTrue(seen["opened"], "a dashboard row opens that profile's editor")
        self.assertFalse(seen["switching_after_row"])
        self.assertEqual(seen["active_after_row"], work.id, "clicking a row does not switch")
        self.assertEqual(seen["active_after_button"], personal.id, f"Ativar switches ({seen['toast']})")

    def test_start_page_is_claude_without_profiles(self):
        model = self.make_model(crafted_config(self.environment, profiles=False))
        seen = {}

        def inspect(app):
            window = app.window
            seen["start"] = (window.current_section, window.selected_sidebar_key(),
                             window.nav.get_visible_page().get_tag())
            seen["perfis"] = window.sidebar_sections()[0]

        self.run_steps(model, [inspect])
        self.assertEqual(seen["start"], ("claude", "claude", "claude"))
        self.assertEqual(seen["perfis"], ("Perfis", ["Visão geral", "Novo perfil"]))

    def test_sidebar_follows_profiles_added_renamed_and_removed(self):
        from multicred.core.editors import ConfigEditor
        from multicred.core.models import Profile, ProfileColor

        config = crafted_config(self.environment)
        model = self.make_model(config)
        work, personal, client = config.profiles
        new = Profile(name="Freela", color=ProfileColor.GREEN)
        seen = {}

        def add(app):
            model.edit(lambda c: ConfigEditor.upsert(new, c))

        def added(app):
            seen["added"] = app.window.sidebar_sections()[0][1]
            seen["new_row"] = app.window.sidebar_row(f"profile:{new.id}") is not None
            renamed = model.config.profile(work.id)
            import copy
            renamed = copy.deepcopy(renamed)
            renamed.name = "Empresa"
            model.edit(lambda c: ConfigEditor.upsert(renamed, c))

        def renamed(app):
            seen["renamed"] = app.window.sidebar_sections()[0][1]
            window = app.window
            window.sidebar.select_row(window.sidebar_row(f"profile:{client.id}"))
            model.edit(lambda c: ConfigEditor.remove_profile(client.id, c))

        def removed(app):
            window = app.window
            seen["removed"] = window.sidebar_sections()[0][1]
            seen["selected_after_remove"] = window.selected_sidebar_key()

        self.run_steps(model, [add, added, renamed, removed])
        self.assertEqual(seen["added"], ["Visão geral", "Trabalho", "Pessoal", "Cliente X", "Freela", "Novo perfil"])
        self.assertTrue(seen["new_row"])
        self.assertEqual(seen["renamed"], ["Visão geral", "Empresa", "Pessoal", "Cliente X", "Freela", "Novo perfil"])
        self.assertEqual(seen["removed"], ["Visão geral", "Empresa", "Pessoal", "Freela", "Novo perfil"])
        self.assertEqual(seen["selected_after_remove"], "overview", "the removed profile's row is not left selected")

    def test_open_page_names_from_the_tray(self):
        from multicred.ui.pages.profile_editor import ProfileEditorPage

        model = self.make_model(crafted_config(self.environment))
        seen = {}

        def walk(app):
            window = app.window
            for name in ("accounts", "verify", "settings", "main"):
                window.open_page(name)
                seen[name] = (window.current_section, window.selected_sidebar_key())
            seen["accounts_tab"] = None
            window.open_page("accounts")
            seen["accounts_tab"] = window.nav.get_visible_page().get_tag()
            window.open_page("new-profile")
            page = window.nav.get_visible_page()
            seen["new-profile"] = (isinstance(page, ProfileEditorPage) and page.is_new, window.selected_sidebar_key())
            window.open_page("add-claude")
            # S-04: "Adicionar conta Claude" is a page pushed on Claude, as on the Mac.
            seen["add-claude"] = (window.current_section,
                                  window.nav.get_visible_page() is window.login_page is not None)

        self.run_steps(model, [walk])
        self.assertEqual(seen["accounts"], ("claude", "claude"), "Contas… opens Claude, as on the Mac")
        self.assertEqual(seen["verify"], ("verify", "verify"))
        self.assertEqual(seen["settings"], ("settings", "settings"))
        self.assertEqual(seen["main"], ("main", "overview"))
        self.assertEqual(seen["accounts_tab"], "claude")
        self.assertEqual(seen["new-profile"], (True, "new-profile"))
        self.assertEqual(seen["add-claude"], ("claude", True))

    # MARK: screenshots

    def test_screenshots(self):
        directory = os.environ.get("MULTICRED_SCREENS_DIR")
        if not directory:
            self.skipTest("MULTICRED_SCREENS_DIR not set")
        Path(directory).mkdir(parents=True, exist_ok=True)
        config = crafted_config(self.environment)
        self.environment.secrets.set_secret(b"sk-seed", config.api_keys[0].secret_account)
        model = self.make_model(config)
        tries = {}

        def setup(app):
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            app.window.set_default_size(980, 660)
            app.window.dismiss_toast()

        def wait(ticks):
            left = [ticks]

            def step(_app):
                left[0] -= 1
                return left[0] > 0
            return step

        def shot(name):
            def step(app):
                tries[name] = tries.get(name, 0) + 1
                if save_png(app.window, Path(directory) / f"S-01-{name}.png"):
                    return False
                if tries[name] > 20:
                    raise AssertionError(f"nothing rendered for {name}")
                return True
            return step

        def select_keys(app):
            app.window.sidebar.select_row(app.window.sidebar_row("keys"))

        self.run_steps(model, [self.mark_active(model, config.profiles[0].id, report("ok", "ok", "ok")), setup, wait(20), shot("visao-geral"), select_keys, wait(20), shot("chaves")])
        for name in ("visao-geral", "chaves"):
            self.assertTrue((Path(directory) / f"S-01-{name}.png").exists())


if __name__ == "__main__":
    unittest.main()
