"""Whether Claude Code adds itself as co-author ("Co-Authored-By: Claude") to commits and PRs (D38).

Claude Code only reads this from settings (no environment variable):
`"attribution": {"commit": "", "pr": ""}` hides it. MultiCred writes that exact
value into the profile's Claude folder `settings.json` when the profile's
GitHub account opts out, and removes only that exact value when it opts back
in, so attribution the user wrote by hand is kept.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..claude_paths import default_config_dir

# The value MultiCred writes to hide attribution.
HIDDEN = {"commit": "", "pr": ""}


class UnreadableSettings(ValueError):
    """settings.json exists but is not a JSON object; MultiCred leaves it alone."""

    def __init__(self):
        super().__init__("settings.json is not a JSON object")


def settings_file(config_dir: str | None, environment) -> Path:
    return Path(config_dir or default_config_dir(environment)) / "settings.json"


def _is_hidden_value(value) -> bool:
    return isinstance(value, dict) and len(value) == 2 and value.get("commit") == "" and value.get("pr") == ""


def updated(current: bytes | None, co_author: bool) -> bytes | None:
    """New settings.json content, or None when nothing has to change.

    The user's key order is kept (the macOS app sorts keys; here a diff of the
    file shows only the attribution change).
    """
    settings: dict = {}
    if current is not None and current.strip(b" \t\r\n"):
        try:
            parsed = json.loads(current)
        except (ValueError, UnicodeDecodeError):
            raise UnreadableSettings() from None
        if not isinstance(parsed, dict):
            raise UnreadableSettings()
        settings = parsed

    if co_author:
        # Opt back in: undo only what MultiCred wrote.
        if not _is_hidden_value(settings.get("attribution")):
            return None
        del settings["attribution"]
    else:
        if _is_hidden_value(settings.get("attribution")):
            return None
        settings["attribution"] = dict(HIDDEN)
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
