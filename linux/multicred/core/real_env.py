"""The only module in multicred.core allowed to look at the real user account.

tests/test_architecture.py fails if os.environ, pwd or Path.home() show up
anywhere else in the core.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Mapping

from .claude.credentials import InMemoryClaudeCredentialStore
from .env import SystemEnvironment
from .models import Config, GitIdentity, Profile, ProfileColor
from .platform import Platform, detect
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


def detect_platform(environ: Mapping[str, str] | None = None) -> Platform:
    """Desktop or Termux, from the real process environment unless one is given."""
    return detect(os.environ if environ is None else environ)


def default_sandbox(platform: Platform | None = None) -> Path:
    """Test mode's default HOME: /tmp/multicred-sandbox on the desktop, under $TMPDIR on
    Termux (/tmp is not writable on Android)."""
    platform = platform or detect_platform()
    if not platform.is_termux:
        return DEFAULT_SANDBOX
    return Path(platform.temp_directory) / DEFAULT_SANDBOX.name


def make_real(environ: Mapping[str, str] | None = None) -> SystemEnvironment:
    """The real system: real processes, Secret Service, ~/.claude*/.credentials.json.
    On Termux (detected from the environment), `make_termux`.

    Building it touches nothing: the stores connect to D-Bus and read files only
    when first used.
    """
    environ = os.environ if environ is None else environ
    if detect(environ).is_termux:
        return make_termux(environ)
    from .secret_service import SecretServiceStore
    from .claude.credentials import FileClaudeCredentialStore
    name, home, shell = _account()
    return SystemEnvironment(mode="real", home=home, login_shell=shell, user_name=name, runner=ProcessRunner(),
                             secrets=SecretServiceStore(), claude_credentials=FileClaudeCredentialStore(home),
                             xdg_config_home=environ.get("XDG_CONFIG_HOME") or None,
                             xdg_runtime_dir=environ.get("XDG_RUNTIME_DIR") or None,
                             dbus_session_bus_address=environ.get("DBUS_SESSION_BUS_ADDRESS") or None)


def make_termux(environ: Mapping[str, str] | None = None) -> SystemEnvironment:
    """The real Termux (Android): real processes, secrets and the Claude login vault in
    0600 files under ~/.config/multicred/secrets (no Secret Service, no D-Bus), Claude
    Code's login in `<folder>/.credentials.json` as on Linux.

    Termux sets HOME (/data/data/com.termux/files/home) and SHELL ($PREFIX/bin/bash,
    bash 5.3); they win over the passwd entry Android's libc makes up. No D-Bus or
    runtime dir: Android has neither. Building it touches nothing.
    """
    from .claude.credentials import FileClaudeCredentialStore
    from .file_secrets import CLAUDE_LOGIN_DIRECTORY_NAME, FileSecretStore, FileVaultBackend, SECRETS_DIRECTORY_NAME
    environ = os.environ if environ is None else environ
    platform = detect(environ)
    if not platform.is_termux:
        platform = Platform.termux(tmpdir=environ.get("TMPDIR"), ld_preload=environ.get("LD_PRELOAD"))
    name, passwd_home, passwd_shell = _account()
    home_text = environ.get("HOME") or ""
    home = Path(home_text) if home_text.startswith("/") else passwd_home
    shell_text = environ.get("SHELL") or ""
    shell = shell_text if shell_text.startswith("/") else (passwd_shell if passwd_shell.startswith(platform.prefix)
                                                            else f"{platform.prefix}/bin/bash")
    secrets = home / ".config" / "multicred" / SECRETS_DIRECTORY_NAME
    return SystemEnvironment(mode="real", home=home, login_shell=shell, user_name=name, runner=ProcessRunner(),
                             secrets=FileSecretStore(secrets),
                             claude_credentials=FileClaudeCredentialStore(
                                 home, vault=FileVaultBackend(secrets / CLAUDE_LOGIN_DIRECTORY_NAME)),
                             xdg_config_home=environ.get("XDG_CONFIG_HOME") or None,
                             platform=platform)


def make_process_scanner(proc_root: Path = Path("/proc")):
    """Counts this user's Claude Code sessions in the real /proc (read-only)."""
    from .adapters.process_scanner import ProcScanner
    return ProcScanner(proc_root, uid=os.getuid(), own_pid=os.getpid())


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
