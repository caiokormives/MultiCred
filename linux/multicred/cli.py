"""`multicred <comando>`: switch and inspect profiles from a terminal (R-09).

The only interface on Termux (Android has no GTK) and the "CLI companheira" on the
desktop. Nothing here imports GTK or PyGObject, directly or through what it uses
(ui/messages.py and ui/feedback.py are plain Python): Termux has no PyGObject.
Messages are the app's (UserMessages / SwitchFeedbackPresenter on the Mac); the
listings are the Mac's menu as text (AppDelegate.swift: "Perfis" and "Trocar só a
conta Claude") and its Verificar page (VerifyPage.swift).

    trocar <perfil|1-9>       the menu's profile items
    status                    the menu header
    perfis                    the menu's "Perfis" section
    claude [conta|N|pasta]    the "Trocar só a conta Claude" submenu (no argument: list it)
    verificar [--corrigir]    the Verificar page; "Corrigir tudo" asks before running

Exit codes: 0 done (verificar: everything de acordo), 1 failure or problems left,
2 bad usage (argparse).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import VERSION

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
    commands.add_parser("perfis", help="lista os perfis (✓ no ativo; o número serve para trocar)",
                        description="Lista os perfis, como o menu da bandeja.")
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
    return parser


def parse_args(argv) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if args.fresh:
        args.test_mode = True
    return args


def build_environment(args):
    """The real system, or the test-mode sandbox (seeded with the demo profiles unless --fresh)."""
    from .core import real_env
    from .core.storage import ConfigRepository

    if not args.test_mode:
        return real_env.make_real()
    sandbox = args.sandbox or real_env.default_sandbox()
    if args.fresh and args.sandbox is None:
        # A new empty home every time: the default sandbox may hold an earlier setup.
        sandbox = Path(tempfile.mkdtemp(prefix="multicred-fresh-"))
        print(f"MultiCred: HOME de teste vazio em {sandbox}", file=sys.stderr)
    environment = real_env.make_sandbox(sandbox)
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
    tools = tools or ToolLocator(environment, well_known=environment.platform.tool_search_directories)
    log = EventLog(environment.logs_directory, environment.clock)
    session = _Session(environment, tools, log, out, err, inp or sys.stdin)
    return {
        "trocar": lambda: session.switch(args.perfil),
        "status": session.status,
        "perfis": session.profiles,
        "claude": lambda: session.claude(args.conta),
        "verificar": lambda: session.verify(args.corrigir),
    }[args.command]()


def resolve_profile(config, text: str):
    """By name, ignoring case, or by position 1–9 (the Ctrl+N of the menu). None when neither."""
    wanted = text.strip().casefold()
    for profile in config.profiles:
        if profile.name.casefold() == wanted:
            return profile
    if wanted.isdigit() and len(wanted) == 1 and 1 <= int(wanted) <= min(9, len(config.profiles)):
        return config.profiles[int(wanted) - 1]
    return None


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
    if wanted.isdigit() and 1 <= int(wanted) <= len(config.claude_accounts):
        return config.claude_accounts[int(wanted) - 1]
    return _UNRESOLVED


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


def _opens_page(fix) -> bool:
    """A fix that leads to a login or a key's editor: the app opens a page, nothing runs."""
    return bool(getattr(fix, "opens_page", False)) or fix.apply is None


def runnable_fixes(report) -> list:
    """What "Corrigir tudo" runs: one fix per key (several checks may share one repair)."""
    fixes = []
    for check in report.fixable:
        fix = check.fix
        if not _opens_page(fix) and fix.key not in {f.key for f in fixes}:
            fixes.append(fix)
    return fixes


def check_lines(check) -> list[str]:
    lines = [f"{_glyph(check)} {check.title}"]
    if check.expected is not None:
        lines.append(f"  Esperado {check.expected}, encontrado {check.found or 'nada'}")
    elif check.found:
        lines.append(f"  {check.found}")
    if check.origin:
        lines.append(f"  definido em {check.origin}")
    if check.hint:
        lines.append(f"  {check.hint}")
    if check.fix is not None and _needs_attention(check):
        # The button's title ("Corrigir", "Entrar…", "Abrir chave…") followed by what it does.
        label = getattr(check.fix, "button_title", "Corrigir").rstrip("…")
        lines.append(f"  {label}: {check.fix.command}")
    return lines


def verify_lines(report, tz=None) -> list[str]:
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
            lines += check_lines(check)
    return lines


def make_verifier(environment, tools, processes=None, engine=None, log=None):
    """The app's Verifier. Without a graphical session (Termux) the check of the Claude
    folder in the session (systemctl --user, environment.d) does not apply, and asking
    for it would run systemctl, so it is left out. TODO(S-12): drop this once
    Verifier.run skips that check itself on such platforms."""
    from .core.engine.verifier import Check, CheckStatus, Verifier

    if environment.platform.has_graphical_session:
        return Verifier(environment, tools, processes, engine=engine, log=log)
    skipped = "cli.no-graphical-session"

    class _WithoutGraphicalSession(Verifier):
        def _session_check(self, profile, env):
            return Check(skipped, "", CheckStatus.INFO)

        def run(self, config, state, deep=False):
            report = super().run(config, state, deep)
            report.checks = [check for check in report.checks if check.id != skipped]
            return report

    return _WithoutGraphicalSession(environment, tools, processes, engine=engine, log=log)


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
        from .ui.messages import describe_switch_error
        return describe_switch_error(error, self.store)

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
            self._say(NOT_CONFIGURED, error=True)
            return None
        engine = SwitchEngine(self.environment, self.tools, self.log)
        try:
            state = engine.current_state()
        except Exception as error:
            self._say(self._describe(error), error=True)
            return None
        return config, engine, state

    # MARK: Claude logins (ui/model.py: has_token, folder_status, needs_claude_login)

    def _has_token(self, account) -> bool:
        """Login accounts live in the vault (the switch preflight checks it); token
        accounts need their secret."""
        from .core.models import ClaudeAccountKind

        if account.kind == ClaudeAccountKind.LOGIN:
            return True
        try:
            return bool(self.environment.secrets.contains_item(account.secret_account))
        except Exception:
            return False

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
        account = config.claude_account(profile.claude_account_id)
        if account is not None:
            return not self._has_token(account)
        status = self._folder_status(profile.claude_config_dir)
        return status is not None and status.kind == "loggedOut"

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
        for index, profile in enumerate(config.profiles, start=1):
            line = f"  {index if index <= 9 else ' '}  {profile.name}"
            if profile.id == state.profile_id:
                line += f"  {ACTIVE_MARK}"
            if self._needs_claude_login(config, profile):
                line += f"  {NEEDS_CLAUDE_LOGIN}"
            lines.append(line)
        self._say(*lines)
        return 0

    def switch(self, text: str) -> int:
        from .core.models import SwitchFeedback
        from .ui.feedback import message_for

        loaded = self._load()
        if loaded is None:
            return 1
        config, engine, previous = loaded
        profile = resolve_profile(config, text)
        if profile is None:
            self._say(f"Nenhum perfil chamado “{text}”. Use o nome ou o número de um destes:",
                      *(f"  {index}  {each.name}" for index, each in enumerate(config.profiles, start=1)),
                      error=True)
            return 1
        try:
            result = engine.switch_profile(profile.id, config)
        except Exception as error:  # SwitchError, or anything else: the app's message, exit 1
            self._say(f"Não consegui trocar para {profile.name}", self._describe(error), error=True)
            return 1
        message = message_for(self._outcome(config, previous, result, claude_only=False), SwitchFeedback.NOTIFICATION)
        self._say(message.title, message.body)
        return 0

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
        from .core.engine.types import PreflightFailure
        from .core.models import ClaudeFolder, SwitchFeedback
        from .ui.feedback import message_for
        from .ui.messages import describe_preflight

        loaded = self._load()
        if loaded is None:
            return 1
        config, engine, previous = loaded
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
        failure_title = "Não consegui trocar a conta Claude"
        if profile is None:
            # The Mac's submenu is off without a profile ("Ative um perfil primeiro").
            target = account.nickname if account is not None else "login da pasta"
            self._say(failure_title, describe_preflight(PreflightFailure("noActiveProfile", target), self.store),
                      error=True)
            return 1
        override = ClaudeFolder.override_for(account.id if account else None, profile)
        try:
            result = engine.switch_claude_only(override, config)
        except Exception as error:
            self._say(failure_title, self._describe(error), error=True)
            return 1
        message = message_for(self._outcome(config, previous, result, claude_only=True), SwitchFeedback.NOTIFICATION)
        self._say(message.title, message.body)
        if account is None:
            status = self._folder_status(profile.claude_config_dir)
            if status is not None and status.kind == "loggedOut":
                self._say(f"A pasta {folder_name(self.environment, profile.claude_config_dir)} não tem login.")
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
        self._say(*verify_lines(report))
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
        try:
            report = verifier.fix_all(report, config)
        except Exception as error:
            self._say(f"Não consegui corrigir: {error}", error=True)
            return 1
        self._say("", *verify_lines(report))
        if report.errors:
            self._say("Não consegui corrigir: " + "; ".join(report.errors), error=True)
        return 0 if report.is_consistent and not report.errors else 1

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
