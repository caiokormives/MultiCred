"""S-12: the switch thread always hands its end back to the UI.

The model runs the engine off the UI thread. Whatever goes wrong there (an
exception that is not a SwitchError, the feedback outcome failing) must reach
`on_done` and clear `switching`, or the tray and the window stay stuck in
"trocando" forever. A request refused with Busy belongs to another switch that
is still running: it reports the refusal and leaves that switch's flag alone.
No display needed: a fake engine and a direct `dispatch`.
"""

import threading

import tests.test_engine as base
from multicred.core.engine.types import Busy, ClaudeTarget, SwitchResult
from multicred.core.models import ActiveState, Config, GitHubAccount, GitIdentity, Profile, ProfileColor
from multicred.core.storage import ConfigRepository
from multicred.ui.model import AppModel
from tests.support import SafeTestCase


class FakeEngine:
    def __init__(self, raises=None, result=None):
        self.raises, self.result = raises, result

    def switch_profile(self, _profile_id, _config):
        if self.raises is not None:
            raise self.raises
        return self.result

    def switch_claude_only(self, _override, _config):
        return self.switch_profile(None, _config)


class RecordingLog:
    def __init__(self):
        self.lines = []

    def log(self, level, message):
        self.lines.append((level, message))


class SwitchThreadTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        git = GitIdentity(nickname="Pessoal", name="Dev", email="dev@x.dev", color=ProfileColor.ORANGE)
        gh = GitHubAccount(nickname="Pessoal", username="dev", color=ProfileColor.ORANGE)
        self.profile = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git.id,
                               github_account_id=gh.id)
        ConfigRepository(self.environment).save(
            Config(git_identities=[git], github_accounts=[gh], profiles=[self.profile]))
        self.log = RecordingLog()
        self.model = AppModel(self.environment, base.FakeTools(), log=self.log)
        self.model.load()
        self.notified = []
        self.model.add_listener(lambda: self.notified.append(self.model.switching))

    def run_switch(self, engine, start=None):
        self.model.engine = engine
        done = threading.Event()
        calls = []
        (start or self.model.switch_profile)(self.profile.id,
                                             lambda result, error: (calls.append((result, error)), done.set()))
        self.assertTrue(done.wait(10), "on_done never ran: the UI would stay in \"trocando\"")
        return calls[0]

    def test_an_unexpected_exception_reaches_on_done_and_clears_switching(self):
        result, error = self.run_switch(FakeEngine(raises=RuntimeError("boom")))
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertFalse(self.model.switching)
        self.assertEqual(self.notified[-1], False, "the UI is told the switch ended")
        self.assertTrue(any(level == "error" and "boom" in message for level, message in self.log.lines))

    def test_the_claude_only_switch_too(self):
        _result, error = self.run_switch(FakeEngine(raises=KeyError("x")), start=self.model.switch_claude_only)
        self.assertIsInstance(error, KeyError)
        self.assertFalse(self.model.switching)

    def test_a_failing_feedback_outcome_still_ends_the_switch(self):
        state = ActiveState(profile_id=self.profile.id)
        engine = FakeEngine(result=SwitchResult(state=state, claude_target=ClaudeTarget("unchanged")))

        def broken_outcome(*_args):
            raise ValueError("no /proc")
        self.model._outcome = broken_outcome
        result, error = self.run_switch(engine)
        self.assertFalse(self.model.switching)
        self.assertIsNone(error, "the machine did switch; only the feedback is lost")
        self.assertIs(result.state, state)
        self.assertEqual(self.model.state.profile_id, self.profile.id, "the UI shows the profile now active")
        self.assertTrue(any(level == "error" and "no /proc" in message for level, message in self.log.lines))

    def test_busy_does_not_end_the_switch_that_is_running(self):
        release, entered = threading.Event(), threading.Event()
        state = ActiveState(profile_id=self.profile.id)

        class OneAtATime(FakeEngine):
            """The real engine's non-blocking lock: a second request while one runs gets Busy."""
            def __init__(self):
                super().__init__(result=SwitchResult(state=state, claude_target=ClaudeTarget("unchanged")))
                self.lock = threading.Lock()

            def switch_profile(self, _profile_id, _config):
                if not self.lock.acquire(blocking=False):
                    raise Busy()
                entered.set()
                release.wait(10)
                self.lock.release()
                return self.result

        engine = OneAtATime()
        self.model.engine = engine
        first_done = threading.Event()
        self.model.switch_profile(self.profile.id, lambda _r, _e: first_done.set())
        self.assertTrue(entered.wait(10))
        self.notified.clear()

        result, error = self.run_switch(engine)

        self.assertIsNone(result)
        self.assertIsInstance(error, Busy)
        self.assertTrue(self.model.switching, "the running switch still owns the flag")
        self.assertNotIn(False, self.notified, "no state change is announced")
        release.set()
        self.assertTrue(first_done.wait(10))
        self.assertFalse(self.model.switching, "the running switch ends it")

    def test_busy_with_nothing_else_running_here_goes_back_to_idle(self):
        # The engine is held by a Verificar fix, which does not go through the model.
        _result, error = self.run_switch(FakeEngine(raises=Busy()))
        self.assertIsInstance(error, Busy)
        self.assertFalse(self.model.switching)
        self.assertEqual(self.notified[-1], False)
