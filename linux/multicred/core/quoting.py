"""Quoting for text that ends up inside generated shell files or command previews."""

import re

_SAFE = re.compile(r"[A-Za-z0-9\-_./=:@%+,]+")


def single_quoted(value: str) -> str:
    """POSIX single-quote quoting: it's -> 'it'\\''s'. Nothing inside is expanded."""
    return "'" + value.replace("'", "'\\''") + "'"


def fish_single_quoted(value: str) -> str:
    """fish single quotes: only \\ and ' are special."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def quote_for_display(value: str) -> str:
    """Quotes only when needed, for logs and previews."""
    return value if _SAFE.fullmatch(value) else single_quoted(value)
