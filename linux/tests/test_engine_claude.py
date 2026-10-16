"""S-04: the Claude parts of the switch engine (the Claude cases of SwitchEngineTests.swift).

Token accounts are exported as CLAUDE_CODE_OAUTH_TOKEN through the env files;
login accounts go into the profile folder (claudeLogin step); co-authorship
follows the profile's GitHub account (claudeSettings step).
"""

import json
import os
import uuid
from datetime import timedelta

import tests.test_engine as base
from multicred.core.claude.login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.engine.types import Preflight, StepFailed, SwitchStepKind
from multicred.core.models import (
    APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, ClaudeFolder, Config, GitHubAccount, GitIdentity,
    Profile, ProfileColor,
)
from multicred.core.paths import AppPaths
from tests.support import FIXED_NOW, SafeTestCase

K = SwitchStepKind
TOKEN_EXPORT = 'CLAUDE_CODE_OAUTH_TOKEN="$('


class ClaudeEngineTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.system = base.FakeSystem(self.runner)
        self.paths = AppPaths(self.environment)
        self.personal_dir = f"{self.home}/.claude-pessoal"
        os.makedirs(self.personal_dir)
        git_work = GitIdentity(nickname="Empresa", name="Dev Trabalho", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
        git_personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.dev", color=ProfileColor.ORANGE)
        gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
        gh_personal = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE)
        # A credential-only Claude account (token), as for "use Marco's account".
        self.marco = ClaudeAccount(nickname="Marco", email="marco@x.com", color=ProfileColor.TEAL)
        openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        gemini = APIKeyAccount(nickname="Pessoal", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
        self.secrets.set_secret(b"sk", openai.secret_account)
        self.secrets.set_secret(b"g", gemini.secret_account)
        self.secrets.set_secret(b"sk-ant-oat01-marco", self.marco.secret_account)
        # Work uses ~/.claude (default); Personal its own folder. Both use the folder's own login.
        self.work = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git_work.id,
                            github_account_id=gh_work.id, api_key_ids=[openai.id])
        self.personal = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git_personal.id,
                                github_account_id=gh_personal.id, claude_config_dir=self.personal_dir,
                                api_key_ids=[gemini.id])
        self.config = Config(git_identities=[git_work, git_personal], github_accounts=[gh_work, gh_personal],
                             claude_accounts=[self.marco], api_keys=[openai, gemini],
                             profiles=[self.work, self.personal])

    def engine(self):
        return SwitchEngine(self.environment, base.FakeTools())

    def env_sh(self):
        return self.read(self.paths.env_sh)

    @property
    def settings(self):
        return f"{self.personal_dir}/settings.json"

    # MARK: step order

    def test_profile_switch_runs_the_claude_steps_in_order(self):
        result = self.engine().switch_profile(self.personal.id, self.config)
        self.assertEqual(result.applied_steps, [K.GIT, K.GITHUB, K.SESSION, K.CLAUDE_SETTINGS, K.ENV_FILES, K.STATE])

        login = ClaudeAccount(nickname="Login", color=ProfileColor.BLUE, kind=ClaudeAccountKind.LOGIN)
        ClaudeLoginSwitcher(self.environment).store_new_account(
            login.id, ClaudeLoginRecord(b'{"accessToken":"sk-ant-oat01-l"}', None))
        self.config.claude_accounts.append(login)
        self.personal.claude_account_id = login.id
        result = self.engine().switch_profile(self.personal.id, self.config)
        self.assertEqual(result.applied_steps,
                         [K.GIT, K.GITHUB, K.SESSION, K.CLAUDE_SETTINGS, K.CLAUDE_LOGIN, K.ENV_FILES, K.STATE])
        self.assertEqual(result.state.applied_claude_account_id, login.id)

        # Leaving a folder that holds an account puts its own login back, even with no account chosen.
        self.personal.claude_account_id = None
        result = self.engine().switch_profile(self.personal.id, self.config)
        self.assertIn(K.CLAUDE_LOGIN, result.applied_steps)
        self.assertIsNone(ClaudeLoginSwitcher(self.environment).occupancy().account_in(self.personal_dir))

    # MARK: token accounts

    def test_claude_only_switch_changes_the_token_not_the_folder(self):
        """The user's case: personal profile, then "use Marco's account". Agents,
        skills and chats (the folder) stay; only the credential changes."""
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        git_before = self.read(self.paths.gitconfig)
        commands_before = len(self.runner.requests)

        result = engine.switch_claude_only(self.marco.id, self.config)
        self.assertEqual(result.applied_steps, [K.ENV_FILES, K.STATE], "no session change: the folder stays")
        self.assertEqual(result.state.profile_id, self.personal.id)
        self.assertEqual(result.state.claude_override_id, self.marco.id)
        self.assertEqual(result.state.applied_claude_account_id, self.marco.id)
        self.assertEqual(self.system.claude, self.personal_dir)
        self.assertEqual(self.read(self.paths.gitconfig), git_before)
        self.assertEqual(self.runner.requests[commands_before:], [], "no gh, no systemctl")

        env_sh = self.env_sh()
        self.assertIn(f"export CLAUDE_CONFIG_DIR='{self.personal_dir}'", env_sh, "same folder")
        self.assertIn(f"CLAUDE_CODE_OAUTH_TOKEN=\"$('/usr/bin/secret-tool' lookup service 'app.multicred.apikey' "
                      f"account '{self.marco.secret_account}'", env_sh)
        self.assertNotIn("sk-ant-oat01-marco", env_sh, "the token itself never goes into the file")
        self.assertIn('GEMINI_API_KEY="$(', env_sh, "API keys still follow the profile")
        self.assertIn("set -gx CLAUDE_CODE_OAUTH_TOKEN", self.read(self.paths.env_fish))

        reapplied = engine.reapply_active_profile(self.config)
        self.assertEqual(reapplied.state.claude_override_id, self.marco.id, "reapply at login keeps the account")
        self.assertIn(TOKEN_EXPORT, self.env_sh())

        # Back to the folder's own login; the token variable is unset.
        back = engine.switch_claude_only(None, self.config)
        self.assertIsNone(back.state.applied_claude_account_id)
        self.assertNotIn(TOKEN_EXPORT, self.env_sh())
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN GEMINI_API_KEY OPENAI_API_KEY", self.env_sh())

    def test_profile_with_a_token_account_uses_it_and_next_profile_clears_it(self):
        self.personal.claude_account_id = self.marco.id
        engine = self.engine()
        first = engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(first.state.applied_claude_account_id, self.marco.id)
        self.assertIn(TOKEN_EXPORT, self.env_sh())

        second = engine.switch_profile(self.work.id, self.config)
        self.assertIsNone(second.state.applied_claude_account_id)
        self.assertNotIn(TOKEN_EXPORT, self.env_sh())

    def test_next_profile_switch_clears_the_override(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.marco.id, self.config)
        result = engine.switch_profile(self.work.id, self.config)
        self.assertIsNone(result.state.claude_override_id)
        self.assertIsNone(result.state.applied_claude_account_id)
        self.assertNotIn(TOKEN_EXPORT, self.env_sh())

    def test_folder_login_override_beats_a_profile_token_account(self):
        self.personal.claude_account_id = self.marco.id
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)

        override = ClaudeFolder.override_for(None, self.personal)
        self.assertEqual(override, ClaudeFolder.OWN_LOGIN_OVERRIDE)
        result = engine.switch_claude_only(override, self.config)
        self.assertIsNone(result.state.applied_claude_account_id)
        self.assertNotIn(TOKEN_EXPORT, self.env_sh())
        reapplied = engine.reapply_active_profile(self.config)
        self.assertIsNone(reapplied.state.applied_claude_account_id, "kept at login")
        self.assertEqual(reapplied.state.claude_override_id, ClaudeFolder.OWN_LOGIN_OVERRIDE)

        self.assertIsNone(ClaudeFolder.override_for(self.marco.id, self.personal), "the profile default needs no override")

    def test_reapply_drops_an_override_for_a_removed_account(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.marco.id, self.config)
        self.config.claude_accounts = []  # e.g. removed by the v1 -> v2 migration

        result = engine.reapply_active_profile(self.config)
        self.assertEqual(result.state.profile_id, self.personal.id)
        self.assertIsNone(result.state.claude_override_id)
        self.assertIsNone(result.state.applied_claude_account_id)

    def test_missing_token_is_a_preflight_failure(self):
        self.secrets.delete_item(self.marco.secret_account)
        self.engine().switch_profile(self.personal.id, self.config)
        before = self.env_sh()
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_claude_only(self.marco.id, self.config)
        self.assertEqual((raised.exception.failure.kind, raised.exception.failure.detail), ("claudeTokenMissing", "Marco"))
        self.assertEqual(self.env_sh(), before, "nothing changed")

    def test_unknown_account_is_a_preflight_failure(self):
        self.engine().switch_profile(self.personal.id, self.config)
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_claude_only(uuid.uuid4(), self.config)
        self.assertEqual(raised.exception.failure.kind, "claudeAccountNotFound")

    def test_generation_increases_and_activated_at_only_changes_on_profile_switch(self):
        clock = [FIXED_NOW]
        object.__setattr__(self.environment, "clock", lambda: clock[0])
        engine = self.engine()
        first = engine.switch_profile(self.work.id, self.config)
        clock[0] = clock[0] + timedelta(hours=1)
        second = engine.switch_claude_only(self.marco.id, self.config)
        self.assertEqual((first.state.generation, second.state.generation), (1, 2))
        self.assertEqual(second.state.activated_at, first.state.activated_at)
        self.assertEqual(self.read(self.paths.generation), "2\n")
        third = engine.switch_profile(self.work.id, self.config)
        self.assertEqual(third.state.activated_at, clock[0])

    # MARK: co-author (D38)

    def test_claude_co_author_follows_the_profiles_github_account(self):
        with open(self.settings, "w") as f:
            f.write('{"model":"opus"}')
        self.config.github_accounts[1].claude_co_author = False  # personal GitHub account

        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        written = json.loads(self.read(self.settings))
        self.assertEqual(written["attribution"], {"commit": "", "pr": ""})
        self.assertEqual(written["model"], "opus", "the rest of settings.json is kept")
        self.assertEqual(self.read(self.settings + ".multicred-backup"), '{"model":"opus"}', "original kept once")
        self.assertEqual(self.mode_of(self.settings + ".multicred-backup"), 0o600)

        # Switching only the Claude account never touches settings.json.
        before = self.read(self.settings)
        engine.switch_claude_only(self.marco.id, self.config)
        self.assertEqual(self.read(self.settings), before)

        # Opting back in removes only what MultiCred wrote.
        self.config.github_accounts[1].claude_co_author = True
        engine.switch_profile(self.personal.id, self.config)
        restored = json.loads(self.read(self.settings))
        self.assertNotIn("attribution", restored)
        self.assertEqual(restored["model"], "opus")
        self.assertEqual(self.read(self.settings + ".multicred-backup"), '{"model":"opus"}', "the backup is not rewritten")

    def test_co_author_change_is_rolled_back_when_a_later_step_fails(self):
        self.config.github_accounts[1].claude_co_author = False
        # Make the env files step fail: its target is a directory.
        self.paths.env_sh.mkdir(parents=True)
        with self.assertRaises(StepFailed) as raised:
            self.engine().switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.ENV_FILES)
        self.assertTrue(raised.exception.rollback.complete)
        self.assertFalse(os.path.exists(self.settings), "the new settings.json was removed again")

    def test_existing_settings_are_restored_on_rollback(self):
        with open(self.settings, "w") as f:
            f.write('{"model":"opus"}')
        self.config.github_accounts[1].claude_co_author = False
        self.paths.env_sh.mkdir(parents=True)
        with self.assertRaises(StepFailed):
            self.engine().switch_profile(self.personal.id, self.config)
        self.assertEqual(self.read(self.settings), '{"model":"opus"}')

    def test_unreadable_settings_are_left_untouched(self):
        with open(self.settings, "w") as f:
            f.write("{not json")
        self.config.github_accounts[1].claude_co_author = False
        result = self.engine().switch_profile(self.personal.id, self.config)
        self.assertIn(K.CLAUDE_SETTINGS, result.applied_steps)
        self.assertEqual(self.read(self.settings), "{not json")
        self.assertFalse(os.path.exists(self.settings + ".multicred-backup"))

    def test_symlinked_settings_are_written_at_their_destination(self):
        real = self.home / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"model":"opus"}')
        os.symlink(real, self.settings)
        self.config.github_accounts[1].claude_co_author = False
        self.engine().switch_profile(self.personal.id, self.config)
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(json.loads(real.read_text())["attribution"], {"commit": "", "pr": ""})

    def test_profile_without_github_does_not_touch_settings(self):
        self.personal.github_account_id = None
        self.config.github_accounts[1].claude_co_author = False
        result = self.engine().switch_profile(self.personal.id, self.config)
        self.assertNotIn(K.CLAUDE_SETTINGS, result.applied_steps)
        self.assertFalse(os.path.exists(self.settings))
