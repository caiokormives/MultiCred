"""S-03 (R-03): the Claude page like the Mac, with "Usar agora", the account and folder
details, and the folder's own login inside the app.

Reference: `MultiCred/Views/ClaudePages.swift` (root page, folder and account details,
removal dialog), `ClaudeFolderPreview.swift` (the folder's facts and conversations) and
`MultiCred/Login/LoginWindowView.swift` ("Entrar no Claude", `claude auth login` with the
folder's CLAUDE_CONFIG_DIR in a pseudo-terminal). Driven in test mode: the login
simulator runs instead of the real `claude`, the dry-run runner records commands, and
the sandbox HOME holds every folder. Texts are the Mac's, with "Keychain" → "chaveiro do
sistema" and "neste Mac" → "neste computador". With MULTICRED_SCREENS_DIR set, the pages
are rendered (stock light Adwaita) into S-03-*.png.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

CODE = "abcdefghijklmnopqrstuvwxyz012345#ABCDEFGHIJKLMNOP"
CLAUDE_NOTE = ("O token de cada conta fica só no chaveiro do sistema (Secret Service) e vale nos terminais "
               "(CLAUDE_CODE_OAUTH_TOKEN). Apps gráficos continuam com o login da pasta.")


class SandboxTools:
    """claude/git/gh "found" at fake paths: the dry-run runner never executes them, and
    test mode runs the login simulator whatever is found (resolve_login_tool)."""

    def locate(self, tool):
        return f"/fake/bin/{tool.value}"

    def search_directories(self):
        return ["/fake/bin"]


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
class ClaudePageTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-claude-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        self.company = str(self.sandbox / ".claude-empresa")
        Path(self.company).mkdir()
        self.screens = os.environ.get("MULTICRED_SCREENS_DIR")
        self.login_requests = []

    # MARK: fixtures

    def full_config(self):
        """Trabalho and Cliente X share ~/.claude-empresa (logged out); Pessoal uses the default
        folder (logged in) and the token account Dev by default; Marco is an unused login
        account; "Sem token" has lost its token."""
        from multicred.core.models import (ClaudeAccount, ClaudeAccountKind, Config, GitIdentity, Profile,
                                           ProfileColor)
        work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.example",
                           color=ProfileColor.INDIGO)
        self.marco = ClaudeAccount(nickname="Marco", email="marco@exemplo.com", color=ProfileColor.ORANGE,
                                   kind=ClaudeAccountKind.LOGIN)
        self.dev = ClaudeAccount(nickname="Dev", color=ProfileColor.BLUE, kind=ClaudeAccountKind.TOKEN)
        self.lost = ClaudeAccount(nickname="Sem token", color=ProfileColor.RED, kind=ClaudeAccountKind.TOKEN)
        self.environment.secrets.set_secret(b"sk-ant-oat01-dev-0000000000000000000000", self.dev.secret_account)
        return Config(git_identities=[work], claude_accounts=[self.marco, self.dev, self.lost], profiles=[
            Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work.id,
                    claude_config_dir=self.company),
            Profile(name="Pessoal", color=ProfileColor.ORANGE, claude_account_id=self.dev.id),
            Profile(name="Cliente X", color=ProfileColor.TEAL, claude_config_dir=self.company),
        ])

    def make_model(self, config, statuses=True, summaries=True):
        from multicred.core.claude.folder_inspector import ClaudeFolderSummary, Conversation
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.models import ClaudeFolder
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        ConfigRepository(self.environment).save(config)
        model = AppModel(self.environment, SandboxTools(), dispatch=GLib.idle_add)
        model.refresh_claude_statuses = lambda on_done=None: None
        model.load_folder_summaries = lambda on_done=None: None
        model.load_folder_summary = lambda config_dir, on_done=None: None
        if statuses:
            # The folders' .claude.json agree with the statuses given, so a status read for real
            # (a page's refresh_folder_status) finds the same thing.
            logged_in = {"oauthAccount": {"emailAddress": "eu@pessoal.example"}}
            for state, data in ((self.sandbox / ".claude.json", logged_in),
                                (Path(self.company) / ".claude.json", {"numStartups": 1})):
                if not state.exists():
                    state.write_text(json.dumps(data))
            model.claude_statuses[ClaudeFolder.own_login(None).id] = ClaudeLoginStatus.logged_in("eu@pessoal.example")
            model.claude_statuses[ClaudeFolder.own_login(self.company).id] = ClaudeLoginStatus.logged_out()
        if summaries:
            now = datetime.now(timezone.utc)
            conversations = [Conversation(f"Conversa {i}", f"~/code/projeto-{i % 3}", now - timedelta(hours=2 + i))
                             for i in range(10)]
            model.folder_summaries[None] = ClaudeFolderSummary(
                None, str(self.sandbox / ".claude"), "eu@pessoal.example", "Pessoal Org", 3, 12,
                now - timedelta(hours=2), conversations)
            model.folder_summaries[self.company] = ClaudeFolderSummary(
                self.company, self.company, None, None, 0, 0, None, [])
        return model

    def run_steps(self, model, steps, seconds=40):
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

    def activate(self, model, index=0):
        """A step: really activates profile `index` (dry-run sandbox), so "Usar agora" can switch."""
        state = {}

        def step(app):
            profile_id = model.config.profiles[index].id
            if not state:
                state["started"] = True
                state["error"] = None
                model.switch_profile(profile_id, lambda _r, error: state.update(error=error))
                if self.screens:
                    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
                app.window.set_default_size(1000, 700)
                return True
            if state["error"] is not None:
                raise AssertionError(f"could not activate the profile: {state['error']}")
            return model.switching or model.state.profile_id != profile_id
        return step

    def shot(self, name):
        """A step: renders the window into S-03-<name>.png when MULTICRED_SCREENS_DIR is set."""
        tries = [0]

        def step(app):
            if not self.screens:
                return False
            if tries[0] == 0:
                app.window.dismiss_toast()
            # Let the page transition, a closing dialog, the toast and the layout settle.
            if tries[0] < 20:
                tries[0] += 1
                return True
            Path(self.screens).mkdir(parents=True, exist_ok=True)
            if save_png(app.window, Path(self.screens) / f"S-03-{name}.png"):
                return False
            if tries[0] > 40:
                raise AssertionError(f"nothing rendered for {name}")
            tries[0] += 1
            return True
        return step

    @staticmethod
    def visible(app):
        return app.window.nav.get_visible_page()

    @staticmethod
    def open_claude(app):
        window = app.window
        window.sidebar.select_row(window.sidebar_row("claude"))
        return window.nav.get_visible_page()

    @staticmethod
    def answer(seen, key, response):
        """A step: once the dialog is on screen, keeps (heading, body, confirm label, destructive)
        in seen[key] and answers it with `response`, like a click on its button
        (AdwAlertDialog closes, then emits the response). Closing a dialog in the same tick
        it was presented leaves it drawn; emitting first and closing after closes it twice."""
        ticks = [0]

        def step(app):
            dialog = app.window.get_visible_dialog()
            if dialog is None or not dialog.get_mapped() or ticks[0] < 3:
                ticks[0] += 1
                if ticks[0] > 100:
                    raise AssertionError("no dialog")
                return True
            seen[key] = (dialog.get_heading(), dialog.get_body(), dialog.get_response_label("confirm"),
                         dialog.get_response_appearance("confirm") == Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.force_close()
            if response != "cancel":
                dialog.emit("response", response)
            return False
        return step

    # MARK: the root page

    def test_folders_come_first_then_accounts_with_use_now(self):
        from multicred.ui.pages.claude import ClaudePage

        model = self.make_model(self.full_config())
        seen = {}

        def inspect(app):
            page = self.open_claude(app)
            seen["class"] = isinstance(page, ClaudePage)
            seen["nav_title"] = app.window.content_page.get_title()
            seen["header"] = page.header_texts()
            seen["actions"] = page.action_labels()
            seen["sections"] = page.section_titles()
            seen["folder_footer"] = page.folder_footer_text()
            seen["folders"] = [row.info() for row in page.folder_rows]
            seen["accounts"] = [row.info() for row in page.account_rows]
            seen["empty_accounts"] = page.empty_accounts_visible()
            seen["note"] = (page.note_text(), page.note_icon())
            seen["commands_before"] = [e.command for e in self.environment.runner.entries]

        def use_now(app):
            page = self.visible(app)
            row = next(r for r in page.account_rows if r.title == "Dev")
            row.buttons["Usar agora"].emit("clicked")

        def switched(app):
            return model.state.applied_claude_account_id != self.dev.id or model.switching

        def after(app):
            page = self.visible(app)
            seen["dev_after"] = next(r.info() for r in page.account_rows if r.title == "Dev")
            seen["profile_after"] = model.active_profile.name
            seen["commands_after"] = [e.command for e in self.environment.runner.entries]
            seen["toast"] = app.window.last_toast

        self.run_steps(model, [self.activate(model), inspect, self.shot("claude"), use_now, switched, after,
                               self.shot("claude-em-uso")])

        self.assertTrue(seen["class"])
        self.assertEqual(seen["nav_title"], "Claude")
        self.assertEqual(seen["header"], ("Claude", "Pastas guardam agents, skills e conversas. Contas são só o "
                                                    "login usado nelas."))
        self.assertEqual(seen["actions"], ["Adicionar conta"])
        self.assertEqual(seen["sections"], ["Pastas dos perfis", "Contas Claude (só credencial)"])
        self.assertEqual(seen["folder_footer"], "Cada perfil usa uma pasta. Trocar a conta não muda a pasta.")
        self.assertEqual(seen["folders"], [
            {"title": "~/.claude", "subtitle": "Perfis: Pessoal", "counts": "3 projetos · 12 conversas",
             "status": "eu@pessoal.example", "buttons": [], "busy": False},
            {"title": "~/.claude-empresa", "subtitle": "Perfis: Trabalho, Cliente X",
             "counts": "0 projetos · 0 conversas", "status": "Sem login", "buttons": ["Entrar…"], "busy": False},
        ])
        menu = ["Usar agora no perfil ativo", "Entrar de novo…", "Remover conta…"]
        self.assertEqual(seen["accounts"], [
            {"title": "Marco", "subtitle": "marco@exemplo.com", "used_in": "—",
             "detail": "login completo · troca até nas conversas abertas", "status": None,
             "buttons": ["Usar agora"], "menu": menu},
            {"title": "Dev", "subtitle": "conta Claude", "used_in": "Pessoal", "detail": "token · só terminais novos",
             "status": None, "buttons": ["Usar agora"], "menu": menu},
            {"title": "Sem token", "subtitle": "conta Claude", "used_in": "—", "detail": "sem token",
             "status": "Sem token", "buttons": [], "menu": menu},
        ])
        self.assertFalse(seen["empty_accounts"])
        self.assertEqual(seen["note"], (CLAUDE_NOTE, "changes-prevent-symbolic"))

        self.assertEqual(seen["dev_after"]["status"], "Em uso")
        self.assertEqual(seen["dev_after"]["buttons"], [])
        self.assertEqual(seen["profile_after"], "Trabalho", "\"Usar agora\" keeps the profile")
        new_commands = seen["commands_after"][len(seen["commands_before"]):]
        self.assertFalse([c for c in new_commands if " config " in c or "/gh " in c or c.startswith("gh ")],
                         f"only Claude switches: {new_commands}")
        self.assertEqual(seen["toast"], "Claude agora usa Dev")

    def test_use_now_is_off_without_an_active_profile_and_the_empty_texts(self):
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.models import ClaudeAccount, ClaudeAccountKind, ClaudeFolder, Config, Profile, ProfileColor

        model = self.make_model(Config(profiles=[Profile(name="Trabalho", color=ProfileColor.INDIGO,
                                                         claude_config_dir=self.company)]),
                                statuses=False, summaries=False)
        model.claude_statuses[ClaudeFolder.own_login(self.company).id] = ClaudeLoginStatus.unknown("x")
        seen = {}

        def inspect(app):
            app.window.set_default_size(1000, 700)
            if self.screens:
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            page = self.open_claude(app)
            seen["folders"] = [row.info() for row in page.folder_rows]
            seen["empty"] = (page.empty_accounts_visible(), page.empty_accounts_text())

        def add_account(app):
            config = model.config
            config.claude_accounts.append(ClaudeAccount(nickname="Ana", color=ProfileColor.PINK,
                                                        kind=ClaudeAccountKind.LOGIN))
            model.save(config)
            row = self.visible(app).account_rows[0]
            seen["use_now_without_profile"] = row.buttons["Usar agora"].get_sensitive()
            seen["empty_after"] = self.visible(app).empty_accounts_visible()

        self.run_steps(model, [inspect, self.shot("claude-vazio"), add_account])

        self.assertEqual(seen["folders"], [
            {"title": "~/.claude", "subtitle": "Pasta padrão (nenhum perfil)", "counts": None, "status": None,
             "buttons": [], "busy": True},
            {"title": "~/.claude-empresa", "subtitle": "Perfis: Trabalho", "counts": None,
             "status": "Status indisponível", "buttons": ["Entrar…"], "busy": False},
        ])
        self.assertEqual(seen["empty"], (True, "Nenhuma conta extra. Adicione uma para usar o login de outra "
                                               "pessoa ou de outra assinatura sem trocar de pasta."))
        self.assertFalse(seen["use_now_without_profile"], "Mac: disabled without an active profile")
        self.assertFalse(seen["empty_after"])

    def test_add_account_and_sign_in_again_open_the_login_pages(self):
        from multicred.ui.pages.add_claude import AddClaudeAccountPage

        model = self.make_model(self.full_config())
        seen = {}

        def add(app):
            page = self.open_claude(app)
            page.add_button.emit("clicked")
            login = self.visible(app)
            seen["add"] = (isinstance(login, AddClaudeAccountPage), login.get_title())
            app.window.nav.pop()

        def again(app):
            page = self.visible(app)
            next(r for r in page.account_rows if r.title == "Dev").activate_menu("Entrar de novo…")
            login = self.visible(app)
            seen["again"] = (isinstance(login, AddClaudeAccountPage), login.get_title())
            app.window.nav.pop()

        def remove_from_menu(app):
            page = self.visible(app)
            next(r for r in page.account_rows if r.title == "Marco").activate_menu("Remover conta…")

        def left(app):
            seen["left"] = [a.nickname for a in model.config.claude_accounts]

        self.run_steps(model, [add, again, remove_from_menu, self.answer(seen, "dialog", "confirm"), left])

        self.assertEqual(seen["add"], (True, "Adicionar conta Claude"))
        self.assertEqual(seen["again"], (True, "Entrar de novo em Dev"))
        self.assertEqual(seen["dialog"], ("Remover a conta Marco?", "O token é apagado do chaveiro do sistema. "
                                          "Nenhuma pasta ou conversa é tocada.", "Remover", True))
        self.assertEqual(seen["left"], ["Dev", "Sem token"])

    # MARK: account detail

    def test_account_detail_shows_the_data_uses_now_and_removes_with_the_mac_text(self):
        from multicred.ui.pages.add_claude import AddClaudeAccountPage
        from multicred.ui.pages.claude import ClaudeAccountDetailPage

        model = self.make_model(self.full_config())
        seen = {}

        def open_dev(app):
            page = self.open_claude(app)
            row = next(r for r in page.account_rows if r.title == "Dev")
            row.emit("activate")

        def inspect_dev(app):
            page = self.visible(app)
            seen["class"] = isinstance(page, ClaudeAccountDetailPage)
            seen["title"] = page.get_title()
            seen["texts"] = page.texts()
            seen["buttons"] = page.button_states()

        def sign_in_again(app):
            self.visible(app).again_button.emit("clicked")
            login = self.visible(app)
            seen["again"] = (isinstance(login, AddClaudeAccountPage), login.get_title())
            app.window.nav.pop()

        def use_now(app):
            self.visible(app).use_now_button.emit("clicked")

        def switched(app):
            return model.state.applied_claude_account_id != self.dev.id or model.switching

        def in_use(app):
            page = self.visible(app)
            seen["in_use"] = (page.texts()["status"], page.button_states()["Usar agora"])

        def remove(app):
            self.visible(app).remove_button.emit("clicked")

        def removed(app):
            seen["after_remove"] = type(self.visible(app)).__name__
            seen["left"] = [a.nickname for a in model.config.claude_accounts]
            seen["secret"] = self.environment.secrets.stored_value(self.dev.secret_account)

        def open_marco(app):
            page = self.visible(app)
            next(r for r in page.account_rows if r.title == "Marco").emit("activate")
            seen["marco"] = self.visible(app).texts()

        def remove_behind(app):
            model.remove_claude_account(self.marco.id)
            page = self.visible(app)
            seen["gone"] = (page.showing_removed(), page.removed_state.get_title())

        def open_lost(app):
            app.window.nav.pop()
            page = self.visible(app)
            next(r for r in page.account_rows if r.title == "Sem token").emit("activate")
            detail = self.visible(app)
            seen["lost"] = (detail.texts()["status"], detail.button_states()["Usar agora"])

        self.run_steps(model, [self.activate(model), open_dev, inspect_dev, self.shot("conta"), sign_in_again,
                               use_now, switched, in_use, remove, self.answer(seen, "dialog", "confirm"), removed,
                               open_marco, remove_behind,
                               self.shot("conta-removida"), open_lost])

        self.assertTrue(seen["class"])
        self.assertEqual(seen["title"], "Dev")
        self.assertEqual(seen["texts"], {
            "name": "Dev", "email": "e-mail não informado", "status": "Token salvo",
            "used": "Padrão nos perfis: Pessoal.",
            "use_now": "\"Usar agora\" mantém o perfil Trabalho e a pasta ~/.claude-empresa (agents, skills, "
                       "conversas). Só o login muda, até a próxima troca de perfil.",
        })
        self.assertEqual(seen["buttons"], {"Remover conta…": True, "Entrar de novo…": True, "Usar agora": True})
        self.assertEqual(seen["again"], (True, "Entrar de novo em Dev"))
        self.assertEqual(seen["in_use"], ("Em uso", False), "already in use: Usar agora is off")
        self.assertEqual(seen["dialog"], ("Remover a conta Dev?",
                                          "Usada em: Pessoal. Esses perfis voltam a usar o login da própria pasta. "
                                          "O token é apagado do chaveiro do sistema. Nenhuma pasta ou conversa é "
                                          "tocada.", "Remover", True))
        self.assertEqual(seen["after_remove"], "ClaudePage", "back to the list after removing (Mac)")
        self.assertEqual(seen["left"], ["Marco", "Sem token"])
        self.assertIsNone(seen["secret"], "the token is deleted")
        self.assertEqual(seen["marco"], {"name": "Marco", "email": "marco@exemplo.com", "status": "Login salvo",
                                         "used": "Nenhum perfil usa esta conta por padrão.",
                                         "use_now": seen["marco"]["use_now"]})
        self.assertEqual(seen["gone"], (True, "Conta removida"))
        self.assertEqual(seen["lost"], ("Sem token", False))

    # MARK: folder detail and logout

    def test_folder_detail_shows_the_preview_and_logs_the_default_folder_out(self):
        from multicred.ui.pages.claude import ClaudeFolderDetailPage

        state_file = self.sandbox / ".claude.json"
        state_file.write_text(json.dumps({"oauthAccount": {"emailAddress": "eu@pessoal.example"},
                                          "numStartups": 3}))
        model = self.make_model(self.full_config())
        seen = {}

        def open_default(app):
            page = self.open_claude(app)
            page.folder_rows[0].emit("activate")
            detail = self.visible(app)
            seen["class"] = isinstance(detail, ClaudeFolderDetailPage)
            seen["title"] = detail.get_title()
            seen["header"] = detail.header_info()
            seen["facts"] = detail.facts()
            seen["conversations"] = detail.conversation_titles()
            seen["first_conversation"] = detail.conversation_rows[0]
            seen["captions"] = detail.captions()
            seen["buttons"] = detail.button_states()

        def log_out(app):
            self.visible(app).logout_button.emit("clicked")

        def logged_out(app):
            status = model.folder_status(None)
            return status is None or status.kind != "loggedOut"

        def after(app):
            detail = self.visible(app)
            seen["after"] = (detail.header_info()["status"], detail.button_states()["Sair do login da pasta…"])
            seen["state_file"] = json.loads(state_file.read_text())

        def company(app):
            app.window.nav.pop()
            self.visible(app).folder_rows[1].emit("activate")
            detail = self.visible(app)
            seen["company"] = (detail.header_info(), detail.facts(), detail.empty_conversations_text(),
                               detail.button_states())

        self.run_steps(model, [self.activate(model), open_default, self.shot("pasta"), log_out,
                               self.answer(seen, "dialog", "confirm"), logged_out, after, company,
                               self.shot("pasta-sem-login")])

        self.assertTrue(seen["class"])
        self.assertEqual(seen["title"], "~/.claude")
        self.assertEqual(seen["header"], {"name": "~/.claude", "profiles": "Perfis: Pessoal",
                                          "status": "eu@pessoal.example"})
        self.assertEqual(seen["facts"], [("Conta", "eu@pessoal.example · Pessoal Org"), ("Projetos", "3"),
                                         ("Conversas", "12"), ("Última atividade", "há 2 horas")])
        self.assertEqual(seen["conversations"], [f"Conversa {i}" for i in range(8)], "at most 8, like the Mac")
        title, project, when = seen["first_conversation"]
        self.assertEqual((title, project), ("Conversa 0", "~/code/projeto-0"))
        self.assertRegex(when, r"^\d{1,2} de [a-z]{3}\. de \d{4}, \d{2}:\d{2}$")
        self.assertEqual(seen["captions"], [
            "Lido só neste computador: e-mail do login e título, projeto e data de cada conversa.",
            "Login da pasta: a conta usada quando o perfil não escolhe uma conta Claude extra (e sempre nos apps "
            "gráficos).",
        ])
        self.assertEqual(seen["buttons"], {"Sair do login da pasta…": True, "Entrar…": True})
        self.assertEqual(seen["dialog"], (
            "Sair do seu login principal do Claude Code (~/.claude)?",
            "~/.claude é a pasta padrão do Claude Code. O login dela será apagado e será preciso entrar de novo. "
            "Conversas, agents e skills ficam.", "Sair", True))
        self.assertEqual(seen["after"], ("Sem login", False))
        self.assertEqual(seen["state_file"], {"numStartups": 3}, "the simulated logout removed only the login")
        header, facts, empty, buttons = seen["company"]
        self.assertEqual(header, {"name": "~/.claude-empresa", "profiles": "Perfis: Trabalho, Cliente X",
                                  "status": "Sem login"})
        self.assertEqual(facts, [("Conta", "sem login"), ("Projetos", "0"), ("Conversas", "0"),
                                 ("Última atividade", "nunca")])
        self.assertEqual(empty, "Nenhuma conversa nesta pasta.")
        self.assertEqual(buttons, {"Sair do login da pasta…": False, "Entrar…": True})

    # MARK: the folder's own login

    def test_folder_login_runs_claude_in_the_folder_and_the_folder_becomes_logged_in(self):
        from multicred.ui.pages.claude import ClaudeFolderLoginPage

        # After the login the page reads the folder's status for real (refresh_folder_status):
        # the dry-run `claude auth status` says nothing, so it comes from the .claude.json
        # the simulator wrote into the folder.
        model = self.make_model(self.full_config())
        seen = {}

        def open_login(app):
            page = self.open_claude(app)
            page.folder_rows[1].buttons["Entrar…"].emit("clicked")
            login = self.visible(app)
            seen["class"] = isinstance(login, ClaudeFolderLoginPage)
            seen["section"] = (app.window.current_section, app.window.selected_sidebar_key())
            seen["title"] = login.get_title()
            seen["header"] = login.header_info()
            seen["notice"] = login.test_notice_text()

        def waiting(app):
            session = self.visible(app).session
            return session is None or not session.phase.accepts_code or "Paste code" not in session.output

        def inspect_waiting(app):
            page = self.visible(app)
            seen["waiting"] = (page.header_info()["status"], page.message_text())
            seen["code_row"] = (page.code_label_text(), page.code_entry.get_placeholder_text(),
                                page.connect_button.get_label(), page.connect_button.get_sensitive())
            seen["footer"] = page.footer_labels()
            seen["output_hidden"] = not page.terminal.get_visible()
            seen["browser_line"] = page.browser_line.get_visible()
            page.output_toggle.emit("clicked")
            seen["footer_open"] = page.footer_labels()
            page.code_entry.set_text(CODE)
            seen["connect_with_code"] = page.connect_button.get_sensitive()

        def send(app):
            self.visible(app).connect_button.emit("clicked")

        def connected(app):
            return self.visible(app).session.phase.kind != "connected"

        def inspect_done(app):
            page = self.visible(app)
            seen["done"] = (page.header_info()["status"], page.message_text(), page.stored_notice_text())
            seen["done_footer"] = page.footer_labels()
            seen["terminal"] = (page.terminal.directory, page.terminal.text())
            seen["code_row_done"] = page.code_row.get_visible()

        def logged_in(app):
            status = model.folder_status(self.company)
            return status is None or not status.is_logged_in

        def finish(app):
            self.visible(app).done_button.emit("clicked")
            page = self.visible(app)
            seen["back"] = type(page).__name__
            seen["row"] = page.folder_rows[1].info()

        self.run_steps(model, [self.activate(model), open_login, waiting, inspect_waiting,
                               self.shot("login-pasta-andamento"), send, connected, inspect_done,
                               self.shot("login-pasta-concluido"), logged_in, finish])

        self.assertTrue(seen["class"], "a page in the app, not a dialog with a command to copy")
        self.assertEqual(seen["section"], ("claude", "claude"))
        self.assertEqual(seen["title"], "Entrar no Claude")
        self.assertEqual(seen["header"], {"title": "Login da pasta ~/.claude-empresa",
                                          "profiles": "Perfis: Trabalho, Cliente X",
                                          "status": "Aguardando navegador"})
        self.assertEqual(seen["notice"], "Simulação do modo de teste: o navegador não abre. Digite qualquer código "
                                         "para conectar, ou \"erro\" para ver a tela de erro.")
        self.assertEqual(seen["waiting"], ("Aguardando código", "Autorize no navegador. Se ele mostrar um código, "
                                                                "cole abaixo; se a página disser que deu certo, é só "
                                                                "aguardar."))
        self.assertEqual(seen["code_row"], ("Código", "Cole o código de autorização", "Conectar", False))
        self.assertEqual(seen["footer"], ["Mostrar saída", "Cancelar"])
        self.assertTrue(seen["output_hidden"])
        self.assertFalse(seen["browser_line"], "the simulator's link is never opened (Mac)")
        self.assertEqual(seen["footer_open"], ["Ocultar saída", "Cancelar"])
        self.assertTrue(seen["connect_with_code"])
        self.assertEqual(seen["done"], ("Conectada", "Pronto. Os perfis que usam ~/.claude-empresa entram com esta "
                                                     "conta quando não escolhem outra conta Claude.",
                                        "Credenciais guardadas pelo Claude Code. O MultiCred não as armazena."))
        self.assertEqual(seen["done_footer"], ["Ocultar saída", "Concluir"])
        directory, terminal = seen["terminal"]
        self.assertEqual(directory, "~/.claude-empresa")
        self.assertIn("$ claude auth login (simulado)", terminal)
        self.assertNotIn(CODE, terminal)
        self.assertFalse(seen["code_row_done"])
        self.assertTrue((Path(self.company) / ".credentials.json").exists(),
                        "Claude Code (the simulator) wrote the login into the folder itself")
        self.assertFalse((self.sandbox / ".claude" / ".credentials.json").exists(), "the default folder is untouched")
        self.assertEqual(seen["back"], "ClaudePage", "Concluir goes back to the list (Mac: path = [])")
        self.assertEqual(seen["row"]["status"], "simulado@example.com", "logged in: the login's e-mail (folderPill)")
        self.assertEqual(seen["row"]["buttons"], [])

    def test_folder_login_failure_retry_and_leaving_stops_it(self):
        from multicred.ui.pages.claude import ClaudeFolderLoginPage

        model = self.make_model(self.full_config())
        seen = {}

        def open_login(app):
            app.window.on_login_requested("folder", self.company)  # the profile editor's and Verificar's "Entrar…"
            seen["class"] = isinstance(self.visible(app), ClaudeFolderLoginPage)
            seen["page"] = self.visible(app)

        def waiting(app):
            session = seen["page"].session
            return session is None or not session.phase.accepts_code or "Paste code" not in session.output

        def bad_code(app):
            page = seen["page"]
            page.code_entry.set_text("erro")
            page.code_entry.emit("activate")

        def failed(app):
            return not seen["page"].session.phase.is_failure

        def inspect_failed(app):
            page = seen["page"]
            seen["failed"] = (page.header_info()["status"], page.message_text())
            seen["output"] = page.terminal.get_visible()
            seen["footer"] = page.footer_labels()
            seen["retry"] = page.connect_button.get_label()

        def retry(app):
            page = seen["page"]
            seen["old_session"] = page.session
            page.connect_button.emit("clicked")
            seen["retried"] = page.session is not seen["old_session"]

        def leave(app):
            seen["session"] = seen["page"].session
            app.window.nav.pop()

        def stopped(app):
            return seen["session"].is_running

        self.run_steps(model, [self.activate(model), open_login, waiting, bad_code, failed, inspect_failed,
                               self.shot("login-pasta-erro"), retry, waiting, leave, stopped])

        self.assertTrue(seen["class"], "\"Entrar…\" from elsewhere lands on the folder login page")
        self.assertEqual(seen["failed"], ("Erro", "Não deu certo (Error: Invalid code or expired). O código vale por "
                                                  "poucos minutos: gere outro no navegador e tente de novo."))
        self.assertTrue(seen["output"], "the output opens on failure (Mac)")
        self.assertEqual(seen["footer"], ["Copiar log", "Voltar"])
        self.assertEqual(seen["retry"], "Tentar de novo")
        self.assertTrue(seen["retried"], "Tentar de novo starts a fresh attempt")
        self.assertFalse((Path(self.company) / ".credentials.json").exists())

    # MARK: the folder's logout from the login's result

    def test_logging_a_folder_out_asks_first_and_runs_the_logout(self):
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.models import ClaudeFolder

        (Path(self.company) / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": None}}))
        (Path(self.company) / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"sk-ant-oat01-SIM"}}')
        model = self.make_model(self.full_config())
        model.claude_statuses[ClaudeFolder.own_login(self.company).id] = ClaudeLoginStatus.logged_in(None)
        seen = {}

        def open_company(app):
            page = self.open_claude(app)
            seen["row"] = page.folder_rows[1].info()["status"]
            page.folder_rows[1].emit("activate")

        def ask(app):
            self.visible(app).logout_button.emit("clicked")

        def cancelled(app):
            if app.window.get_visible_dialog() is not None:
                return True  # wait until it is gone
            seen["still_logged_in"] = (Path(self.company) / ".credentials.json").exists()
            return False

        def logged_out(app):
            status = model.folder_status(self.company)
            return status is None or status.kind != "loggedOut"

        self.run_steps(model, [open_company, ask, self.answer(seen, "cancelled", "cancel"), cancelled,
                               ask, self.answer(seen, "dialog", "confirm"), logged_out])

        self.assertEqual(seen["row"], "Logada", "logged in without a known e-mail (folderPill)")
        self.assertTrue(seen["still_logged_in"], "nothing happens without the confirmation")
        self.assertEqual(seen["dialog"], ("Sair do login de ~/.claude-empresa?",
                                          "O Claude Code apaga o login desta pasta. Conversas, agents e skills "
                                          "ficam; para usar de novo, entre outra vez.", "Sair", False))
        self.assertFalse((Path(self.company) / ".credentials.json").exists())

    # MARK: window

    def test_ctrl_comma_opens_ajustes_and_ctrl_q_quits(self):
        """The Mac's menu shortcuts ⌘, and ⌘Q (AppDelegate.swift; inventory C rows 17-18)."""
        model = self.make_model(self.full_config())
        seen = {}

        def shortcuts(app):
            window = app.window
            seen["comma"] = app.get_actions_for_accel("<Control>comma")
            seen["q"] = app.get_actions_for_accel("<Control>q")
            window.activate_action("win.settings", None)
            seen["section"] = (window.current_section, window.selected_sidebar_key())
            quits = []
            original = app.quit
            app.quit = lambda: (quits.append(True), original())
            window.activate_action("win.quit", None)
            seen["quit"] = bool(quits)

        self.run_steps(model, [shortcuts])
        self.assertEqual(seen["comma"], ["win.settings"])
        self.assertEqual(seen["q"], ["win.quit"])
        self.assertEqual(seen["section"], ("settings", "settings"))
        self.assertTrue(seen["quit"], "Ctrl+Q quits the app")

    def test_a_profile_opens_its_editor_as_the_root_of_its_row(self):
        from multicred.ui.pages.profile_editor import ProfileEditorPage

        model = self.make_model(self.full_config())
        seen = {}

        def click(app):
            window = app.window
            profile = model.config.profiles[1]
            window.sidebar.select_row(window.sidebar_row(f"profile:{profile.id}"))
            page = window.nav.get_visible_page()
            seen["editor"] = isinstance(page, ProfileEditorPage)
            seen["stack"] = window.nav.get_navigation_stack().get_n_items()
            seen["selected"] = window.selected_sidebar_key() == f"profile:{profile.id}"
            seen["title"] = window.content_page.get_title()

        self.run_steps(model, [click])
        self.assertTrue(seen["editor"])
        self.assertEqual(seen["stack"], 1, "Mac: the editor is the page of the profile's row, no back button")
        self.assertTrue(seen["selected"])
        self.assertEqual(seen["title"], "Editar perfil")


if __name__ == "__main__":
    unittest.main()
