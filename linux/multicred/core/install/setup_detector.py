"""What onboarding finds on the machine (design 02b, steps "Preparar" and "Perfis").

Read-only: tool versions and the git identity come from commands run through
the injected runner (recorded, not run, in test mode), gh logins through
GitHubCLI, and Claude folders from the non-secret `.claude.json`. Credentials
are never read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import claude_paths
from ..adapters.github_cli import GitHubCLI, GitHubLogin
from ..claude.status import ClaudeLoginStatus, status_from_state_file
from ..models import ClaudeFolder, Config, GitHubAccount, GitIdentity, Profile, ProfileColor, ShellKind
from ..runner import CommandRequest
from ..tools import Tool, is_executable_file
from .installer import Integrations
from .legacy import Finding, LegacyMigration, LegacyProfile

# What a ~/.claude-* folder must contain to count as a Claude Code config folder.
CLAUDE_FOLDER_MARKERS = (".claude.json", ".credentials.json", "settings.json", "projects", "history.jsonl",
                         "todos", "statsig", "shell-snapshots")

# D6: the native installer for Claude Code; the distribution packages for git and gh.
INSTALL_COMMANDS = {
    Tool.GIT: "sudo apt install git",
    Tool.GH: "sudo apt install gh",
    Tool.CLAUDE: "curl -fsSL https://claude.ai/install.sh | bash",
}

PALETTE = [ProfileColor.INDIGO, ProfileColor.ORANGE, ProfileColor.TEAL, ProfileColor.GREEN, ProfileColor.BLUE,
           ProfileColor.PINK, ProfileColor.RED, ProfileColor.GRAPHITE]


def install_command(tool: Tool, platform=None) -> str:
    """Shown, never run: the app does not install tools (R-08 out of scope). On Termux,
    git and gh come from `pkg` and Claude Code from npm, pinned to the last version that
    runs on Android with the auto-updater off (claude/termux_support.py)."""
    if platform is None or not platform.is_termux:
        return INSTALL_COMMANDS[tool]
    if tool == Tool.CLAUDE:
        from ..claude.termux_support import PIN_COMMAND
        return f"{platform.install_command('nodejs-lts')} && {PIN_COMMAND}"
    return platform.install_command(tool.value)


@dataclass(frozen=True)
class ToolInfo:
    tool: Tool
    path: str | None
    version: str | None


@dataclass(frozen=True)
class DetectedClaudeFolder:
    # None = default ~/.claude.
    config_dir: str | None
    status: ClaudeLoginStatus | None


@dataclass
class DetectedSetup:
    tools: list[ToolInfo] = field(default_factory=list)
    git_name: str | None = None
    git_email: str | None = None
    github_logins: list[GitHubLogin] = field(default_factory=list)
    claude_folders: list[DetectedClaudeFolder] = field(default_factory=list)
    legacy_profiles: list[LegacyProfile] = field(default_factory=list)
    legacy_active_key: str | None = None
    legacy_findings: list[Finding] = field(default_factory=list)
    # The user's login shell, when it is one MultiCred integrates with.
    login_shell: ShellKind | None = None
    # Shells with an executable found on the search path, in ShellKind order.
    installed_shells: list[ShellKind] = field(default_factory=list)

    @property
    def has_legacy_install(self) -> bool:
        return bool(self.legacy_findings or self.legacy_profiles)

    def tool(self, tool: Tool) -> ToolInfo | None:
        return next((info for info in self.tools if info.tool == tool), None)


class SetupDetector:
    def __init__(self, environment, tools):
        self.environment = environment
        self.tools = tools

    def detect(self) -> DetectedSetup:
        setup = DetectedSetup()
        path = self.tools.search_directories()
        env = self.environment.base_process_environment(path)

        located = {tool: self.tools.locate(tool) for tool in Tool}
        for tool in Tool:
            executable = located[tool]
            version = None
            if executable is not None:
                result = self._run(CommandRequest(executable, ["--version"], env, timeout=10))
                if result is not None and result.succeeded:
                    lines = result.stdout.splitlines()
                    version = lines[0].strip() if lines and lines[0].strip() else None
            setup.tools.append(ToolInfo(tool, executable, version))

        git = located[Tool.GIT]
        if git is not None:
            setup.git_name = self._git_value(git, "user.name", env)
            setup.git_email = self._git_value(git, "user.email", env)

        gh = located[Tool.GH]
        if gh is not None:
            try:
                setup.github_logins = GitHubCLI(self.environment.runner, gh, env).logins()
            except Exception:  # gh missing a login, offline, unexpected output: onboarding goes on
                setup.github_logins = []

        setup.claude_folders = self.claude_folders()
        setup.legacy_profiles = LegacyMigration.import_profiles(self.environment)
        setup.legacy_active_key = LegacyMigration.active_profile_key(self.environment)
        setup.legacy_findings = LegacyMigration.findings(self.environment)
        setup.login_shell = shell_kind(self.environment.login_shell)
        setup.installed_shells = [shell for shell in ShellKind
                                  if any(is_executable_file(os.path.join(d, shell.value)) for d in path)]
        return setup

    def _run(self, request: CommandRequest):
        try:
            return self.environment.runner.run(request)
        except Exception:
            return None

    def _git_value(self, git: str, key: str, env: dict[str, str]) -> str | None:
        result = self._run(CommandRequest(git, ["config", "--global", "--includes", "--get", key], env,
                                          cwd=str(self.environment.home), timeout=10))
        if result is None or not result.succeeded:
            return None
        return result.stdout.strip() or None

    def claude_folders(self) -> list[DetectedClaudeFolder]:
        """`~/.claude` and the `~/.claude-*` folders that look like Claude Code config folders."""
        home = Path(self.environment.home)
        try:
            names = sorted(os.listdir(home))
        except OSError:
            return []
        found = []
        for name in names:
            if name != ".claude" and not name.startswith(".claude-"):
                continue
            folder = home / name
            if not folder.is_dir():
                continue
            if name != ".claude" and not any((folder / marker).exists() for marker in CLAUDE_FOLDER_MARKERS):
                continue
            config_dir = None if name == ".claude" else claude_paths.standardize(str(folder))
            status = status_from_state_file(ClaudeFolder.own_login(config_dir), self.environment)
            found.append(DetectedClaudeFolder(config_dir, status))
        return found


def shell_kind(login_shell: str) -> ShellKind | None:
    name = Path(login_shell).name
    return next((shell for shell in ShellKind if shell.value == name), None)


def default_shells(setup: DetectedSetup, environment) -> list[ShellKind]:
    """Shells pre-checked in step 3: installed ones that are the login shell or
    already have an rc file. Falls back to the first installed shell, then bash."""
    chosen = []
    for shell in ShellKind:
        if shell not in setup.installed_shells:
            continue
        has_rc = any(f.exists() for f in Integrations.rc_files(shell, environment))
        if shell == setup.login_shell or has_rc:
            chosen.append(shell)
    if chosen:
        return chosen
    return setup.installed_shells[:1] or [ShellKind.BASH]


def suggested_config(setup: DetectedSetup, environment) -> tuple[Config, object]:
    """Accounts and profiles to propose, plus the id of the profile to activate first.

    The legacy perfis.conf wins when present; otherwise one profile is built from
    the current global git identity, the active gh account and ~/.claude.
    Claude token accounts are added later by the user, so none are suggested.
    """
    config = Config()

    def github_account(username: str, nickname: str, host: str = "github.com"):
        for existing in config.github_accounts:
            if existing.username.lower() == username.lower() and existing.host == host:
                return existing.id
        spelled = next((l.login for l in setup.github_logins if l.login.lower() == username.lower()), username)
        account = GitHubAccount(nickname=nickname, username=spelled, host=host,
                                color=PALETTE[len(config.github_accounts) % len(PALETTE)])
        config.github_accounts.append(account)
        return account.id

    initial = None
    if setup.legacy_profiles:
        for index, legacy in enumerate(setup.legacy_profiles):
            color = PALETTE[index % len(PALETTE)]
            git_id = None
            if legacy.git_email:
                identity = GitIdentity(nickname=legacy.display_name, name=legacy.git_name or legacy.git_email,
                                       email=legacy.git_email, ssh_key_path=legacy.ssh_key, color=color)
                config.git_identities.append(identity)
                git_id = identity.id
            gh_id = github_account(legacy.github_user, legacy.display_name) if legacy.github_user else None
            profile = Profile(name=legacy.display_name, color=color, git_identity_id=git_id,
                              github_account_id=gh_id, claude_config_dir=legacy.claude_dir)
            config.profiles.append(profile)
            if legacy.key == setup.legacy_active_key:
                initial = profile.id
    else:
        git_id = None
        if setup.git_email:
            identity = GitIdentity(nickname="Principal", name=setup.git_name or setup.git_email,
                                   email=setup.git_email, color=ProfileColor.INDIGO)
            config.git_identities.append(identity)
            git_id = identity.id
        active = next((l for l in setup.github_logins if l.active), None)
        gh_id = github_account(active.login, active.login, active.host) if active else None
        profile = Profile(name="Principal", color=ProfileColor.INDIGO, git_identity_id=git_id, github_account_id=gh_id)
        config.profiles.append(profile)
        initial = profile.id

    # Every gh login becomes an account the user can pick later.
    for login in setup.github_logins:
        github_account(login.login, login.login, login.host)
    return config, initial or (config.profiles[0].id if config.profiles else None)
