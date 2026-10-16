"""What a Claude Code config folder contains, so the user can tell folders apart.

Read locally and never sent anywhere. Only metadata is read: the login e-mail
from `.claude.json` and, per conversation, its title, project folder and date.
No tokens, no message bodies.

Claude Code layout (2.1.x): `<folder>/projects/<project>/<session>.jsonl`, one
JSON object per line. Titles come from `custom-title`, then `ai-title`, then
`last-prompt` lines.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import claude_paths

UNTITLED = "(sem título)"
_TITLE_MARKERS = (b'"custom-title"', b'"ai-title"', b'"last-prompt"')


@dataclass(frozen=True)
class Conversation:
    title: str
    # Project folder the conversation ran in (`cwd`), abbreviated.
    project: str | None
    last_activity: datetime


@dataclass(frozen=True)
class ClaudeFolderSummary:
    # None = the default ~/.claude.
    config_dir: str | None
    path: str
    email: str | None
    organization: str | None
    project_count: int
    conversation_count: int
    last_activity: datetime | None
    recent_conversations: list[Conversation] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)


def summarize(config_dir: str | None, environment, limit: int = 8) -> ClaudeFolderSummary:
    path = config_dir if config_dir is not None else claude_paths.default_config_dir(environment)
    email, organization = login_identity(claude_paths.state_file(config_dir, environment))

    projects = Path(path) / "projects"
    try:
        project_folders = [p for p in projects.iterdir() if p.is_dir()]
    except OSError:
        project_folders = []

    sessions: list[tuple[Path, datetime]] = []
    for folder in project_folders:
        try:
            files = list(folder.iterdir())
        except OSError:
            continue
        for file in files:
            if file.suffix != ".jsonl":
                continue
            try:
                modified = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            except OSError:
                modified = datetime.min.replace(tzinfo=timezone.utc)
            sessions.append((file, modified))
    sessions.sort(key=lambda entry: entry[1], reverse=True)

    recent = []
    for file, modified in sessions[:limit]:
        title, cwd = conversation_info(file)
        recent.append(Conversation(title or UNTITLED, environment.abbreviate(cwd) if cwd else None, modified))
    return ClaudeFolderSummary(config_dir=config_dir, path=path, email=email, organization=organization,
                               project_count=len(project_folders), conversation_count=len(sessions),
                               last_activity=sessions[0][1] if sessions else None, recent_conversations=recent)


def discover_folders(environment) -> list[str | None]:
    """`~/.claude` (None) and every `~/.claude-*` folder."""
    try:
        names = sorted(os.listdir(environment.home))
    except OSError:
        return []
    found: list[str | None] = []
    for name in names:
        if name != ".claude" and not name.startswith(".claude-"):
            continue
        full = Path(environment.home) / name
        if full.is_dir():
            found.append(None if name == ".claude" else claude_paths.standardize(str(full)))
    return found


def login_identity(state_file: Path) -> tuple[str | None, str | None]:
    try:
        data = json.loads(Path(state_file).read_bytes())
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None, None
    email, organization = oauth.get("emailAddress"), oauth.get("organizationName")
    return (email if isinstance(email, str) else None, organization if isinstance(organization, str) else None)


def conversation_info(file: Path) -> tuple[str | None, str | None]:
    """Title and project of one session file. Only the few metadata line types are
    decoded; message lines are skipped without parsing."""
    custom = ai_title = last_prompt = cwd = None
    try:
        handle = open(file, "rb")
    except OSError:
        return None, None
    with handle:
        for line in handle:
            is_title = any(marker in line for marker in _TITLE_MARKERS)
            needs_cwd = cwd is None and b'"cwd"' in line
            if not (is_title or needs_cwd):
                continue
            try:
                obj = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "custom-title" and isinstance(obj.get("customTitle"), str):
                custom = obj["customTitle"]
            elif kind == "ai-title" and isinstance(obj.get("aiTitle"), str):
                ai_title = obj["aiTitle"]
            elif kind == "last-prompt" and isinstance(obj.get("lastPrompt"), str):
                last_prompt = obj["lastPrompt"]
            if cwd is None and isinstance(obj.get("cwd"), str):
                cwd = obj["cwd"]
    candidates = [custom, ai_title, _shorten(last_prompt) if last_prompt is not None else None]
    title = next((c.strip() for c in candidates if c is not None and c.strip()), None)
    return title, cwd


def _shorten(text: str) -> str:
    flat = text.replace("\n", " ")
    return flat[:80] + "…" if len(flat) > 80 else flat
