"""S-02: the full switch engine (git, gh, graphical session, shells, API keys) with rollback."""

import threading
import time
import uuid

from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.engine.types import Busy, InvalidConfiguration, Preflight, StepFailed, SwitchStepKind
from multicred.core.models import (
    APIKeyAccount, APIProvider, Config, GitHubAccount, GitIdentity, Profile, ProfileColor,
)
from multicred.core.paths import AppPaths
from multicred.core.runner import CommandResult
from multicred.core.storage import StateStore
from multicred.core.tools import Tool
from tests.support import SafeTestCase

K = SwitchStepKind


class FakeSystem:
    """Stateful stand-in for gh and `systemctl --user` behind FakeCommandRunner."""

    def __init__(self, runner):
        self.lock = threading.Lock()
        self.gh_logins = [("dev-empresa", "success"), ("dev-pessoal", "success")]
        self.gh_active = "dev-empresa"
        self.session = {}
        self.fail_gh_switch_to = None
        self.fail_set = False
        self.fail_unset = False
        self.gh_switch_delay = 0.0
        runner.on("gh", ["auth", "status", "--json", "hosts"], respond=self._gh_status)
        runner.on("gh", ["config", "get", "-h", "github.com", "user"], respond=self._gh_active)
        runner.on("gh", ["auth", "switch"], respond=self._gh_switch)
        runner.on("systemctl", ["--user", "show-environment"], respond=self._show)
        runner.on("systemctl", ["--user", "set-environment"], respond=self._set)
        runner.on("systemctl", ["--user", "unset-environment"], respond=self._unset)

    def _gh_status(self, _request):
        import json
        with self.lock:
            entries = [{"host": "github.com", "login": login, "active": login == self.gh_active, "state": state}
                       for login, state in self.gh_logins]
        return CommandResult(0, json.dumps({"hosts": {"github.com": entries}}).encode())

    def _gh_active(self, _request):
        with self.lock:
            return CommandResult(0, (self.gh_active + "\n").encode())

    def _gh_switch(self, request):
        user = request.arguments[-1]
        if self.gh_switch_delay:
            time.sleep(self.gh_switch_delay)
        with self.lock:
            if user == self.fail_gh_switch_to:
                return CommandResult(1, b"", b"gh: keyring locked")
            if not any(login == user for login, _ in self.gh_logins):
                return CommandResult(1, b"", f"not logged in to github.com account {user}".encode())
            self.gh_active = user
            return CommandResult(0)

    def _show(self, _request):
        with self.lock:
            text = "".join(f"{k}={v}\n" for k, v in sorted(self.session.items()))
        return CommandResult(0, text.encode())

    def _set(self, request):
        with self.lock:
            if self.fail_set:
                return CommandResult(1, b"", b"systemctl failed")
            key, value = request.arguments[2].split("=", 1)
            self.session[key] = value
            return CommandResult(0)

    def _unset(self, request):
        with self.lock:
            if self.fail_unset:
                return CommandResult(1, b"", b"systemctl failed")
            self.session.pop(request.arguments[2], None)
            return CommandResult(0)

    @property
    def claude(self):
        with self.lock:
            return self.session.get("CLAUDE_CONFIG_DIR")


class RecordingLog:
    def __init__(self):
        self.entries = []

    def log(self, level, message, redacting=()):
        self.entries.append((level, message))


class FakeTools:
    def __init__(self, installed=(Tool.GIT, Tool.GH, Tool.CLAUDE)):
        self.installed = set(installed)

    def locate(self, tool):
        return f"/fake/bin/{tool.value}" if tool in self.installed else None

    def search_directories(self):
        return ["/fake/bin"]


class SwitchEngineTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.system = FakeSystem(self.runner)
        self.paths = AppPaths(self.environment)
        self.claude_personal = f"{self.home}/.claude-pessoal"
        (self.home / ".claude-pessoal").mkdir()
        git_work = GitIdentity(nickname="Empresa", name="Dev Trabalho", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
        git_personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.dev",
                                   ssh_key_path="~/.ssh/id_pessoal", color=ProfileColor.ORANGE)
        gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
        gh_personal = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE)
        self.openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.gemini = APIKeyAccount(nickname="Pessoal", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
        self.secrets.set_secret(b"sk", self.openai.secret_account)
        self.secrets.set_secret(b"g", self.gemini.secret_account)
        self.work = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git_work.id,
                            github_account_id=gh_work.id, api_key_ids=[self.openai.id])
        self.personal = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git_personal.id,
                                github_account_id=gh_personal.id, claude_config_dir=self.claude_personal,
                                api_key_ids=[self.gemini.id])
        self.config = Config(git_identities=[git_work, git_personal], github_accounts=[gh_work, gh_personal],
                             api_keys=[self.openai, self.gemini], profiles=[self.work, self.personal])

    def engine(self, tools=None):
        return SwitchEngine(self.environment, tools or FakeTools())

    def snapshot_files(self):
        paths = [self.paths.gitconfig, self.paths.env_sh, self.paths.env_fish, self.paths.state, self.paths.generation,
                 self.home / ".config/environment.d/90-multicred.conf"]
        return {p.name: (p.read_bytes() if p.is_file() else None) for p in paths}

    def mutating_commands(self):
        out = []
        for request in self.runner.requests:
            args = request.arguments
            if args[:2] == ["auth", "switch"] or (len(args) > 1 and args[1] in ("set-environment", "unset-environment")):
                out.append(" ".join(args))
        return out

    # MARK: happy paths

    def test_switch_to_personal_applies_everything(self):
        result = self.engine().switch_profile(self.personal.id, self.config)
        self.assertEqual(result.applied_steps, [K.GIT, K.GITHUB, K.SESSION, K.CLAUDE_SETTINGS, K.ENV_FILES, K.STATE])
        self.assertEqual(self.system.gh_active, "dev-pessoal")
        self.assertEqual(self.system.claude, self.claude_personal)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_personal}",
                      self.read(self.home / ".config/environment.d/90-multicred.conf"))
        gitconfig = self.read(self.paths.gitconfig)
        self.assertIn('email = "dev@pessoal.dev"', gitconfig)
        self.assertIn(f"sshCommand = \"ssh -i '{self.home}/.ssh/id_pessoal' -o IdentitiesOnly=yes\"", gitconfig)
        env_sh = self.read(self.paths.env_sh)
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN GEMINI_API_KEY OPENAI_API_KEY", env_sh)
        self.assertIn(f"export CLAUDE_CONFIG_DIR='{self.claude_personal}'", env_sh)
        self.assertIn('GEMINI_API_KEY="$(', env_sh)
        self.assertNotIn('OPENAI_API_KEY="$(', env_sh)
        self.assertIn("set -gx GEMINI_API_KEY", self.read(self.paths.env_fish))
        self.assertEqual(self.read(self.paths.generation), "1\n")
        state = StateStore(self.paths, self.environment.clock).load()
        self.assertEqual((state.profile_id, state.claude_override_id, state.generation), (self.personal.id, None, 1))
        self.assertEqual(self.mode_of(self.paths.state), 0o600)

    def test_a_removed_key_is_still_unset_by_the_next_env_files(self):
        """S-15: the variable the previous env.sh managed must be unset once more, or open
        terminals keep exporting a key that no longer exists in MultiCred."""
        self.engine().switch_profile(self.work.id, self.config)
        self.assertIn('OPENAI_API_KEY="$(', self.read(self.paths.env_sh))
        self.config.api_keys = [self.gemini]
        self.work.api_key_ids = []
        self.engine().switch_profile(self.work.id, self.config)
        env_sh = self.read(self.paths.env_sh)
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN GEMINI_API_KEY OPENAI_API_KEY", env_sh)
        self.assertNotIn('OPENAI_API_KEY="$(', env_sh)
        self.assertIn("set -q OPENAI_API_KEY; and set -e -g OPENAI_API_KEY", self.read(self.paths.env_fish))
        # Bounded: only the generation right after the removal unsets it. A variable the
        # user exports on their own later is not unset by MultiCred forever.
        self.engine().switch_profile(self.work.id, self.config)
        self.assertEqual(self.read(self.paths.env_sh).splitlines()[1], "unset CLAUDE_CODE_OAUTH_TOKEN GEMINI_API_KEY")

    def test_only_names_the_previous_file_exported_are_carried_and_never_trusted(self):
        self.paths.env_sh.parent.mkdir(parents=True, exist_ok=True)
        self.paths.env_sh.write_text(
            "# Generated by MultiCred. Do not edit. generation=0\n"
            "unset ONLY_UNSET PATH\n"
            "OLD_KEY=\"$('/usr/bin/secret-tool' lookup service 'app.multicred.apikey' account 'X' 2>/dev/null)\" "
            "&& [ -n \"$OLD_KEY\" ] && export OLD_KEY || unset OLD_KEY\n"
            "PATH=\"$(x)\" && export PATH\n"
            "$(touch pwned)=\"$(x)\" && export lower\n")
        self.engine().switch_profile(self.personal.id, self.config)
        first = self.read(self.paths.env_sh).splitlines()[1]
        self.assertEqual(first, "unset CLAUDE_CODE_OAUTH_TOKEN GEMINI_API_KEY OLD_KEY OPENAI_API_KEY",
                         "only exported, valid, non-reserved names are carried over")
        self.assertFalse((self.home / "pwned").exists())

    def test_default_folder_unsets_the_variable(self):
        self.system.session["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        self.engine().switch_profile(self.work.id, self.config)
        self.assertIsNone(self.system.claude)
        self.assertIn("unset CLAUDE_CONFIG_DIR", self.read(self.paths.env_sh))

    def test_already_active_github_account_is_not_switched_again(self):
        self.engine().switch_profile(self.work.id, self.config)
        self.assertFalse(any(c.startswith("auth switch") for c in self.mutating_commands()))

    def test_profile_without_github_still_applies_its_claude_folder(self):
        self.system.session["CLAUDE_CONFIG_DIR"] = self.claude_personal
        bare = Profile(name="Só git", color=ProfileColor.GREEN, git_identity_id=self.config.git_identities[0].id)
        self.config.profiles.append(bare)
        result = self.engine().switch_profile(bare.id, self.config)
        self.assertEqual(result.applied_steps, [K.GIT, K.SESSION, K.ENV_FILES, K.STATE])
        self.assertIsNone(self.system.claude)

    def test_generation_increases_and_activated_at_set_on_profile_switch(self):
        engine = self.engine()
        first = engine.switch_profile(self.work.id, self.config)
        second = engine.reapply_active_profile(self.config)
        self.assertEqual((first.state.generation, second.state.generation), (1, 2))
        self.assertEqual(second.state.activated_at, first.state.activated_at)

    def test_reapply_without_active_profile_does_nothing(self):
        self.assertIsNone(self.engine().reapply_active_profile(self.config))
        self.assertEqual(self.runner.requests, [])

    def test_reapply_when_the_active_profile_was_deleted_does_nothing_and_logs(self):
        """S-22: the profile in the state is gone from the configuration (deleted while
        active): re-applying at session start does nothing instead of failing every time."""
        log = RecordingLog()
        engine = SwitchEngine(self.environment, FakeTools(), log)
        engine.switch_profile(self.work.id, self.config)
        state_before = self.paths.state.read_bytes()
        requests_before = len(self.runner.requests)
        self.config.profiles.remove(self.work)

        self.assertIsNone(engine.reapply_active_profile(self.config))
        self.assertEqual(self.paths.state.read_bytes(), state_before, "the state is not touched")
        self.assertEqual(len(self.runner.requests), requests_before, "nothing is run")
        self.assertTrue(any("no longer exists" in message for _, message in log.entries), log.entries)

    def test_switching_to_an_unknown_profile_is_still_profile_not_found(self):
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_profile(uuid.uuid4(), self.config)
        self.assertEqual(raised.exception.failure.kind, "profileNotFound")

    # MARK: preflight

    def test_preflight_failures_change_nothing(self):
        (self.home / ".claude-pessoal").rmdir()
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_profile(self.personal.id, self.config)
        self.assertEqual((raised.exception.failure.kind, raised.exception.failure.detail),
                         ("claudeDirectoryMissing", self.claude_personal))

        (self.home / ".claude-pessoal").mkdir()
        self.secrets.delete_item(self.gemini.secret_account)
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_profile(self.personal.id, self.config)
        failure = raised.exception.failure
        self.assertEqual((failure.kind, failure.detail, failure.extra), ("apiKeyMissing", "Pessoal", "GEMINI_API_KEY"))

        with self.assertRaises(Preflight) as raised:
            self.engine(FakeTools([Tool.GIT])).switch_profile(self.personal.id, self.config)
        self.assertEqual((raised.exception.failure.kind, raised.exception.failure.detail), ("toolMissing", "gh"))

        self.assertEqual(self.mutating_commands(), [])
        self.assertTrue(all(v is None for v in self.snapshot_files().values()), "a failed preflight writes nothing")

    def test_unavailable_secret_service_is_a_preflight_failure(self):
        class Broken:
            def contains_item(self, account):
                raise RuntimeError("Cannot autolaunch D-Bus without X11")
        object.__setattr__(self.environment, "secrets", Broken())
        with self.assertRaises(Preflight) as raised:
            self.engine().switch_profile(self.work.id, self.config)
        self.assertEqual(raised.exception.failure.kind, "secretsUnavailable")

    def test_switch_never_runs_the_network_bound_github_status(self):
        self.engine().switch_profile(self.personal.id, self.config)
        self.assertFalse(any(r.arguments[:2] == ["auth", "status"] for r in self.runner.requests))

    def test_child_processes_get_an_explicit_environment(self):
        self.engine().switch_profile(self.personal.id, self.config)
        gh = next(r for r in self.runner.requests if r.arguments[:2] == ["auth", "switch"])
        self.assertEqual(gh.executable, "/fake/bin/gh")
        self.assertEqual(gh.environment["HOME"], str(self.home))
        self.assertEqual(gh.environment["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(gh.environment["PATH"], "/fake/bin")
        self.assertEqual(gh.arguments, ["auth", "switch", "--hostname", "github.com", "--user", "dev-pessoal"])

    def test_account_unknown_to_gh_fails_at_github_and_rolls_back_git(self):
        engine = self.engine()
        engine.switch_profile(self.work.id, self.config)
        before = self.snapshot_files()
        self.system.gh_logins = [("dev-empresa", "success")]
        with self.assertRaises(StepFailed) as raised:
            engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.GITHUB)
        self.assertIn("not logged in", raised.exception.reason)
        self.assertTrue(raised.exception.rollback.complete)
        self.assertEqual(self.snapshot_files(), before)

    def test_invalid_configuration_is_rejected_before_anything(self):
        self.config.git_identities[0].email = "x\ny"
        with self.assertRaises(InvalidConfiguration):
            self.engine().switch_profile(self.work.id, self.config)
        self.assertEqual(self.runner.requests, [])

    # MARK: rollback

    def test_failure_at_github_restores_previous_profile_exactly(self):
        engine = self.engine()
        engine.switch_profile(self.work.id, self.config)
        before, session_before = self.snapshot_files(), dict(self.system.session)
        self.system.fail_gh_switch_to = "dev-pessoal"
        with self.assertRaises(StepFailed) as raised:
            engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.GITHUB)
        self.assertIn("keyring locked", raised.exception.reason)
        self.assertTrue(raised.exception.rollback.complete)
        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual(self.system.gh_active, "dev-empresa")
        self.assertEqual(self.system.session, session_before)
        self.assertEqual(engine.current_state().profile_id, self.work.id)

    def test_failure_at_session_undoes_github_and_git(self):
        engine = self.engine()
        engine.switch_profile(self.work.id, self.config)
        before = self.snapshot_files()
        self.system.fail_set = True
        with self.assertRaises(StepFailed) as raised:
            engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.SESSION)
        self.assertTrue(raised.exception.rollback.complete)
        self.assertEqual(self.system.gh_active, "dev-empresa", "gh switched back")
        self.assertEqual(self.snapshot_files(), before, "environment.d restored too")

    def test_failure_writing_env_files_undoes_session_github_and_git(self):
        engine = self.engine()
        engine.switch_profile(self.work.id, self.config)
        before = self.snapshot_files()
        self.paths.env_fish.unlink()
        self.paths.env_fish.mkdir()  # the second write fails
        with self.assertRaises(StepFailed) as raised:
            engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.ENV_FILES)
        self.assertTrue(raised.exception.rollback.complete)
        self.assertIsNone(self.system.claude, "session back to the default folder")
        self.assertEqual(self.system.gh_active, "dev-empresa")
        self.assertEqual(self.paths.env_sh.read_bytes(), before["env.sh"], "env.sh restored after a partial write")
        self.assertEqual(self.paths.gitconfig.read_bytes(), before["gitconfig"])

    def test_failure_at_commit_leaves_shells_on_the_previous_generation(self):
        engine = self.engine()
        engine.switch_profile(self.work.id, self.config)
        before = self.snapshot_files()
        self.paths.generation.unlink()
        self.paths.generation.mkdir()
        with self.assertRaises(StepFailed) as raised:
            engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.STATE)
        self.assertEqual(self.paths.state.read_bytes(), before["state.json"])
        self.assertEqual(self.paths.env_sh.read_bytes(), before["env.sh"])
        self.assertEqual(self.system.gh_active, "dev-empresa")
        self.assertIsNone(self.system.claude)

    def test_rollback_that_fails_is_reported_as_incomplete(self):
        engine = self.engine()
        engine.switch_profile(self.work.id, self.config)  # session variable unset
        self.system.fail_unset = True
        self.paths.env_fish.unlink()
        self.paths.env_fish.mkdir()
        with self.assertRaises(StepFailed) as raised:
            engine.switch_profile(self.personal.id, self.config)
        self.assertEqual(raised.exception.step, K.ENV_FILES)
        self.assertEqual(raised.exception.rollback.failed, (K.SESSION,))
        self.assertEqual(self.system.gh_active, "dev-empresa", "the other steps were still undone")

    def test_concurrent_switch_is_rejected_as_busy(self):
        engine = self.engine()
        self.system.gh_switch_delay = 0.4
        outcome = {}
        first = threading.Thread(target=lambda: outcome.setdefault("first", engine.switch_profile(self.personal.id, self.config)))
        first.start()
        time.sleep(0.1)
        with self.assertRaises(Busy):
            engine.switch_profile(self.work.id, self.config)
        first.join(5)
        self.assertEqual(outcome["first"].state.profile_id, self.personal.id)
