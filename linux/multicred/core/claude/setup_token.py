"""Reads the token printed by `claude setup-token` (port of SetupTokenParser in
ClaudeAccountEditor.swift).

The token is kept only to hand it to the Secret Service; the visible transcript
has it masked.
"""

from __future__ import annotations

import re

from ..log import redact
from .login_flow import LoginEvent, LoginOutputParser

# The token must be followed by something that is not a token character: a
# pseudo-terminal read can end in the middle of it, and the first 20 characters
# alone are not the token (the macOS parser took them).
TOKEN_PATTERN = re.compile(r"sk-ant-oat[0-9]{2}-[A-Za-z0-9_\-]{20,}(?=[^A-Za-z0-9_\-])")


class SetupTokenParser:
    def __init__(self):
        self._login = LoginOutputParser()
        self.token: str | None = None

    def consume(self, data: bytes) -> list[LoginEvent]:
        """Login events (browser, code prompt, success, failure), as for `claude auth login`."""
        events = self._login.consume(data)
        if self.token is None:
            match = TOKEN_PATTERN.search(self._login.transcript)
            if match:
                self.token = match.group(0)
        return events

    @property
    def transcript(self) -> str:
        """Raw visible text (may contain the token). Never shown or logged as is."""
        return self._login.transcript

    @property
    def masked_transcript(self) -> str:
        return redact(self._login.transcript, [self.token] if self.token else [])
