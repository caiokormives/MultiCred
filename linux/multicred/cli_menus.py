"""The terminal versions of the app's setup and account screens (R-10).

    multicred configurar        the onboarding (OnboardingView.swift): Preparar → Perfis → Aplicar
    multicred contas [--menu]   Contas › Git, GitHub, Claude, Chaves de API (AccountsPages.swift,
                                ClaudePages.swift, AddClaudeAccountPage.swift)
    multicred perfis --menu     the profile list and editor (ProfileEditorPage.swift)
    multicred entrar …          the "Entrar…" buttons: an account again, a folder's own login, GitHub

On Termux this is the only interface; on the desktop it is the companion of the window. No GTK
here (Termux has no PyGObject), so the few texts the GTK pages also hold are repeated here, in the
Mac's words with the Linux term table (Keychain → the secret store wording, which names the 0600
files on Termux). Every rule comes from the core the app uses: ConfigEditor and validation for the
accounts, account_editor for Claude, SetupDetector/Installer for the setup, ClaudeLoginSession and
GitHubLoginSession for logins (their simulators in test mode, never the real claude or gh).

Input conventions: a menu lists keys ("1", "n", "s"…) and reads one; Enter takes the menu's
default; a field shows its value in brackets and Enter keeps it ("-" clears an optional one);
end of input leaves (Leave). When the input is not a terminal, every answer is echoed, so a
scripted session reads like a typed one; secrets are never echoed.
"""

from __future__ import annotations

import copy
import getpass
import queue
import unicodedata

from .cli import choice_index
from .core.models import APIProvider, ClaudeAccountKind, GitHubAccount, GitIdentity, Profile, ProfileColor, \
    ProfileIcon, ShellKind

COLOR_NAMES = {
    ProfileColor.INDIGO: "Índigo", ProfileColor.BLUE: "Azul", ProfileColor.TEAL: "Verde-azulado",
    ProfileColor.GREEN: "Verde", ProfileColor.ORANGE: "Laranja", ProfileColor.RED: "Vermelho",
    ProfileColor.PINK: "Rosa", ProfileColor.GRAPHITE: "Grafite",
}
# Components.swift:65, the same names the tray draws (tray/icon.py, which needs cairo).
SYMBOLS = ("briefcase", "house", "building.2", "laptopcomputer", "star", "person", "flask", "globe")
SYMBOL_NAMES = {"briefcase": "Maleta", "house": "Casa", "building.2": "Prédios", "laptopcomputer": "Notebook",
                "star": "Estrela", "person": "Pessoa", "flask": "Frasco", "globe": "Globo"}
YES = {"s", "sim", "y", "yes"}
NO = {"n", "nao", "não", "no"}
SHELL_ORDER = (ShellKind.BASH, ShellKind.ZSH, ShellKind.FISH)
FIELD_NAMES = {"nickname": "apelido", "name": "nome", "email": "e-mail"}
# After a folder login that did not end connected (the code was typed late or mistyped).
FOLDER_LOGIN_FAILURE_SUFFIX = " O código vale por poucos minutos: gere outro no navegador e tente de novo."


def make_autostart(environment):
    """"Abrir ao iniciar a sessão" (tests replace it with one pointing at a stand-in command)."""
    from .core.install.autostart import Autostart
    return Autostart(environment)


class Leave(Exception):
    """End of input (or Ctrl+D) while a menu waited for an answer."""


def plural(count: int, singular: str, plural_: str, none: str | None = None) -> str:
    if count == 0 and none is not None:
        return none
    return f"{count} {singular if count == 1 else plural_}"


def slug(name: str) -> str:
    """ProfileEditorPage.swift:208-213: folded, lower case, other characters → "-"."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    mapped = "".join(ch if ch.isalnum() else "-" for ch in folded)
    return "-".join(part for part in mapped.split("-") if part) or "novo"


def issue_message(issue, is_profile: bool = False) -> str:
    """The validation issue in the app's words (ui/widgets.py issue_message, which needs GTK)."""
    kind, detail = issue.kind, issue.detail
    if kind == "emptyField" and is_profile and detail == "name":
        return "Dê um nome ao perfil."
    return {
        "emptyField": f"O campo {FIELD_NAMES.get(detail, detail)} está vazio.",
        "controlCharacters": f"O campo {FIELD_NAMES.get(detail, detail)} tem quebra de linha ou caractere de controle.",
        "invalidEmail": "E-mail inválido.",
        "invalidGitHubUsername": "Usuário do GitHub inválido (só letras, números e hífen).",
        "invalidHost": "Servidor do GitHub inválido.",
        "invalidEnvVar": f"\"{detail}\" não é um nome de variável válido (use LETRAS_MAIUSCULAS e _).",
        "reservedEnvVar": f"{detail} é reservada e não pode ser usada para uma chave.",
        "duplicateEnvVarInProfile": f"O perfil tem duas chaves para {detail}.",
        "invalidClaudeDirectory": "Pasta do Claude inválida (use um caminho absoluto, sem / no fim).",
        "invalidSSHKeyPath": "Caminho da chave SSH inválido (use caminho absoluto ou começando com ~/).",
        "missingReference": "O perfil aponta para uma conta que não existe mais.",
        "duplicateID": "Configuração com identificadores repetidos.",
    }.get(kind, f"Dados inválidos ({kind}).")


# MARK: terminal I/O

class Terminal:
    def __init__(self, out, inp, interactive: bool):
        self.out, self.inp, self.interactive = out, inp, interactive
        try:
            self.tty = bool(inp.isatty())
        except (AttributeError, ValueError):
            self.tty = False

    def say(self, *lines) -> None:
        for line in lines:
            print(line, file=self.out)
        self.out.flush()

    def read(self, prompt: str, secret: bool = False) -> str:
        if secret and self.tty:
            try:
                return getpass.getpass(prompt, stream=self.out)
            except EOFError:
                raise Leave() from None
        print(prompt, end="", file=self.out, flush=True)
        try:
            line = self.inp.readline()
        except (OSError, ValueError):
            line = ""
        if not line:
            print(file=self.out)
            raise Leave()
        line = line.rstrip("\r\n")
        if not self.tty:
            print("" if secret else line, file=self.out)
        return line

    def ask(self, label: str, current: str | None = None, example: str | None = None,
            optional: bool = False) -> str:
        """The answer, or `current` on Enter; "-" clears an optional field."""
        hint = f" [{current}]" if current else (f" (ex.: {example})" if example else "")
        answer = self.read(f"{label}{hint}: ").strip()
        if optional and answer == "-":
            return ""
        return answer or (current or "")

    def choose(self, entries, default: str | None = None, prompt: str = "Escolha") -> str:
        """Lists `entries` ((key, label), or a plain line) and returns the key typed."""
        keys = []
        for entry in entries:
            if isinstance(entry, str):
                self.say(entry)
                continue
            key, label = entry
            keys.append(key)
            self.say(f"  {key}  {label}")
        while True:
            answer = self.read(f"{prompt}: ").strip().casefold()
            if not answer and default is not None:
                return default
            if answer in keys:
                return answer
            self.say("Opção inválida.")

    def pick(self, title: str, entries, current):
        """A numbered list of (value, label); Enter keeps `current`."""
        for index, (_value, label) in enumerate(entries, start=1):
            self.say(f"  {index}  {label}")
        current_label = next((label for value, label in entries if value == current), None)
        while True:
            answer = self.read(f"{title}" + (f" [{current_label}]" if current_label else "") + ": ").strip()
            if not answer:
                return current
            index = choice_index(answer, len(entries))
            if index is not None:
                return entries[index][0]
            match = [value for value, label in entries if label.casefold() == answer.casefold()]
            if match:
                return match[0]
            self.say("Opção inválida.")

    def color(self, current: ProfileColor) -> ProfileColor:
        colors = list(ProfileColor)
        self.say("Cores: " + " · ".join(f"{i} {COLOR_NAMES[c]}" for i, c in enumerate(colors, start=1)))
        while True:
            answer = self.read(f"Cor [{COLOR_NAMES[current]}]: ").strip()
            if not answer:
                return current
            index = choice_index(answer, len(colors))
            if index is not None:
                return colors[index]
            match = [c for c in colors if COLOR_NAMES[c].casefold() == answer.casefold()]
            if match:
                return match[0]
            self.say("Opção inválida.")

    def confirm(self, question: str, default: bool = False) -> bool:
        while True:
            answer = self.read(f"{question} {'[S/n]' if default else '[s/N]'} ").strip().casefold()
            if not answer:
                return default
            if answer in YES:
                return True
            if answer in NO:
                return False
            self.say("Responda s ou n.")


class _Pump:
    """The login sessions' `dispatch`: callbacks from the pseudo-terminal threads queue up
    here and run on this thread, in order (GLib.idle_add in the app)."""

    def __init__(self):
        self.queue = queue.Queue()

    def __call__(self, fn, *args):
        self.queue.put((fn, args))

    def run(self, wait: float = 0.1) -> None:
        try:
            fn, args = self.queue.get(timeout=wait)
        except queue.Empty:
            return
        fn(*args)
        while True:
            try:
                fn, args = self.queue.get_nowait()
            except queue.Empty:
                return
            fn(*args)


# MARK: the menus

class Menus:
    """One terminal session over cli._Session (environment, tools, log, the secret store wording,
    loading the configuration and the switch helpers)."""

    def __init__(self, session, terminal: Terminal):
        self.session = session
        self.t = terminal
        self.environment = session.environment
        self.tools = session.tools
        self.termux = not self.environment.platform.has_graphical_session
        store = session.store
        # "no chaveiro do sistema" / "em ~/.config/multicred/secrets"
        self.located = store.located
        # "o chaveiro do sistema" / "a pasta ~/.config/multicred/secrets"
        self.into = store.store
        # "do chaveiro do sistema" / "da pasta ~/.config/multicred/secrets"
        self.out_of = "d" + store.store

    # MARK: persistence

    def _load(self):
        return self.session._load()

    def _save(self, config) -> None:
        from .core.storage import ConfigRepository
        ConfigRepository(self.environment).save(config)

    def _edit(self, change) -> bool:
        """Applies a ConfigEditor change and saves it; False after saying why it failed."""
        loaded = self._load()
        if loaded is None:
            return False
        try:
            self._save(change(loaded[0]))
        except Exception as error:
            self.t.say(self.describe(error))
            return False
        return True

    def describe(self, error) -> str:
        """ui/widgets.py describe_error, with the platform's secret store wording."""
        from .core.adapters.github_cli import UnexpectedOutput
        from .core.claude.account_editor import ClaudeAccountError
        from .core.claude.login_vault import ClaudeLoginError
        from .core.editors import EditorError
        from .core.engine.types import SwitchError
        from .core.storage import InvalidConfig

        if isinstance(error, (SwitchError, ClaudeLoginError)):
            return self.session._describe(error)
        if isinstance(error, EditorError):
            if error.kind == "invalid":
                return self._issues(error.issues)
            if error.kind == "emptySecret":
                return "Cole o valor da chave."
            return self._store_failure(error.reason)
        if isinstance(error, InvalidConfig):
            return self._issues(error.issues)
        if isinstance(error, UnexpectedOutput):
            return "O GitHub CLI respondeu algo inesperado."
        if isinstance(error, ClaudeAccountError):
            if error.kind == "invalid":
                return self._issues(error.detail)
            # UserMessages.swift:111-118
            return {"invalidPath": f"Caminho inválido: {error.detail}. Use um caminho absoluto ou começando com ~/.",
                    "invalidToken": "Isso não parece um token do Claude Code. Gere um com \"claude setup-token\" e "
                                    "cole a linha que começa com sk-ant-oat.",
                    "folderMissing": f"A pasta não existe: {error.detail}",
                    "notADirectory": f"Não é uma pasta: {error.detail}",
                    "outsideSandbox": "No modo de teste, a pasta precisa ficar dentro do HOME de teste "
                                      f"({error.detail} está fora).",
                    "createFailed": f"Não consegui criar a pasta: {error.detail}",
                    "secrets": self._store_failure(error.detail),
                    }.get(error.kind, str(error))
        if isinstance(error, OSError):
            return f"Não consegui salvar: {error}"
        return str(error)

    def _store_failure(self, reason) -> str:
        return f"Não consegui gravar {self.located}: {reason}"

    @staticmethod
    def _issues(issues, is_profile: bool = False) -> str:
        issues = list(issues or [])
        if not issues:
            return "Dados inválidos."
        text = issue_message(issues[0], is_profile)
        return f"{text} (+{len(issues) - 1} problema(s))" if len(issues) > 1 else text

    def _has_secret(self, account) -> bool:
        try:
            return bool(self.environment.secrets.contains_item(account.secret_account))
        except Exception:
            return False

    @staticmethod
    def _used_in(profiles) -> str:
        return ", ".join(p.name for p in profiles) or "—"

    def _folder_name(self, config_dir) -> str:
        from .cli import folder_name
        return folder_name(self.environment, config_dir)

    def _reapply_if_active(self, config, state, profile_id) -> None:
        """The active profile changed: apply it again, like the app (switch + notification)."""
        if profile_id is not None and state.profile_id == profile_id:
            self.session.switch_to(profile_id)

    # MARK: contas (the root)

    def accounts(self, interactive: bool) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        if not interactive:
            self.t.say(*self.accounts_listing(*loaded))
            return 0
        first = True
        try:
            while True:
                loaded = self._load()
                if loaded is None:
                    return 1
                config, _, state = loaded
                self.t.say(*(["Contas"] if first else ["", "Contas"]))
                first = False
                key = self.t.choose([("1", self._section_title("git", config)),
                                     ("2", self._section_title("github", config)),
                                     ("3", self._section_title("claude", config)),
                                     ("4", self._section_title("keys", config)), ("0", "Sair")], default="0")
                if key == "0":
                    return 0
                {"1": self.git_page, "2": self.github_page, "3": self.claude_page, "4": self.keys_page}[key]()
        except Leave:
            return 0

    def _section_title(self, section: str, config) -> str:
        """The sidebar's row: title, count and ⚠ by the Mac's rule (MainWindowView.swift:52-88).
        GitHub's ⚠ needs gh's logins (network): shown on its page instead."""
        if section == "git":
            return "Git · " + plural(len(config.git_identities), "identidade", "identidades", "nenhuma identidade")
        if section == "github":
            return "GitHub · " + plural(len(config.github_accounts), "conta", "contas", "nenhuma conta")
        if section == "claude":
            warn = any(self.session._needs_claude_login(config, p) for p in config.profiles)
            return "Claude · " + plural(len(config.claude_accounts), "conta", "contas", "nenhuma conta") + \
                ("  ⚠" if warn else "")
        warn = any(not self._has_secret(k) for k in config.api_keys)
        return "Chaves de API · " + plural(len(config.api_keys), "chave", "chaves", "nenhuma chave") + \
            ("  ⚠" if warn else "")

    def accounts_listing(self, config, _engine, state) -> list[str]:
        lines = ["Contas"]
        for section, rows in (("git", self._git_rows(config)), ("github", self._github_rows(config, logins=False)),
                              ("claude", self._claude_account_rows(config, state)), ("keys", self._key_rows(config))):
            lines.append(self._section_title(section, config))
            lines += [f"  {row}" for row in rows]
        return lines

    # MARK: Git (AccountsPages.swift:56-163)

    def _git_rows(self, config) -> list[str]:
        return [f"{i.nickname} · {i.name} · {i.email} · "
                f"{self.environment.abbreviate(self.environment.expand_tilde(i.ssh_key_path)) if i.ssh_key_path else 'SSH padrão'}"
                f" · usado em {self._used_in([p for p in config.profiles if p.git_identity_id == i.id])}"
                for i in config.git_identities]

    def git_page(self) -> None:
        while True:
            config = self._load()[0]
            self.t.say("", "Identidades git", "Nome e e-mail dos commits (e, se quiser, a chave SSH).")
            entries = []
            if not config.git_identities:
                entries += ["Nenhuma identidade git", "Adicione o nome e o e-mail que cada perfil usa nos commits."]
            entries += [(str(n), row) for n, row in enumerate(self._git_rows(config), start=1)]
            entries += [("n", "Adicionar identidade"), ("0", "Voltar"),
                        "O MultiCred escreve só o arquivo incluído no fim do seu gitconfig global. "
                        "O seu ~/.gitconfig continua seu."]
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            self.git_editor(None if key == "n" else config.git_identities[int(key) - 1])

    def git_editor(self, existing) -> None:
        from .core.editors import ConfigEditor

        config = self._load()[0]
        draft = copy.deepcopy(existing) if existing is not None else GitIdentity(
            nickname="", name="", email="", color=list(ProfileColor)[len(config.git_identities) % 8])
        title = "Nova identidade git" if existing is None else "Editar identidade git"
        footer = ("Com uma chave, o git usa core.sshCommand = ssh -i <chave> -o IdentitiesOnly=yes. Sem chave, vale "
                  "a sua configuração de SSH.")
        fields = [
            ("Apelido", lambda: draft.nickname, lambda v: setattr(draft, "nickname", v), "Trabalho"),
            ("Nome nos commits", lambda: draft.name, lambda v: setattr(draft, "name", v), "Seu Nome"),
            ("E-mail nos commits", lambda: draft.email, lambda v: setattr(draft, "email", v), "voce@empresa.com"),
        ]
        self.t.say("", title)
        if existing is None:
            for label, get, put, example in fields:
                put(self.t.ask(label, get(), example))
            draft.color = self.t.color(draft.color)
            draft.ssh_key_path = self.t.ask("Chave SSH (opcional)", None, "~/.ssh/id_ed25519_trabalho",
                                            optional=True) or None
            self.t.say(footer)
        while True:
            self.t.say("", title)
            entries = [(str(n), f"{label} · {get() or '—'}") for n, (label, get, _, _) in enumerate(fields, start=1)]
            entries += [("4", f"Cor · {COLOR_NAMES[draft.color]}"),
                        ("5", f"Chave SSH (opcional) · {draft.ssh_key_path or '—'}"), ("s", "Salvar")]
            if existing is not None:
                entries.append(("r", "Remover"))
            entries += [("0", "Cancelar"), footer]
            key = self.t.choose(entries, default="s")
            if key == "0":
                return
            if key in ("1", "2", "3"):
                label, get, put, example = fields[int(key) - 1]
                put(self.t.ask(label, get(), example))
            elif key == "4":
                draft.color = self.t.color(draft.color)
            elif key == "5":
                draft.ssh_key_path = self.t.ask("Chave SSH (opcional)", draft.ssh_key_path,
                                                "~/.ssh/id_ed25519_trabalho", optional=True) or None
            elif key == "r":
                self.t.say(f"Remover a identidade {existing.nickname}?", "Os perfis que a usam deixam de trocar o git.")
                if self.t.confirm("Remover?") and self._edit(
                        lambda c: ConfigEditor.remove_git_identity(existing.id, c)):
                    self.t.say(f"Identidade {existing.nickname} removida")
                    return
            elif key == "s":
                for attribute in ("nickname", "name", "email"):
                    setattr(draft, attribute, getattr(draft, attribute).strip(" "))
                missing = [label for label, get, _, _ in fields if not get()]
                if missing:
                    self.t.say(f"Preencha {_join(missing)} para salvar.")
                    continue
                if self._edit(lambda c: ConfigEditor.upsert(draft, c)):
                    self.t.say(f"Identidade {draft.nickname} salva")
                    return

    # MARK: GitHub (AccountsPages.swift:167-446)

    def _github_cli(self):
        from .core.adapters.github_cli import GitHubCLI
        from .core.tools import Tool

        gh = self.tools.locate(Tool.GH)
        if gh is None:
            return None
        return GitHubCLI(self.environment.runner, gh,
                         self.environment.base_process_environment(self.tools.search_directories()))

    def _gh_logins(self):
        """gh's logins ("Conferir logins"), or None when gh did not answer."""
        cli = self._github_cli()
        if cli is None:
            return None
        try:
            return cli.logins()
        except Exception:
            return None

    def _github_rows(self, config, logins) -> list[str]:
        """`logins` False: no status column (the listing does not ask gh)."""
        rows = []
        for account in config.github_accounts:
            login = None
            if logins:
                login = next((l for l in logins if l.host == account.host
                              and l.login.casefold() == account.username.casefold()), None)
            row = (f"{account.nickname} · {account.username} · {account.host}"
                   + (" · ativa no gh" if login is not None and login.active else "")
                   + f" · usado em {self._used_in([p for p in config.profiles if p.github_account_id == account.id])}")
            if logins is not False:
                if logins is None:
                    row += " · Status indisponível"
                elif login is None:
                    row += " · Não está no gh"
                else:
                    row += " · Conectada" if login.is_usable else " · Precisa entrar"
            rows.append(row)
        return rows

    def github_page(self) -> None:
        logins = self._gh_logins()
        while True:
            config = self._load()[0]
            self.t.say("", "Contas GitHub", "Contas logadas no GitHub CLI (gh). A troca usa gh auth switch.")
            entries = []
            if not config.github_accounts:
                entries += ["Nenhuma conta GitHub", "Adicione uma conta que já está no gh ou entre com outra."]
            entries += [(str(n), row) for n, row in enumerate(self._github_rows(config, logins), start=1)]
            entries += [("n", "Adicionar conta"), ("c", "Conferir logins"), ("0", "Voltar"),
                        "O login e o token ficam com o próprio gh. O MultiCred só escolhe a conta ativa."]
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            if key == "c":
                logins = self._gh_logins()
            elif key == "n":
                self.github_editor(None, logins)
                logins = self._gh_logins()
            else:
                self.github_editor(config.github_accounts[int(key) - 1], logins)

    def github_editor(self, existing, logins) -> None:
        from .core.editors import ConfigEditor

        config = self._load()[0]
        title = "Adicionar conta GitHub" if existing is None else "Editar conta GitHub"
        draft = copy.deepcopy(existing) if existing is not None else GitHubAccount(
            nickname="", username="", color=list(ProfileColor)[len(config.github_accounts) % 8])
        self.t.say("", title)
        if existing is None:
            available = [l for l in (logins or []) if not any(
                a.username.casefold() == l.login.casefold() and a.host == l.host for a in config.github_accounts)]
            entries = ["Contas que o gh já conhece"]
            if not available:
                entries.append("Nenhuma conta encontrada no gh." if not logins
                               else "Todas as contas do gh já foram adicionadas.")
            entries += [(str(n), f"{l.login} · {l.host}" + ("  ativa" if l.active else ""))
                        for n, l in enumerate(available, start=1)]
            entries += [("e", "Entrar com outra conta no GitHub…"), ("m", "Preencher à mão"), ("0", "Cancelar")]
            key = self.t.choose(entries, default="m")
            if key == "0":
                return
            if key == "e":
                self.github_login(draft.host)
                return
            if key != "m":
                login = available[int(key) - 1]
                draft.username, draft.host, draft.nickname = login.login, login.host, login.login
            draft.nickname = self.t.ask("Apelido", draft.nickname, "Trabalho")
            draft.username = self.t.ask("Usuário no GitHub", draft.username, "seu-usuario")
            draft.host = self.t.ask("Servidor", draft.host)
            draft.color = self.t.color(draft.color)
            draft.claude_co_author = self.t.confirm("Incluir a Claude como co-autora dos commits?", True)
            self.t.say(self._coauthor_text(draft.claude_co_author))
        else:
            self.t.say(f"Usuário no GitHub · {draft.username}", f"Servidor · {draft.host}")
        while True:
            self.t.say("", title)
            entries = [("1", f"Apelido · {draft.nickname or '—'}")]
            if existing is None:
                entries += [("2", f"Usuário no GitHub · {draft.username or '—'}"), ("3", f"Servidor · {draft.host}")]
            entries += [("4", f"Cor · {COLOR_NAMES[draft.color]}"),
                        ("5", "Incluir a Claude como co-autora dos commits · "
                              + ("sim" if draft.claude_co_author else "não")), ("s", "Salvar")]
            if existing is not None:
                entries += [("e", "Entrar de novo no GitHub…"), ("r", "Remover")]
            entries += [("0", "Cancelar"), self._coauthor_text(draft.claude_co_author)]
            key = self.t.choose(entries, default="s")
            if key == "0":
                return
            if key == "1":
                draft.nickname = self.t.ask("Apelido", draft.nickname, "Trabalho")
            elif key == "2" and existing is None:
                draft.username = self.t.ask("Usuário no GitHub", draft.username, "seu-usuario")
            elif key == "3" and existing is None:
                draft.host = self.t.ask("Servidor", draft.host)
            elif key == "4":
                draft.color = self.t.color(draft.color)
            elif key == "5":
                draft.claude_co_author = self.t.confirm("Incluir a Claude como co-autora dos commits?",
                                                        draft.claude_co_author)
            elif key == "e" and existing is not None:
                self.github_login(existing.host)
            elif key == "r" and existing is not None:
                self.t.say(f"Remover a conta {existing.nickname}?",
                           "Ela continua logada no gh; os perfis que a usam deixam de trocar o GitHub.")
                if self.t.confirm("Remover?") and self._edit(
                        lambda c: ConfigEditor.remove_github_account(existing.id, c)):
                    self.t.say(f"Conta {existing.nickname} removida")
                    return
            elif key == "s":
                draft.nickname, draft.username = draft.nickname.strip(" "), draft.username.strip()
                draft.host = draft.host.strip() or "github.com"
                if not draft.nickname or not draft.username:
                    self.t.say(f"Preencha {_join([l for l, v in (('Apelido', draft.nickname), ('Usuário no GitHub', draft.username)) if not v])} para salvar.")
                    continue
                if not self._edit(lambda c: ConfigEditor.upsert(draft, c)):
                    continue
                self.t.say(f"Conta {draft.nickname} salva")
                # The active profile uses it: apply the co-author choice now (D38), like the Mac.
                config, _, state = self._load()
                active = config.profile(state.profile_id)
                if (existing is not None and existing.claude_co_author != draft.claude_co_author
                        and active is not None and active.github_account_id == draft.id):
                    self.session.switch_to(active.id)
                return

    @staticmethod
    def _coauthor_text(on: bool) -> str:
        return ("Commits e PRs feitos pelo Claude Code nos perfis com esta conta terminam com \"Co-Authored-By: "
                "Claude\" (padrão do Claude Code)." if on else
                "Commits e PRs feitos pelo Claude Code nos perfis com esta conta saem só com o seu nome. O MultiCred "
                "grava isso no settings.json da pasta do Claude do perfil ao ativá-lo.")

    def github_login(self, host: str = "github.com") -> bool:
        """"Entrar no GitHub" (AccountsPages.swift:340-446): gh auth login --web in a pseudo-terminal;
        afterwards the accounts gh knows are added and gh goes back to the profile's account."""
        from .core.adapters.github_cli import GitHubLogin
        from .core.adapters.github_login_flow import GitHubLoginSession
        from .core.editors import ConfigEditor

        loaded = self._load()
        if loaded is None:
            return False
        config, _, state = loaded
        active = config.profile(state.profile_id)
        account = config.github_account(active.github_account_id) if active else None
        switch_back = account.username if account is not None and account.host == host else None
        pump, finished = _Pump(), []
        session = GitHubLoginSession(self.environment, self.tools, host, switch_back, dispatch=pump,
                                     on_finished=finished.append, log=self.session.log)
        self.t.say("", "Entrar no GitHub", host)
        if session.simulated:
            self.t.say("Simulação do modo de teste: o navegador não abre e nenhum login real acontece.")
        self.t.say("Iniciando o GitHub CLI…")
        session.start()
        opened = False
        try:
            while not finished:
                pump.run()
                if session.phase.kind == "showingCode" and not opened and not finished:
                    self.t.say("1. Copie este código. 2. Abra o GitHub e cole o código. 3. Autorize o GitHub CLI.",
                               "", f"    {session.code}", "")
                    if session.device_url:
                        self.t.say(f"Endereço: {session.device_url}")
                    self.t.read("Copie o código e pressione Enter para abrir o GitHub ")
                    session.open_browser()
                    opened = True
                    self.t.say("Aguardando a autorização no navegador…")
        except (Leave, KeyboardInterrupt):
            session.cancel()
            self.t.say("Cancelado.")
            return False
        phase = session.phase
        if phase.kind != "connected":
            self.t.say(self._launch_error(session.launch_error) or f"Não deu certo ({phase.value}).")
            return False
        self.t.say(f"Pronto: {session.login or 'conta'} está logada no gh. A conta ativa voltou para a do perfil "
                   "atual.")
        known = [GitHubLogin(host, session.login, False, "success")] if session.login else []
        self._edit(lambda c: ConfigEditor.add_missing_github_accounts(known + (self._gh_logins() or []), c))
        return True

    # MARK: Chaves de API (AccountsPages.swift:450-580)

    def _key_status(self, key) -> str:
        if self._has_secret(key):
            return "No arquivo" if self.termux else "No chaveiro"
        return "Sem valor"

    def _key_rows(self, config) -> list[str]:
        return [f"{k.nickname} · {k.provider.display_name} · {k.env_var} · usado em "
                f"{self._used_in([p for p in config.profiles if k.id in p.api_key_ids])} · {self._key_status(k)}"
                for k in config.api_keys]

    def keys_page(self) -> None:
        while True:
            config = self._load()[0]
            self.t.say("", "Chaves de API", "Exportadas como variáveis de ambiente nos terminais do perfil.")
            entries = []
            if not config.api_keys:
                entries += ["Nenhuma chave",
                            f"Adicione chaves da OpenAI, Gemini e outras. O valor fica só {self.located}."]
            entries += [(str(n), row) for n, row in enumerate(self._key_rows(config), start=1)]
            entries += [("n", "Adicionar chave"), ("0", "Voltar"),
                        f"O valor de cada chave fica só {self.located}. O MultiCred guarda apenas o nome da variável "
                        "e qual item usar; o terminal lê o valor quando o perfil muda."
                        + ("" if self.termux else " As chaves não são passadas para apps gráficos.")]
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            self.key_editor(None if key == "n" else config.api_keys[int(key) - 1])

    def key_editor(self, existing) -> None:
        from .core.editors import ConfigEditor
        from .core.models import APIKeyAccount

        config = self._load()[0]
        title = "Nova chave de API" if existing is None else "Editar chave de API"
        nickname = existing.nickname if existing else ""
        provider = existing.provider if existing else APIProvider.OPENAI
        env_var = existing.env_var if existing else provider.default_env_var
        color = existing.color if existing else list(ProfileColor)[len(config.api_keys) % 8]
        secret = ""
        providers = [(p, p.display_name) for p in APIProvider]

        def description() -> str:
            if existing is None:
                return f"O valor vai direto para {self.into} e não aparece mais aqui."
            if self._has_secret(existing):
                return f"Há uma chave guardada {self.located}. Deixe em branco para mantê-la."
            return f"Não há valor {self.located} para esta chave."

        def choose_provider(current_provider, current_var):
            chosen = self.t.pick("Provedor", providers, current_provider)
            # The variable follows the provider while it was not edited (AccountsPages.swift:517).
            if not current_var or current_var == current_provider.default_env_var:
                current_var = chosen.default_env_var
            return chosen, current_var

        self.t.say("", title)
        if existing is None:
            nickname = self.t.ask("Apelido", nickname, "Trabalho")
            provider, env_var = choose_provider(provider, env_var)
            env_var = self.t.ask("Variável de ambiente", env_var, "OPENAI_API_KEY")
            color = self.t.color(color)
            self.t.say(description())
            secret = self.t.read("Cole a chave: ", secret=True)
        while True:
            self.t.say("", title)
            entries = [("1", f"Apelido · {nickname or '—'}"), ("2", f"Provedor · {provider.display_name}"),
                       ("3", f"Variável de ambiente · {env_var or '—'}"), ("4", f"Cor · {COLOR_NAMES[color]}"),
                       ("5", ("Cole a chave" if existing is None else "Cole uma nova chave para substituir")
                        + (" · colada" if secret.strip() else "")), ("s", "Salvar")]
            if existing is not None:
                entries.append(("r", "Remover"))
            entries += [("0", "Cancelar"), description()]
            key = self.t.choose(entries, default="s")
            if key == "0":
                return
            if key == "1":
                nickname = self.t.ask("Apelido", nickname, "Trabalho")
            elif key == "2":
                provider, env_var = choose_provider(provider, env_var)
            elif key == "3":
                env_var = self.t.ask("Variável de ambiente", env_var, "OPENAI_API_KEY")
            elif key == "4":
                color = self.t.color(color)
            elif key == "5":
                secret = self.t.read(("Cole a chave" if existing is None
                                      else "Cole uma nova chave para substituir") + ": ", secret=True)
            elif key == "r" and existing is not None:
                self.t.say(f"Remover a chave {existing.nickname}?",
                           f"O valor é apagado {self.out_of} e os perfis deixam de exportar {existing.env_var}.")
                if self.t.confirm("Remover?") and self._remove_key(existing):
                    self.t.say(f"Chave {existing.nickname} removida")
                    return
            elif key == "s":
                nickname, env_var = nickname.strip(" "), env_var.strip()
                missing = [label for label, value in (("Apelido", nickname), ("Variável de ambiente", env_var),
                                                      ("a chave", secret.strip() or existing)) if not value]
                if missing:
                    self.t.say(f"Preencha {_join(missing)} para salvar.")
                    continue
                if self._save_key(existing, nickname, provider, env_var, color, secret, APIKeyAccount, ConfigEditor):
                    self.t.say(f"Chave {nickname} salva")
                    return

    def _save_key(self, existing, nickname, provider, env_var, color, secret, APIKeyAccount, ConfigEditor) -> bool:
        """ui/model.py add_api_key: the value goes to the store first; a failed save deletes it again."""
        config = self._load()[0]
        try:
            if existing is not None:
                updated = copy.deepcopy(existing)
                updated.nickname, updated.provider, updated.env_var, updated.color = nickname, provider, env_var, color
                self._save(ConfigEditor.update_api_key(updated, config))
                if secret.strip():
                    ConfigEditor.replace_secret(updated, secret, self.environment.secrets)
                return True
            updated, key = ConfigEditor.add_api_key(nickname, provider, env_var, secret, color, config,
                                                    self.environment.secrets)
            try:
                self._save(updated)
            except Exception:
                try:
                    self.environment.secrets.delete_item(key.secret_account)
                except Exception:
                    pass
                raise
            return True
        except Exception as error:
            self.t.say(self.describe(error))
            return False

    def _remove_key(self, key) -> bool:
        """ui/model.py remove_api_key: the secret goes after the save; an exported key re-applies
        the active profile so shells stop exporting it."""
        from .core.editors import ConfigEditor

        config, _, state = self._load()
        active = config.profile(state.profile_id)
        exported = active is not None and key.id in active.api_key_ids
        if not self._edit(lambda c: ConfigEditor.remove_api_key(key.id, c)):
            return False
        ConfigEditor.discard_removed_key(key, self.environment.secrets)
        if exported:
            self.session.switch_to(active.id)
        return True

    # MARK: Claude (ClaudePages.swift)

    def _claude_account_rows(self, config, state) -> list[str]:
        rows = []
        for account in config.claude_accounts:
            has_token = self.session._has_token(account)
            if account.kind == ClaudeAccountKind.LOGIN:
                detail = "login completo · troca até nas conversas abertas"
            else:
                detail = "token · só terminais novos" if has_token else "sem token"
            if account.id == state.applied_claude_account_id:
                status = "Em uso"
            else:
                status = "Usar agora" if has_token else "Sem token"
            used = self._used_in([p for p in config.profiles if p.claude_account_id == account.id])
            rows.append(f"{account.nickname} · {account.email or 'conta Claude'} · {detail} · usado em {used} · "
                        f"{status}")
        return rows

    def _folder_status_text(self, config_dir) -> str:
        status = self.session._folder_status(config_dir)
        if status is not None and status.kind == "loggedIn":
            return status.email or "Logada"
        if status is not None and status.kind == "loggedOut":
            return "Sem login"
        return "Status indisponível"

    def _folders_in_use(self, config) -> list:
        folders = [None]
        for profile in config.profiles:
            if profile.claude_config_dir not in folders:
                folders.append(profile.claude_config_dir)
        return folders

    def _folder_profiles(self, config, config_dir) -> list:
        return [p for p in config.profiles if p.claude_config_dir == config_dir]

    def _folder_row(self, config, config_dir) -> str:
        from .core.claude import folder_inspector

        profiles = self._folder_profiles(config, config_dir)
        who = ("Perfis: " + ", ".join(p.name for p in profiles)) if profiles else "Pasta padrão (nenhum perfil)"
        summary = folder_inspector.summarize(config_dir, self.environment)
        counts = (f"{plural(summary.project_count, 'projeto', 'projetos')} · "
                  f"{plural(summary.conversation_count, 'conversa', 'conversas')}")
        return f"{self._folder_name(config_dir)} · {who} · {counts} · {self._folder_status_text(config_dir)}"

    def claude_page(self) -> None:
        while True:
            config, _, state = self._load()
            folders = self._folders_in_use(config)
            self.t.say("", "Claude", "Pastas guardam agents, skills e conversas. Contas são só o login usado nelas.")
            entries = ["Pastas dos perfis"]
            entries += [(f"p{n}", self._folder_row(config, folder)) for n, folder in enumerate(folders, start=1)]
            entries += ["Cada perfil usa uma pasta. Trocar a conta não muda a pasta.", "Contas Claude (só credencial)"]
            if not config.claude_accounts:
                entries.append("Nenhuma conta extra. Adicione uma para usar o login de outra pessoa ou de outra "
                               "assinatura sem trocar de pasta.")
            entries += [(str(n), row) for n, row in enumerate(self._claude_account_rows(config, state), start=1)]
            entries += [("n", "Adicionar conta"), ("0", "Voltar"),
                        f"O token de cada conta fica só {self.located} e vale nos terminais (CLAUDE_CODE_OAUTH_TOKEN)."
                        + ("" if self.termux else " Apps gráficos continuam com o login da pasta.")]
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            if key == "n":
                self.claude_add(None)
            elif key.startswith("p"):
                self.folder_detail(folders[int(key[1:]) - 1])
            else:
                self.claude_account_detail(config.claude_accounts[int(key) - 1].id)

    def folder_detail(self, config_dir) -> None:
        """ClaudePages.swift:159-218: what the folder holds and its own login."""
        from .core.claude import folder_inspector

        while True:
            config = self._load()[0]
            profiles = self._folder_profiles(config, config_dir)
            summary = folder_inspector.summarize(config_dir, self.environment)
            account = " · ".join(x for x in (summary.email, summary.organization) if x) or "sem login"
            self.t.say("", self._folder_name(config_dir),
                       ("Perfis: " + ", ".join(p.name for p in profiles)) if profiles
                       else "Nenhum perfil usa esta pasta.", self._folder_status_text(config_dir),
                       f"Conta · {account}", f"Projetos · {summary.project_count}",
                       f"Conversas · {summary.conversation_count}",
                       "Login da pasta: a conta usada quando o perfil não escolhe uma conta Claude extra"
                       + ("." if self.termux else " (e sempre nos apps gráficos)."))
            key = self.t.choose([("e", "Entrar…"), ("0", "Voltar")], default="0")
            if key == "0":
                return
            self.folder_login(config_dir)

    def claude_account_detail(self, account_id) -> None:
        """ClaudePages.swift:222-272."""
        while True:
            config, _, state = self._load()
            account = config.claude_account(account_id)
            if account is None:
                self.t.say("Conta removida")
                return
            has_token = self.session._has_token(account)
            active = config.profile(state.profile_id)
            in_use = state.applied_claude_account_id == account.id
            if in_use:
                status = "Em uso"
            elif not has_token:
                status = "Sem token"
            else:
                status = "Login salvo" if account.kind == ClaudeAccountKind.LOGIN else "Token salvo"
            users = [p.name for p in config.profiles if p.claude_account_id == account.id]
            lines = ["", account.nickname, account.email or "e-mail não informado", status,
                     f"Padrão nos perfis: {', '.join(users)}." if users else "Nenhum perfil usa esta conta por padrão."]
            if active is not None:
                lines.append(f"\"Usar agora\" mantém o perfil {active.name} e a pasta "
                             f"{self._folder_name(active.claude_config_dir)} (agents, skills, conversas). Só o login "
                             "muda, até a próxima troca de perfil.")
            self.t.say(*lines)
            entries = []
            if active is not None and not in_use and has_token:
                entries.append(("u", "Usar agora"))
            entries += [("e", "Entrar de novo…"), ("r", "Remover conta…"), ("0", "Voltar")]
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            if key == "u":
                self.session.use_claude_account(account.id)
            elif key == "e":
                self.claude_add(account)
            elif key == "r":
                if self.remove_claude_account(account):
                    return

    def remove_claude_account(self, account) -> bool:
        """ClaudePages.swift:134-155 and ui/model.py remove_claude_account: (1) a login account
        live in a folder gives the folder its own login back, (2) the config is saved without it,
        (3) only then its login and token are deleted; in use → back to the folder's login."""
        from .core.claude import account_editor
        from .core.claude.login_vault import ClaudeLoginSwitcher

        config, engine, state = self._load()
        used = [p.name for p in account_editor.profiles_using(account.id, config)]
        self.t.say(f"Remover a conta {account.nickname}?",
                   (f"Usada em: {', '.join(used)}. Esses perfis voltam a usar o login da própria pasta. " if used else "")
                   + f"O token é apagado {self.out_of}. Nenhuma pasta ou conversa é tocada.")
        if not self.t.confirm("Remover?"):
            return False
        switcher = ClaudeLoginSwitcher(self.environment)
        try:
            with engine.exclusive():
                undo = account_editor.release_login(account, switcher)
                try:
                    self._save(account_editor.remove(account.id, config))
                except Exception:
                    undo()
                    raise
                account_editor.discard_removed_account(account, self.environment.secrets, switcher)
        except Exception as error:
            self.t.say(self.describe(error))
            return False
        self.t.say(f"Conta {account.nickname} removida")
        if state.applied_claude_account_id == account.id:
            self.session.use_claude_account(None, quiet_without_profile=True)
        return True

    def claude_add(self, existing):
        """"Adicionar conta Claude" / "Entrar de novo em X" (AddClaudeAccountPage.swift); the
        account saved, or None (cancelled or failed)."""
        from .core.claude.login_flow import NewClaudeAccount

        self.t.say("", f"Entrar de novo em {existing.nickname}" if existing is not None else "Adicionar conta Claude",
                   "Uma conta Claude é só o login. Usada num perfil, ela mantém os agents, skills e conversas da pasta "
                   "do perfil, e a troca vale até nas conversas já abertas.")
        if existing is None:
            nickname = ""
            while not nickname:
                nickname = self.t.ask("Apelido", None, "Marco").strip(" ")
            email = self.t.read("E-mail da conta (opcional): ").strip() or None
            color = self.t.color(ProfileColor.TEAL)
            target = NewClaudeAccount(nickname, email, color)
        else:
            target = existing
        # The Mac offers the choice only for a new account; here pasting a new token into an
        # existing token account is also offered (a terminal has `claude setup-token` at hand).
        if existing is None or existing.kind == ClaudeAccountKind.TOKEN:
            key = self.t.choose([("1", "Entrar pelo navegador"), ("2", "Já tenho um token"), ("0", "Cancelar")],
                                default="1")
        else:
            key = "1"
        if key == "0":
            return None
        account = self._claude_browser_login(target) if key == "1" else self._claude_token(target)
        if account is None:
            return None
        self.t.say(f"Login de {account.nickname} atualizado." if existing is not None
                   else f"Conta {account.nickname} adicionada.")
        if account.kind == ClaudeAccountKind.LOGIN:
            self.t.say(f"O login está só {self.located}. Use agora no perfil ativo: a pasta continua a mesma e até as "
                       "conversas já abertas passam para esta conta.")
        else:
            self.t.say(f"O token está só {self.located}. Vale para terminais novos; escolha esta conta no editor de um "
                       "perfil ou use agora no perfil ativo.")
        config, _, state = self._load()
        active = config.profile(state.profile_id)
        if active is not None and state.applied_claude_account_id != account.id \
                and self.t.confirm(f"Usar agora em {active.name}?"):
            self.session.use_claude_account(account.id)
        return account

    def _saver(self, engine):
        from .core.claude.login_flow import ClaudeAccountSaver
        from .core.storage import ConfigRepository

        repository = ConfigRepository(self.environment)
        return ClaudeAccountSaver(self.environment, repository.load, repository.save, exclusive=engine.exclusive)

    def _claude_token(self, target):
        self.t.say("Se você já rodou \"claude setup-token\" num terminal, cole o token (começa com sk-ant-oat). Um "
                   "token vale só para terminais novos: as conversas já abertas não trocam.")
        while True:
            token = self.t.read("Token (sk-ant-oat01-…): ", secret=True).strip()
            if not token:
                return None
            try:
                return self._saver(self._load()[1]).save_token(target, token)
            except Exception as error:  # validation or the secret store: nothing was added
                self.t.say(self.describe(error))

    def _claude_browser_login(self, target):
        """`claude auth login --claudeai` in a throwaway folder (login_flow.ClaudeLoginSession);
        the code is typed here and forwarded, never stored or echoed."""
        from .core.claude.login_flow import ClaudeLoginSession, LoginMode

        engine = self._load()[1]
        pump, finished = _Pump(), []
        session = ClaudeLoginSession(self.environment, self.tools, LoginMode.LOGIN, target, self._saver(engine),
                                     dispatch=pump, on_finished=finished.append, log=self.session.log)
        self.t.say("O Claude Code mostra um link. Abra-o no navegador em que você quer entrar (ou numa janela "
                   "anônima), clique em Autorizar e copie o código mostrado. Cole o código aqui e o MultiCred guarda o "
                   f"login {self.located}.")
        if session.simulated:
            self.t.say("Modo de teste: simulação; digite qualquer código.")
        self.t.say("1. Entre no navegador com a conta que você quer usar", "2. Clique em Autorizar e copie o código",
                   "3. Salvar o login")
        return self._drive_claude(session, pump, finished, lambda: session.result)

    def _drive_claude(self, session, pump, finished, result, failure_suffix=""):
        session.start()
        if session.command is not None:
            self.t.say(f"$ {session.command.display}")
        shown_url = False
        try:
            while not finished:
                pump.run()
                if session.authorization_url and not shown_url:
                    shown_url = True
                    self.t.say("Link:", session.authorization_url)
                if session.phase.kind == "waitingForCode" and not finished:
                    code = self.t.read("Código: ", secret=True)
                    pump.run(0)
                    if code.strip() and session.phase.accepts_code:
                        session.submit_code(code)
                        self.t.say("Concluindo o login…")
        except (Leave, KeyboardInterrupt):
            session.cancel()
            self.t.say("Cancelado.")
            return None
        phase = session.phase
        if phase.kind == "connected":
            return result()
        from .core.engine.types import SwitchError
        if isinstance(getattr(session, "save_error", None), SwitchError):
            self.t.say(f"Não consegui guardar o login: {self.session._describe(session.save_error)}")
        elif session.launch_error:
            self.t.say(self._launch_error(session.launch_error))
        elif "400" in (phase.message or ""):
            self.t.say("O Claude recusou o código. Ele vale uma vez e por poucos minutos, e só para o link aberto "
                       "nesta tentativa. Tente de novo.")
        else:
            self.t.say(f"Não deu certo ({phase.message})." + failure_suffix)
        return None

    def _launch_error(self, message: str | None) -> str | None:
        """Why a login could not start, in Termux's terms: Claude Code comes from npm pinned to
        the last version that runs on Android (not the desktop's curl installer), and the
        pseudo-terminal's setsid from util-linux."""
        if not message or not self.termux:
            return message
        from .core.claude.login_flow import CLAUDE_NOT_INSTALLED
        from .core.install.setup_detector import install_command
        from .core.tools import Tool

        platform = self.environment.platform
        if message == CLAUDE_NOT_INSTALLED:
            return f"Claude Code não foi encontrado. Instale com: {install_command(Tool.CLAUDE, platform)}"
        if f"{platform.setsid}:" in message:
            return f"{message}. Instale o setsid com: {platform.install_command('util-linux')}"
        return message

    def folder_login(self, config_dir) -> bool:
        """"Entrar no Claude" for a folder (LoginWindowView.swift): `claude auth login` with
        CLAUDE_CONFIG_DIR set to the folder itself; Claude Code keeps the login there. The
        same session as the window's folder page (login_flow.LoginMode.FOLDER): no throwaway
        folder, nothing stored by MultiCred, and in test mode the simulator inside the test HOME."""
        from .core.claude.login_flow import ClaudeLoginSession, LoginMode

        config = self._load()[0]
        name = self._folder_name(config_dir)
        profiles = self._folder_profiles(config, config_dir)
        pump, finished = _Pump(), []
        session = ClaudeLoginSession(self.environment, self.tools, LoginMode.FOLDER, config_dir, None,
                                     dispatch=pump, on_finished=finished.append, log=self.session.log)
        self.t.say("", f"Login da pasta {name}", "Perfis: " + (", ".join(p.name for p in profiles) or "nenhum"))
        if session.simulated:
            self.t.say("Simulação do modo de teste: o navegador não abre. Digite qualquer código para conectar, ou "
                       "\"erro\" para ver a tela de erro.")
        self.t.say("O navegador vai abrir. A conta em que você entrar lá vira o login desta pasta. Se o navegador já "
                   "estiver logado em outra conta Claude, saia dela antes ou abra o link numa janela anônima.")
        if self._drive_claude(session, pump, finished, lambda: True, FOLDER_LOGIN_FAILURE_SUFFIX) is None:
            return False
        self.session.forget_folder_status()
        self.t.say(f"Pronto. Os perfis que usam {name} entram com esta conta quando não escolhem outra conta Claude.",
                   "Credenciais guardadas pelo Claude Code. O MultiCred não as armazena.")
        return True

    # MARK: perfis (ProfileEditorPage.swift)

    def profiles(self) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        first = True
        try:
            while True:
                config, _, state = self._load()
                self.t.say(*(["Perfis"] if first else ["", "Perfis"]))
                first = False
                entries = [] if config.profiles else ["Nenhum perfil configurado."]
                entries += [(str(n), label)
                            for n, label in enumerate(self.session.profile_labels(config, state), start=1)]
                key = self.t.choose(entries + [("n", "Novo perfil"), ("0", "Sair")], default="0")
                if key == "0":
                    return 0
                self.profile_editor(None if key == "n" else config.profiles[int(key) - 1].id)
        except Leave:
            return 0

    def _folder_options(self, config, draft) -> list:
        """ProfileEditorPage.swift:186-193: the default folder, the discovered ones, the
        profiles' folders and the draft's, without repeats."""
        from .core import claude_paths
        from .core.claude import folder_inspector

        default = claude_paths.default_config_dir(self.environment)
        options = [None]
        discovered = folder_inspector.discover_folders(self.environment)
        for folder in discovered + [p.claude_config_dir for p in config.profiles] + [draft.claude_config_dir]:
            if folder not in options and folder != default:
                options.append(folder)
        return options

    def _folder_label(self, folder) -> str:
        from .core.claude import folder_inspector
        summary = folder_inspector.summarize(folder, self.environment)
        return (self._folder_name(folder) + (" (padrão)" if folder is None else "")
                + f" · {plural(summary.conversation_count, 'conversa', 'conversas')}")

    def _own_login_label(self, config_dir) -> str:
        status = self.session._folder_status(config_dir)
        if status is not None and status.kind == "loggedIn" and status.email:
            return f"Login da própria pasta · {status.email}"
        return "Login da própria pasta"

    @staticmethod
    def _providers(config) -> list:
        """OpenAI and Gemini always, plus providers that have keys (ProfileEditorPage.swift:216-220)."""
        return [p for p in APIProvider
                if p in (APIProvider.OPENAI, APIProvider.GEMINI) or any(k.provider == p for k in config.api_keys)]

    def profile_editor(self, profile_id) -> None:
        from .core.editors import ConfigEditor

        config, _, state = self._load()
        existing = config.profile(profile_id)
        if existing is None:
            draft = Profile(name="", color=list(ProfileColor)[len(config.profiles) % 8])
            self.t.say("", "Novo perfil")
            draft.name = self.t.ask("Nome", None, "Trabalho")
            draft.icon = ProfileIcon("letter", draft.name.strip()[:1].upper())
        else:
            draft = copy.deepcopy(existing)
        while True:
            config, _, state = self._load()
            is_new = config.profile(draft.id) is None
            providers = self._providers(config)
            status = self.session._folder_status(draft.claude_config_dir)
            logged_out = draft.claude_account_id is None and status is not None and status.kind == "loggedOut"
            git = config.git_identity(draft.git_identity_id)
            github = config.github_account(draft.github_account_id)
            claude = config.claude_account(draft.claude_account_id)
            icon = (f"letra {draft.name.strip()[:1].upper()}" if draft.icon is None or draft.icon.kind == "letter"
                    else SYMBOL_NAMES.get(draft.icon.value, draft.icon.value))
            header = ["", "Novo perfil" if is_new else f"Editar perfil · {draft.name}"]
            position = next((n for n, p in enumerate(config.profiles, start=1) if p.id == draft.id), None)
            if position is not None and position <= 9:
                header.append(f"No terminal: multicred trocar {position}" if self.termux
                              else f"No painel: Ctrl+{position} no menu")
            self.t.say(*header)
            entries = [("1", f"Nome · {draft.name or '—'}"), ("2", f"Cor · {COLOR_NAMES[draft.color]}"),
                       ("3", f"Ícone · {icon}"), "Composição",
                       ("4", "Git · " + (f"{git.nickname} · {git.email}" if git else "Nenhuma")),
                       ("5", "GitHub · " + (f"{github.username} · {github.host}" if github else "Nenhuma")),
                       ("6", f"Pasta do Claude · {self._folder_label(draft.claude_config_dir)}"),
                       ("7", "Conta Claude · " + (claude.nickname + (f" · {claude.email}" if claude.email else "")
                                                  if claude else self._own_login_label(draft.claude_config_dir)))]
            for n, provider in enumerate(providers, start=8):
                key = next((config.api_key(k) for k in draft.api_key_ids
                            if config.api_key(k) is not None and config.api_key(k).provider == provider), None)
                entries.append((str(n), f"{provider.display_name} · "
                                + (f"{key.nickname} · {key.env_var}" if key else "Nenhuma")))
            entries.append("A pasta guarda agents, skills e conversas. A conta é só o login: trocar a conta não muda "
                           "a pasta.")
            if logged_out:
                entries += [f"⚠ A pasta {self._folder_name(draft.claude_config_dir)} não tem login.", ("e", "Entrar…")]
            entries.append(("s", "Salvar"))
            if not is_new and state.profile_id != draft.id:
                entries.append(("a", "Ativar este perfil"))
            if not is_new:
                entries.append(("x", "Apagar perfil…"))
            entries += [("0", "Cancelar" if is_new else "Voltar"),
                        "“Nenhuma” deixa aquele item como está ao trocar para este perfil (o Claude sempre usa a pasta "
                        f"do perfil). Chaves de API ficam {self.located}; o perfil guarda só qual item usar."]
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            if key == "1":
                draft.name = self.t.ask("Nome", draft.name, "Trabalho")
            elif key == "2":
                draft.color = self.t.color(draft.color)
            elif key == "3":
                letter = draft.name.strip()[:1].upper()
                current = "letter" if draft.icon is None or draft.icon.kind == "letter" else draft.icon.value
                chosen = self.t.pick("Ícone", [("letter", f"Letra ({letter})")]
                                     + [(s, SYMBOL_NAMES[s]) for s in SYMBOLS], current)
                draft.icon = ProfileIcon("letter", letter) if chosen == "letter" else ProfileIcon("symbol", chosen)
            elif key == "4":
                draft.git_identity_id = self.t.pick("Git", [(None, "Nenhuma")] + [
                    (i.id, f"{i.nickname} · {i.email}") for i in config.git_identities], draft.git_identity_id)
            elif key == "5":
                draft.github_account_id = self.t.pick("GitHub", [(None, "Nenhuma")] + [
                    (a.id, f"{a.username} · {a.host}") for a in config.github_accounts], draft.github_account_id)
            elif key == "6":
                self._choose_folder(config, draft)
            elif key == "7":
                draft.claude_account_id = self.t.pick("Conta Claude", [
                    (None, self._own_login_label(draft.claude_config_dir))] + [
                    (a.id, a.nickname + (f" · {a.email}" if a.email else "")) for a in config.claude_accounts],
                    draft.claude_account_id)
            elif (provider_index := choice_index(key, len(providers), first=8)) is not None:
                provider = providers[provider_index]
                keys = [k for k in config.api_keys if k.provider == provider]
                current = next((k for k in draft.api_key_ids if k in [x.id for x in keys]), None)
                chosen = self.t.pick(provider.display_name, [(None, "Nenhuma")] + [
                    (k.id, f"{k.nickname} · {k.env_var}") for k in keys], current)
                draft = ConfigEditor.set_api_key(chosen, provider, draft, config)
            elif key == "e" and logged_out:
                self.folder_login(draft.claude_config_dir)
            elif key == "s":
                self._save_profile(draft)
            elif key == "a" and not is_new:
                self.session.switch_to(draft.id)
            elif key == "x" and not is_new:
                self.t.say(f"Apagar o perfil {draft.name}?", "As contas continuam cadastradas. Se ele estiver ativo, o "
                           "sistema fica como está até você escolher outro perfil.")
                if self.t.confirm("Apagar perfil?") and self._edit(
                        lambda c: ConfigEditor.remove_profile(draft.id, c)):
                    self.t.say(f"Perfil {draft.name} apagado")
                    return

    def _choose_folder(self, config, draft) -> None:
        from .core.claude.account_editor import ClaudeFolderChoice, prepare_folder

        new = object()
        options = self._folder_options(config, draft)
        chosen = self.t.pick("Pasta do Claude", [(f, self._folder_label(f)) for f in options] + [(new, "Nova pasta…")],
                             draft.claude_config_dir)
        if chosen is not new:
            draft.claude_config_dir = chosen
            return
        while True:
            path = self.t.ask("Caminho", f"~/.claude-{slug(draft.name)}")
            try:
                draft.claude_config_dir = prepare_folder(ClaudeFolderChoice.new(path), self.environment)
                return
            except Exception as error:
                self.t.say(self.describe(error))
                if not self.t.confirm("Tentar outro caminho?", True):
                    return

    def _save_profile(self, draft) -> None:
        """"Salvar" (:105-116): stays in the editor; the letter follows the name; the active
        profile is applied again so the machine matches."""
        from .core.editors import ConfigEditor

        draft.name = draft.name.strip(" \t")
        if draft.icon is None or draft.icon.kind == "letter":
            draft.icon = ProfileIcon("letter", draft.name[:1].upper())
        config, _, state = self._load()
        try:
            self._save(ConfigEditor.upsert(draft, config))
        except Exception as error:
            from .core.editors import EditorError
            self.t.say(self._issues(error.issues, True) if isinstance(error, EditorError) and error.kind == "invalid"
                       else self.describe(error))
            return
        self.t.say(f"Perfil {draft.name} salvo")
        self._reapply_if_active(config, state, draft.id)

    # MARK: entrar

    def sign_in(self, kind: str, target: str | None) -> int:
        """`multicred entrar claude <conta>` / `pasta [caminho]` / `github [servidor]`."""
        loaded = self._load()
        if loaded is None:
            return 1
        config, _, state = loaded
        try:
            if kind == "claude":
                from .cli import resolve_claude_account, _UNRESOLVED
                account = resolve_claude_account(config, target or "") if target else _UNRESOLVED
                if account is _UNRESOLVED or account is None:
                    self.session._say(f"Nenhuma conta Claude chamada “{target or ''}”. Use o apelido ou o número:",
                                      *(f"  {n}  {a.nickname}" for n, a in enumerate(config.claude_accounts, start=1)),
                                      error=True)
                    return 1
                return 0 if self.claude_add(account) is not None else 1
            if kind == "pasta":
                folder = self._folder_argument(config, state, target)
                if folder is False:
                    return 1
                return 0 if self.folder_login(folder) else 1
            return 0 if self.github_login(target or "github.com") else 1
        except Leave:
            return 1

    def _folder_argument(self, config, state, text):
        """The folder of `entrar pasta`: the given path, else the active profile's (~/.claude without one)."""
        from .core import claude_paths

        if not text:
            profile = config.profile(state.profile_id)
            return profile.claude_config_dir if profile else None
        try:
            return claude_paths.canonical_config_dir(text, self.environment)
        except claude_paths.PathError:
            self.session._say(f"Caminho inválido: {text}. Use um caminho absoluto ou começando com ~/.", error=True)
            return False

    # MARK: configurar (OnboardingView.swift)

    def configure(self) -> int:
        try:
            return self._configure()
        except (Leave, KeyboardInterrupt):
            self.t.say("Nada foi alterado.")
            return 1

    def _install_command(self, tool) -> str | None:
        """Shown, never run. On Termux, Claude Code's commands come with the guidance lines below
        (termux_support), so none here."""
        from .core.install.setup_detector import install_command
        from .core.tools import Tool

        if self.termux and tool == Tool.CLAUDE:
            return None
        return install_command(tool, self.environment.platform)

    def _claude_guidance(self, folder) -> list[str]:
        """R-12 (core/claude/termux_support.py): Claude Code on Android, what was found and what
        to do, when it is missing or needs attention (broken, or an updater that will break it)."""
        if not self.termux:
            return []
        from .core.claude import termux_support
        try:
            diagnosis = termux_support.diagnose(self.environment, self.tools, folder)
        except Exception as error:  # a diagnosis is advice; never stop the setup for it
            self.session.log.log("warning", f"Claude Code diagnosis failed: {error}")
            return []
        if not (diagnosis.needs_attention or diagnosis.state == termux_support.ClaudeCodeState.MISSING):
            return []
        return [termux_support.summary(diagnosis), *termux_support.guidance_lines(diagnosis)]

    def _configure(self) -> int:
        from .core.install.setup_detector import DetectedSetup, SetupDetector, default_shells, suggested_config
        from .core.storage import ConfigRepository, JSONStoreError
        from .core.tools import Tool

        try:
            existing = ConfigRepository(self.environment).load()
        except JSONStoreError as error:
            self.session._say(f"Não consegui ler a configuração: {error}", error=True)
            return 1
        if existing is not None and not self.t.confirm("O MultiCred já está configurado. Configurar de novo?"):
            self.t.say("Nada foi alterado.")
            return 1

        # Step 1: Preparar
        self.t.say("Bem-vindo ao MultiCred",
                   ("Um comando (multicred trocar) troca" if self.termux else "Um clique na bandeja troca")
                   + ", juntos, a identidade do git, a conta do GitHub CLI, a conta do Claude Code e as chaves de API "
                   "dos terminais.",
                   "  • Nunca vê senhas ou tokens. O login é feito pelas próprias ferramentas.",
                   "  • Altera só um bloco marcado no fim de cada arquivo de shell e do gitconfig, com backup.",
                   "  • Pode desinstalar e restaurar tudo a qualquer momento.", "",
                   "Procurando ferramentas e contas nesta máquina…")
        try:
            setup = SetupDetector(self.environment, self.tools).detect()
        except Exception as error:
            self.t.say(f"Não consegui verificar tudo: {error}")
            setup = DetectedSetup()
        lines = ["", "Ferramentas nesta máquina"]
        for info in setup.tools:
            if info.path is not None:
                lines.append(f"  {info.tool.display_name} · Instalado · {info.version or info.path}")
            else:
                command = self._install_command(info.tool)
                lines.append(f"  {info.tool.display_name} · Faltando" + (f" · instale com: {command}" if command else ""))
        active_id = self._active_profile_id() if existing is not None else None
        active = existing.profile(active_id) if existing is not None else None
        lines += [f"  {line}" for line in self._claude_guidance(active.claude_config_dir if active else None)]
        lines.append("Dá para continuar sem o Claude Code e adicioná-lo depois.")
        self.t.say(*lines)
        if not self.t.confirm("Continuar?", True):
            self.t.say("Nada foi alterado.")
            return 1

        if existing is not None:
            draft = copy.deepcopy(existing)
            initial = active_id if draft.profile(active_id) else None
            shells = [s for s in draft.settings.enabled_shells]
        else:
            draft, initial = suggested_config(setup, self.environment)
            shells = default_shells(setup, self.environment)

        # Step 2: Perfis
        if not self._suggested_profiles(setup, draft):
            self.t.say("Nada foi alterado.")
            return 1
        if initial is None or draft.profile(initial) is None:
            initial = draft.profiles[0].id if draft.profiles else None

        # Step 3: Aplicar
        return self._apply_step(setup, draft, initial, shells, existing)

    def _active_profile_id(self):
        """The active profile, or None when state.json cannot be read (configurar goes on)."""
        try:
            return self._engine().current_state().profile_id
        except Exception:
            return None

    def _engine(self):
        from .core.engine.switch_engine import SwitchEngine
        return SwitchEngine(self.environment, self.tools, self.session.log)

    def _card_lines(self, setup, draft, profile) -> list[str]:
        git = draft.git_identity(profile.git_identity_id)
        github = draft.github_account(profile.github_account_id)
        missing = [profile.git_identity_id is None, profile.github_account_id is None].count(True)
        return [f"{profile.name or '—'} · " + ("Completo" if missing == 0 else f"Falta {missing} item"),
                "     git           " + (f"{git.name} · {git.email}" if git else "—"),
                "     GitHub        " + (github.username if github else "Não trocar"),
                f"     Pasta Claude  {self._onboarding_folder_label(setup, profile.claude_config_dir)}"]

    @staticmethod
    def _onboarding_folder_label(setup, config_dir) -> str:
        """".claude-pessoal · eu@x.com" / "· entrar depois" (OnboardingView.swift:368-375)."""
        name = config_dir.rsplit("/", 1)[-1] if config_dir else ".claude"
        status = next((f.status for f in setup.claude_folders if f.config_dir == config_dir), None)
        if status is not None and status.is_logged_in and status.email:
            return f"{name} · {status.email}"
        if status is not None and status.kind == "loggedOut":
            return f"{name} · entrar depois"
        return name

    def _suggested_profiles(self, setup, draft) -> bool:
        from .core.validation import issues_in

        while True:
            self.t.say("", "Perfis sugeridos",
                       "Montados a partir do que já existe nesta máquina: "
                       f"{plural(1 if setup.git_email else 0, 'identidade git', 'identidades git')}, "
                       f"{plural(len(setup.github_logins), 'conta GitHub', 'contas GitHub')} e "
                       f"{plural(len(setup.claude_folders), 'pasta do Claude', 'pastas do Claude')}. "
                       "Dá para ajustar tudo depois.")
            if setup.has_legacy_install:
                self.t.say("Encontramos a versão antiga em ~/Perfis. Os perfis abaixo vieram do perfis.conf, e no "
                           "próximo passo você pode remover as linhas antigas do seu ~/.bashrc e ~/.gitconfig.")
            entries = []
            for n, profile in enumerate(draft.profiles, start=1):
                first, *rest = self._card_lines(setup, draft, profile)
                entries += [(str(n), first), *rest]
            entries += [("a", "Adicionar perfil"), ("c", "Continuar"), ("0", "Cancelar")]
            key = self.t.choose(entries, default="c")
            if key == "0":
                return False
            if key == "a":
                colors = list(ProfileColor)
                draft.profiles.append(Profile(name="Novo perfil", color=colors[len(draft.profiles) % len(colors)]))
            elif key == "c":
                self._normalize(draft)
                issues = issues_in(draft)
                if not draft.profiles:
                    self.t.say("Adicione um perfil para continuar.")
                elif issues:
                    self.t.say(self._onboarding_issue(issues, draft))
                else:
                    return True
            else:
                self._edit_card(setup, draft, draft.profiles[int(key) - 1])

    @staticmethod
    def _onboarding_issue(issues, config) -> str:
        """ui/pages/onboarding.py describe_issues."""
        issue = issues[0]
        owner = config.profile(issue.owner) or next(
            (p for p in config.profiles if p.git_identity_id == issue.owner), None)
        name = f" do perfil {owner.name}" if owner and owner.name.strip() else ""
        if issue.kind == "emptyField" and config.profile(issue.owner):
            return "Dê um nome ao perfil."
        if issue.kind == "emptyField" and issue.detail == "name":
            return f"Preencha o nome no git{name}."
        if issue.kind in ("emptyField", "invalidEmail"):
            return f"O e-mail do git{name} não parece válido."
        if issue.kind == "controlCharacters":
            return f"Há caracteres de controle em um dos campos{name}."
        return f"A configuração tem um problema ({issue.kind}){name}."

    @staticmethod
    def _normalize(draft) -> None:
        """Drops git identities left empty (the profile then does not switch git); an identity
        without a nickname takes the profile's name (ui/pages/onboarding.py _normalized_draft)."""
        for profile in draft.profiles:
            identity = draft.git_identity(profile.git_identity_id)
            if identity is None:
                continue
            if not identity.name and not identity.email:
                draft.git_identities = [i for i in draft.git_identities if i.id != identity.id]
                profile.git_identity_id = None
            elif not identity.nickname.strip():
                identity.nickname = profile.name.strip() or "Git"

    def _edit_card(self, setup, draft, profile) -> None:
        """One suggested profile: name, git name and e-mail, GitHub, Claude folder (ProfileSuggestionCard)."""
        def identity():
            found = draft.git_identity(profile.git_identity_id)
            if found is None:
                found = GitIdentity(nickname=profile.name, name="", email="", color=profile.color)
                draft.git_identities.append(found)
                profile.git_identity_id = found.id
            return found

        while True:
            git = draft.git_identity(profile.git_identity_id)
            github = draft.github_account(profile.github_account_id)
            self.t.say("", profile.name or "—")
            entries = [("1", f"Nome do perfil · {profile.name or '—'}"),
                       ("2", f"Nome no git · {git.name if git and git.name else '—'}"),
                       ("3", f"E-mail no git · {git.email if git and git.email else '—'}"),
                       ("4", f"GitHub · {github.username if github else 'Não trocar'}"),
                       ("5", f"Pasta Claude · {self._onboarding_folder_label(setup, profile.claude_config_dir)}")]
            if len(draft.profiles) > 1:
                entries.append(("x", "Remover perfil"))
            entries.append(("0", "Voltar"))
            key = self.t.choose(entries, default="0")
            if key == "0":
                return
            if key == "1":
                profile.name = self.t.ask("Nome do perfil", profile.name)
                profile.icon = ProfileIcon("letter", profile.name.strip()[:1].upper())
            elif key == "2":
                found = identity()
                found.name = self.t.ask("Nome no git", found.name or None, "Seu Nome")
            elif key == "3":
                found = identity()
                found.email = self.t.ask("E-mail no git", found.email or None, "voce@empresa.com")
            elif key == "4":
                profile.github_account_id = self.t.pick("GitHub", [(None, "Não trocar")] + [
                    (a.id, a.username) for a in draft.github_accounts], profile.github_account_id)
            elif key == "5":
                options = [None]
                for folder in [f.config_dir for f in setup.claude_folders] + [profile.claude_config_dir]:
                    if folder not in options:
                        options.append(folder)
                profile.claude_config_dir = self.t.pick("Pasta Claude", [
                    (f, self._onboarding_folder_label(setup, f)) for f in options], profile.claude_config_dir)
            elif key == "x":
                draft.profiles = [p for p in draft.profiles if p.id != profile.id]
                return

    def _apply_step(self, setup, draft, initial, shells, existing) -> int:
        from .core.install.installer import InstallError, InstallOptions, Installer, Integrations, MarkerMismatch
        from .core.validation import issues_in

        environment = self.environment
        self.t.say("", "Revise antes de aplicar", "Estas são as únicas mudanças fora da pasta ~/.config/multicred.",
                   "", "Shells que vão carregar o MultiCred")
        installed = [s for s in SHELL_ORDER if s in setup.installed_shells] or [ShellKind.BASH]
        for shell in installed:
            files = (Integrations.rc_files(shell, environment) if shell != ShellKind.FISH
                     else [Integrations.fish_conf_file(environment)])
            mark = " (seu shell)" if shell == setup.login_shell else ""
            self.t.say(f"  {shell.value}{mark} · " + ", ".join(environment.abbreviate(str(f)) for f in files))
        chosen = [s for s in shells if s in installed] or installed[:1]
        while True:
            answer = self.t.ask("Shells", " ".join(s.value for s in chosen))
            names = answer.replace(",", " ").split()
            picked = [s for s in installed if s.value in names]
            if picked and len(picked) == len(set(names)):
                chosen = picked
                break
            self.t.say("Escolha entre: " + ", ".join(s.value for s in installed) + ".")
        remove_legacy = False
        if setup.legacy_findings:
            remove_legacy = self.t.confirm("Remover as linhas da versão antiga (~/Perfis), que brigariam com o "
                                           "MultiCred?", True)
        options = InstallOptions(shells=[s for s in ShellKind if s in chosen], remove_legacy=remove_legacy)
        try:
            changes = Installer(environment).preview(options)
        except InstallError as error:
            self.t.say(self._install_error(error, MarkerMismatch), "Nada foi alterado.")
            return 1
        self.t.say("")
        if not changes:
            self.t.say("Nada a mudar: tudo já está instalado.")
        kinds = {"create": "criar", "modify": "modificar", "delete": "apagar"}
        for change in changes:
            summary = kinds[change.kind.value]
            if change.added_lines:
                summary += f" · +{plural(len(change.added_lines), 'linha', 'linhas')}"
            if change.removed_lines:
                summary += f" · −{plural(len(change.removed_lines), 'linha', 'linhas')}"
            self.t.say(f"{change.path} · {summary}",
                       *(f"  − {line.expandtabs(4)}" for line in change.removed_lines),
                       *(f"  + {line.expandtabs(4)}" for line in change.added_lines))
        self.t.say("", "Backup dos originais em ~/.config/multicred/backups/")
        if len(draft.profiles) > 1:
            self.t.say("Perfil inicial")
            initial = self.t.pick("Perfil inicial", [(p.id, p.name) for p in draft.profiles], initial)
        elif draft.profiles:
            self.t.say(f"Perfil inicial: {draft.profiles[0].name}")
        autostart = make_autostart(environment)
        open_at_login = False
        if environment.platform.has_graphical_session and autostart.is_available():
            open_at_login = self.t.confirm("Abrir ao iniciar a sessão?", True)
        self.t.say("Abra um novo terminal depois de aplicar para que o shell carregue o perfil."
                   + ("" if self.termux else " Apps gráficos pegam a pasta do Claude no próximo login da sessão."))
        if not self.t.confirm("Aplicar e começar?", True):
            self.t.say("Nada foi alterado.")
            return 1

        draft.settings.enabled_shells = list(options.shells)
        draft.settings.open_at_login = open_at_login
        issues = issues_in(draft)
        if issues:
            self.t.say(self._onboarding_issue(issues, draft), "Nada foi alterado.")
            return 1
        try:
            # The installer runs before the save (ui/model.py complete_onboarding): if it
            # fails nothing is configured and `configurar` can simply run again.
            Installer(environment).apply(options)
            self._save(draft)
        except InstallError as error:
            self.t.say(self._install_error(error, MarkerMismatch))
            return 1
        except Exception as error:
            self.t.say(self.describe(error))
            return 1
        if open_at_login:
            try:
                autostart.enable()
            except OSError as error:
                self.session.log.log("error", f"autostart not written: {error}")
        elif existing is not None and environment.platform.has_graphical_session:
            try:
                autostart.disable()
            except OSError:
                pass
        code = 0
        if initial is not None and draft.profile(initial) is not None:
            code = self.session.switch_to(initial)
        self.t.say("", "Pronto. Para trocar de perfil: multicred trocar <perfil>. Contas e perfis: multicred contas, "
                   "multicred perfis.")
        return code

    @staticmethod
    def _install_error(error, MarkerMismatch) -> str:
        if isinstance(error, MarkerMismatch):
            return (f"O bloco do MultiCred em {error.path} foi editado à mão (falta o marcador de fim). Corrija o "
                    "arquivo e tente de novo.")
        return f"Não consegui alterar {error.path}: {error.reason}. Nada foi mudado."


def _join(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " e " + items[-1]
