"""The only module in multicred.core allowed to look at the real user account.

tests/test_architecture.py fails if os.environ, pwd or Path.home() show up
anywhere else in the core.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from .claude.credentials import InMemoryClaudeCredentialStore
from .env import SystemEnvironment
from .models import Config, GitIdentity, Profile, ProfileColor
from .runner import DryRunCommandRunner, ProcessRunner
from .secrets import InMemorySecretStore

DEFAULT_SANDBOX = Path("/tmp/multicred-sandbox")


def _account() -> tuple[str, Path, str]:
    entry = pwd.getpwuid(os.getuid())
    return entry.pw_name, Path(entry.pw_dir), entry.pw_shell or "/bin/bash"


def real_home() -> Path:
    """The real home directory; tests use it only to assert it is never touched."""
    return _account()[1].resolve()


def session_bus_address() -> str | None:
    return os.environ.get("DBUS_SESSION_BUS_ADDRESS")


def make_real() -> SystemEnvironment:
    """The real system: real processes, Secret Service, ~/.claude*/.credentials.json."""
    from .secret_service import SecretServiceStore
    from .claude.credentials import FileClaudeCredentialStore
    name, home, shell = _account()
    return SystemEnvironment(mode="real", home=home, login_shell=shell, user_name=name, runner=ProcessRunner(),
                             secrets=SecretServiceStore(), claude_credentials=FileClaudeCredentialStore(home))


def make_sandbox(sandbox_home: Path = DEFAULT_SANDBOX, responder=None) -> SystemEnvironment:
    """App test mode: a sandbox home, commands recorded but not executed, secrets in memory."""
    name, _, shell = _account()
    sandbox_home = Path(sandbox_home)
    sandbox_home.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    return SystemEnvironment(mode="dryRun", home=sandbox_home.resolve(), login_shell=shell, user_name=name,
                             runner=DryRunCommandRunner(clock, responder), secrets=InMemorySecretStore(),
                             claude_credentials=InMemoryClaudeCredentialStore(), clock=clock)


def seed_sandbox(environment: SystemEnvironment) -> Config:
    """A demo configuration for test mode (two profiles), like the macOS sandbox world."""
    if environment.mode != "dryRun":
        raise RuntimeError("seed_sandbox only runs in test mode")
    work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.example", color=ProfileColor.INDIGO)
    personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.example", color=ProfileColor.ORANGE)
    return Config(git_identities=[work, personal], profiles=[
        Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work.id),
        Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=personal.id),
    ])
