"""S-02 (R-02): the Git, GitHub and Chaves de API pages and their editors, like the Mac.

Reference: `MultiCred/Views/AccountsPages.swift` (pages, rows, editors),
`Components.swift:141-181` (PageHeader, InfoNote) and `Shared.swift:30-74`
(AccountAvatar, StatusPill). Each page has the Mac's header inside the content
(title, subtitle, prominent action), an empty state as a status page, rows with the
"usado em" and detail columns and the Mac's status, and the explanatory note pinned at
the bottom. Editors end with "Remover / Cancelar / Salvar" (Salvar off while required
fields are empty) and show the Mac's examples in the fields. Texts are the Mac's,
with "Keychain" → "chaveiro do sistema". With MULTICRED_SCREENS_DIR set, the pages are
rendered (stock light Adwaita) into S-02-*.png.

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

SECRET = "sk-valor-que-nunca-aparece-456"

GIT_NOTE = "O MultiCred escreve só o arquivo incluído no fim do seu gitconfig global. O seu ~/.gitconfig continua seu."
GITHUB_NOTE = "O login e o token ficam com o próprio gh. O MultiCred só escolhe a conta ativa."
KEYS_NOTE = ("O valor de cada chave fica só no chaveiro do sistema. O MultiCred guarda apenas o nome da variável e "
             "qual item usar; o terminal lê o valor quando o perfil muda. As chaves não são passadas para apps "
             "gráficos.")


class NoTools:
    """Nothing found: no background gh/claude lookups run."""

    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


def accounts_config(environment, empty=False):
    from multicred.core.models import (APIKeyAccount, APIProvider, Config, GitHubAccount, GitIdentity, Profile,
                                       ProfileColor)
    if empty:
        return Config(profiles=[Profile(name="Trabalho", color=ProfileColor.INDIGO)])
    work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.example", color=ProfileColor.INDIGO)
    personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.example",
                           color=ProfileColor.ORANGE, ssh_key_path="~/.ssh/id_pessoal")
    gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    gh_personal = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE,
                                claude_co_author=False)
    gh_client = GitHubAccount(nickname="Cliente", username="dev-cliente", color=ProfileColor.TEAL)
    openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
    gemini = APIKeyAccount(nickname="Pessoal", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
    return Config(
        git_identities=[work, personal], github_accounts=[gh_work, gh_personal, gh_client],
        api_keys=[openai, gemini],
        profiles=[
            Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work.id,
                    github_account_id=gh_work.id, api_key_ids=[openai.id]),
            Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=personal.id,
                    github_account_id=gh_personal.id),
            Profile(name="Cliente X", color=ProfileColor.TEAL, git_identity_id=work.id),
        ])


def logins(*entries):
    """(login, active, usable) → GitHubLogin on github.com."""
    from multicred.core.adapters.github_cli import GitHubLogin
    return [GitHubLogin(host="github.com", login=login, active=active, state="success" if usable else "failure")
            for login, active, usable in entries]


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
class AccountsPagesTests(unittest.TestCase):
    def setUp(self):
        from multicred.core import real_env

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-accounts-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        self.gh_refreshes = []
        self.login_requests = []

    def make_model(self, config):
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        ConfigRepository(self.environment).save(config)
        model = AppModel(self.environment, NoTools(), dispatch=GLib.idle_add)
        model.refresh_claude_statuses = lambda on_done=None: None
        model.load_folder_summaries = lambda on_done=None: None
        # gh is never run: the test answers each refresh itself.
        model.refresh_github_logins = lambda on_done: self.gh_refreshes.append(on_done)
        return model

    def saved(self):
        import json
        return json.loads((self.sandbox / ".config/multicred/config.json").read_text())

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
                if app.window is not None and not getattr(app.window, "_s02_patched", False):
                    # Login flows are S-04's: record what the pages ask for.
                    app.window.on_login_requested = lambda kind, account: self.login_requests.append(
                        (kind, account))
                    app.window._s02_patched = True
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
    def open_section(app, name):
        window = app.window
        window.sidebar.select_row(window.sidebar_row(name))
        return window.nav.get_visible_page()

    @staticmethod
    def page_shape(window, page):
        return {
            "nav_title": window.content_page.get_title(),
            "header": page.header_texts(),
            "actions": page.action_labels(),
            "empty": page.showing_empty(),
            "empty_state": (page.empty_state.get_title(), page.empty_state.get_description(),
                            page.empty_state.get_icon_name()),
            "note": (page.note_text(), page.note_icon()),
        }

    @staticmethod
    def dialog_texts(window):
        dialog = window.get_visible_dialog()
        texts = (dialog.get_heading(), dialog.get_body()) if dialog is not None else None
        if dialog is not None:
            dialog.force_close()
        return texts

    # MARK: Git

    def test_git_page_empty(self):
        from multicred.ui.pages.accounts import GitAccountsPage

        model = self.make_model(accounts_config(self.environment, empty=True))
        seen = {}

        def inspect(app):
            page = self.open_section(app, "git")
            seen["class"] = isinstance(page, GitAccountsPage)
            seen["shape"] = self.page_shape(app.window, page)

        self.run_steps(model, [inspect])
        self.assertTrue(seen["class"])
        self.assertEqual(seen["shape"], {
            "nav_title": "Git",
            "header": ("Identidades git", "Nome e e-mail dos commits (e, se quiser, a chave SSH)."),
            "actions": ["Adicionar identidade"],
            "empty": True,
            "empty_state": ("Nenhuma identidade git", "Adicione o nome e o e-mail que cada perfil usa nos commits.",
                            "multicred-branch-symbolic"),
            "note": (GIT_NOTE, "help-about-symbolic"),
        })

    def test_git_rows_columns_and_edit(self):
        config = accounts_config(self.environment)
        model = self.make_model(config)
        seen = {}

        def inspect(app):
            page = self.open_section(app, "git")
            seen["empty"] = page.showing_empty()
            seen["rows"] = [row.info() for row in page.rows]
            seen["round"] = [row.avatar.has_css_class("mc-avatar-round") for row in page.rows]
            page.rows[1].buttons["Editar"].emit("clicked")
            seen["edit_button"] = app.window.nav.get_visible_page().get_title()
            app.window.nav.pop()

        def activate(app):
            page = app.window.nav.get_visible_page()
            page.rows[0].emit("activate")  # Mac: double-click edits
            editor = app.window.nav.get_visible_page()
            seen["activated"] = (editor.get_title(), editor.nickname_row.get_text())

        self.run_steps(model, [inspect, activate])
        self.assertFalse(seen["empty"])
        self.assertEqual(seen["rows"], [
            {"title": "Trabalho", "subtitle": "Dev Trabalho · dev@empresa.example", "used_in": "Trabalho, Cliente X",
             "detail": "SSH padrão", "status": None, "buttons": ["Editar"], "menu": []},
            {"title": "Pessoal", "subtitle": "Dev Pessoal · dev@pessoal.example", "used_in": "Pessoal",
             "detail": "~/.ssh/id_pessoal", "status": None, "buttons": ["Editar"], "menu": []},
        ])
        self.assertEqual(seen["round"], [True, True], "account avatars are circles, like the Mac")
        self.assertEqual(seen["edit_button"], "Editar identidade git")
        self.assertEqual(seen["activated"], ("Editar identidade git", "Trabalho"))

    def test_git_editor_footer_examples_and_save(self):
        config = accounts_config(self.environment, empty=True)
        model = self.make_model(config)
        seen = {}

        def new(app):
            page = self.open_section(app, "git")
            page.add_button.emit("clicked")
            editor = app.window.nav.get_visible_page()
            seen["title"] = editor.get_title()
            seen["placeholders"] = editor.placeholders()
            seen["footer"] = editor.footer_labels()
            seen["ssh_note"] = editor.ssh_footer.get_label()
            seen["save_off"] = editor.save_button.get_sensitive()
            editor.nickname_row.set_text("Freela")
            editor.name_row.set_text("Fulano")
            seen["save_two_fields"] = editor.save_button.get_sensitive()
            editor.email_row.set_text("fulano@freela.example")
            seen["save_on"] = editor.save_button.get_sensitive()
            editor.cancel_button.emit("clicked")
            seen["after_cancel"] = (app.window.nav.get_visible_page() is page, self.saved()["gitIdentities"])

        def save(app):
            page = app.window.nav.get_visible_page()
            page.add_button.emit("clicked")
            editor = app.window.nav.get_visible_page()
            editor.nickname_row.set_text("Freela")
            editor.name_row.set_text("Fulano")
            editor.email_row.set_text("fulano@freela.example")
            editor.save_button.emit("clicked")
            seen["saved"] = [(i["nickname"], i["email"]) for i in self.saved()["gitIdentities"]]
            seen["back"] = app.window.nav.get_visible_page() is page
            seen["rows"] = [row.info()["title"] for row in page.rows]

        def existing(app):
            page = app.window.nav.get_visible_page()
            page.rows[0].buttons["Editar"].emit("clicked")
            editor = app.window.nav.get_visible_page()
            seen["existing_footer"] = editor.footer_labels()
            seen["existing_save"] = editor.save_button.get_sensitive()
            editor.remove_button.emit("clicked")
            seen["remove_dialog"] = self.dialog_texts(app.window)

        self.run_steps(model, [new, save, existing])
        self.assertEqual(seen["title"], "Nova identidade git")
        self.assertEqual(seen["placeholders"], {"Apelido": "Trabalho", "Nome nos commits": "Seu Nome",
                                                "E-mail nos commits": "voce@empresa.com",
                                                "Chave SSH (opcional)": "~/.ssh/id_ed25519_trabalho"})
        self.assertEqual(seen["footer"], ["Cancelar", "Salvar"], "a new identity has no Remover")
        self.assertEqual(seen["ssh_note"], "Com uma chave, o git usa core.sshCommand = ssh -i <chave> -o "
                                           "IdentitiesOnly=yes. Sem chave, vale a sua configuração de SSH.")
        self.assertFalse(seen["save_off"])
        self.assertFalse(seen["save_two_fields"], "Salvar needs apelido, nome and e-mail")
        self.assertTrue(seen["save_on"])
        self.assertEqual(seen["after_cancel"], (True, []), "Cancelar goes back without saving")
        self.assertEqual(seen["saved"], [("Freela", "fulano@freela.example")])
        self.assertTrue(seen["back"])
        self.assertEqual(seen["rows"], ["Freela"])
        self.assertEqual(seen["existing_footer"], ["Remover", "Cancelar", "Salvar"])
        self.assertTrue(seen["existing_save"])
        self.assertEqual(seen["remove_dialog"], ("Remover a identidade Freela?",
                                                 "Os perfis que a usam deixam de trocar o git."))

    # MARK: GitHub

    def test_github_page_empty_and_checks_logins_on_open(self):
        model = self.make_model(accounts_config(self.environment, empty=True))
        seen = {}

        def inspect(app):
            page = self.open_section(app, "github")
            seen["shape"] = self.page_shape(app.window, page)
            seen["refreshes"] = len(self.gh_refreshes)

        self.run_steps(model, [inspect])
        self.assertEqual(seen["shape"], {
            "nav_title": "GitHub",
            "header": ("Contas GitHub", "Contas logadas no GitHub CLI (gh). A troca usa gh auth switch."),
            "actions": ["Conferir logins", "Adicionar conta"],
            "empty": True,
            "empty_state": ("Nenhuma conta GitHub", "Adicione uma conta que já está no gh ou entre com outra.",
                            "multicred-code-symbolic"),
            "note": (GITHUB_NOTE, "security-low-symbolic"),
        })
        self.assertEqual(seen["refreshes"], 1, "opening the page re-reads gh's logins, like the Mac's .task")

    def test_github_rows_statuses_menu_and_conferir_logins(self):
        config = accounts_config(self.environment)
        model = self.make_model(config)
        seen = {}

        def unknown(app):
            page = self.open_section(app, "github")
            seen["unknown"] = [(row.info()["status"], row.info()["buttons"]) for row in page.rows]

        def known(app):
            model.github_logins = logins(("dev-empresa", True, True), ("dev-pessoal", False, False),
                                         ("outro", False, True))
            model.notify()
            page = app.window.nav.get_visible_page()
            seen["rows"] = [row.info() for row in page.rows]
            seen["round"] = all(row.avatar.has_css_class("mc-avatar-round") for row in page.rows)
            page.rows[1].buttons["Entrar…"].emit("clicked")
            page.rows[0].activate_menu("Entrar de novo no GitHub…")
            seen["login_requests"] = [(kind, account.nickname) for kind, account in self.login_requests]
            page.rows[2].activate_menu("Editar")
            seen["menu_edit"] = app.window.nav.get_visible_page().get_title()
            app.window.nav.pop()

        def conferir(app):
            page = app.window.nav.get_visible_page()
            before = len(self.gh_refreshes)
            page.check_button.emit("clicked")
            seen["asked"] = len(self.gh_refreshes) - before
            seen["while"] = (page.check_button.get_sensitive(), [row.info()["status"] for row in page.rows])
            model.github_logins = logins(("dev-empresa", True, True), ("dev-pessoal", False, True),
                                         ("dev-cliente", False, True))
            self.gh_refreshes[-1](model.github_logins, None)
            seen["after"] = (page.check_button.get_sensitive(), [row.info()["status"] for row in page.rows])

        self.run_steps(model, [unknown, known, conferir])
        self.assertEqual(seen["unknown"], [("Status indisponível", [])] * 3,
                         "gh not read yet: neutral status, no Entrar…")
        self.assertEqual(seen["rows"], [
            {"title": "Empresa", "subtitle": "dev-empresa · github.com", "used_in": "Trabalho", "detail": "ativa no gh",
             "status": "Conectada", "buttons": [], "menu": ["Editar", "Entrar de novo no GitHub…"]},
            {"title": "Pessoal", "subtitle": "dev-pessoal · github.com", "used_in": "Pessoal", "detail": "",
             "status": "Precisa entrar", "buttons": ["Entrar…"], "menu": ["Editar", "Entrar de novo no GitHub…"]},
            {"title": "Cliente", "subtitle": "dev-cliente · github.com", "used_in": "—", "detail": "",
             "status": "Não está no gh", "buttons": ["Entrar…"], "menu": ["Editar", "Entrar de novo no GitHub…"]},
        ])
        self.assertTrue(seen["round"])
        self.assertEqual(seen["login_requests"], [("github", "Pessoal"), ("github", "Empresa")])
        self.assertEqual(seen["menu_edit"], "Editar conta GitHub")
        self.assertEqual(seen["asked"], 1)
        self.assertEqual(seen["while"], (False, [None, None, None]), "a spinner while gh is checked")
        self.assertEqual(seen["after"], (True, ["Conectada", "Conectada", "Conectada"]))

    def test_github_editor_known_logins_and_footer(self):
        config = accounts_config(self.environment)
        model = self.make_model(config)
        seen = {}

        def nothing_in_gh(app):
            page = self.open_section(app, "github")
            page.add_button.emit("clicked")
            editor = app.window.nav.get_visible_page()
            seen["title"] = editor.get_title()
            seen["known_title"] = editor.known_group.get_title()
            seen["no_logins"] = editor.known_texts()
            seen["placeholders"] = editor.placeholders()
            seen["footer"] = editor.footer_labels()
            seen["save_off"] = editor.save_button.get_sensitive()
            seen["coauthor"] = (editor.coauthor_row.get_title(), editor.coauthor_footer.get_label())
            editor.coauthor_row.set_active(False)
            seen["coauthor_off"] = editor.coauthor_footer.get_label()
            app.window.nav.pop()

        def choose(app):
            model.github_logins = logins(("dev-empresa", False, True), ("novo-login", True, True))
            page = app.window.nav.get_visible_page()
            page.add_button.emit("clicked")
            editor = app.window.nav.get_visible_page()
            seen["options"] = editor.known_texts()
            editor.choose_login("novo-login")
            seen["chosen"] = (editor.nickname_row.get_text(), editor.username_row.get_text(),
                              editor.host_row.get_text(), editor.save_button.get_sensitive(),
                              editor.chosen_logins())
            editor.username_row.set_text("outra-pessoa")
            seen["cleared"] = editor.chosen_logins()
            editor.username_row.set_text("novo-login")
            seen["rechosen"] = editor.chosen_logins()
            editor.save_button.emit("clicked")
            seen["saved"] = [a["username"] for a in self.saved()["githubAccounts"]]

        def all_added(app):
            page = app.window.nav.get_visible_page()
            page.add_button.emit("clicked")
            editor = app.window.nav.get_visible_page()
            seen["all_added"] = editor.known_texts()
            editor.other_login_row.emit("activated")
            seen["other_login"] = (app.window.nav.get_visible_page() is page, self.login_requests[-1][0])

        def existing(app):
            page = app.window.nav.get_visible_page()
            page.rows[1].activate_menu("Editar")
            editor = app.window.nav.get_visible_page()
            seen["existing"] = (editor.get_title(), editor.known_group is None, editor.username_row.get_sensitive(),
                                editor.host_row.get_sensitive(), editor.coauthor_row.get_active(),
                                editor.footer_labels())
            editor.remove_button.emit("clicked")
            seen["remove_dialog"] = self.dialog_texts(app.window)

        self.run_steps(model, [nothing_in_gh, choose, all_added, existing])
        self.assertEqual(seen["title"], "Adicionar conta GitHub")
        self.assertEqual(seen["known_title"], "Contas que o gh já conhece")
        self.assertEqual(seen["no_logins"], ["Nenhuma conta encontrada no gh.", "Entrar com outra conta no GitHub…"])
        self.assertEqual(seen["placeholders"], {"Apelido": "Trabalho", "Usuário no GitHub": "seu-usuario",
                                                "Servidor": ""})
        self.assertEqual(seen["footer"], ["Cancelar", "Salvar"])
        self.assertFalse(seen["save_off"])
        self.assertEqual(seen["coauthor"], (
            "Incluir a Claude como co-autora dos commits",
            "Commits e PRs feitos pelo Claude Code nos perfis com esta conta terminam com "
            "\"Co-Authored-By: Claude\" (padrão do Claude Code)."))
        self.assertEqual(seen["coauthor_off"], (
            "Commits e PRs feitos pelo Claude Code nos perfis com esta conta saem só com o seu nome. O MultiCred "
            "grava isso no settings.json da pasta do Claude do perfil ao ativá-lo."))
        self.assertEqual(seen["options"], ["novo-login · github.com ativa", "Entrar com outra conta no GitHub…"],
                         "logins already added are not offered")
        self.assertEqual(seen["chosen"], ("novo-login", "novo-login", "github.com", True, ["novo-login"]))
        self.assertEqual((seen["cleared"], seen["rechosen"]), ([], ["novo-login"]),
                         "the radio follows the user field, like the Mac's filled circle")
        self.assertEqual(seen["saved"], ["dev-empresa", "dev-pessoal", "dev-cliente", "novo-login"])
        self.assertEqual(seen["all_added"], ["Todas as contas do gh já foram adicionadas.",
                                             "Entrar com outra conta no GitHub…"])
        self.assertEqual(seen["other_login"], (True, "github"), "the editor gives way to the GitHub login")
        self.assertEqual(seen["existing"], ("Editar conta GitHub", True, False, False, False,
                                            ["Remover", "Cancelar", "Salvar"]))
        self.assertEqual(seen["remove_dialog"], (
            "Remover a conta Pessoal?", "Ela continua logada no gh; os perfis que a usam deixam de trocar o GitHub."))

    # MARK: Chaves de API

    def test_keys_page_empty(self):
        model = self.make_model(accounts_config(self.environment, empty=True))
        seen = {}

        def inspect(app):
            page = self.open_section(app, "keys")
            seen["shape"] = self.page_shape(app.window, page)

        self.run_steps(model, [inspect])
        self.assertEqual(seen["shape"], {
            "nav_title": "Chaves de API",
            "header": ("Chaves de API", "Exportadas como variáveis de ambiente nos terminais do perfil."),
            "actions": ["Adicionar chave"],
            "empty": True,
            "empty_state": ("Nenhuma chave",
                            "Adicione chaves da OpenAI, Gemini e outras. O valor fica só no chaveiro do sistema.",
                            "dialog-password-symbolic"),
            "note": (KEYS_NOTE, "dialog-password-symbolic"),
        })

    def test_keys_rows_only_editar(self):
        config = accounts_config(self.environment)
        self.environment.secrets.set_secret(b"sk-seed", config.api_keys[0].secret_account)
        model = self.make_model(config)
        seen = {}

        def inspect(app):
            page = self.open_section(app, "keys")
            seen["rows"] = [row.info() for row in page.rows]
            seen["round"] = all(row.avatar.has_css_class("mc-avatar-round") for row in page.rows)
            popover = page.rows[1].popup_menu(10, 10)  # what a right click opens
            seen["popover"] = popover.get_visible()
            popover.popdown()
            page.rows[1].activate_menu("Editar")
            seen["menu_edit"] = app.window.nav.get_visible_page().get_title()
            app.window.nav.pop()

        def activate(app):
            page = app.window.nav.get_visible_page()
            page.rows[0].emit("activate")
            seen["activated"] = app.window.nav.get_visible_page().get_title()

        self.run_steps(model, [inspect, activate])
        self.assertEqual(seen["rows"], [
            {"title": "Empresa", "subtitle": "OpenAI", "used_in": "Trabalho", "detail": "OPENAI_API_KEY",
             "status": "No chaveiro", "buttons": [], "menu": ["Editar"]},
            {"title": "Pessoal", "subtitle": "Gemini", "used_in": "—", "detail": "GEMINI_API_KEY",
             "status": "Sem valor", "buttons": [], "menu": ["Editar"]},
        ])
        self.assertTrue(seen["round"])
        self.assertTrue(seen["popover"], "a right click opens the row's menu")
        self.assertEqual(seen["menu_edit"], "Editar chave de API")
        self.assertEqual(seen["activated"], "Editar chave de API")

    def test_key_editor_texts_save_and_replace(self):
        config = accounts_config(self.environment)
        with_value, without_value = config.api_keys
        self.environment.secrets.set_secret(b"sk-seed", with_value.secret_account)
        model = self.make_model(config)
        seen = {}

        def new(app):
            page = self.open_section(app, "keys")
            page.add_button.emit("clicked")
            editor = app.window.nav.get_visible_page()
            seen["new"] = (editor.get_title(), editor.placeholders(), editor.secret_row.get_title(),
                           editor.secret_footer.get_label(), editor.footer_labels(),
                           editor.save_button.get_sensitive())
            editor.nickname_row.set_text("Freela")
            seen["without_secret"] = editor.save_button.get_sensitive()
            editor.secret_row.set_text(SECRET)
            seen["with_secret"] = editor.save_button.get_sensitive()
            editor.env_var_row.set_text("")
            seen["without_var"] = editor.save_button.get_sensitive()
            editor.env_var_row.set_text("FREELA_API_KEY")
            editor.save_button.emit("clicked")
            key = next(k for k in self.saved()["apiKeys"] if k["nickname"] == "Freela")
            seen["stored"] = self.environment.secrets.stored_value(key["id"])

        def existing(app):
            page = app.window.nav.get_visible_page()
            page.rows[0].activate_menu("Editar")
            editor = app.window.nav.get_visible_page()
            seen["with_value"] = (editor.get_title(), editor.secret_row.get_title(),
                                  editor.secret_footer.get_label(), editor.footer_labels(),
                                  editor.save_button.get_sensitive())
            editor.secret_row.set_text("sk-novo-valor")
            editor.save_button.emit("clicked")
            seen["replaced"] = self.environment.secrets.stored_value(with_value.secret_account)

        def no_value(app):
            page = app.window.nav.get_visible_page()
            page.rows[1].activate_menu("Editar")
            editor = app.window.nav.get_visible_page()
            seen["without_value"] = editor.secret_footer.get_label()
            editor.remove_button.emit("clicked")
            seen["remove_dialog"] = self.dialog_texts(app.window)

        self.run_steps(model, [new, existing, no_value])
        self.assertEqual(seen["new"], (
            "Nova chave de API", {"Apelido": "Trabalho", "Variável de ambiente": "OPENAI_API_KEY"}, "Cole a chave",
            "O valor vai direto para o chaveiro do sistema e não aparece mais aqui.", ["Cancelar", "Salvar"], False))
        self.assertFalse(seen["without_secret"], "a new key needs its value")
        self.assertTrue(seen["with_secret"])
        self.assertFalse(seen["without_var"])
        self.assertEqual(seen["stored"], SECRET.encode())
        self.assertEqual(seen["with_value"], (
            "Editar chave de API", "Cole uma nova chave para substituir",
            "Há uma chave guardada no chaveiro do sistema. Deixe em branco para mantê-la.",
            ["Remover", "Cancelar", "Salvar"], True))
        self.assertEqual(seen["replaced"], b"sk-novo-valor", "the editor replaces the value (no Trocar valor)")
        self.assertEqual(seen["without_value"], "Não há valor no chaveiro do sistema para esta chave.")
        self.assertEqual(seen["remove_dialog"], (
            "Remover a chave Pessoal?",
            "O valor é apagado do chaveiro do sistema e os perfis deixam de exportar GEMINI_API_KEY."))

    # MARK: screenshots

    def test_screenshots(self):
        directory = os.environ.get("MULTICRED_SCREENS_DIR")
        if not directory:
            self.skipTest("MULTICRED_SCREENS_DIR not set")
        Path(directory).mkdir(parents=True, exist_ok=True)
        for empty in (True, False):
            config = accounts_config(self.environment, empty=empty)
            if not empty:
                self.environment.secrets.set_secret(b"sk-seed", config.api_keys[0].secret_account)
            model = self.make_model(config)
            suffix = "-vazio" if empty else ""
            steps = [lambda app: Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)]
            if not empty:
                steps.append(lambda _app, m=model: setattr(m, "github_logins", logins(
                    ("dev-empresa", True, True), ("dev-pessoal", False, False), ("novo-login", False, True))))
            for name, file in (("git", "git"), ("github", "github"), ("keys", "chaves")):
                steps += self.shot_steps(name, Path(directory) / f"S-02-{file}{suffix}.png")
            if not empty:
                steps += self.editor_shot("git", Path(directory) / "S-02-editor-git.png")
                steps += self.editor_shot("github", Path(directory) / "S-02-editor-github.png")
                steps += self.editor_shot("keys", Path(directory) / "S-02-editor-chave.png")
            self.run_steps(model, steps)

    def shot_steps(self, section, path):
        tries = {"n": 0}

        def opened(app):
            app.window.set_default_size(1100, 720)
            self.open_section(app, section)

        def shot(app):
            tries["n"] += 1
            if tries["n"] < 8:  # let the page transition settle
                return True
            return not save_png(app.window, path)

        return [opened, shot]

    def editor_shot(self, section, path):
        tries = {"n": 0}

        def opened(app):
            page = self.open_section(app, section)
            page.add_button.emit("clicked")

        def shot(app):
            tries["n"] += 1
            if tries["n"] < 8:
                return True
            return not save_png(app.window, path)

        return [opened, shot]


if __name__ == "__main__":
    unittest.main()
