"""Every file MultiCred owns, derived from the injected home (docs/PLAN.md §4.2)."""

from __future__ import annotations

from pathlib import Path


class AppPaths:
    def __init__(self, environment):
        self.root: Path = environment.app_support_directory

    @property
    def config(self) -> Path: return self.root / "config.json"

    @property
    def state(self) -> Path: return self.root / "state.json"

    @property
    def generation(self) -> Path:
        """A single integer; shell hooks re-source the env file when it changes."""
        return self.root / "generation"

    @property
    def gitconfig(self) -> Path: return self.root / "gitconfig"

    @property
    def env_sh(self) -> Path: return self.root / "env.sh"

    @property
    def env_fish(self) -> Path: return self.root / "env.fish"

    @property
    def shell_directory(self) -> Path: return self.root / "shell"

    @property
    def backups(self) -> Path: return self.root / "backups"

    @property
    def manifest(self) -> Path: return self.root / "manifest.json"

    @property
    def claude_folders(self) -> Path:
        """Which login each Claude folder holds (D39)."""
        return self.root / "claude-folders.json"

    def hook(self, shell) -> Path:
        return self.shell_directory / f"hook.{shell.value}"
