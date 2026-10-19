"""`multicred <comando>`: switch and inspect profiles from a terminal (R-09).

The only interface on Termux (Android has no GTK) and the "CLI companheira" on the
desktop. Nothing here imports GTK or PyGObject, directly or through what it uses
(ui/messages.py and ui/feedback.py are plain Python): Termux has no PyGObject.
Messages are the app's (UserMessages / SwitchFeedbackPresenter on the Mac); the
listings are the Mac's menu as text (AppDelegate.swift: "Perfis" and "Trocar só a
conta Claude") and its Verificar page (VerifyPage.swift).

    trocar <perfil|1-9>       the menu's profile items
    status                    the menu header
    perfis [--menu]           the menu's "Perfis" section; in a terminal (or --menu) the
                              profile list and editor (cli_menus.py, S-11)
    claude [conta|N|pasta]    the "Trocar só a conta Claude" submenu (no argument: list it)
    verificar [--corrigir]    the Verificar page; "Corrigir tudo" asks before running
    configurar                the onboarding (cli_menus.py)
    contas [--menu]           the Contas pages as text; in a terminal (or --menu) their menus
    entrar claude|pasta|github  the "Entrar…" buttons (a Claude account again, a folder's own
                              login, gh auth login)
    desinstalar               Ajustes › "Desinstalar e restaurar arquivos originais" (the uninstall
                              page: the diff of each file, then "Desinstalar?")

`perfis` and `contas` print a listing when the input is not a terminal (scripts, pipes), so
their output stays stable; `--menu` asks for the menus anyway (scripted input).

Exit codes: 0 done (verificar: everything de acordo), 1 failure or problems left,
2 bad usage (argparse), 130 interrupted (Ctrl+C) in a menu.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import VERSION
from .cli_text import choice_index

NOT_CONFIGURED = "MultiCred ainda não foi configurado."
# The Mac's marks: ✓ on the active item, the warning subtitle of a profile item.
ACTIVE_MARK = "✓"
NEEDS_CLAUDE_LOGIN = "⚠︎ Claude precisa entrar"
# The keyword of `multicred claude` for the profile folder's own login.
FOLDER_LOGIN_KEYWORD = "pasta"
_YES = {"s", "sim", "y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multicred", description="Troca de identidade de dev com um clique.",
        epilog="Sem comando, abre a janela do MultiCred (no Termux, mostra esta ajuda).")
    parser.add_argument("--test-mode", action="store_true",
                        help="roda num HOME de teste; nada do sistema real muda")
    parser.add_argument("--fresh", action="store_true",
                        help="modo de teste sem perfis de exemplo, para passar pela configuração inicial "
                             "(sem --sandbox, usa uma pasta nova a cada vez)")
    parser.add_argument("--sandbox", type=Path, default=None, help="pasta do HOME de teste (com --test-mode)")
    parser.add_argument("--background", action="store_true", help="inicia só na bandeja (início da sessão)")
    parser.add_argument("--version", action="version", version=f"MultiCred {VERSION}")
    commands = parser.add_subparsers(dest="command", title="comandos", metavar="<comando>")
    switch = commands.add_parser("trocar", help="troca para um perfil (nome, ou número 1–9 como no Ctrl+N)",
                                 description="Troca git, GitHub, Claude e chaves para o perfil.")
    switch.add_argument("perfil", help="nome do perfil (maiúsculas não importam) ou número 1–9")
    commands.add_parser("status", help="mostra o perfil ativo e o que ele usa",
                        description="Mostra o perfil ativo e o que ele usa.")
    profiles = commands.add_parser(
        "perfis", help="lista os perfis (✓ no ativo); num terminal, cria, edita e apaga perfis",
        description="Lista os perfis, como o menu da bandeja. Num terminal, abre o menu para criar, editar, "
                    "ativar e apagar perfis.")
    profiles.add_argument("--menu", action="store_true", help="abre o menu mesmo sem terminal (entrada por script)")
    claude = commands.add_parser(
        "claude", help="troca só a conta Claude (sem conta, lista as contas)",
        description="Trocar só a conta Claude: a pasta do perfil ativo fica; git, GitHub e chaves também. "
                    "Agents, skills e conversas não mudam.")
    claude.add_argument("conta", nargs="?",
                        help="apelido da conta (maiúsculas não importam), o número da lista, "
                             f"ou “{FOLDER_LOGIN_KEYWORD}” para o login da própria pasta")
    verify = commands.add_parser("verificar", help="compara o que o perfil ativo espera com o que está na máquina",
                                 description="Compara o que o perfil ativo espera com o que git, gh, os shells "
                                             "e os segredos têm agora.")
    verify.add_argument("--corrigir", action="store_true",
                        help="corrige tudo o que dá para corrigir (mostra os comandos e pergunta antes)")
    commands.add_parser("configurar", help="configuração inicial: detecta, sugere perfis, mostra e instala",
                        description="Procura ferramentas e contas, sugere perfis, mostra o que muda no ~/.bashrc e "
                                    "no gitconfig e instala.")
    accounts = commands.add_parser(
        "contas", help="lista as contas; num terminal, cadastra, edita e remove git, GitHub, Claude e chaves",
        description="Contas › Git, GitHub, Claude e Chaves de API.")
    accounts.add_argument("--menu", action="store_true", help="abre o menu mesmo sem terminal (entrada por script)")
    sign_in = commands.add_parser(
        "entrar", help="entra de novo numa conta Claude, no login de uma pasta do Claude ou no GitHub",
        description="claude <conta>: entrar de novo na conta (claude auth login, ou um token colado); "
                    "pasta [caminho]: o login da própria pasta (a do perfil ativo sem caminho); "
                    "github [servidor]: gh auth login.")
    sign_in.add_argument("tipo", choices=["claude", "pasta", "github"])
    sign_in.add_argument("alvo", nargs="?", help="a conta Claude, a pasta ou o servidor do GitHub")
    commands.add_parser(
        "desinstalar", help="mostra o que volta ao original e, se você confirmar, desinstala",
        description="Desinstalar e restaurar arquivos originais: remove só o bloco do MultiCred de cada arquivo, "
                    "os segredos guardados e ~/.config/multicred (os backups ficam). Pergunta antes.")
    return parser


def parse_args(argv) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if args.fresh:
        args.test_mode = True
    return args


def build_environment(args, platform=None):
    """The real system, or the test-mode sandbox (seeded with the demo profiles unless --fresh).
    `platform` None = detected from the real environment. On Termux the sandbox lives under
    $TMPDIR (never /tmp, which Android does not have) and keeps the Termux platform, so the
    simulated logins run $PREFIX's programs."""
    import dataclasses

    from .core import real_env
    from .core.storage import ConfigRepository

    if not args.test_mode:
        return real_env.make_real()
    platform = platform or real_env.detect_platform()
    sandbox = args.sandbox or real_env.default_sandbox(platform)
    if args.fresh and args.sandbox is None:
        # A new empty home every time: the default sandbox may hold an earlier setup.
        sandbox = Path(tempfile.mkdtemp(prefix="multicred-fresh-", dir=platform.temp_directory))
        print(f"MultiCred: HOME de teste vazio em {sandbox}", file=sys.stderr)
    environment = real_env.make_sandbox(sandbox)
    if platform.is_termux:
        environment = dataclasses.replace(environment, platform=platform)
    repository = ConfigRepository(environment)
    if not args.fresh and repository.load() is None:
        repository.save(real_env.seed_sandbox(environment))
    return environment


def main(argv=None, environment=None, tools=None, out=None, err=None, inp=None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv), environment, tools, out, err, inp)


def run(args, environment=None, tools=None, out=None, err=None, inp=None) -> int:
    """Runs `args.command`; exit code 0 on success, 1 on failure. `inp` answers the
    "Executar?" question of `verificar --corrigir` (stdin by default)."""
    out = out or sys.stdout
    err = err or sys.stderr
    if args.command is None:
        build_parser().print_help(out)
        return 0
    from .core.log import EventLog
    from .core.tools import ToolLocator

    environment = environment or build_environment(args)
    tools = tools or ToolLocator(environment)  # the platform's directories ($PREFIX/bin on Termux)
    log = EventLog(environment.logs_directory, environment.clock)
    inp = inp or sys.stdin
    session = _Session(environment, tools, log, out, err, inp)
    if args.command in ("configurar", "contas", "perfis", "entrar"):
        try:
            interactive = getattr(args, "menu", False) or bool(inp.isatty())
        except (AttributeError, ValueError):
            interactive = bool(getattr(args, "menu", False))
        if args.command == "perfis" and not interactive:
            return session.profiles()
        from .cli_menus import Menus, Terminal

        menus = Menus(session, Terminal(out, inp, interactive))
        try:
            return {
                "configurar": menus.configure,
                "contas": lambda: menus.accounts(interactive),
                "perfis": menus.profiles,
                "entrar": lambda: menus.sign_in(args.tipo, args.alvo),
            }[args.command]()
        except KeyboardInterrupt:
            print(file=out)
            return 130
    return {
        "trocar": lambda: session.switch(args.perfil),
        "status": session.status,
        "perfis": session.profiles,
        "claude": lambda: session.claude(args.conta),
        "verificar": lambda: session.verify(args.corrigir),
        "desinstalar": session.uninstall,
    }[args.command]()


def resolve_profile(config, text: str):
    """By name, ignoring case, or by position 1–9 (the Ctrl+N of the menu). None when neither."""
    wanted = text.strip().casefold()
    for profile in config.profiles:
        if profile.name.casefold() == wanted:
            return profile
    index = choice_index(wanted, min(9, len(config.profiles))) if len(wanted) == 1 else None
    return config.profiles[index] if index is not None else None


_UNRESOLVED = object()


def resolve_claude_account(config, text: str):
    """"pasta" = the folder's own login (None); else an account by nickname, ignoring
    case, or by its number in the `multicred claude` list. `_UNRESOLVED` when neither."""
    wanted = text.strip().casefold()
    if wanted == FOLDER_LOGIN_KEYWORD:
        return None
    for account in config.claude_accounts:
        if account.nickname.casefold() == wanted:
            return account
    index = choice_index(wanted, len(config.claude_accounts))
    return config.claude_accounts[index] if index is not None else _UNRESOLVED


def folder_name(environment, config_dir: str | None) -> str:
    return environment.abbreviate(config_dir) if config_dir is not None else "~/.claude"


def status_lines(config, state, environment, tz=None) -> list[str]:
    """The tray header as text: profile and "Ativo desde H:MM", git, GitHub,
    "Claude · pasta · credencial" (the Mac's MenuHeaderView), plus the profile's keys."""
    profile = config.profile(state.profile_id)
    if profile is None:
        return ["Nenhum perfil ativo", "Escolha um perfil com: multicred trocar <perfil>"]
    title = profile.name
    if state.activated_at is not None:
        title += f" · Ativo desde {state.activated_at.astimezone(tz).strftime('%H:%M')}"
    lines = [title]
    git = config.git_identity(profile.git_identity_id)
    if git is not None:
        lines.append(f"git {git.email}")
    github = config.github_account(profile.github_account_id)
    if github is not None:
        lines.append(f"GitHub {github.username}")
    folder = folder_name(environment, profile.claude_config_dir)
    applied = config.claude_account(state.applied_claude_account_id)
    claude = f"Claude · {folder} · {applied.nickname if applied else 'login da pasta'}"
    if state.claude_override_id is not None:
        claude += " (só Claude)"
    lines.append(claude)
    keys = [key for key in (config.api_key(k) for k in profile.api_key_ids) if key is not None]
    lines.append("Chaves: " + (", ".join(key.env_var for key in keys) if keys else "nenhuma"))
    return lines


# MARK: Verificar as text (VerifyPage.swift)

_SECTION_TITLES = {"tools": "Ferramentas", "integration": "Integração", "sessions": "Sessões"}
# The row's mark (VerifyPage.swift StatusIcon24): ✓ de acordo, ✗ problema, ! atenção, i informação.
_GLYPHS = {"ok": "✓", "info": "i", "warning": "!"}


def _value(item):
    return getattr(item, "value", item)


def _glyph(check) -> str:
    return _GLYPHS.get(_value(check.status), "✗")


def _needs_attention(check) -> bool:
    return _value(check.status) not in ("ok", "info")


def runnable_fixes(report) -> list:
    """What "Corrigir tudo" runs: `report.fixable` (no login or key page), one fix per key
    (several checks may share one repair). The window's page does the same."""
    fixes = []
    for check in report.fixable:
        if check.fix.key not in {f.key for f in fixes}:
            fixes.append(check.fix)
    return fixes


def check_lines(check, terminal_command=None) -> list[str]:
    """`terminal_command(fix)`: the `multicred …` that does what a fix opening a page does
    (the login, the key's editor), shown under it; None leaves it out."""
    lines = [f"{_glyph(check)} {check.title}"]
    if check.expected is not None:
        lines.append(f"  Esperado {check.expected}, encontrado {check.found or 'nada'}")
    elif check.found:
        lines.append(f"  {check.found}")
    if check.origin:
        lines.append(f"  definido em {check.origin}")
    if check.hint:
        # A hint may have several lines (Termux's Claude Code guidance): all stay under the row.
        lines += [f"  {line}" for line in check.hint.splitlines()]
    if check.fix is not None and _needs_attention(check):
        # The button's title ("Corrigir", "Entrar…", "Abrir chave…") followed by what it does.
        label = getattr(check.fix, "button_title", "Corrigir").rstrip("…")
        lines.append(f"  {label}: {check.fix.command}")
        command = terminal_command(check.fix) if terminal_command is not None and check.fix.opens_page else None
        if command:
            lines.append(f"  No terminal: {command}")
    return lines


def verify_lines(report, tz=None, terminal_command=None) -> list[str]:
    """Top ("{perfil}" and "N de M itens de acordo · verificado às HH:MM"), then the rows,
    by section when the verifier gives them (Ferramentas, Integração, Sessões)."""
    lines = [report.profile_name or "Nenhum perfil ativo",
             f"{report.ok_count} de {report.total} itens de acordo · "
             f"verificado às {report.checked_at.astimezone(tz).strftime('%H:%M')}"]
    groups: dict[str | None, list] = {}
    for check in report.checks:
        groups.setdefault(_value(getattr(check, "section", None)), []).append(check)
    order = [key for key in _SECTION_TITLES if key in groups] + [key for key in groups if key not in _SECTION_TITLES]
    for key in order:
        lines.append("")
        if key in _SECTION_TITLES:
            lines.append(_SECTION_TITLES[key])
        for check in groups[key]:
            lines += check_lines(check, terminal_command)
    return lines


def terminal_command_for(fix, config, environment) -> str | None:
    """The `multicred …` for a fix that opens a page in the app (VerifyPage.swift:148-160)."""
    import shlex

    from .core.engine.verifier import FixKind

    kind = getattr(fix, "kind", None)
    if kind == FixKind.LOGIN_CLAUDE:
        account = config.claude_account(fix.target) if config is not None else None
        return f"multicred entrar claude {shlex.quote(account.nickname)}" if account is not None else None
    if kind == FixKind.LOGIN_CLAUDE_FOLDER:
        return f"multicred entrar pasta {shlex.quote(folder_name(environment, fix.target))}"
    if kind == FixKind.LOGIN_GITHUB:
        host = fix.target or "github.com"
        return "multicred entrar github" + ("" if host == "github.com" else f" {shlex.quote(host)}")
    if kind == FixKind.OPEN_API_KEY:
        return "multicred contas (Chaves de API)"
    return None


# MARK: Desinstalar as text (UninstallPage, SettingsPage.swift:114-174; ui/pages/uninstall.py, which
# needs GTK, builds the same lines for the window)

UNINSTALL_TITLE = "Desinstalar o MultiCred"
UNINSTALL_INTRO = ("Estas são as mudanças. Só o bloco do MultiCred é removido de cada arquivo; o resto fica como "
                   "você deixou.")
UNINSTALL_D12 = "Também sair e apagar as pastas do Claude criadas pelo MultiCred"
NOTHING_TO_UNINSTALL = "Nada para desinstalar: o MultiCred não deixou nada nesta máquina."
UNINSTALL_DONE = "MultiCred desinstalado"
UNINSTALL_NEW_TERMINAL = "Abra um novo terminal para que os shells deixem de carregar o MultiCred."
# The window quits after uninstalling (the Mac's "Sair do MultiCred"); from a terminal, the
# tray app may still be running on the desktop.
UNINSTALL_QUIT_TRAY = "Se o MultiCred estiver aberto na bandeja, saia dele (Sair do MultiCred)."


def _lines_count(count: int) -> str:
    return f"{count} {'linha' if count == 1 else 'linhas'}"


def uninstall_file_lines(items, autostart_path: str) -> list[str]:
    """Each file the uninstall changes, as the page's FileChangeView: "path · −N linhas" and
    the removed lines, "· será apagado", or the manual step of a block without its end marker."""
    lines = []
    for item in items:
        if item.kind == "block":
            lines.append(f"{item.path} · −{_lines_count(len(item.lines))}")
            lines += [f"  − {line}" for line in item.lines]
        elif item.kind == "manual":
            lines.append(f"{item.path} · bloco sem o marcador de fim; fica como está e você remove à mão")
        elif item.kind in ("file", "appDir") and item.path != autostart_path:
            lines.append(f"{item.path} · será apagado")
    return lines


def _from_store(store: str) -> str:
    """"o chaveiro do sistema" → "do chaveiro do sistema"; "a pasta X" → "da pasta X"."""
    article, _, rest = store.partition(" ")
    return {"o": "do", "a": "da"}.get(article, "de") + " " + rest if rest else f"de {store}"


def uninstall_also_text(items, autostart_path: str, store: str) -> str:
    """The page's "Também: …" line, from what this uninstall really does (`store`: where the
    secrets live, "o chaveiro do sistema" or Termux's secrets folder); empty when nothing."""
    pieces: list[str] = []
    autostart, backups = False, None
    for item in items:
        if item.kind == "session":
            pieces.append(f"systemctl --user unset-environment {item.path}")
        elif item.kind == "secrets":
            pieces.append(f"apagar {_from_store(store)} as chaves de API e os tokens das contas Claude "
                          "(pastas e conversas do Claude ficam)")
        elif item.kind == "claudeVault":
            plural = "s" if item.count != 1 else ""
            pieces.append(f"apagar {item.count} login{plural} do Claude guardado{plural} pelo MultiCred")
        elif item.kind == "claudeLogin":
            pieces.append(f"devolver a {item.path} o próprio login do Claude")
        elif item.kind == "claudeFolder":
            pieces.append(f"sair (claude auth logout) e apagar {item.path}")
        elif item.kind == "file" and item.path == autostart_path:
            autostart = True
        elif item.kind == "backups":
            backups = item.path
    if autostart:  # last, as on the Mac
        pieces.append("desligar a abertura ao iniciar a sessão")
    text = f"Também: {', '.join(pieces)}." if pieces else ""
    if backups:
        text = f"{text} Os backups vão para {backups}.".strip()
    return text


def uninstall_result_lines(report, platform) -> list[str]:
    """The page's result: restored files, backups, deleted folders, "!" manual steps, "✗"
    problems, then what is left to do (`apt remove`; no sudo on Termux)."""
    lines = [UNINSTALL_DONE]
    restored = report.changed_files + report.removed_files
    if restored:
        lines.append("Arquivos restaurados: " + ", ".join(restored))
    if report.backups_kept_at:
        lines.append(f"Backups guardados em {report.backups_kept_at}.")
    if report.deleted_folders:
        lines.append("Pastas do Claude apagadas: " + ", ".join(report.deleted_folders) + ".")
    lines += [f"! {step}" for step in report.manual_steps]
    lines += [f"✗ {problem}" for problem in report.problems]
    if platform.has_graphical_session:
        lines.append(UNINSTALL_QUIT_TRAY)
    remove = "apt remove multicred" if platform.is_termux else "sudo apt remove multicred"
    lines.append(f"{UNINSTALL_NEW_TERMINAL} Para tirar o app: {remove}.")
    return lines


def make_verifier(environment, tools, processes=None, engine=None, log=None):
    """The app's Verifier; it picks the platform's checks itself (no graphical-session
    check on Termux, a Claude Code check instead)."""
    from .core.engine.verifier import Verifier

    return Verifier(environment, tools, processes, engine=engine, log=log)


class _Session:
    def __init__(self, environment, tools, log, out, err, inp):
        from .ui.messages import secret_store_wording

        self.environment, self.tools, self.log, self.out, self.err, self.inp = environment, tools, log, out, err, inp
        directory = getattr(environment.secrets, "directory", None)
        self.store = (secret_store_wording(environment.platform) if directory is None
                      else secret_store_wording(environment.platform, environment.abbreviate(str(directory))))
        self._status_service = None

    def _say(self, *lines, error=False):
        stream = self.err if error else self.out
        for line in lines:
            print(line, file=stream)

    def _describe(self, error) -> str:
        """The app's message. Termux has no window, so the hints that name its screens
        ("Contas › Claude") name the terminal command instead."""
        import shlex

        from .core.engine.types import Preflight
        from .ui.messages import describe_switch_error

        text = describe_switch_error(error, self.store)
        if self.environment.platform.has_graphical_session or not isinstance(error, Preflight):
            return text
        nickname = error.failure.detail or ""
        command = f"multicred entrar claude {shlex.quote(nickname)}"
        return (text.replace("Gere outro em Contas › Claude.", f"Gere outro com: {command}")
                .replace(f"Em Contas › Claude › {nickname}, use \"Entrar de novo\".", f"Entre de novo com: {command}"))

    def _load(self):
        """(config, engine, state), or None after printing why (exit code 1)."""
        from .core.engine.switch_engine import SwitchEngine
        from .core.storage import ConfigRepository, JSONStoreError

        try:
            config = ConfigRepository(self.environment).load()
        except JSONStoreError as error:
            self._say(f"Não consegui ler a configuração: {error}", error=True)
            return None
        if config is None:
            self._say(NOT_CONFIGURED, "Para configurar: multicred configurar", error=True)
            return None
        engine = SwitchEngine(self.environment, self.tools, self.log)
        try:
            state = engine.current_state()
        except Exception as error:
            self._say(self._describe(error), error=True)
            return None
        return config, engine, state

    # MARK: Claude logins (the rules in core/claude/status.py, shared with ui/model.py; here the
    # folder's status is asked fresh instead of read from the app's cache)

    def _has_token(self, account) -> bool:
        from .core.claude.status import account_has_token
        return account_has_token(account, self.environment.secrets)

    def _folder_status(self, config_dir: str | None):
        """`claude auth status` of the folder, or what its .claude.json says (never a token)."""
        from .core.claude.status import ClaudeStatusService
        from .core.models import ClaudeFolder

        if self._status_service is None:
            self._status_service = ClaudeStatusService(self.environment, self.tools)
        try:
            return self._status_service.status(ClaudeFolder.own_login(config_dir))
        except Exception:
            return None

    def _needs_claude_login(self, config, profile) -> bool:
        from .core.claude.status import needs_claude_login
        return needs_claude_login(profile, config, self._has_token, self._folder_status)

    # MARK: commands

    def status(self) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        config, _, state = loaded
        self._say(*status_lines(config, state, self.environment))
        return 0

    def profiles(self) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        config, _, state = loaded
        lines = ["Perfis"]
        if not config.profiles:
            lines.append("Nenhum perfil configurado.")
        for index, label in enumerate(self.profile_labels(config, state), start=1):
            lines.append(f"  {index if index <= 9 else ' '}  {label}")
        self._say(*lines)
        return 0

    def profile_labels(self, config, state) -> list[str]:
        """Each profile as the menu shows it: name, ✓ on the active one, the Claude warning."""
        labels = []
        for profile in config.profiles:
            label = profile.name
            if profile.id == state.profile_id:
                label += f"  {ACTIVE_MARK}"
            if self._needs_claude_login(config, profile):
                label += f"  {NEEDS_CLAUDE_LOGIN}"
            labels.append(label)
        return labels

    def switch(self, text: str) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        config = loaded[0]
        profile = resolve_profile(config, text)
        if profile is None:
            self._say(f"Nenhum perfil chamado “{text}”. Use o nome ou o número de um destes:",
                      *(f"  {index}  {each.name}" for index, each in enumerate(config.profiles, start=1)),
                      error=True)
            return 1
        return self.switch_to(profile.id, loaded)

    def switch_to(self, profile_id, loaded=None) -> int:
        """Switches to the profile and prints the app's notification (also used by the menus:
        "Ativar este perfil", saving the active profile, the end of `configurar`)."""
        from .core.models import SwitchFeedback
        from .ui.feedback import message_for

        loaded = loaded or self._load()
        if loaded is None:
            return 1
        config, engine, previous = loaded
        profile = config.profile(profile_id)
        try:
            result = engine.switch_profile(profile_id, config)
        except Exception as error:  # SwitchError, or anything else: the app's message, exit 1
            self._say(f"Não consegui trocar para {profile.name if profile else 'o perfil'}", self._describe(error),
                      error=True)
            return 1
        message = message_for(self._outcome(config, previous, result, claude_only=False), SwitchFeedback.NOTIFICATION)
        self._say(message.title, message.body)
        return 0

    def forget_folder_status(self) -> None:
        """After a folder's login changed: ask again."""
        self._status_service = None

    def _claude_choices(self, config, state, profile) -> list[str]:
        """The submenu's items: the folder's own login, then each account."""
        own_account_id = profile.claude_account_id if profile else None
        status = self._folder_status(profile.claude_config_dir if profile else None)
        if status is not None and status.kind == "loggedIn":
            subtitle = status.email or "Logada"
        elif status is not None and status.kind == "loggedOut":
            subtitle = "Sem login"
        else:
            subtitle = "Status indisponível"
        if own_account_id is None:
            subtitle += " · do perfil"
        line = f"  {FOLDER_LOGIN_KEYWORD}  Login da pasta · {subtitle}"
        lines = [line + (f"  {ACTIVE_MARK}" if state.applied_claude_account_id is None else "")]
        for index, account in enumerate(config.claude_accounts, start=1):
            label = account.nickname + ("" if self._has_token(account) else " · sem token")
            subtitle = account.email or "conta Claude"
            if account.id == own_account_id:
                subtitle += " · do perfil"
            line = f"  {index:<{len(FOLDER_LOGIN_KEYWORD)}}  {label} · {subtitle}"
            lines.append(line + (f"  {ACTIVE_MARK}" if account.id == state.applied_claude_account_id else ""))
        return lines

    def claude(self, text: str | None) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        config, _, previous = loaded
        profile = config.profile(previous.profile_id)
        if text is None:
            if profile is None:
                self._say("Conta Claude", "Ative um perfil primeiro")
                return 0
            self._say(f"Conta Claude · pasta {folder_name(self.environment, profile.claude_config_dir)} "
                      f"({profile.name}) fica",
                      *self._claude_choices(config, previous, profile),
                      "Agents, skills e conversas não mudam.",
                      "Vale para novos terminais; sessões abertas continuam.")
            return 0
        account = resolve_claude_account(config, text)
        if account is _UNRESOLVED:
            self._say(f"Nenhuma conta Claude chamada “{text}”. Use o apelido, o número ou “{FOLDER_LOGIN_KEYWORD}”:",
                      *self._claude_choices(config, previous, profile), error=True)
            return 1
        return self.use_claude_account(account.id if account is not None else None, loaded=loaded)

    def use_claude_account(self, account_id, loaded=None, quiet_without_profile=False) -> int:
        """"Usar agora": only the Claude account changes (None = the folder's own login)."""
        from .core.engine.types import PreflightFailure
        from .core.models import ClaudeFolder, SwitchFeedback
        from .ui.feedback import message_for
        from .ui.messages import describe_preflight

        loaded = loaded or self._load()
        if loaded is None:
            return 1
        config, engine, previous = loaded
        profile = config.profile(previous.profile_id)
        account = config.claude_account(account_id)
        failure_title = "Não consegui trocar a conta Claude"
        if profile is None and account_id is not None:
            # The Mac's submenu is off without a profile ("Ative um perfil primeiro").
            self._say(failure_title, describe_preflight(
                PreflightFailure("noActiveProfile", account.nickname if account else "login da pasta"), self.store),
                error=True)
            return 1
        if profile is None and not quiet_without_profile:
            self._say(failure_title, describe_preflight(PreflightFailure("noActiveProfile", "login da pasta"),
                                                        self.store), error=True)
            return 1
        override = ClaudeFolder.override_for(account_id, profile)
        try:
            result = engine.switch_claude_only(override, config)
        except Exception as error:
            self._say(failure_title, self._describe(error), error=True)
            return 1
        message = message_for(self._outcome(config, previous, result, claude_only=True), SwitchFeedback.NOTIFICATION)
        self._say(message.title, message.body)
        if account_id is None and profile is not None:
            status = self._folder_status(profile.claude_config_dir)
            if status is not None and status.kind == "loggedOut":
                self._say(f"A pasta {folder_name(self.environment, profile.claude_config_dir)} não tem login.",
                          "Para entrar: multicred entrar pasta")
        return 0

    def verify(self, fix: bool) -> int:
        loaded = self._load()
        if loaded is None:
            return 1
        config, engine, state = loaded
        verifier = make_verifier(self.environment, self.tools, self._processes(), engine=engine, log=self.log)
        try:
            # deep: also asks gh whether the profile's token still works, like opening the page.
            report = verifier.run(config, state, deep=True)
        except Exception as error:
            self._say(f"Não consegui verificar: {error}", error=True)
            return 1
        hint = lambda fix: terminal_command_for(fix, config, self.environment)  # noqa: E731
        self._say(*verify_lines(report, terminal_command=hint))
        fixes = runnable_fixes(report)
        if not fix:
            if fixes:
                self._say("", "Corrigir tudo: multicred verificar --corrigir",
                          "“Corrigir” mostra o comando antes de executar.")
            return 0 if report.is_consistent else 1
        if not fixes:
            if not report.is_consistent:
                self._say("", "Nada para corrigir automaticamente")
            return 0 if report.is_consistent else 1
        self._say("", "Corrigir tudo?", *(f"• {each.command}" for each in fixes))
        if not self._confirm("Executar? [s/N] "):
            self._say("Nada foi alterado.")
            return 1
        from .core.engine.verifier import RecheckFailed

        try:
            # The report after the repairs has the depth of the first, so the GitHub login
            # row stays while it is not resolved and "N de M" counts the same rows; the
            # verifier re-checks at that depth inside its memo (no second `claude --version`
            # or `claude auth status`). The window keeps the Mac's shallow re-check.
            report = verifier.fix_all(report, config, deep=True)
        except RecheckFailed as error:
            # The repairs ran; only the re-check after them failed.
            self._say(f"Não consegui verificar de novo depois de corrigir: {error.cause}",
                      "Para ver o resultado: multicred verificar", error=True)
            if error.errors:
                self._say("Não consegui corrigir: " + "; ".join(error.errors), error=True)
            return 1
        except Exception as error:
            self._say(f"Não consegui corrigir: {error}", error=True)
            return 1
        self._say("", *verify_lines(report, terminal_command=hint))
        if report.errors:
            self._say("Não consegui corrigir: " + "; ".join(report.errors), error=True)
        return 0 if report.is_consistent and not report.errors else 1

    def uninstall(self) -> int:
        """Ajustes › "Desinstalar e restaurar arquivos originais": the page's preview, the D12
        question when MultiCred created Claude folders, then "Desinstalar? [s/N]". Works
        without a configuration too (blocks left behind). The uninstaller skips the
        graphical session where there is none (Termux)."""
        from .core.install.autostart import Autostart
        from .core.install.uninstaller import Uninstaller

        uninstaller = Uninstaller(self.environment, tools=self.tools, autostart=Autostart(self.environment))
        autostart = self.environment.abbreviate(str(uninstaller.autostart.file))
        items = uninstaller.preview()
        folders = uninstaller.created_claude_folders()
        if not items and not folders:
            self._say(NOTHING_TO_UNINSTALL)
            return 0
        files = uninstall_file_lines(items, autostart)
        self._say(UNINSTALL_TITLE, UNINSTALL_INTRO, *([""] + files if files else []))
        remove_folders = False
        if folders:
            names = ", ".join(self.environment.abbreviate(folder) for folder in folders)
            self._say("")
            remove_folders = self._confirm(f"{UNINSTALL_D12} ({names})? [s/N] ")
            if remove_folders:
                items = uninstaller.preview(remove_claude_folders=True)
        also = uninstall_also_text(items, autostart, self.store.store)
        if also:
            self._say("", also)
        if not self._confirm("Desinstalar? [s/N] "):
            self._say("Nada foi alterado.")
            return 1
        try:
            report = uninstaller.apply(remove_folders)
        except Exception as error:
            self._say(f"Não consegui desinstalar: {error}", error=True)
            return 1
        self.log.log("info", f"uninstalled from the terminal: changed={report.changed_files} "
                             f"removed={report.removed_files} manual={report.unterminated} "
                             f"problems={len(report.problems)}")
        self._say("", *uninstall_result_lines(report, self.environment.platform))
        return 1 if report.problems or report.unterminated else 0

    # MARK: helpers

    def _confirm(self, question: str) -> bool:
        print(question, end="", file=self.out, flush=True)
        try:
            answer = self.inp.readline()
        except (OSError, ValueError):
            answer = ""
        interactive = False
        try:
            interactive = self.inp.isatty()
        except (AttributeError, ValueError):
            pass
        if not interactive or not answer.endswith("\n"):
            print(file=self.out)  # the answer was not echoed: end the question's line
        return answer.strip().casefold() in _YES

    def _outcome(self, config, previous, result, claude_only: bool):
        """Same SwitchOutcome the app's notification is built from (ui/model.py)."""
        from .ui.feedback import SwitchOutcome

        before = config.profile(previous.profile_id)
        after = config.profile(result.state.profile_id)
        claude_changed = (claude_only or before is None
                          or before.claude_config_dir != (after.claude_config_dir if after else None)
                          or previous.applied_claude_account_id != result.state.applied_claude_account_id)
        account = config.claude_account(result.state.applied_claude_account_id)
        return SwitchOutcome(profile_name=after.name if after else None,
                             claude_account_name=account.nickname if account else None,
                             steps=tuple(result.applied_steps), claude_only=claude_only,
                             claude_changed=claude_changed, running_claude_sessions=self._running_claude_sessions())

    def _processes(self):
        """The real /proc only for the real system; test mode has no sessions (ui/model.py)."""
        if self.environment.mode == "real":
            try:
                from .core import real_env
                return real_env.make_process_scanner()
            except Exception:
                return None
        from .core.adapters.process_scanner import FixedProcessScanner
        return FixedProcessScanner(0)

    def _running_claude_sessions(self) -> int:
        """Claude Code sessions still on the previous account (real system only; informative)."""
        if self.environment.mode != "real":
            return 0
        try:
            from .core import real_env
            return real_env.make_process_scanner().claude_code_session_count()
        except Exception:
            return 0
