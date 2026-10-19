"""S-06: pure edits of the configuration for the Contas and profile screens.

Port of `ConfigEditorTests` (Phase4CoreTests.swift) plus the rules the aceite names:
the editors reject what the Mac rejects, API key values live only in the secret
store, and removing a key removes it from the profiles and from the store. The
AppModel tests cover the save path the window uses (no GTK).
"""

import json
import threading
import unittest

from multicred.core.adapters.github_cli import GitHubLogin
from multicred.core.editors import ConfigEditor, EditorError
from multicred.core.models import (APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config,
                                   GitHubAccount, GitIdentity, Profile, ProfileColor, uuid_text)
from multicred.core.storage import ConfigRepository, dumps
from multicred.core.tools import Tool
from multicred.core.validation import issues_in
from tests.support import SafeTestCase


class BrokenSecretStore:
    """A Secret Service that refuses every write (locked keyring, no daemon)."""

    def __init__(self):
        self.deleted = []

    def set_secret(self, value, account):
        raise RuntimeError("keyring locked")

    def contains_item(self, account):
        return False

    def delete_item(self, account):
        raise RuntimeError("keyring locked")

    def all_accounts(self):
        return []


def wait_for(condition, seconds=10):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def kinds(error):
    return [issue.kind for issue in error.issues]


class ConfigEditorTests(SafeTestCase):
    # MARK: ported from Phase4CoreTests.swift

    def test_api_key_lifecycle_keeps_value_only_in_the_secret_store(self):
        config, key = ConfigEditor.add_api_key("NPU", APIProvider.OPENAI, "OPENAI_API_KEY", "  sk-123  ",
                                               ProfileColor.INDIGO, Config(), self.secrets)
        self.assertEqual(self.secrets.stored_value(key.secret_account), b"sk-123")
        self.assertNotIn("sk-123", dumps(config.to_dict()).decode(), "the secret never reaches config.json")

        ConfigEditor.replace_secret(key, "sk-456", self.secrets)
        self.assertEqual(self.secrets.stored_value(key.secret_account), b"sk-456")

        config.profiles.append(Profile(name="P", color=ProfileColor.INDIGO, api_key_ids=[key.id]))
        removed = ConfigEditor.remove_api_key(key.id, config)
        self.assertEqual(removed.api_keys, [])
        self.assertEqual(removed.profiles[0].api_key_ids, [])
        self.assertEqual(self.secrets.stored_value(key.secret_account), b"sk-456",
                         "S-24: pure; the secret stays until the caller has saved")
        self.assertTrue(ConfigEditor.discard_removed_key(key, self.secrets))
        self.assertIsNone(self.secrets.stored_value(key.secret_account))

    def test_invalid_key_is_not_stored(self):
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.add_api_key("x", APIProvider.OTHER, "bad name", "v", ProfileColor.RED, Config(), self.secrets)
        self.assertEqual(caught.exception.kind, "invalid")
        self.assertEqual(kinds(caught.exception), ["invalidEnvVar"])
        self.assertEqual(self.secrets.all_accounts(), [])
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.add_api_key("x", APIProvider.OPENAI, "OPENAI_API_KEY", "  ", ProfileColor.RED, Config(),
                                     self.secrets)
        self.assertEqual(caught.exception, EditorError.empty_secret())
        self.assertEqual(self.secrets.all_accounts(), [])

    def test_one_key_per_provider_in_a_profile(self):
        a = APIKeyAccount(nickname="A", provider=APIProvider.OPENAI, color=ProfileColor.RED)
        b = APIKeyAccount(nickname="B", provider=APIProvider.OPENAI, color=ProfileColor.RED)
        g = APIKeyAccount(nickname="G", provider=APIProvider.GEMINI, color=ProfileColor.RED)
        config = Config(api_keys=[a, b, g])
        profile = Profile(name="P", color=ProfileColor.RED, api_key_ids=[a.id, g.id])
        profile = ConfigEditor.set_api_key(b.id, APIProvider.OPENAI, profile, config)
        self.assertEqual(set(profile.api_key_ids), {b.id, g.id})
        profile = ConfigEditor.set_api_key(None, APIProvider.GEMINI, profile, config)
        self.assertEqual(profile.api_key_ids, [b.id])

    def test_removing_accounts_clears_profile_references(self):
        git = GitIdentity(nickname="g", name="G", email="g@x.com", color=ProfileColor.RED)
        gh = GitHubAccount(nickname="h", username="h", color=ProfileColor.RED)
        config = Config(git_identities=[git], github_accounts=[gh], profiles=[
            Profile(name="P", color=ProfileColor.RED, git_identity_id=git.id, github_account_id=gh.id)])
        config = ConfigEditor.remove_git_identity(git.id, config)
        config = ConfigEditor.remove_github_account(gh.id, config)
        self.assertIsNone(config.profiles[0].git_identity_id)
        self.assertIsNone(config.profiles[0].github_account_id)
        self.assertEqual(issues_in(config), [])

    def test_add_missing_github_accounts_is_case_insensitive(self):
        config = Config(github_accounts=[GitHubAccount(nickname="a", username="Rafael-Leivas", color=ProfileColor.RED)])
        logins = [GitHubLogin("github.com", "rafael-leivas", True, "success"),
                  GitHubLogin("github.com", "NPU-Rafael", False, "success")]
        updated = ConfigEditor.add_missing_github_accounts(logins, config)
        self.assertEqual([a.username for a in updated.github_accounts], ["Rafael-Leivas", "NPU-Rafael"])
        self.assertEqual(updated.github_accounts[1].nickname, "NPU-Rafael")
        self.assertEqual(updated.github_accounts[1].color, ProfileColor.BLUE, "next color of the palette")
        self.assertEqual(len(config.github_accounts), 1, "the input is not changed")

    # MARK: rejects what the Mac rejects (aceite)

    def test_rejects_invalid_email_github_user_host_and_ssh_path(self):
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(GitIdentity(nickname="T", name="Dev", email="sem-arroba", color=ProfileColor.RED),
                                Config())
        self.assertEqual(kinds(caught.exception), ["invalidEmail"])
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(GitIdentity(nickname="T", name="Dev", email="d@x.com", color=ProfileColor.RED,
                                            ssh_key_path="id_rsa"), Config())
        self.assertEqual(kinds(caught.exception), ["invalidSSHKeyPath"])
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(GitHubAccount(nickname="T", username="dev_user", color=ProfileColor.RED), Config())
        self.assertEqual(kinds(caught.exception), ["invalidGitHubUsername"])
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(GitHubAccount(nickname="T", username="dev", host="-evil", color=ProfileColor.RED),
                                Config())
        self.assertEqual(kinds(caught.exception), ["invalidHost"])

    def test_rejects_reserved_variable_and_duplicate_variable_in_a_profile(self):
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.add_api_key("x", APIProvider.OTHER, "PATH", "v", ProfileColor.RED, Config(), self.secrets)
        self.assertEqual(kinds(caught.exception), ["reservedEnvVar"])
        self.assertEqual(self.secrets.all_accounts(), [], "nothing stored for a rejected key")

        a = APIKeyAccount(nickname="A", provider=APIProvider.OPENAI, color=ProfileColor.RED)
        b = APIKeyAccount(nickname="B", provider=APIProvider.OTHER, env_var="OPENAI_API_KEY", color=ProfileColor.RED)
        config = Config(api_keys=[a, b])
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(Profile(name="P", color=ProfileColor.RED, api_key_ids=[a.id, b.id]), config)
        self.assertEqual(kinds(caught.exception), ["duplicateEnvVarInProfile"])

    def test_rejects_empty_profile_name_and_missing_reference(self):
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(Profile(name="  ", color=ProfileColor.RED), Config())
        self.assertEqual(kinds(caught.exception), ["emptyField"])
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(Profile(name="P", color=ProfileColor.RED, git_identity_id=GitIdentity(
                nickname="x", name="x", email="x@x", color=ProfileColor.RED).id), Config())
        self.assertEqual([(i.kind, i.detail) for i in caught.exception.issues], [("missingReference", "git")])

    def test_rejects_non_canonical_claude_folder(self):
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.upsert(Profile(name="P", color=ProfileColor.RED, claude_config_dir="/tmp/x/"), Config())
        self.assertEqual(kinds(caught.exception), ["invalidClaudeDirectory"])

    # MARK: Linux port details

    def test_upsert_replaces_by_id_appends_new_and_never_mutates_the_input(self):
        identity = GitIdentity(nickname="T", name="Dev", email="d@x.com", color=ProfileColor.RED)
        config = ConfigEditor.upsert(identity, Config())
        edited = GitIdentity(id=identity.id, nickname="T2", name="Dev 2", email="d2@x.com", color=ProfileColor.TEAL,
                             ssh_key_path="   ")
        updated = ConfigEditor.upsert(edited, config)
        self.assertEqual(len(updated.git_identities), 1)
        self.assertEqual(updated.git_identities[0].email, "d2@x.com")
        self.assertIsNone(updated.git_identities[0].ssh_key_path, "a blank key path means no key")
        self.assertEqual(config.git_identities[0].email, "d@x.com", "the old config is untouched")
        self.assertEqual(edited.ssh_key_path, "   ", "the argument is untouched")

        profile = Profile(name="P", color=ProfileColor.RED, git_identity_id=identity.id)
        with_profile = ConfigEditor.upsert(profile, updated)
        renamed = ConfigEditor.upsert(Profile(id=profile.id, name="Q", color=ProfileColor.RED), with_profile)
        self.assertEqual([p.name for p in renamed.profiles], ["Q"])
        self.assertEqual(ConfigEditor.remove_profile(profile.id, renamed).profiles, [])
        self.assertEqual(len(renamed.profiles), 1)

    def test_upsert_of_api_key_and_claude_account(self):
        key = APIKeyAccount(nickname="K", provider=APIProvider.OPENAI, color=ProfileColor.RED)
        config = ConfigEditor.upsert(key, Config())
        key.env_var = "MY_OPENAI"
        self.assertEqual(config.api_keys[0].env_var, "OPENAI_API_KEY", "the stored copy is independent")
        config = ConfigEditor.upsert(key, config)
        self.assertEqual(config.api_keys[0].env_var, "MY_OPENAI")
        with self.assertRaises(EditorError):
            ConfigEditor.upsert(APIKeyAccount(id=key.id, nickname="K", provider=APIProvider.OPENAI,
                                              env_var="HOME", color=ProfileColor.RED), config)

        account = ClaudeAccount(nickname="C", color=ProfileColor.RED, kind=ClaudeAccountKind.TOKEN)
        config = ConfigEditor.update_claude_account(account, config)
        with self.assertRaises(EditorError):
            ConfigEditor.update_claude_account(ClaudeAccount(id=account.id, nickname="C", email="nope",
                                                             color=ProfileColor.RED), config)

    def test_secret_store_failure_is_reported_and_nothing_is_added(self):
        broken = BrokenSecretStore()
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.add_api_key("K", APIProvider.OPENAI, "OPENAI_API_KEY", "sk", ProfileColor.RED, Config(),
                                     broken)
        self.assertEqual(caught.exception.kind, "secrets")
        self.assertNotIn("sk", str(caught.exception).replace("secrets", ""))
        key = APIKeyAccount(nickname="K", provider=APIProvider.OPENAI, color=ProfileColor.RED)
        config = Config(api_keys=[key])
        # Removing is pure; a store that refuses the delete afterwards leaves a harmless
        # orphan (no configuration points at it), it does not undo the saved removal.
        self.assertEqual(ConfigEditor.remove_api_key(key.id, config).api_keys, [])
        self.assertFalse(ConfigEditor.discard_removed_key(key, broken))
        with self.assertRaises(EditorError) as caught:
            ConfigEditor.replace_secret(key, " \n", self.secrets)
        self.assertEqual(caught.exception.kind, "emptySecret")

    def test_removing_an_unknown_key_changes_nothing(self):
        config = Config()
        self.assertIs(ConfigEditor.remove_api_key(APIKeyAccount(nickname="x", provider=APIProvider.OPENAI,
                                                                color=ProfileColor.RED).id, config),
                      config)


class FakeTools:
    def __init__(self, installed=(Tool.GIT, Tool.GH, Tool.CLAUDE)):
        self.installed = set(installed)

    def locate(self, tool):
        return f"/fake/bin/{tool.value}" if tool in self.installed else None

    def search_directories(self):
        return ["/fake/bin"]


class AppModelEditTests(SafeTestCase):
    """The save path of the window: through ConfigRepository, with notify()."""

    def setUp(self):
        super().setUp()
        from multicred.ui.model import AppModel
        self.repository = ConfigRepository(self.environment)
        identity = GitIdentity(nickname="T", name="Dev", email="d@x.com", color=ProfileColor.INDIGO)
        self.profile = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=identity.id)
        self.repository.save(Config(git_identities=[identity], profiles=[self.profile]))
        self.model = AppModel(self.environment, FakeTools())
        self.model.load()
        self.notified = []
        self.model.add_listener(lambda: self.notified.append(True))

    def saved(self):
        return json.loads(self.repository.path.read_text())

    def wait(self, start):
        done = threading.Event()
        outcome = {}

        def on_done(result, error):
            outcome.update(result=result, error=error)
            done.set()

        start(on_done)
        self.assertTrue(done.wait(5))
        return outcome

    def test_edit_saves_through_the_repository_and_notifies(self):
        profile = Profile.from_dict(self.profile.to_dict())
        profile.name = "Empresa"
        self.model.edit(lambda config: ConfigEditor.upsert(profile, config))
        self.assertEqual(self.saved()["profiles"][0]["name"], "Empresa")
        self.assertEqual(self.model.config.profiles[0].name, "Empresa")
        self.assertTrue(self.notified)

    def test_removing_a_key_of_the_active_profile_reapplies_it_so_terminals_drop_it(self):
        from multicred.core.models import APIKeyAccount, APIProvider, ActiveState
        key = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.secrets.set_secret(b"sk", key.secret_account)
        config = self.model.config
        config.api_keys.append(key)
        config.profiles[0].api_key_ids.append(key.id)
        self.repository.save(config)
        self.model.state = ActiveState(profile_id=self.profile.id, generation=1)
        reapplied = []
        self.model.reapply_active_profile = lambda on_done=None: reapplied.append(True) or True
        self.model.remove_api_key(key.id)
        self.assertEqual(reapplied, [True], "the active profile's env files drop the key now")

        other = APIKeyAccount(nickname="G", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
        self.secrets.set_secret(b"g", other.secret_account)
        config = self.model.config
        config.api_keys.append(other)
        self.repository.save(config)
        self.model.remove_api_key(other.id)
        self.assertEqual(reapplied, [True], "a key no profile in use exported needs no re-apply")

    def test_removing_a_key_during_a_switch_reapplies_once_when_the_switch_ends(self):
        """S-27: the re-apply is not skipped while a switch runs: it waits for it."""
        from multicred.core.engine.types import ClaudeTarget, SwitchResult
        from multicred.core.models import APIKeyAccount, APIProvider, ActiveState
        key = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.secrets.set_secret(b"sk", key.secret_account)
        config = self.model.config
        config.api_keys.append(key)
        config.profiles[0].api_key_ids.append(key.id)
        self.repository.save(config)
        state = ActiveState(profile_id=self.profile.id, generation=1)
        self.model.state = state
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        class SlowEngine:
            def switch_profile(self, _profile_id, _config):
                entered.set()
                release.wait(10)
                return SwitchResult(state=state, claude_target=ClaudeTarget("unchanged"))
        self.model.engine = SlowEngine()
        reapplied = []
        # Like the real one: refuses (False) while a switch runs, else starts a re-apply.
        self.model.reapply_active_profile = (
            lambda on_done=None: False if self.model.switching else (reapplied.append(True) or True))
        self.model.switch_profile(self.profile.id, lambda _result, _error: done.set())
        self.assertTrue(entered.wait(10))

        self.model.remove_api_key(key.id)
        self.assertEqual(reapplied, [], "not while the switch runs")
        release.set()
        self.assertTrue(done.wait(10))
        self.assertEqual(reapplied, [True], "once the switch ended")

        self.model.switch_profile(self.profile.id, lambda _result, _error: None)
        self.assertTrue(wait_for(lambda: not self.model.switching))
        self.assertEqual(reapplied, [True], "only once")

    def test_removing_a_key_whose_save_fails_keeps_its_secret(self):
        """S-24: the secret is deleted only after the configuration without the key is saved."""
        from multicred.core.models import APIKeyAccount, APIProvider
        key = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.secrets.set_secret(b"sk", key.secret_account)
        config = self.model.config
        config.api_keys.append(key)
        self.repository.save(config)
        self.model.load()
        self.repository.path.unlink()
        self.repository.path.mkdir()  # config.json is now a directory: the atomic write refuses it
        with self.assertRaises(OSError):
            self.model.remove_api_key(key.id)
        self.assertEqual(self.secrets.stored_value(key.secret_account), b"sk", "the secret is still there")
        self.assertIsNotNone(self.model.config.api_key(key.id), "and the configuration still points to it")

        self.repository.path.rmdir()
        self.repository.save(self.model.config)
        self.model.remove_api_key(key.id)
        self.assertIsNone(self.secrets.stored_value(key.secret_account), "deleted after the save")
        self.assertEqual(self.saved()["apiKeys"], [])

    def test_failed_edit_keeps_config_and_file(self):
        before = self.repository.path.read_bytes()
        with self.assertRaises(EditorError):
            self.model.edit(lambda config: ConfigEditor.upsert(Profile(name="", color=ProfileColor.RED), config))
        self.assertEqual(self.repository.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.model.config.profiles], ["Trabalho"])

    def test_add_api_key_stores_value_only_in_the_secret_store(self):
        key = self.model.add_api_key("Pessoal", APIProvider.OPENAI, "OPENAI_API_KEY", "sk-segredo-1", ProfileColor.RED)
        self.assertEqual(self.secrets.stored_value(key.secret_account), b"sk-segredo-1")
        self.assertNotIn("sk-segredo-1", self.repository.path.read_text())
        self.assertEqual(self.saved()["apiKeys"][0]["envVar"], "OPENAI_API_KEY")
        self.assertTrue(self.model.has_secret(key))
        self.model.replace_secret(key, "sk-segredo-2")
        self.assertEqual(self.secrets.stored_value(key.secret_account), b"sk-segredo-2")
        self.model.remove_api_key(key.id)
        self.assertEqual(self.saved()["apiKeys"], [])
        self.assertEqual(self.secrets.all_accounts(), [])

    def test_add_api_key_removes_the_secret_when_saving_fails(self):
        self.repository.path.unlink()
        self.repository.path.mkdir()  # config.json is now a directory: the atomic write refuses it
        with self.assertRaises(OSError):
            self.model.add_api_key("K", APIProvider.OPENAI, "OPENAI_API_KEY", "sk-x", ProfileColor.RED)
        self.assertEqual(self.secrets.all_accounts(), [], "no orphan secret")
        self.assertEqual(self.model.config.api_keys, [])

    def test_import_github_accounts_runs_gh_off_the_ui_thread(self):
        self.runner.on("gh", ["auth", "status"], stdout=json.dumps({"hosts": {"github.com": [
            {"host": "github.com", "login": "dev-empresa", "active": True, "state": "success"},
            {"host": "github.com", "login": "dev-pessoal", "active": False, "state": "success"}]}}))
        threads = []
        self.model.dispatch = lambda fn, *args: (threads.append(threading.current_thread()), fn(*args))
        outcome = self.wait(self.model.import_github_accounts)
        self.assertIsNone(outcome["error"])
        self.assertEqual(outcome["result"], 2)
        self.assertIsNot(threads[0], threading.main_thread())
        self.assertEqual([a["username"] for a in self.saved()["githubAccounts"]], ["dev-empresa", "dev-pessoal"])
        self.assertEqual([l.login for l in self.model.github_logins], ["dev-empresa", "dev-pessoal"])
        again = self.wait(self.model.import_github_accounts)
        self.assertEqual(again["result"], 0)

    def test_import_without_gh_reports_it(self):
        self.model.tools = FakeTools(installed=())
        outcome = self.wait(self.model.import_github_accounts)
        self.assertEqual(getattr(outcome["error"], "kind", None), "toolMissing")

    def test_claude_statuses_are_resolved_off_the_ui_thread(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "d@x.com"}}))
        self.model.tools = FakeTools(installed=())
        self.wait(lambda done: self.model.refresh_claude_statuses(lambda: done(None, None)))
        self.assertEqual(self.model.folder_status(None).email, "d@x.com")
        self.assertFalse(self.model.needs_claude_login(self.model.config.profiles[0]))

    def test_remove_claude_token_account_deletes_its_secret_and_clears_profiles(self):
        account = ClaudeAccount(nickname="Marco", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
        self.secrets.set_secret(b"sk-ant-oat01-xxxxxxxxxxxxxxxxxxxx", account.secret_account)
        profile = Profile.from_dict(self.model.config.profiles[0].to_dict())
        profile.claude_account_id = account.id
        self.model.edit(lambda c: ConfigEditor.upsert(profile, ConfigEditor.update_claude_account(account, c)))
        self.model.remove_claude_account(account.id)
        self.assertEqual(self.saved()["claudeAccounts"], [])
        self.assertNotIn("claudeAccountId", self.saved()["profiles"][0])
        self.assertEqual(self.secrets.all_accounts(), [])

    # MARK: S-13: removing a login account is transactional

    def live_login_account(self):
        from multicred.core.claude.login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher
        account = ClaudeAccount(nickname="Dev", color=ProfileColor.BLUE, kind=ClaudeAccountKind.LOGIN)
        self.secrets.set_secret(b"sk-ant-oat01-leftover-token-xxxxxxxx", account.secret_account)
        folder = str(self.home / ".claude-p")
        (self.home / ".claude-p").mkdir()
        self.claude_store.write_folder_item(folder, b'{"claudeAiOauth":{"accessToken":"own"}}')
        switcher = ClaudeLoginSwitcher(self.environment)
        switcher.store_new_account(account.id, ClaudeLoginRecord(b'{"accessToken":"sk-ant-oat01-dev"}', None))
        switcher.place(account.id, folder)
        self.model.edit(lambda c: ConfigEditor.update_claude_account(account, c))
        return account, folder, switcher

    def test_remove_login_account_keeps_everything_when_saving_fails(self):
        account, folder, switcher = self.live_login_account()
        self.repository.path.unlink()
        self.repository.path.mkdir()  # config.json is now a directory: the atomic write refuses it
        with self.assertRaises(OSError):
            self.model.remove_claude_account(account.id)
        self.assertEqual([a.id for a in self.model.config.claude_accounts], [account.id])
        self.assertIsNotNone(self.claude_store.read_vault(uuid_text(account.id)), "the login is not lost")
        self.assertIsNotNone(self.secrets.stored_value(account.secret_account))
        live = json.loads(self.claude_store.read_folder_item(folder))["claudeAiOauth"]["accessToken"]
        self.assertEqual(live, "sk-ant-oat01-dev", "the folder holds the account again, as the config says")
        self.assertEqual(switcher.occupancy().account_in(folder), account.id)

    def test_remove_login_account_with_an_unreadable_claude_folders_file_removes_nothing(self):
        from multicred.core.claude import login_vault
        from multicred.core.paths import AppPaths
        account, folder, _switcher = self.live_login_account()
        AppPaths(self.environment).claude_folders.write_text("{broken")
        with self.assertRaises(login_vault.OccupancyUnreadable):
            self.model.remove_claude_account(account.id)
        self.assertEqual([a["id"] for a in self.saved()["claudeAccounts"]], [uuid_text(account.id)])
        self.assertIsNotNone(self.claude_store.read_vault(uuid_text(account.id)))

    def test_remove_login_account_gives_the_folder_back_then_deletes_the_login(self):
        account, folder, switcher = self.live_login_account()
        self.model.remove_claude_account(account.id)
        self.assertEqual(self.saved()["claudeAccounts"], [])
        self.assertIsNone(self.claude_store.read_vault(uuid_text(account.id)))
        self.assertIsNone(self.secrets.stored_value(account.secret_account))
        self.assertEqual(json.loads(self.claude_store.read_folder_item(folder))["claudeAiOauth"], {"accessToken": "own"})
        self.assertIsNone(switcher.occupancy().account_in(folder))


if __name__ == "__main__":
    unittest.main()
