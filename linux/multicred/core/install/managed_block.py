"""The only text MultiCred adds to files it does not own (rc files, the global
git config) lives between these markers, so it can be found, replaced and
removed without touching anything else in the file.
"""

from __future__ import annotations

BEGIN = "# >>> MultiCred >>>"
END = "# <<< MultiCred <<<"


class Unterminated(ValueError):
    """A begin marker without an end marker (edited by hand)."""

    def __init__(self):
        super().__init__("MultiCred block without its end marker")


def _is(line: str, marker: str) -> bool:
    # Like Swift's .whitespaces: spaces and tabs, never newlines.
    return line.strip(" \t") == marker


def _blank(line: str) -> bool:
    return not line.strip(" \t")


def contains(text: str) -> bool:
    return any(_is(line, BEGIN) for line in text.split("\n"))


def body(text: str) -> str | None:
    """The block's inner lines, if present."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if _is(line, BEGIN)), None)
    if start is None:
        return None
    stop = next((i for i in range(start, len(lines)) if _is(lines[i], END)), None)
    if stop is None:
        return None
    return "\n".join(lines[start + 1:stop])


def remove(text: str) -> str:
    """Removes every block (and the blank line placed before it on insertion)."""
    lines = text.split("\n")
    while True:
        start = next((i for i, line in enumerate(lines) if _is(line, BEGIN)), None)
        if start is None:
            break
        stop = next((i for i in range(start, len(lines)) if _is(lines[i], END)), None)
        if stop is None:
            raise Unterminated()
        del lines[start:stop + 1]
        if 0 < start <= len(lines) and _blank(lines[start - 1]):
            del lines[start - 1]
    return "\n".join(lines)


def append_at_end(block_body: str, text: str) -> str:
    """Removes any existing block and appends a fresh one at the end. Running it
    twice gives the same text (idempotent)."""
    base = remove(text)
    while base.endswith("\n\n"):
        base = base[:-1]
    if base and not base.endswith("\n"):
        base += "\n"
    separator = "\n" if base else ""
    return base + separator + BEGIN + "\n" + block_body + "\n" + END + "\n"


def is_last(text: str) -> bool:
    """True when the block exists and only blank lines or comments follow it."""
    lines = text.split("\n")
    stops = [i for i, line in enumerate(lines) if _is(line, END)]
    if not stops:
        return False
    for line in lines[stops[-1] + 1:]:
        trimmed = line.strip(" \t")
        if trimmed and not trimmed.startswith("#") and not trimmed.startswith(";"):
            return False
    return True
