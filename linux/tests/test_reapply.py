"""S-09: re-applying the active profile when the app starts (PLAN §5.8).

systemd forgets `systemctl --user set-environment` on reboot, so at session
start the app runs the same SwitchEngine with the active profile. A failure
marks the model inconsistent and is reported once: no retry loop.
"""

import threading
import time

import tests.test_engine as base
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.models import Config, GitHubAccount, GitIdentity, Profile, ProfileColor
from multicred.core.storage import ConfigRepository
from tests.support import SafeTestCase


class ReapplyTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        from multicred.ui.model import AppModel

        self.system = base.FakeSystem(self.runner)
        self.claude_dir = f"{self.home}/.claude-pessoal"
        (self.home / ".claude-pessoal").mkdir()
        git = GitIdentity(nickname="P", name="Dev", email="d@x.dev", color=ProfileColor.ORANGE)
        gh = GitHubAccount(nickname="P", username="dev-pessoal", color=ProfileColor.ORANGE)
        self.profile = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git.id,
                               github_account_id=gh.id, claude_config_dir=self.claude_dir)
        self.config = Config(git_identities=[git], github_accounts=[gh], profiles=[self.profile])
        ConfigRepository(self.environment).save(self.config)
        self.model = AppModel(self.environment, base.FakeTools())
        self.calls = []
        original = self.model.engine.reapply_active_profile

        def counting(config):
            self.calls.append(config)
            return original(config)
        self.model.engine.reapply_active_profile = counting

    def activate(self):
        SwitchEngine(self.environment, base.FakeTools()).switch_profile(self.profile.id, self.config)

    def reapply(self):
        done = threading.Event()
        seen = {}

        def on_done(result, error):
            seen["result"], seen["error"] = result, error
            done.set()

        started = self.model.reapply_active_profile(on_done)
        if started:
            self.assertTrue(done.wait(10), "reapply never finished")
        return started, seen

    def test_startup_with_an_active_profile_reapplies_once(self):
        self.activate()
        generation = self.model.engine.current_state().generation
        # The reboot: systemd's user manager lost the variable.
        self.system.session.clear()
        self.model.load()

        started, seen = self.reapply()

        self.assertTrue(started)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(seen["error"])
        self.assertEqual(self.system.claude, self.claude_dir, "the graphical session has the folder again")
        self.assertEqual(self.model.state.generation, generation + 1)
        self.assertEqual(self.model.state.profile_id, self.profile.id)
        self.assertFalse(self.model.inconsistent)
        self.assertFalse(self.model.switching)

    def test_nothing_happens_without_an_active_profile(self):
        self.model.load()
        started, _ = self.reapply()
        self.assertFalse(started)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner.requests, [])

    def test_nothing_happens_before_setup(self):
        self.activate()
        (self.environment.app_support_directory / "config.json").unlink()
        self.model.load()
        started, _ = self.reapply()
        self.assertFalse(started)
        self.assertEqual(self.calls, [])

    def test_failure_marks_inconsistent_and_does_not_retry(self):
        self.activate()
        self.model.load()
        self.system.fail_set = True
        self.system.session.clear()
        failures = []
        self.model.reapply_listeners.append(lambda error: failures.append(error))

        started, seen = self.reapply()
        time.sleep(0.3)  # a retry loop would call the engine again meanwhile

        self.assertTrue(started)
        self.assertIsNotNone(seen["error"])
        self.assertTrue(self.model.inconsistent)
        self.assertFalse(self.model.switching)
        self.assertEqual(len(self.calls), 1, "no retry loop")
        self.assertEqual(len(failures), 1)

    def test_a_preflight_failure_also_marks_inconsistent(self):
        self.activate()
        self.model.load()
        (self.home / ".claude-pessoal").rmdir()
        started, seen = self.reapply()
        self.assertTrue(started)
        self.assertIsNotNone(seen["error"])
        self.assertTrue(self.model.inconsistent)
        self.assertEqual(len(self.calls), 1)

    def test_a_deleted_active_profile_is_nothing_to_reapply_not_an_inconsistency(self):
        """S-22: the active profile was removed from the configuration."""
        self.activate()
        self.model.load()
        self.model.config.profiles.clear()
        failures = []
        self.model.reapply_listeners.append(failures.append)
        started, seen = self.reapply()
        self.assertTrue(started)
        self.assertEqual((seen["result"], seen["error"]), (None, None))
        self.assertFalse(self.model.inconsistent)
        self.assertFalse(self.model.switching)
        self.assertEqual(failures, [])

    def test_reapply_is_silent_no_switch_feedback(self):
        self.activate()
        self.model.load()
        outcomes = []
        self.model.switch_listeners.append(outcomes.append)
        self.reapply()
        self.assertEqual(outcomes, [])


class FreshSandboxTests(SafeTestCase):
    """`multicred --fresh`: test mode with an unseeded sandbox, so the onboarding shows."""

    def test_fresh_implies_test_mode_and_seeds_nothing(self):
        from multicred.__main__ import build_model, parse_args

        args = parse_args(["--fresh", "--sandbox", str(self.home / "sandbox")])
        self.assertTrue(args.test_mode)
        model = build_model(args)
        self.assertEqual(model.environment.mode, "dryRun")
        self.assertEqual(model.environment.home, (self.home / "sandbox").resolve())
        model.load()
        self.assertIsNone(model.config, "no seeded profiles: the onboarding runs")
        self.assertFalse((self.home / "sandbox/.config/multicred/config.json").exists())

    def test_fresh_without_a_sandbox_gets_a_new_empty_one_each_time(self):
        import shutil
        from multicred.__main__ import build_model, parse_args

        first = build_model(parse_args(["--fresh"])).environment.home
        second = build_model(parse_args(["--fresh"])).environment.home
        self.addCleanup(shutil.rmtree, first, True)
        self.addCleanup(shutil.rmtree, second, True)
        self.assertNotEqual(first, second)
        self.assertEqual(list(first.iterdir()), [])

    def test_test_mode_still_seeds(self):
        from multicred.__main__ import build_model, parse_args

        model = build_model(parse_args(["--test-mode", "--sandbox", str(self.home / "seeded")]))
        model.load()
        self.assertEqual([p.name for p in model.config.profiles], ["Trabalho", "Pessoal"])
