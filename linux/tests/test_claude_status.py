"""S-04: Claude login status without reading tokens (ClaudeStatusTests in ClaudeTests.swift)."""

import dataclasses
import json
import os
import unittest
from datetime import timedelta

import tests.test_engine as base
from multicred.core.claude.status import ClaudeCLI, ClaudeLoginStatus, ClaudeStatusService, state_file, status_from_state_file
from multicred.core.models import ClaudeAccount, ProfileColor
from multicred.core.runner import CommandResult
from multicred.core.tools import Tool
from tests.support import FIXED_NOW, SafeTestCase


def account(folder):
    return ClaudeAccount(nickname="x", color=ProfileColor.TEAL, config_dir=folder)


class ParseStatusTests(unittest.TestCase):
    def test_parse_status_variants(self):
        def parse(text):
            return ClaudeCLI.parse_status(text.encode())
        self.assertEqual(parse('{"loggedIn": true, "email": "a@b.com"}'), ClaudeLoginStatus.logged_in("a@b.com"))
        self.assertEqual(parse('{"loggedIn": false}'), ClaudeLoginStatus.logged_out())
        self.assertEqual(parse('{"authenticated": true, "account": {"emailAddress": "c@d.com"}}'),
                         ClaudeLoginStatus.logged_in("c@d.com"))
        self.assertEqual(parse('{"oauthAccount": {"emailAddress": "e@f.com"}}'), ClaudeLoginStatus.logged_in("e@f.com"))
        self.assertIsNone(parse('{"something": "else"}'))
        self.assertIsNone(parse("not json"))
        self.assertIsNone(parse("[1]"))

    def test_real_claude_code_2_1_transcripts(self):
        """Shapes printed by `claude auth status --json` (Claude Code 2.1.x)."""
        logged_in = ('{\n  "loggedIn": true,\n  "authMethod": "claude.ai",\n  "apiProvider": "firstParty",\n'
                     '  "email": "dev@empresa.com.br",\n  "orgId": "6f0e…",\n  "orgName": "Empresa",\n'
                     '  "subscriptionType": "max"\n}\n')
        self.assertEqual(ClaudeCLI.parse_status(logged_in.encode()), ClaudeLoginStatus.logged_in("dev@empresa.com.br"))
        logged_out = '{\n  "loggedIn": false,\n  "authMethod": "none",\n  "apiProvider": "firstParty"\n}\n'
        self.assertEqual(ClaudeCLI.parse_status(logged_out.encode()), ClaudeLoginStatus.logged_out())
        token = '{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}'
        self.assertEqual(ClaudeCLI.parse_status(token.encode()), ClaudeLoginStatus.logged_in(None))

    def test_not_an_email_is_ignored(self):
        self.assertEqual(ClaudeCLI.parse_status(b'{"loggedIn": true, "email": "nobody"}'), ClaudeLoginStatus.logged_in(None))
        self.assertTrue(ClaudeLoginStatus.logged_in(None).is_logged_in)
        self.assertFalse(ClaudeLoginStatus.unknown("x").is_logged_in)


class ClaudeStatusTests(SafeTestCase):
    def test_state_file_location_and_oauth_entry(self):
        default = account(None)
        self.assertEqual(state_file(default, self.environment), self.home / ".claude.json")
        self.assertIsNone(status_from_state_file(default, self.environment))

        (self.home / ".claude.json").write_text('{"userID": "u", "oauthAccount": {"emailAddress": "me@x.com", "accountUuid": "1"}}')
        self.assertEqual(status_from_state_file(default, self.environment), ClaudeLoginStatus.logged_in("me@x.com"))

        folder = self.home / ".claude-x"
        folder.mkdir()
        (folder / ".claude.json").write_text('{"userID": "u"}')
        self.assertEqual(status_from_state_file(account(str(folder)), self.environment), ClaudeLoginStatus.logged_out())
        (folder / ".claude.json").write_text("{broken")
        self.assertIsNone(status_from_state_file(account(str(folder)), self.environment))

    def test_service_prefers_cli_and_fills_email_from_state_file(self):
        (self.home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "file@x.com"}}')
        self.runner.on("claude", ["auth", "status", "--json"], stdout='{"loggedIn": true}')
        service = ClaudeStatusService(self.environment, base.FakeTools())
        self.assertEqual(service.status(account(None)), ClaudeLoginStatus.logged_in("file@x.com"))

        request = self.runner.requests[0]
        self.assertEqual(request.executable, "/fake/bin/claude")
        self.assertEqual(request.arguments, ["auth", "status", "--json"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", request.environment, "default account runs without the variable")
        self.assertEqual(request.environment["HOME"], str(self.home))
        self.assertEqual(request.environment["NO_COLOR"], "1")
        self.assertEqual(request.environment["PATH"], "/fake/bin")

    def test_service_sets_config_dir_and_falls_back_on_unexpected_output(self):
        folder = self.home / ".claude-y"
        folder.mkdir()
        self.runner.on("claude", ["auth", "status"], stdout="Logged in (text, not json)")
        service = ClaudeStatusService(self.environment, base.FakeTools())
        status = service.status(account(str(folder)))
        self.assertEqual(status, ClaudeLoginStatus.unknown("unexpected output (exit 0)"),
                         "no state file: unknown, never 'logged out'")
        self.assertEqual(self.runner.requests[0].environment["CLAUDE_CONFIG_DIR"], str(folder))

    def test_failing_cli_falls_back_to_the_state_file(self):
        folder = self.home / ".claude-z"
        folder.mkdir()
        (folder / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "z@x.com"}}')
        self.runner.on("claude", ["auth", "status"], respond=lambda _r: CommandResult(1, b"", b"boom"))
        service = ClaudeStatusService(self.environment, base.FakeTools())
        self.assertEqual(service.status(account(str(folder))), ClaudeLoginStatus.logged_in("z@x.com"))

    def test_cli_that_cannot_start_is_unknown(self):
        service = ClaudeStatusService(self.environment, base.FakeTools())  # no rule: the fake raises LaunchFailed
        status = service.status(account(None))
        self.assertEqual(status.kind, "unknown")

    def test_service_caches_until_forced_or_expired(self):
        now = [FIXED_NOW]
        environment = dataclasses.replace(self.environment, clock=lambda: now[0])
        self.runner.on("claude", ["auth", "status"], stdout='{"loggedIn": false}')
        service = ClaudeStatusService(environment, base.FakeTools(), ttl=300)
        acct = account(None)
        self.assertIsNone(service.cached(acct.id))
        service.status(acct)
        service.status(acct)
        self.assertEqual(len(self.runner.requests), 1)
        self.assertEqual(service.cached(acct.id), ClaudeLoginStatus.logged_out())
        service.status(acct, force=True)
        self.assertEqual(len(self.runner.requests), 2)
        now[0] += timedelta(seconds=301)
        service.status(acct)
        self.assertEqual(len(self.runner.requests), 3)
        service.invalidate(acct.id)
        self.assertIsNone(service.cached(acct.id))
        service.status(acct)
        service.invalidate()
        self.assertIsNone(service.cached(acct.id))

    def test_without_claude_installed_uses_state_file_only(self):
        service = ClaudeStatusService(self.environment, base.FakeTools(installed=[Tool.GIT, Tool.GH]))
        self.assertEqual(service.status(account(None)), ClaudeLoginStatus.unknown("Claude Code not found"))
        self.assertEqual(self.runner.requests, [])
        (self.home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "f@x.com"}}')
        self.assertEqual(service.status(account(None), force=True), ClaudeLoginStatus.logged_in("f@x.com"))

    def test_logout_runs_claude_with_the_folder(self):
        folder = self.home / ".claude-w"
        folder.mkdir()
        self.runner.on("claude", ["auth", "logout"])
        ClaudeCLI(self.runner, "/fake/bin/claude", {"HOME": str(self.home)}).logout(account(str(folder)))
        request = self.runner.requests[0]
        self.assertEqual(request.arguments, ["auth", "logout"])
        self.assertEqual(request.environment, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(folder), "NO_COLOR": "1"})

    def test_status_never_reads_the_credentials_file(self):
        claude = self.home / ".claude"
        claude.mkdir()
        credentials = claude / ".credentials.json"
        credentials.write_text('{"claudeAiOauth": {"accessToken": "sk-ant-oat01-secret"}}')
        os.chmod(credentials, 0)  # reading it would fail
        status = ClaudeStatusService(self.environment, base.FakeTools(installed=[])).status(account(None))
        self.assertEqual(status.kind, "unknown")
        self.assertNotIn("sk-ant", json.dumps(dataclasses.asdict(status)))


if __name__ == "__main__":
    unittest.main()
