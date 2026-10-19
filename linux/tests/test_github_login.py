"""`gh auth login --web` in the pseudo-terminal (D34). Ports GitHubLoginTests (Phase4CoreTests.swift).

The session drives a fake `gh` script that lives in the temporary HOME through
the real pseudo-terminal; switching the active account back goes through the
FakeCommandRunner. The real gh is never run.
"""

import dataclasses
import threading
import unittest

from multicred.core.adapters.github_login_flow import (
    GitHubLoginEvent, GitHubLoginParser, GitHubLoginPhase, GitHubLoginSession, GitHubLoginSimulator,
    GitHubLoginStateMachine, login_arguments,
)
from multicred.core.pty import PseudoTerminalProcess

from tests.login_support import SerialDispatcher, ToolsAt, wait_until
from tests.support import FakeCommandRunner, SafeTestCase


class GitHubLoginParserTests(unittest.TestCase):
    def test_real_output_shape(self):
        parser, machine = GitHubLoginParser(), GitHubLoginStateMachine()
        chunks = ["\x1b[0;33m!\x1b[0m First copy your one-time code: \x1b[1mAB12-CD34\x1b[0m\n",
                  "Press Enter to open https://github.com/login/device in your browser... "]
        for chunk in chunks:
            for event in parser.consume(chunk.encode()):
                machine.handle(event)
        self.assertEqual(machine.phase, GitHubLoginPhase.showing_code("AB12-CD34"))
        self.assertEqual(parser.device_url, "https://github.com/login/device")
        machine.browser_opened()
        self.assertEqual(machine.phase, GitHubLoginPhase.waiting_for_browser("AB12-CD34"))
        for event in parser.consume("\n✓ Authentication complete.\n- gh config set -h github.com git_protocol https\n"
                                    "✓ Logged in as octocat\n".encode()):
            machine.handle(event)
        self.assertEqual(machine.phase, GitHubLoginPhase.connected("octocat"))
        self.assertEqual(parser.logged_in_as, "octocat")

    def test_question_and_failure(self):
        parser = GitHubLoginParser()
        events = parser.consume(b"? Authenticate Git with your GitHub credentials? (Y/n) \nerror: authorization expired\n")
        self.assertEqual(events[0], GitHubLoginEvent.question("? Authenticate Git with your GitHub credentials? (Y/n)"))
        self.assertEqual(events[-1], GitHubLoginEvent.failed("error: authorization expired"))

    def test_exit_and_cancel(self):
        exited = GitHubLoginStateMachine()
        exited.process_exited(1, None)
        self.assertEqual(exited.phase, GitHubLoginPhase.failed("processo encerrado (código 1)"))
        clean = GitHubLoginStateMachine()
        clean.process_exited(0, "octocat")
        self.assertEqual(clean.phase, GitHubLoginPhase.connected("octocat"))
        cancelled = GitHubLoginStateMachine()
        cancelled.cancelled()
        self.assertEqual(cancelled.phase, GitHubLoginPhase.failed("cancelado"))

    def test_login_arguments(self):
        self.assertEqual(login_arguments("github.example.com"),
                         ["auth", "login", "--hostname", "github.example.com", "--git-protocol", "https", "--web"])

    def test_simulator_through_real_pty(self):
        lock = threading.Lock()
        parser, machine = GitHubLoginParser(), GitHubLoginStateMachine()
        done = threading.Event()

        def output(data):
            with lock:
                for event in parser.consume(data):
                    machine.handle(event)

        def exited(status):
            with lock:
                machine.process_exited(status, parser.logged_in_as)
            done.set()

        pty = PseudoTerminalProcess()
        pty.start(GitHubLoginSimulator.EXECUTABLE, GitHubLoginSimulator.arguments(), {"PATH": "/usr/bin:/bin"},
                  on_output=output, on_exit=exited)
        self.addCleanup(pty.terminate)
        self.assertTrue(wait_until(lambda: machine.code is not None, 5))
        self.assertEqual(machine.code, "TEST-1234")
        pty.write(b"\r")
        self.assertTrue(done.wait(10))
        self.assertEqual(machine.phase, GitHubLoginPhase.connected(GitHubLoginSimulator.SIMULATED_LOGIN))


FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" > "$HOME/gh-args"
printf '\\033[0;33m!\\033[0m First copy your one-time code: \\033[1mWXYZ-9876\\033[0m\\n'
printf 'Press Enter to open https://github.com/login/device in your browser... '
IFS= read -r enter
printf '? Authenticate Git with your GitHub credentials? (Y/n) '
IFS= read -r answer
printf 'answer=[%s]\\n' "$answer" > "$HOME/gh-answer"
case "$FAKE_GH_RESULT" in
  fail) printf 'error: authorization expired\\n'; exit 1 ;;
esac
printf '\\342\\234\\223 Authentication complete.\\n'
printf '\\342\\234\\223 Logged in as new-login\\n'
"""


class GitHubLoginSessionTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.gh = str(self.make_executable("fakebin/gh", FAKE_GH))
        self.dispatch = SerialDispatcher()
        self.finished = []
        self.runner.on("gh", ["auth", "switch"])

    def session(self, switch_back_to="profile-user", simulated=False, tools=None):
        session = GitHubLoginSession(self.environment, tools or ToolsAt(gh=self.gh), host="github.com",
                                     switch_back_to=switch_back_to, dispatch=self.dispatch,
                                     on_finished=self.finished.append, simulated=simulated)
        self.addCleanup(session.stop)
        return session

    def wait_finished(self, session):
        self.assertTrue(wait_until(lambda: bool(self.finished), 10), session.output)
        self.dispatch.drain()

    def switch_requests(self):
        return [r.arguments for r in self.runner.requests if r.arguments[:2] == ["auth", "switch"]]

    def test_login_shows_the_code_answers_questions_and_switches_back_to_the_profile_account(self):
        session = self.session()
        session.start()
        self.assertTrue(wait_until(lambda: session.code is not None, 5), session.output)
        self.assertEqual(session.code, "WXYZ-9876")
        self.assertEqual(session.phase, GitHubLoginPhase.showing_code("WXYZ-9876"))
        self.assertEqual(self.read(self.home / "gh-args").strip(),
                         "auth login --hostname github.com --git-protocol https --web")
        session.open_browser()
        self.assertEqual(session.phase, GitHubLoginPhase.waiting_for_browser("WXYZ-9876"))
        self.wait_finished(session)

        self.assertEqual(session.phase, GitHubLoginPhase.connected("new-login"))
        self.assertEqual(self.read(self.home / "gh-answer").strip(), "answer=[]", "the default answer (Enter)")
        self.assertEqual(self.switch_requests(), [["auth", "switch", "--hostname", "github.com", "--user", "profile-user"]])
        switch = next(r for r in self.runner.requests if r.arguments[:2] == ["auth", "switch"])
        self.assertEqual(switch.executable, self.gh)
        self.assertEqual(switch.environment["HOME"], str(self.home))
        self.assertTrue(session.switched_back)
        self.assertEqual(self.finished, [session])

    def test_no_profile_account_means_no_switch(self):
        session = self.session(switch_back_to=None)
        session.start()
        self.assertTrue(wait_until(lambda: session.code is not None, 5))
        session.open_browser()
        self.wait_finished(session)
        self.assertEqual(session.phase, GitHubLoginPhase.connected("new-login"))
        self.assertEqual(self.switch_requests(), [])
        self.assertFalse(session.switched_back)

    def test_failed_login_does_not_switch(self):
        session = self.session()
        session.extra_environment = {"FAKE_GH_RESULT": "fail"}
        session.start()
        self.assertTrue(wait_until(lambda: session.code is not None, 5))
        session.open_browser()
        self.wait_finished(session)
        self.assertEqual(session.phase, GitHubLoginPhase.failed("error: authorization expired"))
        self.assertEqual(self.switch_requests(), [])

    def test_switch_back_failure_is_reported_but_the_login_stays_connected(self):
        self.runner = FakeCommandRunner()
        self.runner.on("gh", ["auth", "switch"], exit_code=1, stderr="no such account")
        self.environment = dataclasses.replace(self.environment, runner=self.runner)
        session = self.session()
        session.start()
        self.assertTrue(wait_until(lambda: session.code is not None, 5))
        session.open_browser()
        self.wait_finished(session)
        self.assertEqual(session.phase, GitHubLoginPhase.connected("new-login"))
        self.assertFalse(session.switched_back)
        self.assertIn("no such account", session.switch_back_error)

    def test_cancel(self):
        session = self.session()
        session.start()
        self.assertTrue(wait_until(lambda: session.code is not None, 5))
        session.cancel()
        self.assertEqual(session.phase, GitHubLoginPhase.failed("cancelado"))
        self.assertTrue(wait_until(lambda: not session.is_running, 4))
        self.dispatch.drain()
        self.assertEqual(self.finished, [session])
        self.assertEqual(self.switch_requests(), [])

    def test_simulated_mode_runs_the_simulator(self):
        session = self.session(simulated=True, tools=ToolsAt())
        session.start()
        self.assertEqual(session.command.executable, GitHubLoginSimulator.EXECUTABLE)
        self.assertTrue(wait_until(lambda: session.code == "TEST-1234", 5))
        session.open_browser()
        self.wait_finished(session)
        self.assertEqual(session.phase, GitHubLoginPhase.connected(GitHubLoginSimulator.SIMULATED_LOGIN))
        self.assertEqual(self.switch_requests(), [["auth", "switch", "--hostname", "github.com", "--user", "profile-user"]])

    def test_a_real_gh_outside_the_test_home_is_never_run(self):
        session = self.session(tools=ToolsAt(gh="/usr/bin/gh"))
        session.start()
        self.assertIsNone(session.command)
        self.assertTrue(session.phase.is_finished)
        self.assertIn("HOME de teste", session.launch_error)


if __name__ == "__main__":
    unittest.main()
