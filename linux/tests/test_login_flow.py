"""`claude auth login` in the pseudo-terminal: parser, state machine and the login session.

Ports LoginOutputParserTests / LoginStateMachineTests (ClaudeTests.swift) and
LoginEndToEndTests.swift. The session runs the LoginSimulator (or a fake
`claude` script inside the temporary HOME), never the real Claude Code.
"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from multicred.core.claude.credentials import FileClaudeCredentialStore
from multicred.core.claude.login_flow import (
    LOGIN_TIMEOUT, ClaudeAccountSaver, ClaudeLoginSession, LoginEvent, LoginMode, LoginOutputParser, LoginPhase,
    LoginStateMachine, NewClaudeAccount, strip_escapes,
)
from multicred.core.claude.login_simulator import LoginSimulator, SetupTokenSimulator
from multicred.core.claude.login_vault import ClaudeLoginRecord
from multicred.core.env import SystemEnvironment
from multicred.core.models import ClaudeAccount, ClaudeAccountKind, Config, ProfileColor, uuid_text
from multicred.core.pty import PseudoTerminalProcess
from multicred.core.storage import ConfigRepository

from tests.login_support import SerialDispatcher, ToolsAt, wait_until
from tests.support import SafeTestCase

AUTH_URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"
# Shape of the real transcript from the Phase 1 spike: OSC 8 hyperlink, colors,
# prompt without newline, success on the same line.
REAL_TRANSCRIPT = ("Opening browser to sign in…\r\n"
                   f"If the browser didn't open, visit: \x1b]8;;{AUTH_URL}\x07"
                   f"\x1b[94m{AUTH_URL}\x1b[39m\x1b]8;;\x07\r\n"
                   "Paste code here if prompted > ")


class LoginOutputParserTests(unittest.TestCase):
    def test_real_transcript_produces_events_in_order(self):
        parser = LoginOutputParser()
        events = parser.consume(REAL_TRANSCRIPT.encode())
        self.assertEqual(events, [LoginEvent.opening_browser(), LoginEvent.authorization_url(AUTH_URL),
                                  LoginEvent.prompting_for_code()])
        self.assertEqual(parser.consume(b"Login successful.\r\n"), [LoginEvent.succeeded()])
        self.assertNotIn("\x1b", parser.transcript)

    def test_events_split_across_chunks_fire_once(self):
        parser = LoginOutputParser()
        events = []
        for byte in REAL_TRANSCRIPT.encode():
            events += parser.consume(bytes([byte]))
        events += parser.consume(b"Paste")
        self.assertEqual(events.count(LoginEvent.prompting_for_code()), 1)
        self.assertEqual(events[0], LoginEvent.opening_browser())
        # Linux port: escapes and URLs cut between reads are reassembled.
        self.assertIn(LoginEvent.authorization_url(AUTH_URL), events)
        self.assertEqual(parser.transcript, strip_escapes(REAL_TRANSCRIPT) + "Paste")

    def test_failure_line(self):
        parser = LoginOutputParser()
        events = parser.consume(b"Paste code here if prompted > \r\nError: Invalid code or expired\r\n")
        self.assertEqual(events, [LoginEvent.prompting_for_code(), LoginEvent.failed("Error: Invalid code or expired")])

    def test_failure_words_inside_urls_are_ignored(self):
        # Regression: the URL host "example.invalid" was read as a failure.
        parser = LoginOutputParser()
        url = "https://example.invalid/auth?error_uri=x&denied=0"
        events = parser.consume(f"If the browser didn't open, visit: {url}\r\n".encode())
        self.assertEqual(events, [LoginEvent.authorization_url(url)])

    def test_strip_escapes(self):
        self.assertEqual(strip_escapes("\x1b[1;32mok\x1b[0m \x1b]0;title\x1b\\done\x07"), "ok done")

    def test_multibyte_characters_split_across_reads(self):
        parser = LoginOutputParser()
        data = "Opening browser to sign in…\r\n".encode()
        cut = data.index("…".encode()) + 1
        parser.consume(data[:cut])
        parser.consume(data[cut:])
        self.assertEqual(parser.transcript, "Opening browser to sign in…\r\n")


class LoginStateMachineTests(unittest.TestCase):
    def test_full_manual_code_flow(self):
        machine = LoginStateMachine()
        machine.handle(LoginEvent.opening_browser())
        self.assertEqual(machine.phase, LoginPhase.WAITING_FOR_BROWSER)
        machine.handle(LoginEvent.authorization_url("https://example.com/a"))
        machine.handle(LoginEvent.prompting_for_code())
        self.assertEqual(machine.phase, LoginPhase.WAITING_FOR_CODE)
        machine.code_submitted()
        self.assertEqual(machine.phase, LoginPhase.CONNECTING)
        machine.handle(LoginEvent.succeeded())
        self.assertEqual(machine.phase, LoginPhase.CONNECTED)
        self.assertEqual(machine.authorization_url, "https://example.com/a")

    def test_browser_callback_completes_without_code(self):
        # D26: with the browser already logged in, the CLI finishes on its own.
        machine = LoginStateMachine()
        machine.handle(LoginEvent.opening_browser())
        machine.handle(LoginEvent.prompting_for_code())
        machine.handle(LoginEvent.succeeded())
        self.assertEqual(machine.phase, LoginPhase.CONNECTED)

    def test_final_states_absorb_later_events(self):
        machine = LoginStateMachine()
        machine.handle(LoginEvent.failed("Invalid code"))
        machine.handle(LoginEvent.succeeded())
        machine.process_exited(0)
        self.assertEqual(machine.phase, LoginPhase.failed("Invalid code"))

        connected = LoginStateMachine()
        connected.handle(LoginEvent.succeeded())
        connected.process_exited(1)
        connected.timed_out()
        self.assertEqual(connected.phase, LoginPhase.CONNECTED)

    def test_exit_timeout_and_cancel(self):
        exited = LoginStateMachine()
        exited.process_exited(1)
        self.assertEqual(exited.phase, LoginPhase.failed("processo encerrado (código 1)"))

        clean_exit = LoginStateMachine()
        clean_exit.handle(LoginEvent.prompting_for_code())
        clean_exit.process_exited(0)
        self.assertEqual(clean_exit.phase, LoginPhase.CONNECTED)

        timeout = LoginStateMachine()
        timeout.timed_out()
        self.assertEqual(timeout.phase, LoginPhase.failed("tempo esgotado"))

        cancel = LoginStateMachine()
        cancel.cancelled()
        self.assertEqual(cancel.phase, LoginPhase.failed("cancelado"))

    def test_code_ignored_before_the_prompt_or_after_the_end(self):
        machine = LoginStateMachine()
        machine.code_submitted()
        self.assertEqual(machine.phase, LoginPhase.STARTING)
        machine.handle(LoginEvent.succeeded())
        machine.code_submitted()
        self.assertEqual(machine.phase, LoginPhase.CONNECTED)

    def test_phase_helpers(self):
        self.assertTrue(LoginPhase.WAITING_FOR_CODE.accepts_code)
        self.assertTrue(LoginPhase.WAITING_FOR_BROWSER.accepts_code)
        self.assertFalse(LoginPhase.CONNECTING.accepts_code)
        self.assertTrue(LoginPhase.failed("x").is_finished)
        self.assertTrue(LoginPhase.failed("x").is_failure)
        self.assertEqual(LOGIN_TIMEOUT, 600)


class SimulatorThroughPTYTests(SafeTestCase):
    """LoginSimulator → real pseudo-terminal → parser → state machine (LoginEndToEndTests)."""

    def run_simulator(self, code, config_dir):
        parser, machine = LoginOutputParser(), LoginStateMachine()
        dispatch = SerialDispatcher()
        environment = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        if config_dir is not None:
            environment["CLAUDE_CONFIG_DIR"] = config_dir
        pty = PseudoTerminalProcess()
        status = {}

        def output(data):
            for event in parser.consume(data):
                machine.handle(event)

        def exited(code_):
            status["code"] = code_
            machine.process_exited(code_)

        pty.start(LoginSimulator.EXECUTABLE, LoginSimulator.arguments(), environment, cwd=str(self.home),
                  on_output=lambda d: dispatch(output, d), on_exit=lambda c: dispatch(exited, c))
        self.addCleanup(pty.terminate)
        self.assertTrue(wait_until(lambda: machine.phase == LoginPhase.WAITING_FOR_CODE, 5), parser.transcript)
        pty.submit_line(code)
        dispatch(machine.code_submitted)
        self.assertTrue(wait_until(lambda: "code" in status, 10))
        dispatch.drain()
        return machine, status["code"]

    def test_successful_login_writes_the_fake_login_into_the_folder(self):
        folder = self.home / ".claude-x"
        folder.mkdir()
        machine, code = self.run_simulator("ABC123", str(folder))
        self.assertEqual(machine.phase, LoginPhase.CONNECTED)
        credentials = json.loads((folder / ".credentials.json").read_text())
        self.assertIn("accessToken", credentials["claudeAiOauth"])
        self.assertEqual(self.mode_of(folder / ".credentials.json"), 0o600)
        state = json.loads((folder / ".claude.json").read_text())
        self.assertEqual(state["oauthAccount"]["emailAddress"], LoginSimulator.SIMULATED_EMAIL)
        self.assertFalse((self.home / ".claude.json").exists(), "the default folder was not touched")

    def test_rejected_code_ends_in_failure(self):
        machine, code = self.run_simulator("erro-expirado", None)
        self.assertEqual(machine.phase, LoginPhase.failed("Error: Invalid code or expired"))
        self.assertFalse((self.home / ".claude").exists())

    def test_simulator_refuses_to_write_outside_home(self):
        outside = Path(tempfile.mkdtemp(prefix="mc-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        for target in (str(outside), f"{self.home}/../{outside.name}"):
            machine, code = self.run_simulator("ABC123", target)
            self.assertEqual(code, 3)
            self.assertEqual(machine.phase, LoginPhase.failed("processo encerrado (código 3)"))
            self.assertEqual(list(outside.iterdir()), [])


class LoginSessionTests(SafeTestCase):
    """The orchestration the login dialog uses (the Mac SetupTokenSession + AppModel capture)."""

    def setUp(self):
        super().setUp()
        self.repository = ConfigRepository(self.environment)
        self.repository.save(Config())
        self.saver = ClaudeAccountSaver(self.environment, self.repository.load, self.repository.save)
        self.dispatch = SerialDispatcher()
        self.finished = []

    def session(self, mode=LoginMode.LOGIN, target=None, tools=None, simulated=None, timeout=LOGIN_TIMEOUT,
                environment=None):
        target = target or NewClaudeAccount("Marco", None, ProfileColor.TEAL)
        session = ClaudeLoginSession(environment or self.environment, tools or ToolsAt(), mode, target, self.saver,
                                     dispatch=self.dispatch, on_finished=self.finished.append,
                                     simulated=simulated, timeout=timeout)
        self.addCleanup(session.stop)
        return session

    def wait_phase(self, session, phase, timeout=8):
        self.assertTrue(wait_until(lambda: session.phase == phase, timeout),
                        f"phase {session.phase}, output: {session.output!r}")

    def wait_finished(self, session, timeout=10):
        self.assertTrue(wait_until(lambda: session.phase.is_finished, timeout), session.output)
        self.dispatch.drain()

    def scratch_root(self):
        return self.home / ".config" / "multicred" / "scratch"

    def test_simulated_full_login_stores_the_account_in_the_vault_and_removes_the_scratch(self):
        session = self.session()
        session.start()
        command = session.command
        scratch = Path(command.scratch)
        self.assertEqual(scratch.parent, self.scratch_root())
        self.assertEqual(self.mode_of(scratch), 0o700)
        self.assertEqual(command.environment["HOME"], str(self.home))
        self.assertEqual(command.environment["CLAUDE_CONFIG_DIR"], str(scratch))
        self.assertEqual(command.executable, LoginSimulator.EXECUTABLE)

        self.wait_phase(session, LoginPhase.WAITING_FOR_CODE)
        code = "abcdefghijklmnopqrstuvwxyz012345#ABCDEFGHIJKLMNOP"
        session.submit_code(code)
        self.assertEqual(session.phase, LoginPhase.CONNECTING)
        self.wait_finished(session)

        self.assertEqual(session.phase, LoginPhase.CONNECTED)
        account = session.result
        self.assertEqual(account.kind, ClaudeAccountKind.LOGIN)
        self.assertEqual(account.email, LoginSimulator.SIMULATED_EMAIL, "the login's e-mail wins")
        record = ClaudeLoginRecord.decode(self.claude_store.vault[uuid_text(account.id)])
        self.assertIn("accessToken", json.loads(record.oauth))
        self.assertEqual([a.id for a in self.repository.load().claude_accounts], [account.id])
        self.assertFalse(scratch.exists(), "the throwaway folder and its .credentials.json are gone")
        self.assertEqual(list(self.scratch_root().iterdir()), [])
        self.assertNotIn(code, session.output)
        self.assertNotIn(code, session.redacted_log)
        self.assertEqual(self.finished, [session])
        self.assertFalse(self.claude_store.folders, "no profile folder was touched")

    def test_rejected_code_stores_nothing(self):
        session = self.session()
        session.start()
        scratch = Path(session.command.scratch)
        self.wait_phase(session, LoginPhase.WAITING_FOR_CODE)
        session.submit_code("erro")
        self.wait_finished(session)
        self.assertEqual(session.phase, LoginPhase.failed("Error: Invalid code or expired"))
        self.assertIsNone(session.result)
        self.assertEqual(self.claude_store.vault, {})
        self.assertEqual(self.repository.load().claude_accounts, [])
        self.assertTrue(wait_until(lambda: not scratch.exists(), 3))

    def fake_claude(self, body):
        return str(self.make_executable("fakebin/claude", "#!/bin/sh\n"
                                        "printf '%s\\n' \"$*\" > \"$HOME/claude-args\"\n"
                                        "printf '%s\\n' \"$BROWSER\" > \"$HOME/claude-browser\"\n" + body))

    def test_login_completes_without_a_code_d26(self):
        claude = self.fake_claude(
            "printf 'Opening browser to sign in...\\r\\n'\n"
            "printf \"If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?x=1\\r\\n\"\n"
            "printf 'Paste code here if prompted > '\n"
            "sleep 0.5\n"
            "umask 077\n"
            "printf '{\"claudeAiOauth\":{\"accessToken\":\"sk-ant-oat01-FAKE\",\"refreshToken\":\"r\"}}' "
            "> \"$CLAUDE_CONFIG_DIR/.credentials.json\"\n"
            "printf '{\"oauthAccount\":{\"emailAddress\":\"auto@example.com\"}}' > \"$CLAUDE_CONFIG_DIR/.claude.json\"\n"
            "printf 'Login successful.\\r\\n'\n")
        session = self.session(tools=ToolsAt(claude=claude), simulated=False)
        session.start()
        self.assertEqual(session.command.arguments, ["auth", "login", "--claudeai"])
        self.wait_finished(session)
        self.assertEqual(session.phase, LoginPhase.CONNECTED)
        self.assertEqual(session.result.email, "auto@example.com")
        self.assertEqual(session.authorization_url, "https://claude.com/cai/oauth/authorize?x=1")
        self.assertEqual(self.read(self.home / "claude-args").strip(), "auth login --claudeai")
        self.assertEqual(self.read(self.home / "claude-browser").strip(), "/usr/bin/true",
                         "the CLI never opens a browser by itself; the dialog does")

    def test_success_text_is_held_until_the_login_is_saved(self):
        # "Login successful." arrives before the process exits; the step is done only once saved.
        claude = self.fake_claude("printf 'Login successful.\\r\\n'\nsleep 1\nexit 0\n")
        session = self.session(tools=ToolsAt(claude=claude), simulated=False)
        session.start()
        time.sleep(0.5)
        self.assertFalse(session.phase.is_finished)
        self.wait_finished(session)
        self.assertEqual(session.phase, LoginPhase.failed("processo encerrado (código 1)"))
        self.assertEqual(session.launch_error, "O login terminou, mas não encontrei a credencial. Tente de novo.")

    def test_timeout_stops_the_process_and_removes_the_scratch(self):
        session = self.session(timeout=0.8)
        session.start()
        scratch = Path(session.command.scratch)
        self.wait_finished(session, timeout=6)
        self.assertEqual(session.phase, LoginPhase.failed("tempo esgotado"))
        self.assertTrue(wait_until(lambda: not session.is_running, 4))
        self.assertTrue(wait_until(lambda: not scratch.exists(), 3))
        self.assertEqual(self.finished, [session])

    def test_cancel(self):
        session = self.session()
        session.start()
        scratch = Path(session.command.scratch)
        self.wait_phase(session, LoginPhase.WAITING_FOR_CODE)
        session.cancel()
        self.assertEqual(session.phase, LoginPhase.failed("cancelado"))
        self.assertTrue(wait_until(lambda: not session.is_running, 4))
        self.assertFalse(scratch.exists())
        self.dispatch.drain()
        self.assertEqual(self.finished, [session], "on_finished fires once")

    def test_signing_in_again_turns_a_token_account_into_a_login_account(self):
        existing = ClaudeAccount(nickname="Marco", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
        config = Config()
        config.claude_accounts.append(existing)
        self.repository.save(config)
        self.secrets.set_secret(b"sk-ant-oat01-old-token-0000000000000", existing.secret_account)
        session = self.session(target=existing)
        session.start()
        self.wait_phase(session, LoginPhase.WAITING_FOR_CODE)
        session.submit_code("ABC123")
        self.wait_finished(session)
        self.assertEqual(session.phase, LoginPhase.CONNECTED)
        saved = self.repository.load().claude_account(existing.id)
        self.assertEqual(saved.kind, ClaudeAccountKind.LOGIN)
        self.assertIn(uuid_text(existing.id), self.claude_store.vault)
        self.assertFalse(self.secrets.contains_item(existing.secret_account), "the old token is deleted")

    def test_setup_token_stores_the_token_in_the_secret_store(self):
        session = self.session(mode=LoginMode.SETUP_TOKEN)
        session.start()
        self.assertEqual(session.command.executable, SetupTokenSimulator.EXECUTABLE)
        self.wait_phase(session, LoginPhase.WAITING_FOR_CODE)
        session.submit_code("CODE-123")
        self.wait_finished(session)
        self.assertEqual(session.phase, LoginPhase.CONNECTED)
        account = session.result
        self.assertEqual(account.kind, ClaudeAccountKind.TOKEN)
        self.assertEqual(self.secrets.stored_value(account.secret_account), SetupTokenSimulator.FAKE_TOKEN.encode())
        self.assertNotIn(SetupTokenSimulator.FAKE_TOKEN, session.output)
        self.assertNotIn(SetupTokenSimulator.FAKE_TOKEN, session.redacted_log)
        self.assertEqual(self.claude_store.vault, {})

    def test_a_real_tool_outside_the_test_home_is_never_run(self):
        session = self.session(tools=ToolsAt(claude="/usr/bin/true"), simulated=False)
        session.start()
        self.assertIsNone(session.command)
        self.assertTrue(session.phase.is_failure)
        self.assertIn("HOME de teste", session.launch_error)

    def test_claude_not_installed(self):
        real_like = SystemEnvironment(mode="real", home=self.home, login_shell="/bin/bash", user_name="tester",
                                      runner=self.runner, secrets=self.secrets, claude_credentials=self.claude_store)
        session = self.session(environment=real_like, tools=ToolsAt())
        session.start()
        self.assertTrue(session.phase.is_failure)
        self.assertIn("Claude Code não foi encontrado", session.launch_error)
        self.assertFalse(self.scratch_root().exists() and any(self.scratch_root().iterdir()))


class ClaudeAccountSaverTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.repository = ConfigRepository(self.environment)
        self.repository.save(Config())
        self.saver = ClaudeAccountSaver(self.environment, self.repository.load, self.repository.save)

    def record(self):
        return ClaudeLoginRecord(b'{"accessToken":"a","refreshToken":"r"}', b'{"emailAddress":"x@example.com"}')

    def test_save_failure_removes_the_vault_entry_again(self):
        def failing_save(_config):
            raise OSError("disk full")
        saver = ClaudeAccountSaver(self.environment, self.repository.load, failing_save)
        with self.assertRaises(OSError):
            saver.save_login(NewClaudeAccount("Marco", None, ProfileColor.TEAL), self.record())
        self.assertEqual(self.claude_store.vault, {})

    def test_unconfigured_app_is_refused(self):
        saver = ClaudeAccountSaver(self.environment, lambda: None, self.repository.save)
        with self.assertRaises(Exception):
            saver.save_token(NewClaudeAccount("Marco", None, ProfileColor.TEAL), "sk-ant-oat01-" + "a" * 30)
        self.assertEqual(self.secrets.all_accounts(), [])

    def test_capture_reads_the_scratch_file_even_with_an_in_memory_store(self):
        # Test mode keeps the vault in memory, but the simulator writes a real file.
        scratch = self.home / "scratch"
        scratch.mkdir()
        FileClaudeCredentialStore(self.home).write_folder_item(str(scratch), b'{"claudeAiOauth":{"accessToken":"a"}}')
        record = self.saver.capture(str(scratch))
        self.assertEqual(json.loads(record.oauth), {"accessToken": "a"})


if __name__ == "__main__":
    unittest.main()
