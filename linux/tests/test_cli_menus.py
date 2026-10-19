"""S-11: `multicred configurar` and the interactive menus `multicred contas` / `multicred perfis`,
plus `multicred entrar` for the logins (R-10).

The menus are driven with scripted input (`inp=`), in the desktop app's test mode (dry-run runner,
in-memory secrets) and in the simulated Termux (temporary HOME and PREFIX, secrets in 0600 files,
a runner that refuses anything outside $PREFIX). Logins run the core's simulators in a real
pseudo-terminal (test mode never runs the real claude or gh). Texts are the Mac's
(OnboardingView.swift, AccountsPages.swift, ClaudePages.swift, AddClaudeAccountPage.swift,
ProfileEditorPage.swift) with the Linux term translations.
"""

import dataclasses
import io
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from multicred import cli
from multicred.core import real_env
from multicred.core.claude.login_simulator import LoginSimulator
from multicred.core.install.autostart import Autostart
from multicred.core.models import (
    APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config, Profile,
    ProfileColor,
)
from multicred.core.runner import CommandResult
from multicred.core.paths import AppPaths
from multicred.core.storage import ConfigRepository, StateStore
from tests.login_support import ToolsAt
from tests.support import SafeTestCase
from tests.test_cli import _PrefixOnlyRunner
from tests.test_termux import TermuxWorld

LINUX = Path(__file__).resolve().parent.parent
TOKEN = "sk-ant-oat01-" + "a" * 40
GH_LOGINS = {"hosts": {"github.com": [
    {"host": "github.com", "login": "dev-a", "active": True, "state": "success"},
    {"host": "github.com", "login": "dev-b", "active": False, "state": "success"},
]}}


def script(*answers):
    return "".join(f"{answer}\n" for answer in answers)


def run(argv, environment, tools=None, inp=""):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), environment=environment, tools=tools, out=out, err=err, inp=io.StringIO(inp))
    return code, out.getvalue(), err.getvalue()


def responder(request):
    """Answers of the dry-run runner: the global git identity and gh's logins."""
    arguments = list(request.arguments)
    if arguments[-2:] == ["--get", "user.email"]:
        return CommandResult(0, b"dev@a.example\n")
    if arguments[-2:] == ["--get", "user.name"]:
        return CommandResult(0, b"Dev A\n")
    if arguments[:2] == ["auth", "status"]:
        return CommandResult(0, json.dumps(GH_LOGINS).encode())
    return None


class _MenuWorld(SafeTestCase):
    """The desktop app's test mode: a sandbox HOME, dry-run runner, in-memory secrets."""

    seed = True

    def setUp(self):
        super().setUp()
        self.sandbox = real_env.make_sandbox(self.home / "sandbox", responder=responder)
        if self.seed:
            ConfigRepository(self.sandbox).save(real_env.seed_sandbox(self.sandbox))
        # gh and git where the switch expects them; nothing runs (dry-run runner).
        self.tools = ToolsAt(git="/usr/bin/git", gh="/usr/bin/gh")
        # "Abrir ao iniciar a sessão" only from the installed package: a stand-in command.
        command = self.home / "bin" / "multicred"
        command.parent.mkdir()
        command.write_text("#!/bin/sh\n")
        command.chmod(0o755)
        original = cli_menus().make_autostart
        cli_menus().make_autostart = lambda environment: Autostart(environment, command_path=str(command))
        self.addCleanup(setattr, cli_menus(), "make_autostart", original)

    def cli(self, *argv, inp=""):
        return run(argv, self.sandbox, self.tools, inp=inp)

    @property
    def config(self):
        return ConfigRepository(self.sandbox).load()

    def state(self):
        return StateStore(AppPaths(self.sandbox), self.sandbox.clock).load()

    def secret(self, account):
        return self.sandbox.secrets.stored_value(account)


def cli_menus():
    from multicred import cli_menus as module
    return module


class CliMenuTests(_MenuWorld):
    """Desktop test mode: contas (git, GitHub, Claude, chaves), perfis, entrar."""

    def test_help_lists_the_new_commands(self):
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        for command in ("configurar", "contas", "perfis", "entrar"):
            self.assertIn(command, out)

    # contas: the root and the listing without a terminal

    def test_contas_without_a_terminal_prints_the_pages_as_text(self):
        code, out, err = self.cli("contas")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "Contas")
        self.assertIn("Git · 2 identidades", lines)
        self.assertIn("  Trabalho · Dev Trabalho · dev@empresa.example · SSH padrão · usado em Trabalho", lines)
        self.assertIn("GitHub · nenhuma conta", lines)
        self.assertIn("Claude · nenhuma conta", lines)
        self.assertIn("Chaves de API · nenhuma chave", lines)
        self.assertNotIn("Escolha: ", out)

    def test_contas_menu_ends_at_end_of_input(self):
        code, out, _ = self.cli("contas", "--menu", inp="")
        self.assertEqual(code, 0)
        self.assertIn("  1  Git · 2 identidades", out.splitlines())
        self.assertIn("  0  Sair", out.splitlines())

    # Git (AccountsPages.swift:56-163)

    def test_git_identity_is_added_edited_and_removed(self):
        code, out, err = self.cli("contas", "--menu", inp=script(
            "1", "n", "Casa", "Dev Casa", "dev@casa.example", "5", "", "s", "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Identidades git", "Nome e e-mail dos commits (e, se quiser, a chave SSH).",
                     "O MultiCred escreve só o arquivo incluído no fim do seu gitconfig global. "
                     "O seu ~/.gitconfig continua seu.", "Nova identidade git", "Apelido (ex.: Trabalho): ",
                     "Nome nos commits (ex.: Seu Nome): ", "E-mail nos commits (ex.: voce@empresa.com): ",
                     "Com uma chave, o git usa core.sshCommand = ssh -i <chave> -o IdentitiesOnly=yes. "
                     "Sem chave, vale a sua configuração de SSH.", "Identidade Casa salva"):
            self.assertIn(text, out)
        casa = next(i for i in self.config.git_identities if i.nickname == "Casa")
        self.assertEqual((casa.name, casa.email, casa.color, casa.ssh_key_path),
                         ("Dev Casa", "dev@casa.example", ProfileColor.ORANGE, None))

        code, out, _ = self.cli("contas", "--menu", inp=script("1", "3", "3", "novo@casa.example", "s", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("Editar identidade git", out)
        self.assertIn("E-mail nos commits [dev@casa.example]: ", out)
        self.assertEqual(self.config.git_identity(casa.id).email, "novo@casa.example")

        trabalho = self.config.git_identities[0]
        code, out, _ = self.cli("contas", "--menu", inp=script("1", "1", "r", "s", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("Remover a identidade Trabalho?", out)
        self.assertIn("Os perfis que a usam deixam de trocar o git.", out)
        self.assertIsNone(self.config.git_identity(trabalho.id))
        self.assertIsNone(self.config.profiles[0].git_identity_id)

    def test_git_editor_keeps_the_core_validation(self):
        before = self.config.to_dict()
        code, out, _ = self.cli("contas", "--menu", inp=script("1", "1", "3", "sem-arroba", "s", "0", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("E-mail inválido.", out)
        self.assertEqual(self.config.to_dict(), before)
        # Salvar is off while a required field is empty (the Mac's disabled button).
        code, out, _ = self.cli("contas", "--menu", inp=script("1", "n", "", "", "", "", "", "s", "0", "0", "0"))
        self.assertIn("Preencha Apelido, Nome nos commits e E-mail nos commits para salvar.", out)
        self.assertEqual(self.config.to_dict(), before)

    # GitHub (AccountsPages.swift:167-337)

    def test_github_account_from_the_logins_gh_already_knows(self):
        code, out, err = self.cli("contas", "--menu", inp=script("2", "n", "2", "", "", "", "", "n", "s", "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Contas GitHub", "Contas logadas no GitHub CLI (gh). A troca usa gh auth switch.",
                     "Nenhuma conta GitHub", "Adicione uma conta que já está no gh ou entre com outra.",
                     "Adicionar conta GitHub", "Contas que o gh já conhece", "  1  dev-a · github.com  ativa",
                     "  2  dev-b · github.com", "Entrar com outra conta no GitHub…", "Apelido [dev-b]: ",
                     "Usuário no GitHub [dev-b]: ", "Servidor [github.com]: ",
                     "Incluir a Claude como co-autora dos commits? [S/n] ",
                     "O login e o token ficam com o próprio gh. O MultiCred só escolhe a conta ativa."):
            self.assertIn(text, out)
        account, = self.config.github_accounts
        self.assertEqual((account.nickname, account.username, account.host, account.claude_co_author),
                         ("dev-b", "dev-b", "github.com", False))

        # The page shows gh's status; the editor of an existing account locks user and server.
        code, out, _ = self.cli("contas", "--menu", inp=script("2", "1", "r", "s", "0", "0"))
        self.assertIn("  1  dev-b · dev-b · github.com · usado em — · Conectada", out.splitlines())
        self.assertIn("Editar conta GitHub", out)
        self.assertIn("Remover a conta dev-b?", out)
        self.assertIn("Ela continua logada no gh; os perfis que a usam deixam de trocar o GitHub.", out)
        self.assertEqual(self.config.github_accounts, [])

    def test_github_login_with_another_account_runs_gh_auth_login(self):
        code, out, err = self.cli("contas", "--menu", inp=script("2", "n", "e", "", "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Entrar no GitHub", "Simulação do modo de teste: o navegador não abre e nenhum login real "
                     "acontece.", "1. Copie este código. 2. Abra o GitHub e cole o código. 3. Autorize o GitHub CLI.",
                     "TEST-1234", "Pronto: usuario-teste está logada no gh. A conta ativa voltou para a do perfil "
                     "atual."):
            self.assertIn(text, out)
        self.assertIn("usuario-teste", [a.username for a in self.config.github_accounts])

    # Chaves de API (AccountsPages.swift:450-580)

    def test_api_key_is_added_its_value_replaced_and_removed(self):
        code, out, err = self.cli("contas", "--menu", inp=script("4", "n", "Empresa", "", "", "", "sk-test-1", "s",
                                                                 "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Chaves de API", "Exportadas como variáveis de ambiente nos terminais do perfil.", "Nenhuma chave",
                     "Adicione chaves da OpenAI, Gemini e outras. O valor fica só no chaveiro do sistema.",
                     "Nova chave de API", "Provedor [OpenAI]: ", "Variável de ambiente [OPENAI_API_KEY]: ",
                     "O valor vai direto para o chaveiro do sistema e não aparece mais aqui.", "Cole a chave: ",
                     "Chave Empresa salva"):
            self.assertIn(text, out)
        self.assertNotIn("sk-test-1", out)  # the value is never echoed
        key, = self.config.api_keys
        self.assertEqual((key.nickname, key.provider, key.env_var), ("Empresa", APIProvider.OPENAI, "OPENAI_API_KEY"))
        self.assertEqual(self.secret(key.secret_account), b"sk-test-1")
        self.assertNotIn("sk-test-1", (self.sandbox.home / ".config/multicred/config.json").read_text())

        code, out, _ = self.cli("contas", "--menu", inp=script("4", "1", "5", "sk-test-2", "s", "0", "0"))
        self.assertIn("  1  Empresa · OpenAI · OPENAI_API_KEY · usado em — · No chaveiro", out.splitlines())
        self.assertIn("Há uma chave guardada no chaveiro do sistema. Deixe em branco para mantê-la.", out)
        self.assertEqual(self.secret(key.secret_account), b"sk-test-2")

        def use_key(config):
            config.profiles[0].api_key_ids = [key.id]
        self.edit(use_key)
        code, out, _ = self.cli("contas", "--menu", inp=script("4", "1", "r", "s", "0", "0"))
        self.assertIn("Remover a chave Empresa?", out)
        self.assertIn("O valor é apagado do chaveiro do sistema e os perfis deixam de exportar OPENAI_API_KEY.", out)
        self.assertEqual(self.config.api_keys, [])
        self.assertEqual(self.config.profiles[0].api_key_ids, [])
        self.assertIsNone(self.secret(key.secret_account))

    def test_api_key_rules_are_the_core_ones(self):
        code, out, _ = self.cli("contas", "--menu", inp=script("4", "n", "X", "6", "PATH", "", "valor", "s", "0", "0",
                                                               "0"))
        self.assertEqual(code, 0)
        self.assertIn("PATH é reservada e não pode ser usada para uma chave.", out)
        self.assertEqual(self.config.api_keys, [])
        self.assertEqual(self.sandbox.secrets.all_accounts(), [])

    def edit(self, change):
        repository = ConfigRepository(self.sandbox)
        config = repository.load()
        change(config)
        repository.save(config)

    # Claude (ClaudePages.swift, AddClaudeAccountPage.swift)

    def test_claude_account_with_a_pasted_token(self):
        code, out, err = self.cli("contas", "--menu", inp=script("3", "n", "Ana", "ana@example.com", "", "2", TOKEN,
                                                                 "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Pastas guardam agents, skills e conversas. Contas são só o login usado nelas.",
                     "Pastas dos perfis", "Contas Claude (só credencial)",
                     "Nenhuma conta extra. Adicione uma para usar o login de outra pessoa ou de outra assinatura sem "
                     "trocar de pasta.", "Adicionar conta Claude",
                     "Uma conta Claude é só o login. Usada num perfil, ela mantém os agents, skills e conversas da "
                     "pasta do perfil, e a troca vale até nas conversas já abertas.", "Apelido (ex.: Marco): ",
                     "E-mail da conta (opcional): ", "  1  Entrar pelo navegador", "  2  Já tenho um token",
                     "Se você já rodou \"claude setup-token\" num terminal, cole o token (começa com sk-ant-oat). Um "
                     "token vale só para terminais novos: as conversas já abertas não trocam.", "Conta Ana adicionada.",
                     "O token está só no chaveiro do sistema. Vale para terminais novos; escolha esta conta no editor "
                     "de um perfil ou use agora no perfil ativo."):
            self.assertIn(text, out)
        self.assertNotIn(TOKEN, out)
        account, = self.config.claude_accounts
        self.assertEqual((account.nickname, account.email, account.kind), ("Ana", "ana@example.com",
                                                                          ClaudeAccountKind.TOKEN))
        self.assertEqual(self.secret(account.secret_account), TOKEN.encode())

    def test_a_pasted_text_that_is_not_a_token_is_refused(self):
        code, out, _ = self.cli("contas", "--menu", inp=script("3", "n", "Ana", "", "", "2", "abc", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("Isso não parece um token do Claude Code. Gere um com \"claude setup-token\" e cole a linha "
                      "que começa com sk-ant-oat.", out)
        self.assertEqual(self.config.claude_accounts, [])

    def test_claude_account_through_claude_auth_login_in_the_terminal(self):
        self.assertEqual(self.cli("trocar", "Trabalho")[0], 0)
        code, out, err = self.cli("contas", "--menu", inp=script("3", "n", "Marco", "", "", "1", "codigo-123", "s",
                                                                 "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Modo de teste: simulação; digite qualquer código.",
                     "1. Entre no navegador com a conta que você quer usar", "2. Clique em Autorizar e copie o código",
                     "3. Salvar o login", "$ claude auth login --claudeai (simulado)",
                     "https://example.invalid/oauth/authorize?simulado=1", "Código: ", "Conta Marco adicionada.",
                     "O login está só no chaveiro do sistema. Use agora no perfil ativo: a pasta continua a mesma e "
                     "até as conversas já abertas passam para esta conta.", "Usar agora em Trabalho? [s/N] ",
                     "Conta Claude: Marco"):
            self.assertIn(text, out)
        self.assertNotIn("codigo-123", out)
        account, = self.config.claude_accounts
        self.assertEqual((account.kind, account.email), (ClaudeAccountKind.LOGIN, LoginSimulator.SIMULATED_EMAIL))
        from multicred.core.models import uuid_text
        self.assertIn(uuid_text(account.id), self.sandbox.claude_credentials.vault_keys())
        self.assertEqual(self.state().applied_claude_account_id, account.id)
        scratch = self.sandbox.home / ".config/multicred/scratch"
        self.assertEqual(list(scratch.iterdir()) if scratch.exists() else [], [], "the throwaway folder is gone")

    def test_a_rejected_code_shows_the_failure_and_adds_nothing(self):
        code, out, _ = self.cli("contas", "--menu", inp=script("3", "n", "Marco", "", "", "1", "erro", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("Não deu certo (Error: Invalid code or expired).", out)
        self.assertEqual(self.config.claude_accounts, [])

    def test_claude_account_detail_uses_now_and_removes_with_the_mac_text(self):
        ana = ClaudeAccount(nickname="Ana", color=ProfileColor.ORANGE, email="ana@example.com")
        self.sandbox.secrets.set_secret(TOKEN.encode(), ana.secret_account)

        def add(config):
            config.claude_accounts.append(ana)
            config.profiles[0].claude_account_id = ana.id
        self.edit(add)
        self.assertEqual(self.cli("trocar", "Pessoal")[0], 0)
        code, out, _ = self.cli("contas", "--menu", inp=script("3", "1", "u", "0", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("  1  Ana · ana@example.com · token · só terminais novos · usado em Trabalho · Usar agora",
                      out.splitlines())
        self.assertIn("Token salvo", out)
        self.assertIn("Padrão nos perfis: Trabalho.", out)
        self.assertIn("\"Usar agora\" mantém o perfil Pessoal e a pasta ~/.claude (agents, skills, conversas). Só o "
                      "login muda, até a próxima troca de perfil.", out)
        self.assertIn("Conta Claude: Ana", out)
        self.assertEqual(self.state().applied_claude_account_id, ana.id)

        code, out, _ = self.cli("contas", "--menu", inp=script("3", "1", "r", "s", "0", "0"))
        self.assertIn("Remover a conta Ana?", out)
        self.assertIn("Usada em: Trabalho. Esses perfis voltam a usar o login da própria pasta. O token é apagado do "
                      "chaveiro do sistema. Nenhuma pasta ou conversa é tocada.", out)
        self.assertEqual(self.config.claude_accounts, [])
        self.assertIsNone(self.config.profiles[0].claude_account_id)
        self.assertIsNone(self.secret(ana.secret_account))
        # It was in use: Claude goes back to the folder's own login, like the Mac.
        self.assertIsNone(self.state().applied_claude_account_id)

    def test_entrar_claude_signs_an_account_in_again(self):
        ana = ClaudeAccount(nickname="Ana", color=ProfileColor.ORANGE)
        self.edit(lambda config: config.claude_accounts.append(ana))
        code, out, err = self.cli("entrar", "claude", "ana", inp=script("1", "codigo"))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Entrar de novo em Ana", out)
        self.assertIn("Login de Ana atualizado.", out)
        self.assertEqual(self.config.claude_accounts[0].kind, ClaudeAccountKind.LOGIN, "a token account becomes a login")
        code, out, err = self.cli("entrar", "claude", "Zeca")
        self.assertEqual(code, 1)
        self.assertIn("Nenhuma conta Claude chamada “Zeca”", err)

    def test_entrar_pasta_runs_claude_auth_login_in_the_folder(self):
        folder = self.sandbox.home / ".claude-pessoal"
        folder.mkdir()
        (folder / ".claude.json").write_text("{}")
        self.edit(lambda config: setattr(config.profiles[1], "claude_config_dir", str(folder)))
        self.assertEqual(self.cli("trocar", "Pessoal")[0], 0)
        self.assertIn("Login da pasta · Sem login", self.cli("claude")[1])
        code, out, err = self.cli("entrar", "pasta", inp=script("codigo"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Login da pasta ~/.claude-pessoal", "Perfis: Pessoal",
                     "Simulação do modo de teste: o navegador não abre. Digite qualquer código para conectar, ou "
                     "\"erro\" para ver a tela de erro.",
                     "O navegador vai abrir. A conta em que você entrar lá vira o login desta pasta.",
                     "Pronto. Os perfis que usam ~/.claude-pessoal entram com esta conta quando não escolhem outra "
                     "conta Claude.", "Credenciais guardadas pelo Claude Code. O MultiCred não as armazena."):
            self.assertIn(text, out)
        self.assertTrue((folder / ".credentials.json").exists())
        self.assertIn("Login da pasta · simulado@example.com", self.cli("claude")[1])
        # S-14: the window's folder login session (LoginMode.FOLDER), so both run the Mac's
        # `claude auth login` (LoginSession.swift) the same way.
        self.assertIn("$ claude auth login (simulado)", out)

    def test_entrar_pasta_in_test_mode_refuses_a_folder_outside_the_test_home(self):
        """The shared folder session's guard (FOLDER_OUTSIDE_TEST_HOME): test mode never logs a
        real folder in."""
        outside = self.home / "fora"
        outside.mkdir()
        code, out, err = self.cli("entrar", "pasta", str(outside), inp=script("codigo"))
        self.assertEqual(code, 1)
        self.assertIn("No modo de teste, a pasta precisa ficar dentro do HOME de teste.", out + err)
        self.assertFalse((outside / ".credentials.json").exists())

    def test_verificar_names_the_terminal_command_of_a_login_fix(self):
        ana = ClaudeAccount(nickname="Ana", color=ProfileColor.ORANGE)
        self.sandbox.secrets.set_secret(TOKEN.encode(), ana.secret_account)
        self.edit(lambda config: config.claude_accounts.append(ana))
        self.assertEqual(self.cli("trocar", "Trabalho")[0], 0)
        self.assertEqual(self.cli("claude", "Ana")[0], 0)
        self.sandbox.secrets.delete_item(ana.secret_account)
        lines = self.cli("verificar")[1].splitlines()
        self.assertIn("  No terminal: multicred entrar claude Ana", lines)

    def test_every_line_of_a_hint_stays_under_its_row(self):
        from multicred.core.engine.verifier import Check, CheckStatus
        check = Check("claude.code", "Claude Code", CheckStatus.ERROR, found="quebrado", hint="linha 1\nlinha 2")
        self.assertEqual(cli.check_lines(check), ["✗ Claude Code", "  quebrado", "  linha 1", "  linha 2"])

    def test_claude_pasta_without_login_points_to_entrar(self):
        (self.sandbox.home / ".claude.json").write_text("{}")
        self.assertEqual(self.cli("trocar", "Trabalho")[0], 0)
        code, out, _ = self.cli("claude", "pasta")
        self.assertEqual(code, 0)
        self.assertIn("A pasta ~/.claude não tem login.", out)
        self.assertIn("Para entrar: multicred entrar pasta", out)

    # perfis (ProfileEditorPage.swift)

    def test_perfis_without_a_terminal_keeps_the_listing(self):
        code, out, _ = self.cli("perfis")
        self.assertEqual((code, out.splitlines()), (0, ["Perfis", "  1  Trabalho", "  2  Pessoal"]))

    def test_perfis_menu_creates_edits_and_deletes_a_profile(self):
        key = APIKeyAccount(nickname="Empresa", provider=APIProvider.GEMINI, color=ProfileColor.BLUE)
        self.sandbox.secrets.set_secret(b"k", key.secret_account)
        self.edit(lambda config: config.api_keys.append(key))
        trabalho = self.config.git_identities[0]
        code, out, err = self.cli("perfis", "--menu", inp=script(
            "n", "Celular", "4", "2", "9", "2", "s", "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        for text in ("  n  Novo perfil", "Novo perfil", "Nome (ex.: Trabalho): ", "Composição", "Git · Nenhuma",
                     "OpenAI · Nenhuma", "Gemini · Nenhuma",
                     "A pasta guarda agents, skills e conversas. A conta é só o login: trocar a conta não muda a pasta.",
                     "“Nenhuma” deixa aquele item como está ao trocar para este perfil (o Claude sempre usa a pasta do "
                     "perfil). Chaves de API ficam no chaveiro do sistema; o perfil guarda só qual item usar.",
                     "Perfil Celular salvo", "No painel: Ctrl+3 no menu"):
            self.assertIn(text, out)
        celular = self.config.profiles[2]
        self.assertEqual((celular.name, celular.git_identity_id, celular.api_key_ids), ("Celular", trabalho.id,
                                                                                      [key.id]))
        self.assertEqual(celular.icon.value, "C")

        code, out, _ = self.cli("perfis", "--menu", inp=script("3", "1", "Celular 2", "3", "3", "s", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("Editar perfil", out)
        edited = self.config.profiles[2]
        self.assertEqual((edited.name, edited.icon.kind, edited.icon.value), ("Celular 2", "symbol", "house"))

        code, out, _ = self.cli("perfis", "--menu", inp=script("3", "x", "s", "0"))
        self.assertIn("Apagar o perfil Celular 2?", out)
        self.assertIn("As contas continuam cadastradas. Se ele estiver ativo, o sistema fica como está até você "
                      "escolher outro perfil.", out)
        self.assertEqual([p.name for p in self.config.profiles], ["Trabalho", "Pessoal"])
        self.assertIn(key.id, [k.id for k in self.config.api_keys], "the accounts stay")

    def test_perfis_menu_activates_a_profile_and_saving_the_active_one_applies_it(self):
        code, out, _ = self.cli("perfis", "--menu", inp=script("2", "a", "0", "0"))
        self.assertEqual(code, 0)
        self.assertIn("  a  Ativar este perfil", out)
        self.assertIn("Agora em Pessoal", out)
        self.assertEqual(self.state().profile_id, self.config.profiles[1].id)
        listing = self.cli("perfis", "--menu", inp=script("0"))[1]
        self.assertIn("  2  Pessoal  ✓", listing.splitlines())
        generation = self.state().generation
        code, out, _ = self.cli("perfis", "--menu", inp=script("2", "s", "0", "0"))
        self.assertNotIn("  a  Ativar este perfil", out)
        self.assertGreater(self.state().generation, generation, "the active profile was applied again")

    def test_new_claude_folder_from_the_profile_editor(self):
        code, out, _ = self.cli("perfis", "--menu", inp=script("2", "6", "2", "", "s", "0", "0"))
        self.assertEqual(code, 0, out)
        self.assertIn("  1  ~/.claude (padrão) · 0 conversas", out.splitlines())
        self.assertIn("  2  Nova pasta…", out.splitlines())
        self.assertIn("Caminho [~/.claude-pessoal]: ", out)
        folder = self.sandbox.home / ".claude-pessoal"
        self.assertTrue(folder.is_dir())
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(self.config.profiles[1].claude_config_dir, str(folder))
        # A folder without a login (Claude Code's state without oauthAccount): the editor warns
        # and offers "Entrar…".
        (folder / ".claude.json").write_text("{}")
        code, out, _ = self.cli("perfis", "--menu", inp=script("2", "0", "0"))
        self.assertIn("A pasta ~/.claude-pessoal não tem login.", out)
        self.assertIn("  e  Entrar…", out)

    def test_menus_need_a_configuration(self):
        empty = real_env.make_sandbox(self.home / "empty")
        for argv in (["contas"], ["contas", "--menu"], ["perfis", "--menu"], ["entrar", "pasta"]):
            code, out, err = run(argv, empty, self.tools)
            self.assertEqual((code, out), (1, ""), argv)
            self.assertIn("MultiCred ainda não foi configurado", err)
            self.assertIn("multicred configurar", err)


class ConfigureTests(_MenuWorld):
    """`multicred configurar`: Preparar → Perfis → Aplicar (OnboardingView.swift)."""

    seed = False

    def test_configurar_detects_creates_profiles_shows_the_diff_and_installs(self):
        code, out, err = self.cli("configurar", inp=script(
            "",              # Preparar: Continuar
            "1", "1", "Notebook", "0",  # edit the suggested profile's name
            "a",             # Adicionar perfil
            "2", "1", "Celular", "2", "Dev Celular", "3", "dev@celular.example", "0",
            "c",             # Continuar
            "bash",          # Shells
            "2",             # Perfil inicial: Celular
            "",              # Abrir ao iniciar a sessão? [S/n]
            ""))             # Aplicar e começar? [S/n]
        self.assertEqual((code, err), (0, ""), out)
        for text in ("Bem-vindo ao MultiCred",
                     "Nunca vê senhas ou tokens. O login é feito pelas próprias ferramentas.",
                     "Altera só um bloco marcado no fim de cada arquivo de shell e do gitconfig, com backup.",
                     "Pode desinstalar e restaurar tudo a qualquer momento.",
                     "Procurando ferramentas e contas nesta máquina…", "Ferramentas nesta máquina",
                     "Dá para continuar sem o Claude Code e adicioná-lo depois.", "Perfis sugeridos",
                     "Montados a partir do que já existe nesta máquina: 1 identidade git, 2 contas GitHub e 0 pastas "
                     "do Claude. Dá para ajustar tudo depois.", "Principal · Completo", "  a  Adicionar perfil",
                     "Novo perfil · Falta 2 item", "Revise antes de aplicar",
                     "Estas são as únicas mudanças fora da pasta ~/.config/multicred.",
                     "Shells que vão carregar o MultiCred", "~/.bashrc · criar", "~/.gitconfig · criar",
                     "Backup dos originais em ~/.config/multicred/backups/", "Perfil inicial",
                     "Abrir ao iniciar a sessão? [S/n] ",
                     "Abra um novo terminal depois de aplicar para que o shell carregue o perfil.",
                     "Aplicar e começar? [S/n] ", "Agora em Celular"):
            self.assertIn(text, out)
        hook = '. "$HOME/.config/multicred/shell/hook.bash"'
        self.assertTrue(any(line.startswith("  + ") and hook in line for line in out.splitlines()), out)
        self.assertIn(hook, (self.sandbox.home / ".bashrc").read_text())
        self.assertIn("multicred/gitconfig", (self.sandbox.home / ".gitconfig").read_text())
        self.assertTrue((self.sandbox.home / ".config/multicred/shell/hook.bash").exists())
        config = self.config
        self.assertEqual([p.name for p in config.profiles], ["Notebook", "Celular"])
        notebook, celular = config.profiles
        self.assertEqual(config.git_identity(notebook.git_identity_id).email, "dev@a.example")
        self.assertEqual(config.github_account(notebook.github_account_id).username, "dev-a")
        self.assertEqual(config.git_identity(celular.git_identity_id).email, "dev@celular.example")
        self.assertEqual(sorted(a.username for a in config.github_accounts), ["dev-a", "dev-b"])
        self.assertEqual(config.settings.enabled_shells[0].value, "bash")
        self.assertTrue(config.settings.open_at_login)
        self.assertTrue((self.sandbox.home / ".config/autostart/app.multicred.MultiCred.desktop").exists())
        self.assertEqual(self.state().profile_id, celular.id)

    def test_cancelling_changes_nothing(self):
        code, out, _ = self.cli("configurar", inp=script("", "c", "", "", "n"))
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], "Nada foi alterado.")
        self.assertFalse((self.sandbox.home / ".bashrc").exists())
        self.assertIsNone(ConfigRepository(self.sandbox).load())
        code, out, _ = self.cli("configurar", inp="")  # end of input on the first question
        self.assertEqual(code, 1)
        self.assertIsNone(ConfigRepository(self.sandbox).load())

    def test_configuring_again_starts_from_the_current_configuration(self):
        ConfigRepository(self.sandbox).save(real_env.seed_sandbox(self.sandbox))
        code, out, _ = self.cli("configurar", inp=script("n"))
        self.assertEqual(code, 1)
        self.assertIn("O MultiCred já está configurado. Configurar de novo? [s/N] ", out)
        code, out, _ = self.cli("configurar", inp=script("s", "", "c", "", "", "n", ""))
        self.assertEqual(code, 0, out)
        self.assertIn("  1  Trabalho · Falta 1 item", out.splitlines())
        self.assertEqual([p.name for p in self.config.profiles], ["Trabalho", "Pessoal"])
        self.assertFalse(self.config.settings.open_at_login)
        self.assertTrue((self.sandbox.home / ".bashrc").exists())


def _link_system_tools(test, names=("setsid", "sh", "true", "sleep", "dirname", "mkdir", "cat", "git")):
    """What the pseudo-terminal, the login simulators and the switch need, from this machine into $PREFIX/bin."""
    for name in names:
        real = shutil.which(name)
        if real is None:
            test.skipTest(f"{name} is not installed here")
        target = test.prefix / "bin" / name
        if not target.exists():
            target.symlink_to(real)


class TermuxMenuTests(TermuxWorld):
    """The same menus in the simulated Termux: secrets in 0600 files, nothing outside $PREFIX."""

    def setUp(self):
        super().setUp()
        _link_system_tools(self)
        self.guard = _PrefixOnlyRunner(self.recording, self.prefix)
        self.termux = dataclasses.replace(self.termux, runner=self.guard)
        self.secrets_dir = self.home / ".config/multicred/secrets"

    def cli(self, *argv, inp=""):
        return run(argv, self.termux, self.termux_tools(), inp=inp)

    def assert_stayed_in_termux(self):
        self.assertEqual(self.guard.refused, [], [r.executable for r in self.guard.refused])
        self.assert_nothing_forbidden_ran()

    def real_termux(self):
        """The real (not test) mode of the simulated Termux: logins would run real programs, so
        these tests stop before any starts (the program or setsid is missing)."""
        real = dataclasses.replace(self.termux, mode="real")
        ConfigRepository(real).save(Config(profiles=[Profile(name="Celular", color=ProfileColor.GREEN)]))
        return real

    def test_claude_code_missing_names_the_termux_install_not_the_curl_installer(self):
        """S-14: on Termux Claude Code comes from npm, pinned to 2.1.112 (termux_support)."""
        real = self.real_termux()
        code, out, _ = run(["entrar", "pasta"], real, self.termux_tools(real))
        self.assertEqual(code, 1)
        self.assertIn("Claude Code não foi encontrado. Instale com: pkg install nodejs-lts && "
                      "DISABLE_AUTOUPDATER=1 npm install -g @anthropic-ai/claude-code@2.1.112", out)
        self.assertNotIn("curl", out)
        self.assert_stayed_in_termux()

    def test_missing_setsid_says_to_install_util_linux(self):
        (self.prefix / "bin" / "setsid").unlink()
        real = self.real_termux()
        code, out, _ = run(["entrar", "github"], real, self.termux_tools(real), inp="\n")
        self.assertEqual(code, 1)
        self.assertIn("setsid: No such file or directory", out)
        self.assertIn("Instale o setsid com: pkg install util-linux", out)
        self.assert_stayed_in_termux()

    def test_configurar_on_termux(self):
        code, out, err = self.cli("configurar", inp=script("", "1", "2", "Dev A", "3", "dev@a.example", "0", "c", "",
                                                           ""))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Um comando (multicred trocar) troca, juntos, a identidade do git, a conta do GitHub CLI, a "
                      "conta do Claude Code e as chaves de API dos terminais.", out)
        self.assertIn("pkg install", out + "")  # Termux's install commands, never apt or sudo
        self.assertNotIn("sudo", out)
        self.assertNotIn("Abrir ao iniciar a sessão", out)  # Android has no session to start with
        self.assertNotIn("Apps gráficos", out)
        self.assertIn("~/.bashrc · criar", out)
        self.assertIn("Agora em Principal", out)
        self.assertIn('. "$HOME/.config/multicred/shell/hook.bash"', (self.home / ".bashrc").read_text())
        config = ConfigRepository(self.termux).load()
        self.assertFalse(config.settings.open_at_login)
        self.assertEqual(config.git_identity(config.profiles[0].git_identity_id).email, "dev@a.example")
        self.assertFalse((self.home / ".config/autostart").exists())
        self.assert_stayed_in_termux()

    def configured(self):
        ConfigRepository(self.termux).save(Config(profiles=[Profile(name="Celular", color=ProfileColor.GREEN)]))

    def test_api_key_goes_to_a_private_file(self):
        self.configured()
        code, out, err = self.cli("contas", "--menu", inp=script("4", "n", "O", "", "", "", "sk-termux", "s", "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("O valor vai direto para a pasta ~/.config/multicred/secrets e não aparece mais aqui.", out)
        self.assertNotIn("chaveiro", out)
        key, = ConfigRepository(self.termux).load().api_keys
        file = self.secrets_dir / key.secret_account
        self.assertEqual(file.read_bytes(), b"sk-termux")
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.secrets_dir.stat().st_mode), 0o700)
        code, out, _ = self.cli("contas", "--menu", inp=script("4", "1", "r", "s", "0", "0"))
        self.assertFalse(file.exists())
        self.assert_stayed_in_termux()

    def test_claude_login_and_token_on_termux(self):
        self.configured()
        code, out, err = self.cli("contas", "--menu", inp=script("3", "n", "Marco", "", "", "1", "codigo", "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Conta Marco adicionada.", out)
        self.assertIn("O login está só em ~/.config/multicred/secrets.", out)
        marco, = ConfigRepository(self.termux).load().claude_accounts
        from multicred.core.models import uuid_text
        self.assertTrue((self.secrets_dir / "claude-login" / uuid_text(marco.id)).exists())
        code, out, err = self.cli("contas", "--menu", inp=script("3", "n", "Ana", "", "", "2", TOKEN, "0", "0"))
        self.assertEqual((code, err), (0, ""), out)
        ana = ConfigRepository(self.termux).load().claude_accounts[1]
        self.assertEqual((self.secrets_dir / ana.secret_account).read_text(), TOKEN)
        self.assert_stayed_in_termux()

    def test_termux_hints_name_the_cli_command(self):
        token = ClaudeAccount(nickname="Marco", color=ProfileColor.GREEN)
        ConfigRepository(self.termux).save(Config(claude_accounts=[token], profiles=[
            Profile(name="Celular", color=ProfileColor.GREEN)]))
        self.assertEqual(self.cli("trocar", "Celular")[0], 0)
        code, out, err = self.cli("claude", "Marco")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Não consegui trocar a conta Claude\n"
                              "O token da conta Claude Marco não está em ~/.config/multicred/secrets. "
                              "Gere outro com: multicred entrar claude Marco\n")

    def test_menus_run_without_gtk(self):
        self.configured()
        blocker = self.home / "blocked"
        (blocker / "gi").mkdir(parents=True)
        (blocker / "gi" / "__init__.py").write_text("raise ImportError('PyGObject is not available on Termux')\n")
        environ = dict(self.termux_environ, PYTHONPATH=f"{blocker}:{LINUX}", PATH=f"{self.prefix}/bin",
                       LANG="C.UTF-8")
        for argv, code, first in ((["contas"], 0, "Contas"), (["contas", "--menu"], 0, "Contas"),
                                  (["perfis", "--menu"], 0, "Perfis"), (["configurar"], 1, "O MultiCred já está")):
            result = subprocess.run([sys.executable, "-m", "multicred", *argv], capture_output=True, text=True,
                                    env=environ, cwd=str(self.home), timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, code, (argv, result.stdout, result.stderr))
            self.assertTrue(result.stdout.startswith(first), (argv, result.stdout))
            self.assertNotIn("Traceback", result.stderr, argv)


if __name__ == "__main__":
    import unittest
    unittest.main()
