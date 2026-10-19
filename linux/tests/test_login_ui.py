"""S-04 (R-04): the login flows are pages pushed in the content, as on the Mac.

"Adicionar conta Claude" / "Entrar de novo em X" (AddClaudeAccountPage.swift) and
"Entrar no GitHub" (GitHubLoginPage, AccountsPages.swift:340-446), driven in test
mode with the simulators (no real `claude`/`gh`, the browser never opens). With
MULTICRED_SCREENS_DIR set, each state is rendered (stock light Adwaita) into
S-04-*.png.

Runs only under tests/run-ui-tests.sh (private Xvfb display and private bus):
without it GTK would fall back to the user's real Wayland desktop.
"""

import os
import shutil
import tempfile
import unittest
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
TOKEN = "sk-ant-oat01-" + "colado" * 8


class SandboxTools:
    """git/gh/claude "found" at fake paths: the dry-run runner never executes them, and
    test mode runs the login simulators whatever is found (resolve_login_tool)."""

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
class LoginPageTests(unittest.TestCase):
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
        self.model.refresh_claude_statuses = lambda on_done=None: None
        self.model.load_folder_summaries = lambda on_done=None: None
        self.screens = os.environ.get("MULTICRED_SCREENS_DIR")
        self.shots = []

    # MARK: harness

    def run_steps(self, steps, seconds=40):
        """Runs `steps` one after another on the main loop; a step returns True to run again."""
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

    def activate_first_profile(self, app):
        """A step: really activates "Trabalho" (dry-run sandbox), so "Usar agora" can switch."""
        profile_id = self.model.config.profiles[0].id
        if not getattr(self, "_activating", False):
            self._activating = True
            self.activation_error = None
            self.model.switch_profile(profile_id, lambda _r, error: setattr(self, "activation_error", error))
            if self.screens:
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
                app.window.set_default_size(980, 700)
            return True
        if self.activation_error is not None:
            raise AssertionError(f"could not activate the profile: {self.activation_error}")
        return self.model.switching or self.model.state.profile_id != profile_id

    def shot(self, name):
        """A step: renders the window into S-04-<name>.png when MULTICRED_SCREENS_DIR is set."""
        tries = [0]

        def step(app):
            if not self.screens:
                return False
            if tries[0] < 8:  # let the page transition and the layout settle
                tries[0] += 1
                return True
            Path(self.screens).mkdir(parents=True, exist_ok=True)
            app.window.dismiss_toast()
            if save_png(app.window, Path(self.screens) / f"S-04-{name}.png"):
                self.shots.append(name)
                return False
            if tries[0] > 30:
                raise AssertionError(f"nothing rendered for {name}")
            tries[0] += 1
            return True
        return step

    @staticmethod
    def visible(app):
        return app.window.nav.get_visible_page()

    # MARK: Adicionar conta Claude — browser

    def test_add_claude_account_in_the_browser_reaches_done_and_uses_it_now(self):
        from multicred.core.claude.login_flow import LoginPhase
        from multicred.core.models import ClaudeAccountKind, ProfileColor, uuid_text
        from multicred.ui.pages.add_claude import AddClaudeAccountPage
        seen = {}

        def open_page(app):
            app.window.open_page("add-claude")

        def check_setup(app):
            page = self.visible(app)
            seen["page"] = isinstance(page, AddClaudeAccountPage)
            seen["section"] = (app.window.current_section, app.window.selected_sidebar_key())
            seen["title"] = page.get_title()
            seen["state"] = page.state
            seen["intro"] = page.intro_label.get_label()
            seen["form"] = page.form_group.get_visible()
            seen["placeholders"] = (page.nickname_entry.get_placeholder_text(), page.email_entry.get_placeholder_text())
            seen["rows"] = [page.nickname_row.get_title(), page.email_row.get_title(), page.color_row.get_title()]
            seen["sources"] = [page.browser_toggle.get_label(), page.token_toggle.get_label()]
            seen["source_visible"] = page.source_box.get_visible()
            seen["browser_text"] = page.browser_text.get_label()
            seen["browser_row"] = page.browser_row.get_title()
            seen["browser_options"] = page.browser_options()
            seen["test_notice"] = (page.test_notice.get_visible(), page.test_notice_label.get_label())
            seen["buttons"] = (page.setup_cancel_button.get_label(), page.continue_button.get_label())
            seen["continue_without_name"] = page.continue_button.get_sensitive()
            page.nickname_entry.set_text("Marco")
            page.email_entry.set_text("marco@exemplo.com")
            page.color_buttons[ProfileColor.ORANGE.value].set_active(True)
            seen["continue_with_name"] = page.continue_button.get_sensitive()

        def start(app):
            self.visible(app).continue_button.emit("clicked")

        def waiting(app):
            page = self.visible(app)
            return page.session is not None and page.session.phase == LoginPhase.WAITING_FOR_CODE

        def check_running(app):
            page = self.visible(app)
            seen["running_state"] = page.state
            seen["identity"] = (page.identity_title.get_label(), page.identity_subtitle.get_label())
            seen["step_titles"] = [s.title_text for s in page.steps]
            seen["steps_waiting"] = [s.state for s in page.steps]
            seen["link_status"] = page.link_status_label.get_label()
            seen["link_actions_in_test_mode"] = page.link_actions.get_visible()
            seen["code_placeholder"] = page.code_entry.get_placeholder_text()
            seen["send"] = (page.send_button.get_label(), page.send_button.get_sensitive())
            seen["details"] = (page.details_expander.get_label(), page.details_expander.get_expanded())
            seen["running_cancel"] = page.running_cancel_button.get_label()
            page.details_expander.set_expanded(True)

        def send_code(app):
            page = self.visible(app)
            page.code_entry.set_text(CODE)
            seen["send_with_code"] = page.send_button.get_sensitive()
            page.send_button.emit("clicked")
            seen["steps_connecting"] = [s.state for s in page.steps]
            seen["connecting_text"] = (page.connecting_label.get_visible(), page.connecting_label.get_label())

        def done(app):
            return self.visible(app).state == "done"

        def check_done(app):
            page = self.visible(app)
            seen["terminal"] = page.terminal.text()
            seen["directory"] = page.terminal.directory
            seen["done_title"] = page.done_title.get_label()
            seen["done_text"] = page.done_text.get_label()
            seen["use_now"] = (page.use_now_button.get_visible(), page.use_now_button.get_label())
            seen["done_button"] = page.done_button.get_label()
            seen["account"] = page.saved

        def use_now(app):
            self.visible(app).use_now_button.emit("clicked")

        def switched(app):
            return self.model.state.applied_claude_account_id is not None and not self.model.switching

        def back_at_root(app):
            return self.visible(app) is app.window._section_pages.get("claude")

        self.run_steps([self.activate_first_profile, open_page, check_setup, self.shot("adicionar-navegador"),
                        start, lambda app: not waiting(app),
                        check_running, self.shot("adicionar-andamento"), send_code, lambda app: not done(app),
                        check_done, self.shot("adicionar-concluido"), use_now, lambda app: not switched(app),
                        lambda app: not back_at_root(app)])

        self.assertTrue(seen["page"], "a page pushed in the content, not a dialog")
        self.assertEqual(seen["section"], ("claude", "claude"))
        self.assertEqual(seen["title"], "Adicionar conta Claude")
        self.assertEqual(seen["state"], "setup")
        self.assertEqual(seen["intro"], "Uma conta Claude é só o login. Usada num perfil, ela mantém os agents, "
                                        "skills e conversas da pasta do perfil, e a troca vale até nas conversas "
                                        "já abertas.")
        self.assertTrue(seen["form"])
        self.assertEqual(seen["placeholders"], ("Ex.: Marco", "opcional"))
        self.assertEqual(seen["rows"], ["Apelido", "E-mail da conta", "Cor"])
        self.assertEqual(seen["sources"], ["Entrar pelo navegador", "Já tenho um token"])
        self.assertTrue(seen["source_visible"])
        self.assertIn("O link do Claude abre no navegador escolhido abaixo. Entre com a conta que você quer "
                      "adicionar, clique em Autorizar e copie o código mostrado.", seen["browser_text"])
        self.assertIn("chaveiro do sistema", seen["browser_text"])
        self.assertNotIn("Keychain", seen["browser_text"])
        self.assertEqual(seen["browser_row"], "Abrir o link em")
        self.assertTrue(seen["browser_options"][0].startswith("Navegador padrão"), seen["browser_options"])
        self.assertEqual(seen["browser_options"][-1], "Não abrir, só copiar o link")
        self.assertEqual(seen["test_notice"], (True, "Modo de teste: simulação; digite qualquer código."))
        self.assertEqual(seen["buttons"], ("Cancelar", "Continuar no navegador"))
        self.assertFalse(seen["continue_without_name"])
        self.assertTrue(seen["continue_with_name"])

        self.assertEqual(seen["running_state"], "running")
        self.assertEqual(seen["identity"], ("Marco", "marco@exemplo.com"))
        self.assertEqual(seen["step_titles"], ["Entre no navegador com a conta que você quer usar",
                                               "Clique em Autorizar e copie o código", "Salvar o login"])
        self.assertEqual(seen["steps_waiting"], ["done", "active", "pending"])
        self.assertTrue(seen["link_status"].startswith("O link abriu em Navegador padrão"), seen["link_status"])
        self.assertIn("Abriu no navegador errado, ou já logado em outra conta? Copie o link e cole em outro "
                      "navegador ou numa janela anônima.", seen["link_status"])
        self.assertFalse(seen["link_actions_in_test_mode"], "the simulator has no link to open (Mac too)")
        self.assertEqual(seen["send"], ("Enviar", False))
        self.assertTrue(seen["send_with_code"])
        self.assertEqual(seen["details"], ("Detalhes técnicos", False), "collapsed by default")
        self.assertEqual(seen["running_cancel"], "Cancelar")
        self.assertEqual(seen["steps_connecting"], ["done", "done", "active"])
        self.assertEqual(seen["connecting_text"], (True, "Concluindo o login…"))

        self.assertEqual(seen["directory"], "pasta temporária (apagada no fim)")
        self.assertIn("$ claude auth login --claudeai", seen["terminal"])
        self.assertNotIn(CODE, seen["terminal"])
        self.assertEqual(seen["done_title"], "Conta Marco adicionada.")
        self.assertEqual(seen["done_text"], "O login está só no chaveiro do sistema. Use agora no perfil ativo: a "
                                            "pasta continua a mesma e até as conversas já abertas passam para "
                                            "esta conta.")
        self.assertEqual(seen["use_now"], (True, "Usar agora em Trabalho"))
        self.assertEqual(seen["done_button"], "Concluir")

        account = seen["account"]
        self.assertEqual((account.nickname, account.kind, account.color),
                         ("Marco", ClaudeAccountKind.LOGIN, ProfileColor.ORANGE))
        self.assertIn(account.id, [a.id for a in self.model.config.claude_accounts])
        self.assertIn(uuid_text(account.id), self.environment.claude_credentials.vault)
        self.assertEqual(self.model.state.applied_claude_account_id, account.id, "\"Usar agora\" switched Claude")
        self.assertEqual(list((self.sandbox / ".config/multicred/scratch").iterdir()), [],
                         "the throwaway folder is gone")

    # MARK: Adicionar conta Claude — "Já tenho um token"

    def test_pasting_a_token_saves_it_without_running_the_cli(self):
        from multicred.core.models import ClaudeAccountKind
        seen = {}

        def open_page(app):
            app.window.open_page("add-claude")

        def choose_token(app):
            page = self.visible(app)
            page.nickname_entry.set_text("Cliente Y")
            page.token_toggle.set_active(True)
            seen["token_text"] = page.token_text.get_label()
            seen["browser_hidden"] = not page.browser_box.get_visible()
            seen["token_visible"] = page.token_box.get_visible()
            seen["placeholder"] = page.token_entry.get_property("placeholder-text")
            seen["buttons"] = (page.token_cancel_button.get_label(), page.save_button.get_label())
            seen["save_empty"] = page.save_button.get_sensitive()
            page.token_entry.set_text(TOKEN)
            seen["save_filled"] = page.save_button.get_sensitive()

        def save(app):
            page = self.visible(app)
            page.save_button.emit("clicked")
            seen["state"] = page.state
            seen["session"] = page.session
            seen["done_title"] = page.done_title.get_label()
            seen["done_text"] = page.done_text.get_label()
            seen["account"] = page.saved
            seen["token_cleared"] = page.token_entry.get_text() == ""

        self.run_steps([self.activate_first_profile, open_page, choose_token, self.shot("adicionar-token"),
                        save, self.shot("adicionar-token-concluido")])

        self.assertEqual(seen["token_text"], "Se você já rodou \"claude setup-token\" num terminal, cole o token "
                                             "(começa com sk-ant-oat). Um token vale só para terminais novos: as "
                                             "conversas já abertas não trocam.")
        self.assertTrue(seen["browser_hidden"])
        self.assertTrue(seen["token_visible"])
        self.assertEqual(seen["placeholder"], "sk-ant-oat01-…")
        self.assertEqual(seen["buttons"], ("Cancelar", "Salvar"))
        self.assertFalse(seen["save_empty"])
        self.assertTrue(seen["save_filled"])
        self.assertEqual(seen["state"], "done")
        self.assertIsNone(seen["session"], "the CLI does not run for a pasted token")
        self.assertEqual(seen["done_title"], "Conta Cliente Y adicionada.")
        self.assertEqual(seen["done_text"], "O token está só no chaveiro do sistema. Vale para terminais novos; "
                                            "escolha esta conta no editor de um perfil ou use agora no perfil "
                                            "ativo.")
        self.assertTrue(seen["token_cleared"])
        account = seen["account"]
        self.assertEqual(account.kind, ClaudeAccountKind.TOKEN)
        self.assertEqual(self.environment.secrets.stored_value(account.secret_account), TOKEN.encode())
        commands = [entry.command for entry in self.environment.runner.entries]
        self.assertFalse(any("claude" in c for c in commands), commands)

    # MARK: Entrar de novo em X

    def test_signing_in_again_skips_the_form_and_converts_a_token_account(self):
        from multicred.core.claude.login_flow import LoginPhase
        from multicred.core.models import ClaudeAccount, ClaudeAccountKind, ProfileColor, uuid_text
        from multicred.ui.pages.add_claude import AddClaudeAccountPage

        account = ClaudeAccount(nickname="Dev", email="dev@x.com", color=ProfileColor.BLUE,
                                kind=ClaudeAccountKind.TOKEN)
        seen = {}

        def add_account(app):
            config = self.model.config
            config.claude_accounts.append(account)
            self.model.save(config)
            self.environment.secrets.set_secret(b"sk-ant-oat01-old", account.secret_account)
            app.window.show_section("main")
            app.window.on_login_requested("login", account)

        def check_setup(app):
            page = self.visible(app)
            seen["page"] = isinstance(page, AddClaudeAccountPage)
            seen["section"] = app.window.current_section
            seen["title"] = page.get_title()
            seen["form"] = page.form_group.get_visible()
            seen["source"] = page.source_box.get_visible()
            seen["browser"] = page.browser_box.get_visible()
            seen["continue"] = page.continue_button.get_sensitive()

        def waiting(app):
            page = self.visible(app)
            return page.session is not None and page.session.phase == LoginPhase.WAITING_FOR_CODE

        def check_running(app):
            page = self.visible(app)
            seen["identity"] = (page.identity_title.get_label(), page.identity_subtitle.get_label())
            page.code_entry.set_text(CODE)
            page.code_entry.emit("activate")

        def check_done(app):
            page = self.visible(app)
            seen["done_title"] = page.done_title.get_label()
            seen["done_text"] = page.done_text.get_label()

        self.run_steps([self.activate_first_profile, add_account, check_setup, self.shot("entrar-de-novo"),
                        lambda app: self.visible(app).continue_button.emit("clicked"),
                        lambda app: not waiting(app), check_running,
                        lambda app: self.visible(app).state != "done", check_done])

        self.assertTrue(seen["page"])
        self.assertEqual(seen["section"], "claude", "opened in the Claude section, as on the Mac")
        self.assertEqual(seen["title"], "Entrar de novo em Dev")
        self.assertFalse(seen["form"], "no nickname/e-mail/color form")
        self.assertFalse(seen["source"], "no browser/token choice")
        self.assertTrue(seen["browser"])
        self.assertTrue(seen["continue"])
        self.assertEqual(seen["identity"], ("Dev", "dev@x.com"))
        self.assertEqual(seen["done_title"], "Login de Dev atualizado.")
        self.assertTrue(seen["done_text"].startswith("O login está só no chaveiro do sistema."))
        updated = self.model.config.claude_account(account.id)
        self.assertEqual(updated.kind, ClaudeAccountKind.LOGIN, "the browser flow is a full login (Mac D39)")
        self.assertIn(uuid_text(account.id), self.environment.claude_credentials.vault)
        self.assertEqual(len(self.model.config.claude_accounts), 1)

    # MARK: failures, cancel, stall

    def test_rejected_code_shows_the_error_retry_and_cancel_back_to_the_form(self):
        from multicred.core.claude.login_flow import LoginPhase
        seen = {}

        def open_page(app):
            app.window.open_page("add-claude")
            page = self.visible(app)
            page.nickname_entry.set_text("Cliente X")
            page.continue_button.emit("clicked")

        def waiting(app):
            page = self.visible(app)
            return (page.session is not None and page.session.phase.accepts_code
                    and "Paste code" in page.session.output)

        def send_bad_code(app):
            page = self.visible(app)
            page.code_entry.set_text("erro")
            page.send_button.emit("clicked")

        def check_failed(app):
            page = self.visible(app)
            seen["steps"] = [s.state for s in page.steps]
            seen["error"] = (page.error_label.get_visible(), page.error_label.get_label())
            seen["retry"] = (page.retry_button.get_visible(), page.retry_button.get_label())
            seen["details_open"] = page.details_expander.get_expanded()
            page.running_cancel_button.emit("clicked")
            seen["after_cancel"] = page.state
            seen["name_kept"] = page.nickname_entry.get_text()
            seen["session_after_cancel"] = page.session

        self.run_steps([self.activate_first_profile, open_page, lambda app: not waiting(app), send_bad_code,
                        lambda app: not self.visible(app).session.phase.is_finished, self.shot("adicionar-erro"),
                        check_failed])

        self.assertEqual(seen["steps"], ["done", "done", "failed"])
        self.assertTrue(seen["error"][0])
        self.assertEqual(seen["error"][1], "Não deu certo (Error: Invalid code or expired).")
        self.assertEqual(seen["retry"], (True, "Tentar de novo"))
        self.assertFalse(seen["details_open"], "Detalhes técnicos stays as the user left it (Mac)")
        self.assertEqual(seen["after_cancel"], "setup", "Cancelar while running goes back to the form (Mac)")
        self.assertEqual(seen["name_kept"], "Cliente X")
        self.assertIsNone(seen["session_after_cancel"])
        self.assertEqual(self.environment.claude_credentials.vault, {})
        self.assertEqual([a.nickname for a in self.model.config.claude_accounts], [])

    def test_a_slow_answer_shows_the_stall_notice_and_leaving_the_page_stops_the_login(self):
        from multicred.ui.pages import add_claude
        seen = {}
        self.addCleanup(setattr, add_claude.AddClaudeAccountPage, "STALL_AFTER_MS",
                        add_claude.AddClaudeAccountPage.STALL_AFTER_MS)
        add_claude.AddClaudeAccountPage.STALL_AFTER_MS = 150  # the simulator answers after 1 s

        def open_page(app):
            app.window.open_page("add-claude")
            page = self.visible(app)
            page.nickname_entry.set_text("Lenta")
            page.continue_button.emit("clicked")
            seen["page"] = page

        def waiting(app):
            page = seen["page"]
            return (page.session is not None and page.session.phase.accepts_code
                    and "Paste code" in page.session.output)

        def send(app):
            page = seen["page"]
            page.code_entry.set_text(CODE)
            page.send_button.emit("clicked")

        def stalled(app):
            return seen["page"].stall_label.get_visible()

        def check_stall(app):
            page = seen["page"]
            seen["stall"] = page.stall_label.get_label()
            seen["retry"] = page.retry_button.get_visible()
            seen["session"] = page.session
            app.window.nav.pop()

        def stopped(app):
            return not seen["session"].is_running and seen["page"].get_parent() is None

        self.run_steps([self.activate_first_profile, open_page, lambda app: not waiting(app), send,
                        lambda app: not stalled(app), check_stall, lambda app: not stopped(app)])

        self.assertEqual(seen["stall"], "Está demorando mais que o normal. Abra os detalhes técnicos abaixo ou tente "
                                        "de novo; a saída (sem códigos nem tokens) foi registrada no log.")
        self.assertTrue(seen["retry"], "\"Tentar de novo\" also when it stalls (Mac)")
        self.assertEqual([a.nickname for a in self.model.config.claude_accounts], [],
                         "leaving the page stopped the login before it was saved")

    def test_signing_in_again_during_a_switch_shows_the_busy_message_and_keeps_the_old_login(self):
        """S-23: the save of an existing account runs under the switch lock."""
        from multicred.core.claude.login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher
        from multicred.core.models import ClaudeAccount, ClaudeAccountKind, ProfileColor, uuid_text

        account = ClaudeAccount(nickname="Dev", email="dev@x.com", color=ProfileColor.BLUE,
                                kind=ClaudeAccountKind.LOGIN)
        seen = {}

        def prepare(app):
            config = self.model.config
            config.claude_accounts.append(account)
            self.model.save(config)
            ClaudeLoginSwitcher(self.environment).store_new_account(
                account.id, ClaudeLoginRecord(b'{"accessToken":"old"}', None))
            seen["old"] = self.environment.claude_credentials.vault[uuid_text(account.id)]
            held = self.model.engine.exclusive()  # a switch is running
            held.__enter__()
            self.addCleanup(held.__exit__, None, None, None)
            app.window.on_login_requested("login", account)
            self.visible(app).continue_button.emit("clicked")

        def waiting(app):
            page = self.visible(app)
            return (page.session is not None and page.session.phase.accepts_code
                    and "Paste code" in page.session.output)

        def send(app):
            page = self.visible(app)
            page.code_entry.set_text(CODE)
            page.send_button.emit("clicked")

        def check(app):
            seen["error"] = self.visible(app).error_label.get_label()

        self.run_steps([prepare, lambda app: not waiting(app), send,
                        lambda app: not self.visible(app).session.phase.is_finished, check])
        self.assertIn("Uma troca já está em andamento.", seen["error"])
        self.assertEqual(self.environment.claude_credentials.vault[uuid_text(account.id)], seen["old"])

    # MARK: Entrar no GitHub

    def test_github_login_page_reaches_connected_with_the_mac_texts(self):
        from multicred.core.adapters.github_login_flow import GitHubLoginSimulator
        from multicred.ui.pages.login import GitHubLoginPage
        seen = {}
        imported = []
        self.model.import_github_accounts = lambda on_done: imported.append(on_done)

        def open_page(app):
            app.window.show_section("main")
            app.window.on_login_requested("github", None)
            page = self.visible(app)
            seen["page"] = isinstance(page, GitHubLoginPage)
            seen["section"] = (app.window.current_section, app.window.selected_sidebar_key())
            seen["title"] = (page.get_title(), page.heading_label.get_label(), page.host_label.get_label())
            seen["test_notice"] = page.test_notice.get_visible() and page.test_notice_label.get_label()
            seen["first_pill"] = page.status_pill.get_label()

        def code_shown(app):
            page = self.visible(app)
            return page.session is not None and page.session.phase.kind == "showingCode"

        def check_code(app):
            page = self.visible(app)
            seen["pill_code"] = page.status_pill.get_label()
            seen["instructions"] = page.instructions_label.get_label()
            seen["code"] = page.code_label.get_label()
            seen["open"] = (page.open_button.get_label(), page.open_button.get_sensitive())
            seen["cancel"] = page.cancel_button.get_visible() and page.cancel_button.get_label()
            seen["toggle"] = page.output_toggle_label.get_label()
            seen["terminal_hidden"] = not page.terminal.get_visible()
            page.output_toggle.emit("clicked")
            seen["toggle_open"] = page.output_toggle_label.get_label()
            seen["terminal_shown"] = page.terminal.get_visible()
            seen["command"] = page.terminal.command

        def open_github(app):
            page = self.visible(app)
            page.open_button.emit("clicked")
            seen["pill_browser"] = page.status_pill.get_label()
            seen["waiting"] = page.waiting_label.get_visible() and page.waiting_label.get_label()
            seen["open_after"] = page.open_button.get_sensitive()

        def connected(app):
            page = self.visible(app)
            return page.session.phase.kind == "connected" and page.reported

        def check_connected(app):
            page = self.visible(app)
            seen["pill_done"] = page.status_pill.get_label()
            seen["done"] = page.done_label.get_label()
            seen["buttons"] = [b.get_label() for b in (page.done_button, page.back_button, page.retry_button,
                                                       page.cancel_button) if b.get_visible()]

        def finish(app):
            self.visible(app).done_button.emit("clicked")

        def at_root(app):
            return self.visible(app) is app.window._section_pages.get("github")

        self.run_steps([self.activate_first_profile, open_page, lambda app: not code_shown(app), check_code,
                        self.shot("github-codigo"), open_github, lambda app: not connected(app), check_connected,
                        self.shot("github-conectada"), finish, lambda app: not at_root(app)])

        self.assertTrue(seen["page"], "a page pushed in the content, not a dialog")
        self.assertEqual(seen["section"], ("github", "github"))
        self.assertEqual(seen["title"], ("Entrar no GitHub", "Entrar no GitHub", "github.com"))
        self.assertEqual(seen["test_notice"],
                         "Simulação do modo de teste: o navegador não abre e nenhum login real acontece.")
        self.assertIn(seen["first_pill"], ("Iniciando", "Aguardando você"))
        self.assertEqual(seen["pill_code"], "Aguardando você")
        self.assertEqual(seen["instructions"],
                         "1. Copie este código. 2. Abra o GitHub e cole o código. 3. Autorize o GitHub CLI.")
        self.assertEqual(seen["code"], "TEST-1234")
        self.assertEqual(seen["open"], ("Copiar código e abrir o GitHub", True))
        self.assertEqual(seen["cancel"], "Cancelar")
        self.assertEqual(seen["toggle"], "Mostrar saída")
        self.assertTrue(seen["terminal_hidden"])
        self.assertEqual(seen["toggle_open"], "Ocultar saída")
        self.assertTrue(seen["terminal_shown"])
        self.assertEqual(seen["command"], "gh auth login --web (simulado)")
        self.assertEqual(seen["pill_browser"], "Aguardando navegador")
        self.assertEqual(seen["waiting"], "Aguardando a autorização no navegador…")
        self.assertFalse(seen["open_after"])
        self.assertEqual(seen["pill_done"], "Conectada")
        self.assertEqual(seen["done"], f"Pronto: {GitHubLoginSimulator.SIMULATED_LOGIN} está logada no gh. "
                                       "A conta ativa voltou para a do perfil atual.")
        self.assertEqual(seen["buttons"], ["Concluir"])
        self.assertEqual(len(imported), 1, "the logins gh now knows are added (Mac didLogInToGitHub)")
        commands = [entry.command for entry in self.environment.runner.entries]
        self.assertTrue(any(c.endswith("auth switch --hostname github.com --user dev-trabalho") for c in commands),
                        commands)

    def test_github_cancel_goes_back(self):
        seen = {}

        def open_page(app):
            app.window.open_page("github")
            app.window.on_login_requested("github", None)
            seen["page"] = self.visible(app)

        def code_shown(app):
            page = seen["page"]
            return page.session is not None and page.session.phase.kind == "showingCode"

        def cancel(app):
            seen["page"].cancel_button.emit("clicked")

        def gone(app):
            return self.visible(app) is app.window._section_pages.get("github")

        self.run_steps([open_page, lambda app: not code_shown(app), cancel, lambda app: not gone(app)])
        self.assertTrue(seen["page"].session.phase.is_failure)
        self.assertFalse(seen["page"].session.is_running)


if __name__ == "__main__":
    unittest.main()
