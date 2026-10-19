"""S-09: what the desktop shows after a switch (SwitchFeedbackPresenter.swift, design 08).

On Linux the HUD is a short desktop notification (Wayland has no positioned
overlay): "hud" = notification withdrawn after ~2 s, "notification" = a normal
one, "none" = nothing. With Claude Code sessions open after a switch that
changed the Claude account or folder, the notification says so and stays.
The decision logic is pure; the presenter is tested with a fake application.
"""

import threading
import unittest

import tests.test_engine as base
from multicred.core.adapters.process_scanner import FixedProcessScanner
from multicred.core.engine.types import SwitchStepKind as K
from multicred.core.models import (
    ClaudeAccount, ClaudeAccountKind, Config, GitHubAccount, GitIdentity, Profile, ProfileColor, SwitchFeedback,
)
from multicred.core.storage import ConfigRepository
from multicred.ui.feedback import (
    HUD_MILLISECONDS, FeedbackMessage, SwitchFeedbackPresenter, SwitchOutcome, message_for,
)
from tests.support import SafeTestCase


def outcome(**overrides):
    values = dict(profile_name="Pessoal", claude_account_name=None, steps=(K.GIT, K.GITHUB, K.SESSION, K.STATE),
                  claude_only=False, claude_changed=True, running_claude_sessions=0)
    values.update(overrides)
    return SwitchOutcome(**values)


class MessageTests(unittest.TestCase):
    def test_none_shows_nothing_even_with_open_sessions(self):
        self.assertIsNone(message_for(outcome(), SwitchFeedback.NONE))
        self.assertIsNone(message_for(outcome(running_claude_sessions=3), SwitchFeedback.NONE))

    def test_hud_is_a_short_notification(self):
        message = message_for(outcome(), SwitchFeedback.HUD)
        self.assertEqual(message, FeedbackMessage("Agora em Pessoal", "git, GitHub e Claude trocados.", True))

    def test_notification_stays(self):
        message = message_for(outcome(), SwitchFeedback.NOTIFICATION)
        self.assertEqual(message.title, "Agora em Pessoal")
        self.assertFalse(message.transient)

    def test_changed_items_follow_the_steps(self):
        self.assertEqual(message_for(outcome(steps=(K.GIT, K.STATE), claude_changed=False),
                                     SwitchFeedback.HUD).body, "git trocado.")
        self.assertEqual(message_for(outcome(steps=(K.GIT, K.SESSION, K.STATE), claude_changed=True),
                                     SwitchFeedback.HUD).body, "git e Claude trocados.")
        self.assertEqual(message_for(outcome(steps=(K.ENV_FILES, K.STATE), claude_changed=False),
                                     SwitchFeedback.HUD).body, "Perfil trocado.")

    def test_open_sessions_after_a_claude_change_warn_and_stay(self):
        message = message_for(outcome(running_claude_sessions=2, claude_account_name="Dev HINC"), SwitchFeedback.HUD)
        self.assertFalse(message.transient, "the warning must not vanish after 2 s")
        self.assertIn("2 sessões do Claude Code já abertas continuam na conta anterior. "
                      "Reinicie-as para usar Dev HINC.", message.body)

    def test_one_open_session_is_singular(self):
        message = message_for(outcome(running_claude_sessions=1), SwitchFeedback.NOTIFICATION)
        self.assertIn("1 sessão do Claude Code já aberta continua na conta anterior. "
                      "Reinicie-a para usar o login da pasta.", message.body)

    def test_open_sessions_without_a_claude_change_do_not_warn(self):
        message = message_for(outcome(running_claude_sessions=4, claude_changed=False,
                                      steps=(K.GIT, K.STATE)), SwitchFeedback.HUD)
        self.assertNotIn("sessão", message.body)
        self.assertNotIn("sessões", message.body)
        self.assertTrue(message.transient)

    def test_claude_only_switch(self):
        message = message_for(outcome(claude_only=True, claude_account_name="Cliente X",
                                      steps=(K.CLAUDE_LOGIN, K.ENV_FILES, K.STATE)), SwitchFeedback.NOTIFICATION)
        self.assertEqual(message.title, "Conta Claude: Cliente X")
        self.assertEqual(message.body, "Só o login do Claude mudou; agents, skills e conversas ficam.")
        folder = message_for(outcome(claude_only=True, claude_account_name=None), SwitchFeedback.HUD)
        self.assertEqual(folder.title, "Conta Claude: login da pasta")

    def test_claude_only_with_sessions_warns(self):
        message = message_for(outcome(claude_only=True, claude_account_name="Cliente X", claude_changed=False,
                                      running_claude_sessions=1), SwitchFeedback.HUD)
        self.assertIn("Reinicie-a para usar Cliente X.", message.body)


class FakeApplication:
    def __init__(self):
        self.sent, self.withdrawn = [], []

    def send_notification(self, identifier, notification):
        self.sent.append((identifier, notification))

    def withdraw_notification(self, identifier):
        self.withdrawn.append(identifier)


class PresenterTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApplication()
        self.timers = {}
        self.next_id = 0

        def schedule(milliseconds, callback):
            self.next_id += 1
            self.timers[self.next_id] = (milliseconds, callback)
            return self.next_id

        self.presenter = SwitchFeedbackPresenter(self.app, schedule=schedule,
                                                 cancel=lambda source: self.timers.pop(source, None))

    def fire(self):
        for _ms, callback in list(self.timers.values()):
            callback()
        self.timers.clear()

    def test_hud_is_sent_then_withdrawn_after_about_two_seconds(self):
        self.presenter.present(outcome(), SwitchFeedback.HUD)
        self.assertEqual([i for i, _ in self.app.sent], ["switch"])
        self.assertEqual([ms for ms, _ in self.timers.values()], [HUD_MILLISECONDS])
        self.assertTrue(1500 <= HUD_MILLISECONDS <= 2500)
        self.fire()
        self.assertEqual(self.app.withdrawn, ["switch"])

    def test_a_new_switch_replaces_the_previous_notification(self):
        self.presenter.present(outcome(), SwitchFeedback.HUD)
        self.presenter.present(outcome(profile_name="Trabalho"), SwitchFeedback.NOTIFICATION)
        self.assertEqual([i for i, _ in self.app.sent], ["switch", "switch"], "same id: the desktop replaces it")
        self.assertEqual(self.timers, {}, "the first HUD timer must not withdraw the second notification")

    def test_notification_and_session_warning_are_not_withdrawn(self):
        self.presenter.present(outcome(), SwitchFeedback.NOTIFICATION)
        self.presenter.present(outcome(running_claude_sessions=2), SwitchFeedback.HUD)
        self.assertEqual(self.timers, {})

    def test_none_sends_nothing(self):
        self.presenter.present(outcome(running_claude_sessions=2), SwitchFeedback.NONE)
        self.assertEqual(self.app.sent, [])

    def test_error_notification(self):
        self.presenter.notify_error("Não consegui trocar para Pessoal", "gh: keyring locked")
        self.assertEqual([i for i, _ in self.app.sent], ["error"])


class ModelSwitchOutcomeTests(SafeTestCase):
    """Every successful switch (window, tray, shortcuts) reaches the switch listeners
    with the number of Claude Code sessions counted right after it."""

    def setUp(self):
        super().setUp()
        from multicred.ui.model import AppModel

        self.system = base.FakeSystem(self.runner)
        (self.home / ".claude-pessoal").mkdir()
        git = GitIdentity(nickname="P", name="Dev", email="d@x.dev", color=ProfileColor.ORANGE)
        gh_work = GitHubAccount(nickname="E", username="dev-empresa", color=ProfileColor.INDIGO)
        gh = GitHubAccount(nickname="P", username="dev-pessoal", color=ProfileColor.ORANGE)
        self.token = ClaudeAccount(nickname="Cliente X", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
        self.secrets.set_secret(b"tok", self.token.secret_account)
        self.work = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git.id,
                            github_account_id=gh_work.id)
        self.personal = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git.id,
                                github_account_id=gh.id, claude_config_dir=f"{self.home}/.claude-pessoal")
        self.same_folder = Profile(name="Outro", color=ProfileColor.TEAL, git_identity_id=git.id)
        config = Config(git_identities=[git], github_accounts=[gh_work, gh], claude_accounts=[self.token],
                        profiles=[self.work, self.personal, self.same_folder])
        ConfigRepository(self.environment).save(config)
        self.model = AppModel(self.environment, base.FakeTools(), processes=FixedProcessScanner(2))
        self.model.load()
        self.outcomes = []
        self.model.switch_listeners.append(self.outcomes.append)

    def switch(self, start):
        done = threading.Event()
        errors = []
        start(lambda _result, error: (errors.append(error), done.set()))
        self.assertTrue(done.wait(10))
        return errors[0]

    def test_profile_switch_reports_the_outcome(self):
        self.assertIsNone(self.switch(lambda cb: self.model.switch_profile(self.personal.id, cb)))
        self.assertEqual(len(self.outcomes), 1)
        result = self.outcomes[0]
        self.assertEqual(result.profile_name, "Pessoal")
        self.assertEqual(result.running_claude_sessions, 2)
        self.assertTrue(result.claude_changed)
        self.assertFalse(result.claude_only)
        self.assertIn(K.GIT, result.steps)

    def test_same_folder_and_account_is_not_a_claude_change(self):
        self.switch(lambda cb: self.model.switch_profile(self.work.id, cb))
        self.switch(lambda cb: self.model.switch_profile(self.same_folder.id, cb))
        self.assertFalse(self.outcomes[-1].claude_changed)

    def test_claude_only_switch_reports_the_account(self):
        self.switch(lambda cb: self.model.switch_profile(self.work.id, cb))
        self.switch(lambda cb: self.model.switch_claude_only(self.token.id, cb))
        result = self.outcomes[-1]
        self.assertTrue(result.claude_only)
        self.assertTrue(result.claude_changed)
        self.assertEqual(result.claude_account_name, "Cliente X")

    def test_failed_switch_reports_nothing(self):
        self.system.fail_gh_switch_to = "dev-pessoal"
        self.assertIsNotNone(self.switch(lambda cb: self.model.switch_profile(self.personal.id, cb)))
        self.assertEqual(self.outcomes, [])
