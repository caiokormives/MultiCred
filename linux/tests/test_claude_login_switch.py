"""S-04: live Claude account switching (D39), with the in-memory store and with real
`.credentials.json` files in the temporary HOME (ClaudeLoginSwitchTests.swift)."""

import json
import os
import stat
import unittest
from pathlib import Path

import tests.test_engine as base
from multicred.core import claude_paths
from multicred.core.claude.credentials import (
    CLAUDE_LOGIN_SERVICE, FileClaudeCredentialStore, InMemoryClaudeCredentialStore, InMemoryVaultBackend,
)
from multicred.core.claude.login_vault import (
    ClaudeFolderOccupancy, ClaudeLoginRecord, ClaudeLoginSwitcher, NoLoginInFolder,
)
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.engine.types import Preflight, StepFailed, SwitchStepKind
from multicred.core.models import (
    ClaudeAccount, ClaudeAccountKind, ClaudeFolder, Config, GitIdentity, Profile, ProfileColor, uuid_text,
)
from multicred.core.paths import AppPaths
from tests.support import SafeTestCase

K = SwitchStepKind


def oauth(name):
    return {"accessToken": f"sk-ant-oat01-{name}", "refreshToken": f"sk-ant-ort01-{name}", "expiresAt": 1,
            "scopes": ["user:inference", "user:profile"]}


def as_json(obj):
    return json.dumps(obj, sort_keys=True).encode()


class LoginSwitchCases:
    """The Swift cases; subclasses choose the credential store."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.store = self.make_store()
        object.__setattr__(self.environment, "claude_credentials", self.store)
        self.system = base.FakeSystem(self.runner)
        self.personal_dir = f"{self.home}/.claude-pessoal"
        os.makedirs(self.personal_dir)
        # The personal folder is logged in on its own, and has an MCP login.
        self.store.write_folder_item(self.personal_dir, as_json(
            {"claudeAiOauth": oauth("own"), "mcpOAuth": {"notion": {"accessToken": "mcp"}}}))
        with open(f"{self.personal_dir}/.claude.json", "wb") as f:
            f.write(as_json({"oauthAccount": {"emailAddress": "own@x.com"}, "numStartups": 7}))
        # ~/.claude (work profile) is logged in on its own too.
        self.store.write_folder_item(None, as_json({"claudeAiOauth": oauth("work-own")}))
        (self.home / ".claude.json").write_bytes(as_json({"oauthAccount": {"emailAddress": "work@x.com"}}))

        self.dev = ClaudeAccount(nickname="Dev HINC", email="dev@x.com", color=ProfileColor.BLUE,
                                 kind=ClaudeAccountKind.LOGIN)
        self.arvor = ClaudeAccount(nickname="Arvor", email="arvor@x.com", color=ProfileColor.TEAL,
                                   kind=ClaudeAccountKind.LOGIN)
        switcher = self.switcher()
        switcher.store_new_account(self.dev.id, ClaudeLoginRecord(as_json(oauth("dev")), as_json({"emailAddress": "dev@x.com"})))
        switcher.store_new_account(self.arvor.id, ClaudeLoginRecord(as_json(oauth("arvor")), as_json({"emailAddress": "arvor@x.com"})))

        git = GitIdentity(nickname="P", name="R", email="r@x.com", color=ProfileColor.ORANGE)
        self.config = Config(git_identities=[git], claude_accounts=[self.dev, self.arvor], profiles=[
            Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git.id, claude_config_dir=self.personal_dir),
            Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git.id, claude_config_dir=None),
        ])
        self.personal, self.work = self.config.profiles

    # helpers

    def switcher(self):
        return ClaudeLoginSwitcher(self.environment, self.store)

    def engine(self):
        return SwitchEngine(self.environment, base.FakeTools())

    def item(self, folder):
        data = self.store.read_folder_item(folder)
        return None if data is None else json.loads(data)

    def folder_access_token(self, folder):
        return ((self.item(folder) or {}).get("claudeAiOauth") or {}).get("accessToken")

    def displayed_email(self, folder):
        path = claude_paths.state_file(folder, self.environment)
        if not path.exists():
            return None
        return (json.loads(path.read_bytes()).get("oauthAccount") or {}).get("emailAddress")

    # cases

    def test_claude_only_switch_puts_the_login_into_the_folder_item_keeping_everything_else(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        result = engine.switch_claude_only(self.dev.id, self.config)

        self.assertEqual(result.applied_steps, [K.CLAUDE_LOGIN, K.ENV_FILES, K.STATE])
        self.assertEqual(result.state.applied_claude_account_id, self.dev.id)
        self.assertEqual(self.folder_access_token(self.personal_dir), "sk-ant-oat01-dev")
        self.assertEqual(self.item(self.personal_dir)["mcpOAuth"], {"notion": {"accessToken": "mcp"}}, "MCP logins are kept")
        self.assertEqual(self.displayed_email(self.personal_dir), "dev@x.com", "Claude Code shows the right account")
        self.assertEqual(json.loads(Path(f"{self.personal_dir}/.claude.json").read_bytes())["numStartups"], 7)
        self.assertEqual(self.folder_access_token(None), "sk-ant-oat01-work-own", "other folders untouched")
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN="$(', self.read(AppPaths(self.environment).env_sh),
                         "no env token for login accounts")
        # The folder's own login was kept to come back to.
        self.assertIsNotNone(self.store.read_vault(ClaudeLoginSwitcher.own_login_key(self.personal_dir)))
        self.assertEqual(self.switcher().occupancy().account_in(self.personal_dir), self.dev.id)

    def test_rotated_tokens_are_saved_back_and_the_own_login_comes_back(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.dev.id, self.config)
        # Claude Code refreshes while dev is live: the folder now has newer tokens.
        self.store.write_folder_item(self.personal_dir, as_json({"claudeAiOauth": oauth("dev-rotated"), "mcpOAuth": {}}))

        engine.switch_claude_only(self.arvor.id, self.config)
        self.assertEqual(self.folder_access_token(self.personal_dir), "sk-ant-oat01-arvor")
        saved = json.loads(self.store.read_vault(uuid_text(self.dev.id)))
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], "sk-ant-oat01-dev-rotated")

        # "Login da pasta" puts the folder's original login back.
        back = engine.switch_claude_only(ClaudeFolder.override_for(None, self.personal), self.config)
        self.assertIsNone(back.state.applied_claude_account_id)
        self.assertEqual(self.folder_access_token(self.personal_dir), "sk-ant-oat01-own")
        self.assertEqual(self.displayed_email(self.personal_dir), "own@x.com")
        self.assertEqual(self.item(self.personal_dir)["mcpOAuth"], {}, "the folder's current MCP logins stay")

    def test_an_account_lives_in_one_folder_at_a_time(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.dev.id, self.config)

        self.work.claude_account_id = self.dev.id
        engine.switch_profile(self.work.id, self.config)
        self.assertEqual(self.folder_access_token(None), "sk-ant-oat01-dev", "dev moved to ~/.claude")
        self.assertEqual(self.folder_access_token(self.personal_dir), "sk-ant-oat01-own",
                         "the personal folder got its own login back")
        self.assertEqual(self.displayed_email(self.personal_dir), "own@x.com")
        occupancy = self.switcher().occupancy()
        self.assertEqual((occupancy.account_in(None), occupancy.account_in(self.personal_dir)), (self.dev.id, None))

    def test_moving_an_account_out_of_a_folder_that_never_had_its_own_login(self):
        """The user's case: Dev HINC was put into a test folder that never had a login
        of its own, then asked for in the work profile (~/.claude)."""
        test_dir = f"{self.home}/.claude-teste"
        os.makedirs(test_dir)
        test_profile = Profile(name="Teste", color=ProfileColor.GRAPHITE, git_identity_id=self.config.git_identities[0].id,
                               claude_config_dir=test_dir, claude_account_id=self.dev.id)
        self.config.profiles.append(test_profile)
        engine = self.engine()
        engine.switch_profile(test_profile.id, self.config)
        self.assertEqual(self.folder_access_token(test_dir), "sk-ant-oat01-dev")

        engine.switch_profile(self.work.id, self.config)
        result = engine.switch_claude_only(self.dev.id, self.config)
        self.assertEqual(result.state.applied_claude_account_id, self.dev.id)
        self.assertEqual(self.folder_access_token(None), "sk-ant-oat01-dev", "dev moved to ~/.claude")
        self.assertIsNone(self.item(test_dir), "the test folder is back to no login, as it was")
        self.assertIsNone(self.displayed_email(test_dir))

        # And back: ~/.claude gets its own login again.
        engine.switch_claude_only(ClaudeFolder.override_for(None, self.work), self.config)
        self.assertEqual(self.folder_access_token(None), "sk-ant-oat01-work-own")
        self.assertEqual(self.displayed_email(None), "work@x.com")

    def test_a_failed_switch_puts_the_old_login_back(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        item_before = self.store.read_folder_item(self.personal_dir)
        env_sh = AppPaths(self.environment).env_sh
        env_sh.unlink()
        env_sh.mkdir()

        with self.assertRaises(StepFailed) as raised:
            engine.switch_claude_only(self.dev.id, self.config)
        self.assertEqual(raised.exception.step, K.ENV_FILES)
        self.assertTrue(raised.exception.rollback.complete)
        self.assertEqual(self.store.read_folder_item(self.personal_dir), item_before)
        self.assertEqual(self.displayed_email(self.personal_dir), "own@x.com")
        self.assertIsNone(self.switcher().occupancy().account_in(self.personal_dir))
        self.assertIsNone(self.store.read_vault(ClaudeLoginSwitcher.own_login_key(self.personal_dir)),
                          "the saved own login is undone too")

    def test_a_failed_switch_into_a_folder_without_login_leaves_it_without_one(self):
        """Linux: undoing a write into a folder that had no credentials file removes it again."""
        test_dir = f"{self.home}/.claude-teste"
        os.makedirs(test_dir)
        test_profile = Profile(name="Teste", color=ProfileColor.GRAPHITE, git_identity_id=self.config.git_identities[0].id,
                               claude_config_dir=test_dir, claude_account_id=self.dev.id)
        self.config.profiles.append(test_profile)
        paths = AppPaths(self.environment)
        paths.env_sh.parent.mkdir(parents=True, exist_ok=True)
        paths.env_sh.mkdir()
        with self.assertRaises(StepFailed):
            self.engine().switch_profile(test_profile.id, self.config)
        self.assertIsNone(self.store.read_folder_item(test_dir))
        self.assertFalse(os.path.exists(f"{test_dir}/.claude.json"))

    def test_missing_login_is_a_preflight_failure(self):
        self.store.delete_vault(uuid_text(self.dev.id))
        self.engine().switch_profile(self.personal.id, self.config)
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_claude_only(self.dev.id, self.config)
        self.assertEqual((raised.exception.failure.kind, raised.exception.failure.detail), ("claudeLoginMissing", "Dev HINC"))

    def test_removing_a_live_account_gives_the_folder_its_own_login_back(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.dev.id, self.config)
        self.switcher().remove_account(self.dev.id)
        self.assertEqual(self.folder_access_token(self.personal_dir), "sk-ant-oat01-own")
        self.assertIsNone(self.store.read_vault(uuid_text(self.dev.id)))
        self.assertIsNone(self.switcher().occupancy().account_in(self.personal_dir))

    def test_replacing_the_login_of_a_live_account_updates_its_folder(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.dev.id, self.config)
        self.switcher().replace_login(self.dev.id, ClaudeLoginRecord(as_json(oauth("dev-new")), as_json({"emailAddress": "dev2@x.com"})))
        self.assertEqual(self.folder_access_token(self.personal_dir), "sk-ant-oat01-dev-new")
        self.assertEqual(self.displayed_email(self.personal_dir), "dev2@x.com")
        self.assertIn("mcpOAuth", self.item(self.personal_dir))

    def test_capture_reads_a_new_login_from_a_throwaway_folder(self):
        scratch = f"{self.home}/scratch-login"
        os.makedirs(scratch)
        self.store.write_folder_item(scratch, as_json({"claudeAiOauth": oauth("new")}))
        with open(f"{scratch}/.claude.json", "wb") as f:
            f.write(as_json({"oauthAccount": {"emailAddress": "new@x.com"}}))
        record = self.switcher().capture(scratch)
        self.assertEqual(record.email, "new@x.com")
        self.assertEqual(json.loads(record.oauth)["accessToken"], "sk-ant-oat01-new")

    def test_capture_without_a_login_fails(self):
        scratch = f"{self.home}/scratch-empty"
        os.makedirs(scratch)
        with self.assertRaises(NoLoginInFolder):
            self.switcher().capture(scratch)


class InMemoryStoreLoginSwitchTests(LoginSwitchCases, SafeTestCase):
    def make_store(self):
        return InMemoryClaudeCredentialStore()


class FileStoreLoginSwitchTests(LoginSwitchCases, SafeTestCase):
    """Same cases on real `<folder>/.credentials.json` files; the vault is in memory."""

    def make_store(self):
        return FileClaudeCredentialStore(self.home, vault=InMemoryVaultBackend())

    def credentials(self, folder):
        return self.home / ".claude" / ".credentials.json" if folder is None else self.home / folder / ".credentials.json"

    def test_switch_writes_only_claude_ai_oauth_in_the_real_file_with_mode_0600(self):
        personal_file = self.credentials(self.personal_dir)
        os.chmod(personal_file, 0o644)  # looser than Claude Code's own 0600
        default_before = self.credentials(None).read_bytes()
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.dev.id, self.config)

        written = json.loads(personal_file.read_bytes())
        self.assertEqual(set(written), {"claudeAiOauth", "mcpOAuth"})
        self.assertEqual(written["mcpOAuth"], {"notion": {"accessToken": "mcp"}})
        self.assertEqual(written["claudeAiOauth"], oauth("dev"))
        self.assertEqual(stat.S_IMODE(os.lstat(personal_file).st_mode), 0o600)
        self.assertEqual(self.credentials(None).read_bytes(), default_before, "~/.claude untouched")
        state = json.loads(Path(f"{self.personal_dir}/.claude.json").read_bytes())
        self.assertEqual(state, {"numStartups": 7, "oauthAccount": {"emailAddress": "dev@x.com"}},
                         "only oauthAccount changes in .claude.json")

    def test_rotated_tokens_in_the_real_file_are_saved_back(self):
        engine = self.engine()
        engine.switch_profile(self.personal.id, self.config)
        engine.switch_claude_only(self.dev.id, self.config)
        # Claude Code rewrites the file itself when it refreshes.
        self.credentials(self.personal_dir).write_bytes(as_json({"claudeAiOauth": oauth("dev-rotated"),
                                                                 "mcpOAuth": {"notion": {"accessToken": "mcp2"}}}))
        engine.switch_claude_only(None, self.config)  # back to the profile's choice: the folder's own login
        self.assertEqual(json.loads(self.store.read_vault(uuid_text(self.dev.id)))["claudeAiOauth"]["accessToken"],
                         "sk-ant-oat01-dev-rotated")
        written = json.loads(self.credentials(self.personal_dir).read_bytes())
        self.assertEqual(written["claudeAiOauth"]["accessToken"], "sk-ant-oat01-own")
        self.assertEqual(written["mcpOAuth"], {"notion": {"accessToken": "mcp2"}})

    def test_folder_without_own_login_has_no_credentials_file_afterwards(self):
        test_dir = f"{self.home}/.claude-teste"
        os.makedirs(test_dir)
        switcher = self.switcher()
        switcher.place(self.dev.id, test_dir)
        self.assertTrue(os.path.exists(f"{test_dir}/.credentials.json"))
        switcher.place(None, test_dir)
        self.assertFalse(os.path.exists(f"{test_dir}/.credentials.json"))


class FileClaudeCredentialStoreTests(SafeTestCase):
    def store(self):
        return FileClaudeCredentialStore(self.home, vault=InMemoryVaultBackend())

    def test_folder_item_is_the_credentials_file(self):
        store = self.store()
        self.assertIsNone(store.read_folder_item(None))
        store.write_folder_item(None, b'{"claudeAiOauth":{}}')
        path = self.home / ".claude" / ".credentials.json"
        self.assertEqual(path.read_bytes(), b'{"claudeAiOauth":{}}')
        self.assertEqual(self.mode_of(path), 0o600)
        self.assertEqual(self.mode_of(path.parent), 0o700, "a missing ~/.claude is created private")
        folder = self.home / ".claude-x"
        folder.mkdir()
        store.write_folder_item(str(folder), b"{}")
        self.assertEqual((folder / ".credentials.json").read_bytes(), b"{}")
        self.assertEqual(store.read_folder_item(str(folder)), b"{}")
        store.delete_folder_item(str(folder))
        store.delete_folder_item(str(folder))
        self.assertIsNone(store.read_folder_item(str(folder)))

    def test_mode_is_0600_unless_the_existing_file_is_stricter(self):
        store = self.store()
        path = self.home / ".claude" / ".credentials.json"
        path.parent.mkdir()
        path.write_bytes(b"{}")
        os.chmod(path, 0o664)
        store.write_folder_item(None, b'{"a":1}')
        self.assertEqual(self.mode_of(path), 0o600)
        os.chmod(path, 0o400)
        store.write_folder_item(None, b'{"a":2}')
        self.assertEqual(self.mode_of(path), 0o400)
        self.assertEqual(path.read_bytes(), b'{"a":2}')

    def test_no_temporary_files_are_left_behind(self):
        store = self.store()
        store.write_folder_item(None, b"{}")
        store.write_folder_item(None, b"{}")
        self.assertEqual(os.listdir(self.home / ".claude"), [".credentials.json"])

    def test_vault_goes_to_the_injected_backend(self):
        backend = InMemoryVaultBackend()
        store = FileClaudeCredentialStore(self.home, vault=backend)
        store.write_vault("KEY", b"secret")
        self.assertEqual(store.read_vault("KEY"), b"secret")
        self.assertEqual(backend.accounts(), ["KEY"])
        store.delete_vault("KEY")
        self.assertIsNone(store.read_vault("KEY"))

    def test_default_vault_is_the_claude_login_service(self):
        try:
            from multicred.core.secret_service import SecretServiceBackend
        except (ImportError, ValueError) as error:  # no libsecret typelib
            self.skipTest(str(error))
        store = FileClaudeCredentialStore(self.home)
        self.assertIsInstance(store.vault, SecretServiceBackend)  # built lazily, nothing is read
        self.assertEqual(store.vault.service, CLAUDE_LOGIN_SERVICE)
        self.assertEqual(CLAUDE_LOGIN_SERVICE, "app.multicred.claude-login")


class RealEnvironmentTests(unittest.TestCase):
    def test_make_real_uses_the_files_and_the_secret_service_without_touching_them(self):
        try:
            from multicred.core import real_env
            environment = real_env.make_real()  # builds objects only; nothing is read or written
        except (ImportError, ValueError) as error:  # no libsecret typelib
            self.skipTest(str(error))
        store = environment.claude_credentials
        self.assertIsInstance(store, FileClaudeCredentialStore)
        self.assertEqual(store.folder_file(None), environment.home / ".claude" / ".credentials.json")
        self.assertEqual(environment.home.resolve(), real_env.real_home())
        self.assertEqual(store.folder_file("/x/.claude-w").as_posix(), "/x/.claude-w/.credentials.json")
        self.assertEqual(type(environment.secrets).__name__, "SecretServiceStore")


class OccupancyTests(SafeTestCase):
    def test_round_trip_and_lookup(self):
        account = ClaudeAccount(nickname="x", color=ProfileColor.RED).id
        occupancy = ClaudeFolderOccupancy({"~/.claude": account})
        self.assertEqual(occupancy.account_in(None), account)
        self.assertEqual(occupancy.key_holding(account), "~/.claude")
        self.assertIsNone(occupancy.key_holding(ClaudeAccount(nickname="y", color=ProfileColor.RED).id))
        self.assertEqual(ClaudeFolderOccupancy.from_dict(occupancy.to_dict()), occupancy)
        self.assertEqual(occupancy.to_dict(), {"accountByFolder": {"~/.claude": uuid_text(account)}})

    def test_file_is_private_and_a_broken_file_reads_as_empty(self):
        switcher = ClaudeLoginSwitcher(self.environment, self.claude_store)
        self.assertEqual(switcher.occupancy(), ClaudeFolderOccupancy())
        dev = ClaudeAccount(nickname="d", color=ProfileColor.RED)
        switcher.store_new_account(dev.id, ClaudeLoginRecord(as_json(oauth("d")), None))
        folder = self.home / ".claude-d"
        folder.mkdir()
        switcher.place(dev.id, str(folder))
        path = AppPaths(self.environment).claude_folders
        self.assertEqual(self.mode_of(path), 0o600)
        path.write_text("{broken")
        self.assertEqual(switcher.occupancy(), ClaudeFolderOccupancy())


if __name__ == "__main__":
    unittest.main()
