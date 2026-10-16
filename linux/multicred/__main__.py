"""`multicred [--test-mode] [--background]`"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import VERSION


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="multicred", description="Troca de identidade de dev com um clique.")
    parser.add_argument("--test-mode", action="store_true",
                        help="roda num HOME de teste; nada do sistema real muda")
    parser.add_argument("--sandbox", type=Path, default=None, help="pasta do HOME de teste (com --test-mode)")
    parser.add_argument("--background", action="store_true", help="inicia só na bandeja (início da sessão)")
    parser.add_argument("--version", action="version", version=f"MultiCred {VERSION}")
    return parser.parse_args(argv)


def build_model(args):
    from .core import real_env
    from .core.log import EventLog
    from .core.storage import ConfigRepository
    from .core.tools import ToolLocator
    from .ui.model import AppModel

    if args.test_mode:
        environment = real_env.make_sandbox(args.sandbox or real_env.DEFAULT_SANDBOX)
        repository = ConfigRepository(environment)
        if repository.load() is None:
            repository.save(real_env.seed_sandbox(environment))
    else:
        environment = real_env.make_real()
    log = EventLog(environment.logs_directory, environment.clock)
    return AppModel(environment, ToolLocator(environment), log)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    model = build_model(args)
    from .ui.app import run
    return run(model, background=args.background)


if __name__ == "__main__":
    sys.exit(main())
