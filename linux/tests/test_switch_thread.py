"""S-12: the switch thread always hands its end back to the UI.

The model runs the engine off the UI thread. Whatever goes wrong there (an
exception that is not a SwitchError, the feedback outcome failing) must reach
`on_done` and clear `switching`, or the tray and the window stay stuck in
"trocando" forever. A request refused with Busy belongs to another switch that
is still running: it reports the refusal and leaves that switch's flag alone.
No display needed: a fake engine and a direct `dispatch`.
"""

import subprocess
import sys
import threading
import unittest
from pathlib import Path

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


VERIFY_WITHOUT_GTK = r"""
import sys, tempfile, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])

class Blocked:
    # Importing Gtk (or Adw) fails, as on a machine without a display stack.
    def find_spec(self, name, path=None, target=None):
        if name in ("gi.repository.Gtk", "gi.repository.Adw", "gi.repository.Gdk"):
            raise ImportError(f"{name} is blocked in this test")
        return None
sys.meta_path.insert(0, Blocked())

from multicred.core.models import Config, GitIdentity, Profile, ProfileColor
from multicred.core.storage import ConfigRepository
from multicred.ui.model import AppModel
from tests.support import SafeTestCase
import tests.test_engine as base

class Case(SafeTestCase):
    def runTest(self):
        git = GitIdentity(nickname="P", name="Dev", email="dev@x.dev", color=ProfileColor.ORANGE)
        ConfigRepository(self.environment).save(Config(git_identities=[git], profiles=[
            Profile(name="P", color=ProfileColor.ORANGE, git_identity_id=git.id)]))
        model = AppModel(self.environment, base.FakeTools())
        model.load()
        used = []
        import multicred.core.engine.verifier as verifier_module
        real = verifier_module.Verifier
        def recording(environment, tools, processes, **kwargs):
            used.append(processes)
            return real(environment, tools, processes, **kwargs)
        verifier_module.Verifier = recording
        done = threading.Event()
        model.verify(lambda report, error: done.set())
        assert done.wait(20), "verify never finished"
        assert used == [model.processes], used
        assert model.verification is not None
        assert "gi.repository.Gtk" not in sys.modules, "Gtk was imported"
        print("VERIFY-OK")

case = Case()
case.setUp()
try:
    case.runTest()
finally:
    case.doCleanups()
"""


class ModelWithoutGtkTests(unittest.TestCase):
    def test_verify_runs_without_importing_gtk_and_uses_the_models_scanner(self):
        """S-30: AppModel has no GTK; Verificar from the tray must not need it."""
        root = str(Path(__file__).resolve().parent.parent)
        done = subprocess.run([sys.executable, "-I", "-c", VERIFY_WITHOUT_GTK, root], cwd=root,
                              env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                              capture_output=True, text=True, timeout=60)
        self.assertIn("VERIFY-OK", done.stdout, done.stdout + done.stderr)


class RemovingTheAppliedClaudeAccountTests(SafeTestCase):
    """S-34: removing the Claude account in use always brings Claude back to the folder's
    own login, with or without an active profile, and after a running switch."""

    def setUp(self):
        super().setUp()
        from multicred.core.models import ActiveState, ClaudeAccount, ClaudeAccountKind
        self.token = ClaudeAccount(nickname="Marco", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
        self.secrets.set_secret(b"sk-ant-oat01-marco", self.token.secret_account)
        ConfigRepository(self.environment).save(Config(claude_accounts=[self.token]))
        self.model = AppModel(self.environment, base.FakeTools())
        self.model.load()
        self.model.state = ActiveState(applied_claude_account_id=self.token.id, generation=3)  # no profile
        self.resets = []
        self.model.switch_claude_only = lambda account, on_done: self.resets.append(account)

    def test_without_an_active_profile_the_token_is_dropped_at_once(self):
        self.model.remove_claude_account(self.token.id)
        self.assertEqual(self.resets, [None], "env files stop exporting the removed token")
        self.assertIsNone(self.secrets.stored_value(self.token.secret_account))

    def test_during_a_switch_the_reset_waits_for_it_and_runs_once(self):
        self.model.switching = True
        self.model.remove_claude_account(self.token.id)
        self.assertEqual(self.resets, [], "not while another switch runs")
        self.model.switching = False
        self.model._run_pending_reapply()
        self.model._run_pending_reapply()
        self.assertEqual(self.resets, [None])


class ClaudeLoginLockTests(SafeTestCase):
    """S-23: removing a Claude account (and signing one in again) changes the folders'
    logins like a switch does, so it runs under the same lock: never two place() at once."""

    def setUp(self):
        super().setUp()
        from multicred.core.claude.login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher
        from multicred.core.models import ClaudeAccount, ClaudeAccountKind

        git = GitIdentity(nickname="Pessoal", name="Dev", email="dev@x.dev", color=ProfileColor.ORANGE)
        self.profile = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git.id)
        self.dev = ClaudeAccount(nickname="Dev", email="dev@x.com", color=ProfileColor.BLUE,
                                 kind=ClaudeAccountKind.LOGIN)
        ConfigRepository(self.environment).save(
            Config(git_identities=[git], claude_accounts=[self.dev], profiles=[self.profile]))
        self.claude_store.write_folder_item(None, b'{"claudeAiOauth":{"accessToken":"own"}}')
        self.switcher = ClaudeLoginSwitcher(self.environment)
        self.switcher.store_new_account(self.dev.id, ClaudeLoginRecord(b'{"accessToken":"dev"}', None))
        self.switcher.place(self.dev.id, None)  # dev is live in ~/.claude
        self.model = AppModel(self.environment, base.FakeTools())
        self.model.load()

    def snapshot(self):
        return (dict(self.claude_store.vault), self.claude_store.read_folder_item(None),
                self.switcher.occupancy().to_dict(), ConfigRepository(self.environment).load().to_dict())

    def test_exclusive_is_busy_while_a_switch_runs(self):
        engine = self.model.engine
        entered, release = threading.Event(), threading.Event()
        original = engine._transaction

        def slow(config, operation):
            entered.set()
            release.wait(10)
            return original(config, operation)
        engine._transaction = slow
        worker = threading.Thread(target=lambda: engine.switch_profile(self.profile.id, self.model.config))
        worker.start()
        self.addCleanup(worker.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(10))
        with self.assertRaises(Busy):
            with engine.exclusive():
                self.fail("ran while a switch was running")
        release.set()
        worker.join(10)
        with engine.exclusive():
            pass  # free again

    def test_a_switch_is_busy_while_exclusive_is_held(self):
        with self.model.engine.exclusive():
            with self.assertRaises(Busy):
                self.model.engine.switch_profile(self.profile.id, self.model.config)

    def test_removing_a_claude_account_during_a_switch_is_busy_and_changes_nothing(self):
        before = self.snapshot()
        with self.model.engine.exclusive():  # the switch that is running
            with self.assertRaises(Busy):
                self.model.remove_claude_account(self.dev.id)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNotNone(self.model.config.claude_account(self.dev.id))

        self.model.remove_claude_account(self.dev.id)  # once it ended
        self.assertIsNone(self.model.config.claude_account(self.dev.id))
        self.assertEqual(self.claude_store.read_folder_item(None), b'{"claudeAiOauth":{"accessToken":"own"}}')

    def test_removing_holds_the_lock_for_the_whole_sequence(self):
        seen = []
        original = self.model.repository.save

        def save(config):
            seen.append(self.model.engine._lock.locked())
            original(config)
        self.model.repository.save = save
        self.model.remove_claude_account(self.dev.id)
        self.assertEqual(seen, [True])
        self.assertFalse(self.model.engine._lock.locked())
