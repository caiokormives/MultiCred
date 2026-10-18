"""The shell-script version MultiCred replaces (`~/Perfis`: trocar-perfil.sh,
claude-env.sh, instalar.sh, perfis.conf).

Its rc line and git include would fight with MultiCred's, so onboarding offers
to remove them, and its perfis.conf becomes suggested profiles. Nothing is ever
executed or sourced: perfis.conf is parsed as plain `KEY="value"` text and only
`$HOME`, `${HOME}` and a leading `~` are expanded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import claude_paths

_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
_SUFFIXES = ("_GIT_NOME", "_GIT_EMAIL", "_GH_USUARIO", "_SSH_CHAVE", "_CLAUDE_DIR")


@dataclass(frozen=True)
class Finding:
    file: Path
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LegacyProfile:
    key: str
    git_name: str | None = None
    git_email: str | None = None
    github_user: str | None = None
    ssh_key: str | None = None
    # None = default Claude account (~/.claude).
    claude_dir: str | None = None

    @property
    def display_name(self) -> str:
        return self.key[:1].upper() + self.key[1:].lower()


def _trim(line: str) -> str:
    return line.strip(" \t")


class LegacyMigration:
    @staticmethod
    def folder(environment) -> Path:
        return Path(environment.home) / "Perfis"

    @staticmethod
    def is_legacy_rc_line(line: str) -> bool:
        return "Perfis/claude-env.sh" in line

    @staticmethod
    def is_legacy_include_path(line: str) -> bool:
        trimmed = _trim(line)
        return trimmed.startswith("path") and "Perfis/git-atual.gitconfig" in trimmed

    @staticmethod
    def findings(environment) -> list[Finding]:
        """Lines from the legacy install still present in rc files and the global git config."""
        from .installer import Integrations
        found = []
        for file in Integrations.all_block_files(environment):
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [line for line in text.split("\n")
                     if LegacyMigration.is_legacy_rc_line(line) or LegacyMigration.is_legacy_include_path(line)]
            if lines:
                found.append(Finding(file, lines))
        return found

    @staticmethod
    def remove_legacy_lines(text: str) -> str:
        """Removes the legacy rc line (and the comment instalar.sh put above it) and
        the legacy `[include]` section. Everything else stays byte for byte."""
        lines = text.split("\n")
        index = 0
        while index < len(lines):
            line = lines[index]
            if LegacyMigration.is_legacy_rc_line(line):
                del lines[index]
                if index > 0 and _trim(lines[index - 1]).startswith("# Perfis"):
                    del lines[index - 1]
                    index -= 1
                index = _remove_blank_before(lines, index)
                continue
            if LegacyMigration.is_legacy_include_path(line):
                del lines[index]
                header = index - 1
                section_now_empty = (index >= len(lines) or _trim(lines[index]).startswith("[")
                                     or not _trim(lines[index]))
                if header >= 0 and _trim(lines[header]).lower() == "[include]" and section_now_empty:
                    del lines[header]
                    index = _remove_blank_before(lines, header)
                continue
            index += 1
        return "\n".join(lines)

    @staticmethod
    def import_profiles(environment) -> list[LegacyProfile]:
        """Profiles from `~/Perfis/perfis.conf`, in file order."""
        try:
            text = (LegacyMigration.folder(environment) / "perfis.conf").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return LegacyMigration.profiles_from_conf(text, environment)

    @staticmethod
    def active_profile_key(environment) -> str | None:
        """The legacy active profile (`~/Perfis/atual`), upper-cased like the keys."""
        try:
            text = (LegacyMigration.folder(environment) / "atual").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return text.strip().upper() or None

    @staticmethod
    def profiles_from_conf(text: str, environment) -> list[LegacyProfile]:
        values = LegacyMigration.parse_conf(text)
        order: list[str] = []
        for key, _ in values:
            suffix = next((s for s in _SUFFIXES if key.endswith(s)), None)
            if suffix is None:
                continue
            prefix = key[:-len(suffix)]
            if prefix and prefix not in order:
                order.append(prefix)

        def value(prefix: str, suffix: str) -> str | None:
            matches = [v for k, v in values if k == prefix + suffix]
            if not matches:
                return None
            expanded = LegacyMigration.expand(matches[-1], environment)
            return expanded or None

        profiles = []
        for prefix in order:
            claude_dir = value(prefix, "_CLAUDE_DIR")
            if claude_dir is not None:
                try:
                    claude_dir = claude_paths.canonical_config_dir(claude_dir, environment)
                except claude_paths.PathError:
                    claude_dir = None
            profiles.append(LegacyProfile(key=prefix, git_name=value(prefix, "_GIT_NOME"),
                                          git_email=value(prefix, "_GIT_EMAIL"),
                                          github_user=value(prefix, "_GH_USUARIO"),
                                          ssh_key=value(prefix, "_SSH_CHAVE"), claude_dir=claude_dir))
        return profiles

    @staticmethod
    def parse_conf(text: str) -> list[tuple[str, str]]:
        """`KEY="value"` / `KEY='value'` / `KEY=value` lines; comments and anything
        else are ignored. Values are never evaluated."""
        parsed = []
        for raw in text.split("\n"):
            line = _trim(raw)
            if line.startswith("#") or "=" not in line:
                continue
            key, rest = line.split("=", 1)
            if not _KEY.fullmatch(key):
                continue
            rest = _trim(rest)
            if rest[:1] in ("\"", "'"):
                quote, rest = rest[0], rest[1:]
                close = rest.find(quote)
                if close < 0:
                    continue
                parsed.append((key, rest[:close]))
                continue
            comment = rest.find("#")
            if comment >= 0:
                rest = rest[:comment]
            parsed.append((key, _trim(rest)))
        return parsed

    @staticmethod
    def expand(value: str, environment) -> str:
        home = str(environment.home)
        return environment.expand_tilde(value.replace("${HOME}", home).replace("$HOME", home))


def _remove_blank_before(lines: list[str], index: int) -> int:
    if 0 < index <= len(lines) and not _trim(lines[index - 1]):
        del lines[index - 1]
        return index - 1
    return index
