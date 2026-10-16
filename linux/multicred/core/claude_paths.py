"""Rules for Claude Code config directories (docs/spikes/claude-config-dir.md).

- Default mode (no CLAUDE_CONFIG_DIR) uses ~/.claude plus ~/.claude.json.
- On Linux, each folder keeps its login in `<folder>/.credentials.json`.
- Claude Code treats `/x` and `/x/` as different folders, so MultiCred always
  stores and exports one canonical form: absolute, normalized, no trailing
  slash, no `~`.
"""

from __future__ import annotations

import posixpath
import unicodedata
from pathlib import Path

from .validation import has_control_characters


class PathError(ValueError):
    pass


def standardize(path: str) -> str:
    normalized = posixpath.normpath(path)
    # POSIX keeps a leading "//"; Claude Code does not.
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def default_config_dir(environment) -> str:
    return standardize(f"{environment.home}/.claude")


def canonical_config_dir(text: str, environment) -> str | None:
    """Canonical directory for `text`, or None when it is the default ~/.claude
    (which must be used with CLAUDE_CONFIG_DIR unset, never set to it)."""
    trimmed = text.strip(" ")
    if not trimmed:
        raise PathError("empty")
    if has_control_characters(trimmed):
        raise PathError("controlCharacters")
    expanded = environment.expand_tilde(trimmed)
    if not expanded.startswith("/"):
        raise PathError(f"notAbsolute: {text}")
    canonical = standardize(expanded)
    return None if canonical == default_config_dir(environment) else canonical


def effective_directory(config_dir: str | None, environment) -> str:
    """The directory a folder actually uses on disk."""
    return config_dir if config_dir is not None else default_config_dir(environment)


def credentials_file(config_dir: str | None, environment) -> Path:
    """Where Claude Code on Linux keeps the folder's login (`claudeAiOauth`, `mcpOAuth`)."""
    return Path(effective_directory(config_dir, environment)) / ".credentials.json"


def state_file(config_dir: str | None, environment) -> Path:
    """`.claude.json` with `oauthAccount` (who is logged in). Default mode keeps it in HOME."""
    if config_dir is None:
        return Path(environment.home) / ".claude.json"
    return Path(config_dir) / ".claude.json"


def suggested_config_dir(nickname: str, environment) -> str:
    """Suggested folder for a new account: ~/.claude-<slug>."""
    folded = unicodedata.normalize("NFKD", nickname)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    mapped = "".join(ch if ch.isalnum() else "-" for ch in folded)
    slug = "-".join(part for part in mapped.split("-") if part)
    return standardize(f"{environment.home}/.claude-{slug or 'conta'}")
