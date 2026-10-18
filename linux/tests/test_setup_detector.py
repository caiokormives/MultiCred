"""S-05: what onboarding detects (tools, git identity, gh logins, Claude folders, legacy, shells)."""

import dataclasses
import json
import os
import tempfile
from pathlib import Path

from multicred.core.adapters.github_cli import GitHubLogin
from multicred.core.install.legacy import LegacyProfile
from multicred.core.install.setup_detector import (
    DetectedSetup, SetupDetector, default_shells, install_command, suggested_config,
)
from multicred.core.models import ShellKind
from multicred.core.runner import DryRunCommandRunner
from multicred.core.tools import Tool
from multicred.core.validation import issues_in
from tests.support import FIXED_NOW, SafeTestCase

GH_STATUS = json.dumps({"hosts": {"github.com": [
    {"host": "github.com", "login": "rafael-npu", "active": True, "state": "success"},
    {"host": "github.com", "login": "rafaelb", "active": False, "state": "success"},
]}})


class DirectoryTools:
    """Tools found in one temporary directory, like ToolLocator over a fixed search path."""

    def __init__(self, directory, installed=(Tool.GIT, Tool.GH, Tool.CLAUDE)):
        self.directory = str(directory)
        self.installed = set(installed)

    def locate(self, tool):
        return f"{self.directory}/{tool.value}" if tool in self.installed else None

    def search_directories(self):
        return [self.directory]


class SetupDetectorTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.home / "bin"
        self.make_executable("bin/bash")
        self.tools = DirectoryTools(self.bin)

    def answer_everything(self):
        self.runner.on("git", ["--version"], stdout="git version 2.43.0\n")
        self.runner.on("gh", ["--version"], stdout="gh version 2.45.0 (2024-03-04)\nhttps://github.com/cli/cli\n")
        self.runner.on("claude", ["--version"], stdout="2.1.288 (Claude Code)\n")
        self.runner.on("git", ["config", "--global", "--includes", "--get", "user.name"], stdout="Rafael\n")
        self.runner.on("git", ["config", "--global", "--includes", "--get", "user.email"], stdout="r@npu.com.br\n")
        self.runner.on("gh", ["auth", "status", "--json", "hosts"], stdout=GH_STATUS)

    def test_detects_tools_git_github_and_claude_folders(self):
        for name in [".claude", ".claude-pessoal", ".claude-vazia", ".claude-notes"]:
            (self.home / name).mkdir()
        (self.home / ".claude-pessoal/.claude.json").write_text('{"oauthAccount":{"emailAddress":"p@x.com"}}')
        (self.home / ".claude-vazia/settings.json").write_text("{}")
        (self.home / ".claude.json").write_text("{}")
        (self.home / ".claude-file").write_text("not a folder")
        self.answer_everything()

        setup = SetupDetector(self.environment, self.tools).detect()

        self.assertEqual([t.version for t in setup.tools], ["git version 2.43.0", "gh version 2.45.0 (2024-03-04)",
                                                            "2.1.288 (Claude Code)"])
        self.assertEqual(setup.tool(Tool.GH).path, f"{self.bin}/gh")
        self.assertEqual((setup.git_name, setup.git_email), ("Rafael", "r@npu.com.br"))
        self.assertEqual([l.login for l in setup.github_logins], ["rafael-npu", "rafaelb"])
        self.assertEqual([f.config_dir for f in setup.claude_folders],
                         [None, f"{self.home}/.claude-pessoal", f"{self.home}/.claude-vazia"],
                         "a ~/.claude-* folder with nothing of Claude Code inside is not a Claude folder")
        self.assertEqual(setup.claude_folders[0].status.kind, "loggedOut")
        self.assertEqual(setup.claude_folders[1].status.email, "p@x.com")
        self.assertFalse(setup.has_legacy_install)

        git_config = next(r for r in self.runner.requests if r.arguments[:2] == ["config", "--global"])
        self.assertEqual(git_config.cwd, str(self.home))
        self.assertEqual(git_config.environment["HOME"], str(self.home))
        self.assertEqual(git_config.environment["PATH"], str(self.bin))

    def test_suggested_config_from_detection(self):
        self.answer_everything()
        setup = SetupDetector(self.environment, self.tools).detect()
        config, initial = suggested_config(setup, self.environment)
        self.assertEqual(issues_in(config), [])
        self.assertEqual([p.name for p in config.profiles], ["Principal"])
        self.assertEqual(initial, config.profiles[0].id)
        identity = config.git_identity(config.profiles[0].git_identity_id)
        self.assertEqual((identity.name, identity.email), ("Rafael", "r@npu.com.br"))
        self.assertEqual(config.github_account(config.profiles[0].github_account_id).username, "rafael-npu")
        self.assertEqual(len(config.github_accounts), 2)
        self.assertIsNone(config.profiles[0].claude_config_dir, "the main profile uses ~/.claude")
        self.assertEqual(config.claude_accounts, [], "token accounts are added by the user later")

    def test_missing_tools_and_failing_commands_are_tolerated(self):
        self.runner.on("git", ["--version"], exit_code=1)
        self.runner.on("git", ["config"], exit_code=1)
        tools = DirectoryTools(self.bin, installed=(Tool.GIT, Tool.GH))
        # No rule for gh: the fake runner raises, like a gh that cannot start.
        setup = SetupDetector(self.environment, tools).detect()
        self.assertEqual([(t.tool, t.path, t.version) for t in setup.tools],
                         [(Tool.GIT, f"{self.bin}/git", None), (Tool.GH, f"{self.bin}/gh", None), (Tool.CLAUDE, None, None)])
        self.assertIsNone(setup.git_email)
        self.assertEqual(setup.github_logins, [])
        config, initial = suggested_config(setup, self.environment)
        self.assertEqual(issues_in(config), [])
        self.assertEqual(config.profiles[0].git_identity_id, None)
        self.assertEqual(initial, config.profiles[0].id)

    def test_shells_login_and_installed(self):
        self.make_executable("bin/fish")
        (self.bin / "zsh").write_text("not executable")
        self.runner.on("git", exit_code=1)
        self.runner.on("gh", exit_code=1)
        self.runner.on("claude", exit_code=1)
        setup = SetupDetector(self.environment, self.tools).detect()
        self.assertEqual(setup.login_shell, ShellKind.BASH)
        self.assertEqual(setup.installed_shells, [ShellKind.BASH, ShellKind.FISH])
        self.assertEqual(default_shells(setup, self.environment), [ShellKind.BASH])

        fish_user = dataclasses.replace(self.environment, login_shell="/usr/bin/fish")
        setup = SetupDetector(fish_user, self.tools).detect()
        self.assertEqual(setup.login_shell, ShellKind.FISH)
        self.assertEqual(default_shells(setup, fish_user), [ShellKind.FISH])

    def test_default_shells_add_installed_shells_that_have_an_rc_file(self):
        setup = DetectedSetup(login_shell=ShellKind.BASH, installed_shells=[ShellKind.ZSH, ShellKind.BASH])
        (self.home / ".zshrc").write_text("# mine\n")
        self.assertEqual(default_shells(setup, self.environment), [ShellKind.ZSH, ShellKind.BASH])
        not_installed = DetectedSetup(login_shell=ShellKind.BASH, installed_shells=[ShellKind.BASH])
        self.assertEqual(default_shells(not_installed, self.environment), [ShellKind.BASH])

    def test_legacy_install_is_reported_and_its_profiles_win(self):
        folder = self.home / "Perfis"
        folder.mkdir()
        (folder / "perfis.conf").write_text('TRABALHO_GIT_EMAIL="r@npu.com.br"\nTRABALHO_GH_USUARIO="NPU-Rafael"\n')
        (self.home / ".bashrc").write_text('[ -f "$HOME/Perfis/claude-env.sh" ] && source "$HOME/Perfis/claude-env.sh"\n')
        self.runner.on("git", exit_code=1)
        self.runner.on("gh", exit_code=1)
        self.runner.on("claude", exit_code=1)
        setup = SetupDetector(self.environment, self.tools).detect()
        self.assertTrue(setup.has_legacy_install)
        self.assertEqual([f.file.name for f in setup.legacy_findings], [".bashrc"])
        self.assertEqual([p.key for p in setup.legacy_profiles], ["TRABALHO"])

    def test_legacy_profiles_win_and_active_profile_is_kept(self):
        setup = DetectedSetup(
            legacy_profiles=[
                LegacyProfile(key="TRABALHO", git_name="NPU", git_email="r@npu.com.br", github_user="NPU-Rafael"),
                LegacyProfile(key="PESSOAL", git_name="Rafael", git_email="r@gmail.com", github_user="Rafael-L",
                              claude_dir=f"{self.home}/.claude-pessoal"),
            ],
            legacy_active_key="PESSOAL",
            github_logins=[GitHubLogin("github.com", "rafael-l", True, "success")],
        )
        config, initial = suggested_config(setup, self.environment)
        self.assertEqual([p.name for p in config.profiles], ["Trabalho", "Pessoal"])
        self.assertEqual(initial, config.profiles[1].id)
        self.assertEqual(config.github_account(config.profiles[1].github_account_id).username, "rafael-l", "gh's spelling")
        self.assertEqual([p.claude_config_dir for p in config.profiles], [None, f"{self.home}/.claude-pessoal"])
        self.assertEqual([p.claude_account_id for p in config.profiles], [None, None])
        self.assertEqual(issues_in(config), [])

    def test_install_commands_for_linux(self):
        self.assertEqual(install_command(Tool.CLAUDE), "curl -fsSL https://claude.ai/install.sh | bash")
        self.assertEqual(install_command(Tool.GIT), "sudo apt install git")
        self.assertEqual(install_command(Tool.GH), "sudo apt install gh")

    def test_test_mode_touches_nothing_real(self):
        """With the app's dry-run runner nothing runs: every command is only recorded."""
        sandbox = Path(tempfile.mkdtemp(prefix="multicred-detect-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(sandbox, True))
        runner = DryRunCommandRunner(lambda: FIXED_NOW)
        environment = dataclasses.replace(self.environment, mode="dryRun", home=sandbox, runner=runner)
        before = sorted(os.listdir(sandbox))
        setup = SetupDetector(environment, self.tools).detect()
        self.assertEqual(setup.git_email, None)
        self.assertEqual([t.version for t in setup.tools], [None, None, None])
        self.assertEqual(setup.github_logins, [])
        self.assertTrue(runner.entries, "commands were recorded instead of run")
        self.assertEqual(sorted(os.listdir(sandbox)), before)
