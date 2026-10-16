"""Append-only log in ~/.local/state/multicred/multicred.log.

Callers pass known secrets in `redacting`; they are replaced before anything is
written. Rotation keeps multicred.log plus .1 and .2, each up to `max_bytes`.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

MASK = "••••"


def redact(text: str, secrets=()) -> str:
    """Replaces every occurrence of each non-empty secret with a mask."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


def redact_credential_like(text: str, secrets=()) -> str:
    """Also masks anything that looks like a credential (sk-ant-… or 40+ token characters)."""
    text = redact(text, secrets)
    text = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", MASK, text)
    return re.sub(r"[A-Za-z0-9_\-#]{40,}", MASK, text)


class EventLog:
    def __init__(self, directory, clock: Callable[[], datetime], max_bytes: int = 1_000_000):
        self.directory = Path(directory)
        self.clock = clock
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @property
    def current_file(self) -> Path:
        return self.directory / "multicred.log"

    def log(self, level: str, message: str, redacting=()) -> None:
        clean = redact(message, redacting).replace("\n", "\\n")
        stamp = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"{stamp} [{level}] {clean}\n".encode("utf-8")
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.current_file.exists() and self.current_file.stat().st_size + len(line) > self.max_bytes:
                    self._rotate()
                fd = os.open(self.current_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                pass  # logging must never break a switch

    def tail(self, count: int) -> list[str]:
        """Last `count` lines of the current file, for "Copiar diagnóstico"."""
        with self._lock:
            try:
                lines = self.current_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        return [line for line in lines if line][-count:]

    def _rotate(self) -> None:
        one, two = self.directory / "multicred.log.1", self.directory / "multicred.log.2"
        for source, target in ((one, two), (self.current_file, one)):
            if source.exists():
                os.replace(source, target)
