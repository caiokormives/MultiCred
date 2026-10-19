"""S-04: Claude folders for profiles and Claude accounts (ClaudeAccountEditorTests in ClaudeTests.swift)."""

import dataclasses
import json
import os
import uuid

from multicred.core.claude import account_editor
from multicred.core.claude.account_editor import (
    ClaudeAccountError, ClaudeFolderChoice, add_login_account, add_token_account, discard_replaced_token,
    is_plausible_token, prepare_folder, profiles_using, remove, replace_login, replace_token,
)
from multicred.core.claude.login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher
from multicred.core.models import ClaudeAccountKind, Config, Profile, ProfileColor, uuid_text
from multicred.core.storage import dumps
from tests.support import SafeTestCase

TOKEN = "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123456789"
RECORD = ClaudeLoginRecord(b'{"accessToken":"sk-ant-oat01-login","refreshToken":"r"}', b'{"emailAddress":"l@x.com"}')


class ClaudeAccountEditorTests(SafeTestCase):
    # MARK: profile folders

    def test_new_folder_is_created_privately_and_canonical(self):
        folder = prepare_folder(ClaudeFolderChoice.new("~/.claude-cliente-x/"), self.environment)
        self.assertEqual(folder, f"{self.home}/.claude-cliente-x")
        self.assertEqual(self.mode_of(self.home / ".claude-cliente-x"), 0o700)

    def test_default_folder_is_none_and_explicit_tilde_claude_too(self):
        self.assertIsNone(prepare_folder(ClaudeFolderChoice.default(), self.environment))
        self.assertTrue((self.home / ".claude").is_dir(), "a missing default folder is created")
        self.assertIsNone(prepare_folder(ClaudeFolderChoice.existing("~/.claude"), self.environment))

    def test_existing_folder_must_exist_and_be_a_directory(self):
        with self.assertRaises(ClaudeAccountError) as raised:
            prepare_folder(ClaudeFolderChoice.existing("~/.claude-missing"), self.environment)
        self.assertEqual(raised.exception, ClaudeAccountError("folderMissing", f"{self.home}/.claude-missing"))
        (self.home / ".claude-file").write_text("x")
        with self.assertRaises(ClaudeAccountError) as raised:
            prepare_folder(ClaudeFolderChoice.existing("~/.claude-file"), self.environment)
        self.assertEqual(raised.exception.kind, "notADirectory")

    def test_invalid_paths_are_rejected(self):
        for path in ("relative/dir", "", "~/a\nb"):
            with self.assertRaises(ClaudeAccountError) as raised:
                prepare_folder(ClaudeFolderChoice.new(path), self.environment)
            self.assertEqual(raised.exception.kind, "invalidPath")

    def test_test_mode_refuses_folders_outside_the_sandbox(self):
        target = f"/tmp/should-not-be-created-{uuid.uuid4()}"
        with self.assertRaises(ClaudeAccountError) as raised:
            prepare_folder(ClaudeFolderChoice.new(target), self.environment)
        self.assertEqual(raised.exception.kind, "outsideSandbox")
        self.assertFalse(os.path.exists(target))

    # MARK: token accounts

    def test_plausible_tokens(self):
        self.assertTrue(is_plausible_token(TOKEN))
        self.assertFalse(is_plausible_token("short"))
        self.assertFalse(is_plausible_token("has spaces in the middle of it ok"))
        self.assertFalse(is_plausible_token("x" * 2049))
        self.assertFalse(is_plausible_token(TOKEN + "\n"))

    def test_token_account_keeps_the_token_only_in_the_secret_service(self):
        config, account = add_token_account(" Marco ", "marco@x.com", ProfileColor.TEAL, f"  {TOKEN}\n", Config(), self.secrets)
        self.assertEqual(account.nickname, "Marco")
        self.assertEqual(account.kind, ClaudeAccountKind.TOKEN)
        self.assertEqual(config.claude_accounts, [account])
        self.assertEqual(self.secrets.stored_value(account.secret_account), TOKEN.encode())
        self.assertNotIn(TOKEN, dumps(config.to_dict()).decode(), "the token never reaches config.json")

    def test_blank_email_is_dropped(self):
        _, account = add_token_account("M", "  ", ProfileColor.TEAL, TOKEN, Config(), self.secrets)
        self.assertIsNone(account.email)

    def test_invalid_token_or_config_is_rejected_and_nothing_stored(self):
        with self.assertRaises(ClaudeAccountError) as raised:
            add_token_account("X", None, ProfileColor.RED, "not a token", Config(), self.secrets)
        self.assertEqual(raised.exception.kind, "invalidToken")
        with self.assertRaises(ClaudeAccountError) as raised:
            add_token_account("X", "not-an-email", ProfileColor.RED, TOKEN, Config(), self.secrets)
        self.assertEqual(raised.exception.kind, "invalid")
        self.assertEqual(self.secrets.all_accounts(), [])

    def test_secret_service_error_adds_nothing(self):
        class Broken:
            def set_secret(self, value, account):
                raise RuntimeError("locked")
        original = Config()
        with self.assertRaises(ClaudeAccountError) as raised:
            add_token_account("X", None, ProfileColor.RED, TOKEN, original, Broken())
        self.assertEqual(raised.exception.kind, "secrets")
        self.assertEqual(original.claude_accounts, [])

    def test_replace_token(self):
        config, account = add_token_account("M", None, ProfileColor.TEAL, TOKEN, Config(), self.secrets)
        replace_token(account, TOKEN[::-1].replace("-", "x"), self.secrets)
        self.assertEqual(self.secrets.stored_value(account.secret_account), TOKEN[::-1].replace("-", "x").encode())
        with self.assertRaises(ClaudeAccountError):
            replace_token(account, "bad token", self.secrets)

    def test_remove_deletes_token_and_falls_back_to_folder_login(self):
        config, account = add_token_account("Marco", None, ProfileColor.TEAL, TOKEN, Config(), self.secrets)
        config.profiles.append(Profile(name="Pessoal", color=ProfileColor.ORANGE,
                                       claude_config_dir=f"{self.home}/.claude-pessoal", claude_account_id=account.id))
        self.assertEqual([p.name for p in profiles_using(account.id, config)], ["Pessoal"])

        updated = remove(account.id, config)
        self.assertEqual(updated.claude_accounts, [])
        self.assertIsNone(updated.profiles[0].claude_account_id)
        self.assertEqual(updated.profiles[0].claude_config_dir, f"{self.home}/.claude-pessoal", "the profile keeps its folder")
        self.assertIsNotNone(self.secrets.stored_value(account.secret_account),
                             "S-13: remove is pure; the token goes only after the caller saved")
        self.assertEqual(config.profiles[0].claude_account_id, account.id, "the input config is not mutated")
        account_editor.discard_removed_account(account, self.secrets, ClaudeLoginSwitcher(self.environment))
        self.assertIsNone(self.secrets.stored_value(account.secret_account))

    # MARK: login accounts (D39)

    def test_login_account_goes_to_the_vault_with_the_login_email(self):
        switcher = ClaudeLoginSwitcher(self.environment)
        config, account = add_login_account(" Dev ", None, ProfileColor.BLUE, RECORD, Config(), switcher)
        self.assertEqual((account.nickname, account.kind, account.email), ("Dev", ClaudeAccountKind.LOGIN, "l@x.com"))
        self.assertEqual(config.claude_accounts, [account])
        stored = json.loads(self.claude_store.read_vault(uuid_text(account.id)))
        self.assertEqual(stored["claudeAiOauth"]["accessToken"], "sk-ant-oat01-login")
        self.assertNotIn("sk-ant-oat01-login", dumps(config.to_dict()).decode())

    def test_signing_a_token_account_in_again_turns_it_into_a_login_account(self):
        switcher = ClaudeLoginSwitcher(self.environment)
        config, account = add_token_account("Marco", None, ProfileColor.TEAL, TOKEN, Config(), self.secrets)
        updated = replace_login(account, RECORD, config, switcher, self.secrets)
        converted = updated.claude_account(account.id)
        self.assertEqual((converted.kind, converted.email), (ClaudeAccountKind.LOGIN, "l@x.com"))
        self.assertIsNotNone(self.secrets.stored_value(account.secret_account),
                             "the token stays until the caller saved the converted config")
        discard_replaced_token(account, self.secrets)
        self.assertIsNone(self.secrets.stored_value(account.secret_account))
        self.assertIsNotNone(self.claude_store.read_vault(uuid_text(account.id)))

    def test_removing_a_live_login_account_gives_the_folder_back(self):
        switcher = ClaudeLoginSwitcher(self.environment)
        config, account = add_login_account("Dev", None, ProfileColor.BLUE, RECORD, Config(), switcher)
        folder = self.home / ".claude-p"
        folder.mkdir()
        self.claude_store.write_folder_item(str(folder), b'{"claudeAiOauth":{"accessToken":"own"},"mcpOAuth":{}}')
        switcher.place(account.id, str(folder))
        # S-13: (1) the folder gets its own login back, (2) the caller saves, (3) only then
        # the vault entry goes.
        account_editor.release_login(account, switcher)
        self.assertEqual(json.loads(self.claude_store.read_folder_item(str(folder)))["claudeAiOauth"], {"accessToken": "own"})
        self.assertIsNotNone(self.claude_store.read_vault(uuid_text(account.id)), "kept until the save")
        updated = remove(account.id, config)
        self.assertEqual(updated.claude_accounts, [])
        self.assertIsNotNone(self.claude_store.read_vault(uuid_text(account.id)), "remove is pure")
        account_editor.discard_removed_account(account, self.secrets, switcher)
        self.assertIsNone(self.claude_store.read_vault(uuid_text(account.id)))

    def test_releasing_a_live_login_can_be_undone_when_the_save_fails(self):
        switcher = ClaudeLoginSwitcher(self.environment)
        config, account = add_login_account("Dev", None, ProfileColor.BLUE, RECORD, Config(), switcher)
        folder = self.home / ".claude-p"
        folder.mkdir()
        self.claude_store.write_folder_item(str(folder), b'{"claudeAiOauth":{"accessToken":"own"},"mcpOAuth":{}}')
        switcher.place(account.id, str(folder))
        undo = account_editor.release_login(account, switcher)
        undo()
        live = json.loads(self.claude_store.read_folder_item(str(folder)))["claudeAiOauth"]
        self.assertEqual(live["accessToken"], "sk-ant-oat01-login", "the account is live there again")
        self.assertEqual(switcher.occupancy().account_in(str(folder)), account.id)

    def test_releasing_an_account_that_lives_nowhere_changes_nothing(self):
        switcher = ClaudeLoginSwitcher(self.environment)
        _config, account = add_login_account("Dev", None, ProfileColor.BLUE, RECORD, Config(), switcher)
        _config, token_account = add_token_account("Marco", None, ProfileColor.TEAL, TOKEN, Config(), self.secrets)
        account_editor.release_login(account, switcher)()
        account_editor.release_login(token_account, switcher)()
        self.assertEqual(self.claude_store.folders, {})
        self.assertIsNotNone(self.claude_store.read_vault(uuid_text(account.id)))

    def test_real_mode_allows_folders_outside_home(self):
        environment = dataclasses.replace(self.environment, mode="real")
        target = self.home.parent / f"MultiCredTests-outside-{uuid.uuid4()}"
        self.addCleanup(lambda: os.path.isdir(target) and os.rmdir(target))
        self.assertEqual(prepare_folder(ClaudeFolderChoice.new(str(target)), environment), str(target))
