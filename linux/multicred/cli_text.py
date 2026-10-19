"""Input rules both the command dispatcher (cli.py) and the menus (cli_menus.py) use.

It imports neither: cli.py loads cli_menus only when a menu command runs, and the menus
must not pull the dispatcher back in at import time.
"""

from __future__ import annotations


def choice_index(text: str, count: int, first: int = 1) -> int | None:
    """The 0-based position of a typed number among `count` choices numbered from `first`,
    or None. Only ASCII 0-9: str.isdigit() also takes "²" (int() then raises) and
    int() reads "١" or "１" as 1, which nobody typing a menu number means."""
    if not (text.isascii() and text.isdecimal()):
        return None
    index = int(text) - first
    return index if 0 <= index < count else None
