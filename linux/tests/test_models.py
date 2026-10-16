import json
import uuid
import unittest

from multicred.core import claude_paths
from multicred.core.models import (
    APIKeyAccount, APIProvider, ActiveState, ClaudeAccount, ClaudeAccountKind, ClaudeFolder, Config,
    ConfigMigration, GitHubAccount, GitIdentity, Profile, ProfileColor, ProfileIcon,
)
from multicred.core.validation import Issue, issues_in
from tests.support import SafeTestCase


class ClaudePathsTests(SafeTestCase):
    def test_canonicalization_removes_tilde_trailing_slash_and_dots(self):
        env = self.environment
        self.assertEqual(claude_paths.canonical_config_dir("~/.claude-work/", env), f"{self.home}/.claude-work")
        self.assertEqual(claude_paths.canonical_config_dir("  ~/a/../.claude-x//  ", env), f"{self.home}/.claude-x")

    def test_default_directory_means_no_variable(self):
        self.assertIsNone(claude_paths.canonical_config_dir("~/.claude", self.environment))
        self.assertIsNone(claude_paths.canonical_config_dir(f"{self.home}/.claude/", self.environment))

    def test_rejects_relative_empty_and_control_characters(self):
        for bad in ["", "claude-x", "~/.claude-x\nexport X=1"]:
            with self.assertRaises(claude_paths.PathError):
                claude_paths.canonical_config_dir(bad, self.environment)

    def test_suggested_directory_slug(self):
        env = self.environment
        self.assertEqual(claude_paths.suggested_config_dir("Cliente X — Ação", env), f"{self.home}/.claude-cliente-x-acao")
        self.assertEqual(claude_paths.suggested_config_dir("!!!", env), f"{self.home}/.claude-conta")

    def test_credentials_file_of_each_folder(self):
        # Linux Claude Code keeps the login in <folder>/.credentials.json.
        self.assertEqual(claude_paths.credentials_file(None, self.environment), self.home / ".claude/.credentials.json")
        self.assertEqual(claude_paths.credentials_file("/x/.claude-w", self.environment).as_posix(), "/x/.claude-w/.credentials.json")

    def test_state_file_of_each_folder(self):
        # Default mode keeps its state in ~/.claude.json, outside the folder.
        self.assertEqual(claude_paths.state_file(None, self.environment), self.home / ".claude.json")
        self.assertEqual(claude_paths.state_file("/x/.claude-w", self.environment).as_posix(), "/x/.claude-w/.claude.json")


def sample_config():
    git = GitIdentity(nickname="NPU", name="Rafael Leivas", email="r@npu.com.br", color=ProfileColor.INDIGO)
    gh = GitHubAccount(nickname="NPU", username="rafael-npu", color=ProfileColor.INDIGO)
    claude = ClaudeAccount(nickname="NPU", email="r@npu.com.br", color=ProfileColor.INDIGO)
    key = APIKeyAccount(nickname="NPU", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
    profile = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git.id, github_account_id=gh.id,
                      claude_config_dir="/home/r/.claude-npu", claude_account_id=claude.id, api_key_ids=[key.id])
    return Config(git_identities=[git], github_accounts=[gh], claude_accounts=[claude], api_keys=[key], profiles=[profile])


class ValidationTests(unittest.TestCase):
    def test_valid_config_has_no_issues(self):
        self.assertEqual(issues_in(sample_config()), [])

    def test_newlines_and_bad_email_are_rejected(self):
        config = sample_config()
        config.git_identities[0].name = "Evil\n[core]\n\tsshCommand = touch /tmp/x"
        config.git_identities[0].email = "not-an-email"
        issues = issues_in(config)
        owner = config.git_identities[0].id
        self.assertIn(Issue("controlCharacters", owner, "name"), issues)
        self.assertIn(Issue("invalidEmail", owner), issues)

    def test_env_var_rules(self):
        config = sample_config()
        key = config.api_keys[0]
        key.env_var = "openai key"
        self.assertIn(Issue("invalidEnvVar", key.id, "openai key"), issues_in(config))
        key.env_var = "PATH"
        self.assertIn(Issue("reservedEnvVar", key.id, "PATH"), issues_in(config))

    def test_two_keys_with_same_variable_in_one_profile(self):
        config = sample_config()
        second = APIKeyAccount(nickname="Pessoal", provider=APIProvider.OPENAI, color=ProfileColor.ORANGE)
        config.api_keys.append(second)
        config.profiles[0].api_key_ids.append(second.id)
        self.assertIn(Issue("duplicateEnvVarInProfile", config.profiles[0].id, "OPENAI_API_KEY"), issues_in(config))

    def test_profile_claude_folder_must_be_canonical(self):
        config = sample_config()
        for bad in ["/home/x/.claude-x/", "relative/.claude"]:
            config.profiles[0].claude_config_dir = bad
            self.assertIn(Issue("invalidClaudeDirectory", config.profiles[0].id), issues_in(config))

    def test_token_variable_is_reserved_and_always_managed(self):
        config = sample_config()
        config.api_keys[0].env_var = "CLAUDE_CODE_OAUTH_TOKEN"
        self.assertIn(Issue("reservedEnvVar", config.api_keys[0].id, "CLAUDE_CODE_OAUTH_TOKEN"), issues_in(config))
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", Config().managed_env_vars)

    def test_v1_folder_accounts_are_still_validated_before_migration(self):
        config = sample_config()
        trailing = ClaudeAccount(nickname="X", color=ProfileColor.TEAL, kind=ClaudeAccountKind.FOLDER, config_dir="/home/x/.claude-x/")
        default_one = ClaudeAccount(nickname="Y", color=ProfileColor.TEAL, kind=ClaudeAccountKind.FOLDER)
        default_again = ClaudeAccount(nickname="Z", color=ProfileColor.TEAL, kind=ClaudeAccountKind.FOLDER)
        config.claude_accounts += [trailing, default_one, default_again]
        issues = issues_in(config)
        self.assertIn(Issue("invalidClaudeDirectory", trailing.id), issues)
        self.assertIn(Issue("duplicateClaudeDirectory", default_again.id), issues)

    def test_dangling_references_and_bad_github_username(self):
        config = sample_config()
        config.profiles[0].claude_account_id = uuid.uuid4()
        config.github_accounts[0].username = "bad user;rm"
        issues = issues_in(config)
        self.assertIn(Issue("missingReference", config.profiles[0].id, "claude"), issues)
        self.assertIn(Issue("invalidGitHubUsername", config.github_accounts[0].id), issues)

    def test_github_host_and_user_cannot_start_with_a_dash(self):
        config = sample_config()
        account = config.github_accounts[0]
        account.host, account.username = "-h", "-x"
        issues = issues_in(config)
        self.assertIn(Issue("invalidHost", account.id), issues)
        self.assertIn(Issue("invalidGitHubUsername", account.id), issues)
        account.host, account.username = "github.example.com:8443", "rafael-npu"
        self.assertEqual(issues_in(config), [])

    def test_duplicate_ids_are_rejected(self):
        config = sample_config()
        config.profiles[0].id = config.git_identities[0].id
        self.assertIn(Issue("duplicateID", config.git_identities[0].id), issues_in(config))

    def test_managed_env_vars_are_unique_and_sorted(self):
        config = sample_config()
        config.api_keys.append(APIKeyAccount(nickname="G", provider=APIProvider.GEMINI, color=ProfileColor.GREEN))
        config.api_keys.append(APIKeyAccount(nickname="O2", provider=APIProvider.OPENAI, color=ProfileColor.GREEN))
        self.assertEqual(config.managed_env_vars, ["CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "OPENAI_API_KEY"])


class CodingTests(unittest.TestCase):
    def test_config_round_trips_through_json(self):
        config = sample_config()
        self.assertEqual(Config.from_dict(json.loads(json.dumps(config.to_dict()))), config)

    def test_json_uses_the_mac_field_names(self):
        # Same config.json shape as the macOS app, so a config can move between them.
        data = sample_config().to_dict()
        self.assertEqual(data["version"], 2)
        profile = data["profiles"][0]
        self.assertEqual(set(profile), {"id", "name", "color", "icon", "gitIdentityId", "githubAccountId",
                                        "claudeConfigDir", "claudeAccountId", "apiKeyIds"})
        self.assertEqual(profile["icon"], {"letter": {"_0": "T"}})
        self.assertEqual(profile["id"], profile["id"].upper())
        self.assertEqual(data["settings"], {"switchFeedback": "hud", "enabledShells": ["bash"], "openAtLogin": True})

    def test_missing_optional_fields_take_mac_defaults(self):
        account = GitHubAccount.from_dict({"id": str(uuid.uuid4()).upper(), "nickname": "n", "username": "u",
                                           "host": "github.com", "color": "blue"})
        self.assertTrue(account.claude_co_author)
        claude = ClaudeAccount.from_dict({"id": str(uuid.uuid4()).upper(), "nickname": "n", "color": "blue"})
        self.assertEqual(claude.kind, ClaudeAccountKind.FOLDER)

    def test_state_dates_are_iso8601(self):
        from tests.support import FIXED_NOW
        state = ActiveState(profile_id=uuid.uuid4(), activated_at=FIXED_NOW, generation=3)
        data = state.to_dict()
        self.assertEqual(data["activatedAt"], "2026-09-21T14:13:20Z")
        self.assertEqual(ActiveState.from_dict(data), state)

    def test_profile_icon_defaults_to_first_letter(self):
        self.assertEqual(Profile(name="  pessoal", color=ProfileColor.ORANGE).icon, ProfileIcon("letter", "P"))

    def test_own_login_id_is_stable_per_folder(self):
        a = ClaudeFolder.own_login("/x/.claude-a")
        self.assertEqual(a.id, ClaudeFolder.own_login("/x/.claude-a").id)
        self.assertNotEqual(a.id, ClaudeFolder.own_login(None).id)


class MigrationTests(unittest.TestCase):
    def test_v1_folder_accounts_move_onto_profiles(self):
        folder = ClaudeAccount(nickname="W", color=ProfileColor.BLUE, kind=ClaudeAccountKind.FOLDER, config_dir="/h/.claude-w")
        profile = Profile(name="W", color=ProfileColor.BLUE, claude_account_id=folder.id)
        config = Config(claude_accounts=[folder], profiles=[profile])
        config.version = 1
        migrated, changed = ConfigMigration.migrate(config)
        self.assertTrue(changed)
        self.assertEqual(migrated.version, 2)
        self.assertEqual(migrated.claude_accounts, [])
        self.assertEqual(migrated.profiles[0].claude_config_dir, "/h/.claude-w")
        self.assertIsNone(migrated.profiles[0].claude_account_id)

    def test_current_config_is_left_alone(self):
        config = sample_config()
        self.assertEqual(ConfigMigration.migrate(config), (config, False))


if __name__ == "__main__":
    unittest.main()
