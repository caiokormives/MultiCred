"""S-09: the uninstaller (port of UninstallerTests in InstallTests.swift, Linux files).

Everything runs in a temporary HOME with FakeSystem standing in for gh and
`systemctl --user`, the in-memory secret store and the in-memory Claude store:
nothing on the real machine is touched.
"""

import json
import os
import uuid
from pathlib import Path

import tests.test_engine as base
from multicred.core.claude.login_vault import ClaudeLoginRecord, ClaudeLoginSwitcher
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.install import managed_block
from multicred.core.install.autostart import Autostart
from multicred.core.install.installer import Installer, InstallOptions, Integrations
from multicred.core.install.uninstaller import Uninstaller, remember_created_claude_folder
from multicred.core.models import (
    APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config, GitHubAccount, GitIdentity, Profile,
    ProfileColor, ShellKind, uuid_text,
)
from multicred.core.paths import AppPaths
from multicred.core.storage import ConfigRepository
from tests.support import SafeTestCase

Z, B, F = ShellKind.ZSH, ShellKind.BASH, ShellKind.FISH
ENV_D = ".config/environment.d/90-multicred.conf"


def oauth(name):
    return {"accessToken": f"sk-ant-oat01-{name}", "refreshToken": f"sk-ant-ort01-{name}", "expiresAt": 1}


def as_json(obj):
    return json.dumps(obj, sort_keys=True).encode()


class UninstallerTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.system = base.FakeSystem(self.runner)
        self.paths = AppPaths(self.environment)
        self.zshrc = self.home / ".zshrc"
        self.claude_dir = f"{self.home}/.claude-pessoal"
        os.makedirs(self.claude_dir)
        self.git = GitIdentity(nickname="Pessoal", name="Dev", email="dev@pessoal.dev", color=ProfileColor.ORANGE)
        self.gh = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE,
                                claude_co_author=False)
        self.key = APIKeyAccount(nickname="OpenAI", provider=APIProvider.OPENAI, color=ProfileColor.ORANGE)
        self.login = ClaudeAccount(nickname="Dev HINC", color=ProfileColor.BLUE, kind=ClaudeAccountKind.LOGIN)
        self.profile = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=self.git.id,
                               github_account_id=self.gh.id, claude_config_dir=self.claude_dir,
                               claude_account_id=self.login.id, api_key_ids=[self.key.id])
        self.config = Config(git_identities=[self.git], github_accounts=[self.gh], claude_accounts=[self.login],
                             api_keys=[self.key], profiles=[self.profile])
        self.autostart = Autostart(self.environment, command_path=str(self.make_executable("usr/bin/multicred")))

    # MARK: helpers

    def write(self, path, text):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")

    def install(self, shells=(Z, B, F)):
        Installer(self.environment).apply(InstallOptions(shells=list(shells)))

    def activate_profile(self):
        """A real switch: gitconfig, gh, systemctl + environment.d, settings.json, live login, state."""
        self.secrets.set_secret(b"sk-openai", self.key.secret_account)
        # The folder has its own login before MultiCred puts the account in it.
        self.claude_store.write_folder_item(self.claude_dir, as_json(
            {"claudeAiOauth": oauth("own"), "mcpOAuth": {"notion": {"accessToken": "mcp"}}}))
        Path(self.claude_dir, ".claude.json").write_bytes(as_json({"oauthAccount": {"emailAddress": "own@x.com"}}))
        switcher = ClaudeLoginSwitcher(self.environment)
        switcher.store_new_account(self.login.id, ClaudeLoginRecord(
            as_json(oauth("dev")), as_json({"emailAddress": "dev@x.com"})))
        ConfigRepository(self.environment).save(self.config)
        SwitchEngine(self.environment, base.FakeTools()).switch_profile(self.profile.id, self.config)

    def uninstaller(self):
        return Uninstaller(self.environment, tools=base.FakeTools(), autostart=self.autostart)

    def requests_after(self, count):
        return self.runner.requests[count:]

    # MARK: the Swift cases

    def test_uninstall_restores_files_and_removes_everything(self):
        self.write(self.zshrc, "export A=1\n")
        self.install()
        self.activate_profile()
        self.autostart.enable()
        self.assertEqual(self.system.claude, self.claude_dir)
        self.assertTrue((self.home / ENV_D).exists())

        preview = self.uninstaller().preview()
        self.assertIn(("block", "~/.zshrc"), [(i.kind, i.path) for i in preview])
        self.assertIn(("appDir", "~/.config/multicred"), [(i.kind, i.path) for i in preview])

        report = self.uninstaller().apply()

        self.assertEqual(report.problems, [])
        self.assertEqual(report.manual_steps, [])
        self.assertEqual(self.zshrc.read_text(), "export A=1\n", "back to the original bytes")
        self.assertFalse((self.home / ".bashrc").exists(), "created by MultiCred and now empty")
        self.assertFalse((self.home / ".gitconfig").exists())
        self.assertFalse(Integrations.fish_conf_file(self.environment).exists())
        self.assertFalse((self.home / ENV_D).exists())
        self.assertFalse(self.autostart.file.exists())
        self.assertFalse(self.environment.app_support_directory.exists())
        self.assertEqual(self.secrets.all_accounts(), [])
        self.assertIn(["--user", "unset-environment", "CLAUDE_CONFIG_DIR"],
                      [r.arguments for r in self.runner.requests])
        self.assertIsNone(self.system.claude)
        kept = report.backups_kept_at
        self.assertIsNotNone(kept)
        self.assertTrue(Path(self.environment.expand_tilde(kept)).is_dir())

    def test_user_edits_outside_the_block_survive_byte_for_byte(self):
        self.write(self.zshrc, "# mine\texport A=1\n")
        self.install([Z])
        edited = "# before\n" + self.zshrc.read_text() + "alias ll='ls -la'\n"
        self.write(self.zshrc, edited)
        self.uninstaller().apply()
        self.assertEqual(self.zshrc.read_text(), "# before\n# mine\texport A=1\nalias ll='ls -la'\n")

    def test_broken_marker_is_refused_with_a_manual_step_and_nothing_changes_in_that_file(self):
        broken = f"x\n{managed_block.BEGIN}\nbroken\n"
        self.install([B])  # .bashrc and .gitconfig get proper blocks
        self.write(self.zshrc, broken)  # then the zsh block was edited by hand
        report = self.uninstaller().apply()
        self.assertEqual(len(report.manual_steps), 1)
        self.assertIn("~/.zshrc", report.manual_steps[0])
        self.assertIn(managed_block.BEGIN, report.manual_steps[0])
        self.assertEqual(report.unterminated, ["~/.zshrc"])
        self.assertEqual(self.zshrc.read_bytes(), broken.encode(), "no partial edit")
        self.assertFalse((self.home / ".bashrc").exists(), "the other files are still cleaned")
        self.assertFalse((self.home / ".gitconfig").exists())

    def test_broken_marker_shows_up_in_the_preview(self):
        self.write(self.zshrc, f"x\n{managed_block.BEGIN}\nbroken\n")
        kinds = [(i.kind, i.path) for i in self.uninstaller().preview()]
        self.assertIn(("manual", "~/.zshrc"), kinds)

    # MARK: Linux files

    def test_someone_elses_fish_file_is_left_alone(self):
        fish = Integrations.fish_conf_file(self.environment)
        self.write(fish, "set -gx EDITOR vim\n")
        self.install([B])
        report = self.uninstaller().apply()
        self.assertEqual(fish.read_text(), "set -gx EDITOR vim\n")
        self.assertNotIn("~/.config/fish/conf.d/multicred.fish", report.changed_files)

    def test_our_fish_file_is_removed(self):
        self.install([F])
        fish = Integrations.fish_conf_file(self.environment)
        self.assertTrue(fish.exists())
        self.uninstaller().apply()
        self.assertFalse(fish.exists())

    def test_fish_and_environment_d_under_xdg_config_home_are_removed(self):
        """S-28: uninstall looks where install and the switch wrote."""
        xdg = self.home / "xdg"
        object.__setattr__(self.environment, "xdg_config_home", str(xdg))
        self.install([F])
        self.activate_profile()
        fish, env_d = xdg / "fish/conf.d/multicred.fish", xdg / "environment.d/90-multicred.conf"
        self.assertTrue(fish.is_file())
        self.assertTrue(env_d.is_file())
        paths = [i.path for i in self.uninstaller().preview()]
        self.assertIn("~/xdg/fish/conf.d/multicred.fish", paths)
        self.assertIn("~/xdg/environment.d/90-multicred.conf", paths)
        report = self.uninstaller().apply()
        self.assertEqual(report.problems, [])
        self.assertFalse(fish.exists())
        self.assertFalse(env_d.exists())
        self.assertIn(["--user", "unset-environment", "CLAUDE_CONFIG_DIR"],
                      [r.arguments for r in self.runner.requests])

    def test_session_variable_is_left_alone_when_multicred_never_set_it(self):
        self.install([B])
        self.system.session["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        report = self.uninstaller().apply()
        self.assertEqual(report.problems, [])
        self.assertNotIn("unset-environment", [a for r in self.runner.requests for a in r.arguments])
        self.assertEqual(self.system.claude, "/somewhere/else")
        self.assertNotIn("session", [i.kind for i in self.uninstaller().preview()])

    def test_environment_d_file_alone_is_removed_and_the_variable_unset(self):
        self.install([B])
        self.write(self.home / ENV_D, "# Generated by MultiCred. Do not edit.\nCLAUDE_CONFIG_DIR=/x\n")
        self.system.session["CLAUDE_CONFIG_DIR"] = "/x"
        self.uninstaller().apply()
        self.assertFalse((self.home / ENV_D).exists())
        self.assertIsNone(self.system.claude)

    def test_failed_unset_is_reported_and_the_rest_still_runs(self):
        self.install([B])
        self.activate_profile()
        self.system.fail_unset = True
        report = self.uninstaller().apply()
        self.assertEqual(len(report.problems), 1)
        self.assertIn("unset-environment", report.problems[0])
        self.assertFalse(self.environment.app_support_directory.exists())

    def test_autostart_file_is_removed(self):
        self.install([B])
        self.autostart.enable()
        self.assertIn(("file", self.environment.abbreviate(str(self.autostart.file))),
                      [(i.kind, i.path) for i in self.uninstaller().preview()])
        self.uninstaller().apply()
        self.assertFalse(self.autostart.file.exists())

    def test_secrets_of_the_app_are_deleted(self):
        self.install([B])
        for _ in range(3):
            self.secrets.set_secret(b"x", uuid_text(uuid.uuid4()))
        preview = {i.kind: i for i in self.uninstaller().preview()}
        self.assertEqual(preview["secrets"].count, 3)
        self.uninstaller().apply()
        self.assertEqual(self.secrets.all_accounts(), [])

    def test_claude_folder_holding_a_multicred_login_gets_its_own_login_back(self):
        self.install([B])
        self.activate_profile()
        live = json.loads(self.claude_store.read_folder_item(self.claude_dir))
        self.assertEqual(live["claudeAiOauth"]["accessToken"], "sk-ant-oat01-dev")
        self.assertIn(("claudeLogin", "~/.claude-pessoal"), [(i.kind, i.path) for i in self.uninstaller().preview()])

        report = self.uninstaller().apply()

        self.assertEqual(report.problems, [])
        restored = json.loads(self.claude_store.read_folder_item(self.claude_dir))
        self.assertEqual(restored["claudeAiOauth"]["accessToken"], "sk-ant-oat01-own")
        self.assertEqual(restored["mcpOAuth"], {"notion": {"accessToken": "mcp"}}, "MCP logins kept")
        state = json.loads(Path(self.claude_dir, ".claude.json").read_text())
        self.assertEqual(state["oauthAccount"]["emailAddress"], "own@x.com")
        self.assertEqual(self.claude_store.vault, {}, "the login vault is emptied (accounts and own logins)")
        self.assertTrue(Path(self.claude_dir).is_dir(), "Claude folders are kept by default (D12)")

    def test_own_logins_of_folders_no_profile_uses_any_more_are_deleted_too(self):
        """S-32: a folder's own login saved while a now-deleted profile used it."""
        from multicred.core.claude.login_vault import ClaudeLoginSwitcher
        self.install([B])
        orphan_key = ClaudeLoginSwitcher.own_login_key(f"{self.home}/.claude-apagado")
        self.claude_store.write_vault(orphan_key, b'{"claudeAiOauth":{"accessToken":"old","refreshToken":"r"}}')
        report = self.uninstaller().apply()
        self.assertEqual(report.problems, [])
        self.assertNotIn(orphan_key, self.claude_store.vault, "no refresh token left in the keyring")

    def test_a_folder_that_could_not_get_its_own_login_back_keeps_its_vault_entries(self):
        """S-11: a failed restore must not delete the folder's original login (the only
        copy, in the vault) nor the account that still lives in that folder."""
        self.install([B])
        other = ClaudeAccount(nickname="Outra", color=ProfileColor.GREEN, kind=ClaudeAccountKind.LOGIN)
        self.config.claude_accounts.append(other)
        self.activate_profile()
        ClaudeLoginSwitcher(self.environment).store_new_account(other.id, ClaudeLoginRecord(
            as_json(oauth("outra")), None))
        ConfigRepository(self.environment).save(self.config)
        own_key = ClaudeLoginSwitcher.own_login_key(self.claude_dir)
        default_key = ClaudeLoginSwitcher.own_login_key(None)
        self.claude_store.write_vault(default_key, as_json({"claudeAiOauth": oauth("default")}))
        self.assertIn(own_key, self.claude_store.vault)

        real_write = self.claude_store.write_folder_item

        def failing_write(config_dir, data):
            if config_dir == self.claude_dir:
                raise OSError("disco cheio")
            real_write(config_dir, data)
        self.claude_store.write_folder_item = failing_write

        report = self.uninstaller().apply()

        self.assertIn(own_key, self.claude_store.vault, "the folder's own login is the only copy")
        self.assertIn(uuid_text(self.login.id), self.claude_store.vault, "the account still lives there")
        self.assertNotIn(uuid_text(other.id), self.claude_store.vault, "accounts living nowhere are deleted")
        self.assertNotIn(default_key, self.claude_store.vault, "folders that were restored are emptied")
        live = json.loads(self.claude_store.read_folder_item(self.claude_dir))
        self.assertEqual(live["claudeAiOauth"]["accessToken"], "sk-ant-oat01-dev", "the folder keeps the account")
        problem = next(p for p in report.problems if "~/.claude-pessoal" in p)
        self.assertIn("Dev HINC", problem)
        self.assertIn("nada foi perdido", problem)

    def test_an_unreadable_claude_folders_file_keeps_the_login_vault(self):
        """S-18: without knowing which folder holds which account, the vault may hold the
        only copy of a folder's own login: keep it all and say so."""
        self.install([B])
        self.activate_profile()
        vault = dict(self.claude_store.vault)
        self.paths.claude_folders.write_text("{broken")
        preview = self.uninstaller().preview()
        self.assertNotIn("claudeVault", [i.kind for i in preview], "the vault is not going to be emptied")
        report = self.uninstaller().apply()
        self.assertEqual(self.claude_store.vault, vault)
        self.assertTrue(any("claude-folders.json" in p for p in report.problems), report.problems)
        self.assertFalse(self.environment.app_support_directory.exists(), "the rest still runs")

    def test_co_author_opt_out_written_by_multicred_is_undone(self):
        self.install([B])
        self.activate_profile()
        settings = Path(self.claude_dir, "settings.json")
        self.assertEqual(json.loads(settings.read_text())["attribution"], {"commit": "", "pr": ""})
        self.uninstaller().apply()
        self.assertNotIn("attribution", json.loads(settings.read_text()))

    def test_a_folder_no_profile_uses_any_more_that_held_a_login_is_cleaned_too(self):
        """S-25: the folders are listed before the own logins are given back (that empties
        the occupancy), so a folder that left the profiles but still held a MultiCred
        login gets its own-login vault entry deleted and its co-author opt-out undone."""
        self.install([B])
        self.activate_profile()
        settings = Path(self.claude_dir, "settings.json")
        self.assertEqual(json.loads(settings.read_text())["attribution"], {"commit": "", "pr": ""})
        # The profile now points to another folder (saved, not switched yet).
        other_dir = f"{self.home}/.claude-outra"
        os.makedirs(other_dir)
        self.profile.claude_config_dir = other_dir
        ConfigRepository(self.environment).save(self.config)

        report = self.uninstaller().apply()

        self.assertEqual(report.problems, [])
        restored = json.loads(self.claude_store.read_folder_item(self.claude_dir))
        self.assertEqual(restored["claudeAiOauth"]["accessToken"], "sk-ant-oat01-own")
        self.assertEqual(self.claude_store.vault, {}, "its own login left the vault too")
        self.assertNotIn("attribution", json.loads(settings.read_text()))

    def test_gh_accounts_are_never_touched(self):
        self.install([B])
        self.activate_profile()
        before = len(self.runner.requests)
        self.uninstaller().apply()
        self.assertEqual([r for r in self.requests_after(before) if Path(r.executable).name == "gh"], [])
        self.assertEqual(self.system.gh_active, "dev-pessoal")

    def test_backups_are_moved_out_and_the_rest_of_the_app_folder_is_deleted(self):
        self.write(self.zshrc, "export ORIGINAL=1\n")
        self.install([Z])
        self.write(self.paths.root / "stray.txt", "x")
        report = self.uninstaller().apply()
        stamp = Installer.stamp(self.environment.clock())
        destination = self.home / ".config" / f"multicred-backups-{stamp}"
        self.assertEqual(report.backups_kept_at, f"~/.config/multicred-backups-{stamp}")
        copies = list(destination.rglob("*zshrc*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "export ORIGINAL=1\n")
        self.assertFalse(self.paths.root.exists())

    def test_no_backups_means_nothing_is_kept(self):
        self.install([B])  # every file was created by MultiCred: no backup copies
        report = self.uninstaller().apply()
        self.assertIsNone(report.backups_kept_at)
        self.assertEqual(list((self.home / ".config").glob("multicred-backups-*")), [])

    def test_symlinked_rc_file_is_cleaned_at_its_target_and_the_link_kept(self):
        dotfiles = self.home / "dotfiles"
        self.write(dotfiles / "zshrc", "export A=1\n")
        self.zshrc.symlink_to(dotfiles / "zshrc")
        self.install([Z])
        self.uninstaller().apply()
        self.assertTrue(self.zshrc.is_symlink())
        self.assertEqual((dotfiles / "zshrc").read_text(), "export A=1\n")

    def test_preview_lists_everything_and_changes_nothing(self):
        self.write(self.zshrc, "export A=1\n")
        self.install([Z, B, F])
        self.activate_profile()
        self.autostart.enable()
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file() and not p.is_symlink()}
        requests = len(self.runner.requests)
        items = self.uninstaller().preview()
        after = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file() and not p.is_symlink()}
        self.assertEqual(before, after)
        self.assertEqual(self.requests_after(requests), [], "the preview runs no command")
        kinds = {(i.kind, i.path) for i in items}
        for expected in [("block", "~/.zshrc"), ("block", "~/.bashrc"), ("block", "~/.gitconfig"),
                         ("file", "~/.config/fish/conf.d/multicred.fish"), ("file", "~/" + ENV_D),
                         ("file", "~/.config/autostart/app.multicred.MultiCred.desktop"),
                         ("session", "CLAUDE_CONFIG_DIR"), ("claudeLogin", "~/.claude-pessoal"),
                         ("appDir", "~/.config/multicred")]:
            self.assertIn(expected, kinds)
        zsh = next(i for i in items if i.path == "~/.zshrc")
        self.assertEqual(zsh.lines[0], managed_block.BEGIN)
        self.assertEqual(zsh.lines[-1], managed_block.END)
        self.assertEqual({i.kind for i in items} & {"secrets", "claudeVault", "backups"},
                         {"secrets", "claudeVault", "backups"})
        self.assertNotIn("claudeFolder", {i.kind for i in items}, "D12 option is off by default")

    def test_running_it_twice_is_harmless(self):
        self.write(self.zshrc, "export A=1\n")
        self.install([Z])
        self.uninstaller().apply()
        report = self.uninstaller().apply()
        self.assertEqual(report.problems, [])
        self.assertEqual(self.zshrc.read_text(), "export A=1\n")

    # MARK: D12: also log out and delete Claude folders created by MultiCred

    def created_folder(self):
        folder = f"{self.home}/.claude-novo"
        os.makedirs(folder)
        Path(folder, "history.jsonl").write_text("{}\n")
        remember_created_claude_folder(self.environment, folder)
        return folder

    def test_created_folders_are_kept_unless_asked(self):
        self.install([B])
        folder = self.created_folder()
        self.assertEqual(self.uninstaller().created_claude_folders(), [folder])
        self.uninstaller().apply()
        self.assertTrue(Path(folder).is_dir())
        self.assertNotIn("claude", [Path(r.executable).name for r in self.runner.requests])

    def test_created_folders_are_logged_out_then_deleted_when_asked(self):
        self.install([B])
        folder = self.created_folder()
        self.runner.on("claude", ["auth", "logout"])
        self.assertIn(("claudeFolder", "~/.claude-novo"),
                      [(i.kind, i.path) for i in self.uninstaller().preview(remove_claude_folders=True)])
        report = self.uninstaller().apply(remove_claude_folders=True)
        logout = [r for r in self.runner.requests if Path(r.executable).name == "claude"]
        self.assertEqual([r.arguments for r in logout], [["auth", "logout"]])
        self.assertEqual(logout[0].environment.get("CLAUDE_CONFIG_DIR"), folder)
        self.assertFalse(Path(folder).exists())
        self.assertEqual(report.deleted_folders, ["~/.claude-novo"])
        self.assertEqual(report.problems, [])

    def test_a_folder_whose_logout_fails_is_kept(self):
        self.install([B])
        folder = self.created_folder()
        self.runner.on("claude", ["auth", "logout"], exit_code=1, stderr="network down")
        report = self.uninstaller().apply(remove_claude_folders=True)
        self.assertTrue(Path(folder).is_dir())
        self.assertEqual(len(report.problems), 1)
        self.assertIn("~/.claude-novo", report.problems[0])

    def test_the_default_claude_folder_is_never_deleted(self):
        self.install([B])
        default = self.home / ".claude"
        default.mkdir()
        remember_created_claude_folder(self.environment, str(default))
        self.runner.on("claude", ["auth", "logout"])
        self.assertEqual(self.uninstaller().created_claude_folders(), [])
        self.uninstaller().apply(remove_claude_folders=True)
        self.assertTrue(default.is_dir())

    def test_a_remembered_folder_outside_home_is_never_deleted(self):
        self.install([B])
        remember_created_claude_folder(self.environment, "/etc")
        remember_created_claude_folder(self.environment, f"{self.home}/../escape")
        self.assertEqual(self.uninstaller().created_claude_folders(), [])


    def test_a_folder_created_by_the_account_editor_is_remembered(self):
        from multicred.core.claude.account_editor import ClaudeFolderChoice, prepare_folder

        prepare_folder(ClaudeFolderChoice.new("~/.claude-cliente"), self.environment)
        prepare_folder(ClaudeFolderChoice.existing("~/.claude-pessoal"), self.environment)
        self.assertEqual(self.uninstaller().created_claude_folders(), [f"{self.home}/.claude-cliente"])
