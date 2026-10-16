"""Shared test helpers: a temporary HOME that is never the real one, and fakes."""

import os
import pwd
import shutil
import stat
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from multicred.core.env import SystemEnvironment
from multicred.core.runner import CommandResult, LaunchFailed
from multicred.core.secrets import InMemorySecretStore
from multicred.core.claude.credentials import InMemoryClaudeCredentialStore

FIXED_NOW = datetime.fromtimestamp(1_790_000_000, tz=timezone.utc)


class FakeCommandRunner:
    """Scripted runner. Records every request; answers from the first matching rule."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rules = []
        self._requests = []

    @property
    def requests(self):
        with self._lock:
            return list(self._requests)

    def on(self, tool, prefix=(), stdout="", exit_code=0, stderr="", respond=None):
        prefix = list(prefix)

        def matches(request):
            return Path(request.executable).name == tool and request.arguments[: len(prefix)] == prefix

        if respond is None:
            def respond(_request, stdout=stdout, exit_code=exit_code, stderr=stderr):
                return CommandResult(exit_code, stdout.encode(), stderr.encode())

        with self._lock:
            self._rules.append((matches, respond))

    def run(self, request):
        with self._lock:
            self._requests.append(request)
            rule = next((r for m, r in self._rules if m(request)), None)
        if rule is None:
            raise LaunchFailed(Path(request.executable).name, f"no fake rule for {request.display_string}")
        return rule(request)


def real_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


class SafeTestCase(unittest.TestCase):
    """Every test gets a fresh temporary HOME and fake collaborators."""

    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="MultiCredTests-")).resolve()
        real = real_home()
        assert self.home != real and self.home not in real.parents, "Test HOME must never be the real home"
        self.addCleanup(shutil.rmtree, self.home, True)
        self.runner = FakeCommandRunner()
        self.secrets = InMemorySecretStore()
        self.claude_store = InMemoryClaudeCredentialStore()
        self.environment = SystemEnvironment(
            mode="test",
            home=self.home,
            login_shell="/bin/bash",
            user_name="tester",
            runner=self.runner,
            secrets=self.secrets,
            claude_credentials=self.claude_store,
            clock=lambda: FIXED_NOW,
        )

    def make_executable(self, relative, content="#!/bin/sh\n"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
        return path

    def read(self, path):
        return Path(path).read_text(encoding="utf-8")

    def mode_of(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)


def new_id():
    return uuid.uuid4()
