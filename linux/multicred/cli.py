"""`multicred <comando>`: switch and inspect profiles from a terminal (R-09).

The only interface on Termux (Android has no GTK) and the "CLI companheira" on the
desktop. Nothing here imports GTK or PyGObject, directly or through what it uses
(ui/messages.py and ui/feedback.py are plain Python): Termux has no PyGObject.
Messages are the app's (UserMessages / SwitchFeedbackPresenter on the Mac).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import VERSION

NOT_CONFIGURED = "MultiCred ainda não foi configurado."


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


def main(argv=None, environment=None, tools=None, out=None, err=None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv), environment, tools, out, err)


def run(args, environment=None, tools=None, out=None, err=None) -> int:
    """Runs `args.command`; exit code 0 on success, 1 on failure."""
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
    session = _Session(environment, tools, log, out, err)
    return {"trocar": lambda: session.switch(args.perfil), "status": session.status}[args.command]()


def resolve_profile(config, text: str):
    """By name, ignoring case, or by position 1–9 (the Ctrl+N of the menu). None when neither."""
    wanted = text.strip().casefold()
    for profile in config.profiles:
        if profile.name.casefold() == wanted:
            return profile
    if wanted.isdigit() and len(wanted) == 1 and 1 <= int(wanted) <= min(9, len(config.profiles)):
        return config.profiles[int(wanted) - 1]
    return None


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
    folder = environment.abbreviate(profile.claude_config_dir) if profile.claude_config_dir else "~/.claude"
    applied = config.claude_account(state.applied_claude_account_id)
    claude = f"Claude · {folder} · {applied.nickname if applied else 'login da pasta'}"
    if state.claude_override_id is not None:
        claude += " (só Claude)"
    lines.append(claude)
    keys = [key for key in (config.api_key(k) for k in profile.api_key_ids) if key is not None]
    lines.append("Chaves: " + (", ".join(key.env_var for key in keys) if keys else "nenhuma"))
    return lines


class _Session:
    def __init__(self, environment, tools, log, out, err):
        self.environment, self.tools, self.log, self.out, self.err = environment, tools, log, out, err

    def _say(self, *lines, error=False):
        stream = self.err if error else self.out
        for line in lines:
            print(line, file=stream)

    def _load(self):
        """(config, engine), or None after printing why (exit code 1)."""
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
        return config, SwitchEngine(self.environment, self.tools, self.log)

    def status(self) -> int:
        from .ui.messages import describe_switch_error

        loaded = self._load()
        if loaded is None:
            return 1
        config, engine = loaded
        try:
            state = engine.current_state()
        except Exception as error:
            self._say(describe_switch_error(error), error=True)
            return 1
        self._say(*status_lines(config, state, self.environment))
        return 0

    def switch(self, text: str) -> int:
        from .core.models import SwitchFeedback
        from .ui.feedback import message_for
        from .ui.messages import describe_switch_error

        loaded = self._load()
        if loaded is None:
            return 1
        config, engine = loaded
        profile = resolve_profile(config, text)
        if profile is None:
            self._say(f"Nenhum perfil chamado “{text}”. Use o nome ou o número de um destes:",
                      *(f"  {index}  {each.name}" for index, each in enumerate(config.profiles, start=1)),
                      error=True)
            return 1
        try:
            previous = engine.current_state()
            result = engine.switch_profile(profile.id, config)
        except Exception as error:  # SwitchError, or anything else: the app's message, exit 1
            self._say(f"Não consegui trocar para {profile.name}", describe_switch_error(error), error=True)
            return 1
        message = message_for(self._outcome(config, previous, result), SwitchFeedback.NOTIFICATION)
        self._say(message.title, message.body)
        return 0

    def _outcome(self, config, previous, result):
        """Same SwitchOutcome the app's notification is built from (ui/model.py)."""
        from .ui.feedback import SwitchOutcome

        before = config.profile(previous.profile_id)
        after = config.profile(result.state.profile_id)
        claude_changed = (before is None
                          or before.claude_config_dir != (after.claude_config_dir if after else None)
                          or previous.applied_claude_account_id != result.state.applied_claude_account_id)
        account = config.claude_account(result.state.applied_claude_account_id)
        return SwitchOutcome(profile_name=after.name if after else None,
                             claude_account_name=account.nickname if account else None,
                             steps=tuple(result.applied_steps), claude_changed=claude_changed,
                             running_claude_sessions=self._running_claude_sessions())

    def _running_claude_sessions(self) -> int:
        """Claude Code sessions still on the previous account (real system only; informative)."""
        if self.environment.mode != "real":
            return 0
        try:
            from .core import real_env
            return real_env.make_process_scanner().claude_code_session_count()
        except Exception:
            return 0
