"""`multicred [--test-mode [--fresh]] [--background]` opens the app;
`multicred <comando>` (trocar, status…) runs in the terminal (cli.py). On Termux,
which has no graphical interface, no command prints the help."""

from __future__ import annotations

import sys

from . import cli
from .cli import parse_args  # noqa: F401  (the entry point's parser; tests use it)


def build_model(args):
    from .core.log import EventLog
    from .core.tools import ToolLocator
    from .ui.model import AppModel

    environment = cli.build_environment(args)
    log = EventLog(environment.logs_directory, environment.clock)
    return AppModel(environment, ToolLocator(environment), log)


def run_gui(model, background: bool) -> int:
    from .ui.app import run
    return run(model, background=background)


def main(argv=None, platform=None) -> int:
    """`platform` None = detected from the real environment (real_env)."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command is not None:
        return cli.run(args)
    if platform is None:
        from .core import real_env
        platform = real_env.detect_platform()
    if platform.is_termux:
        cli.build_parser().print_help()
        return 0
    return run_gui(build_model(args), background=args.background)


if __name__ == "__main__":
    sys.exit(main())
