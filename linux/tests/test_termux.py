"""S-09: MultiCred on Termux (Android), simulated on this machine.

There is no device here, so a Termux world is built from temporary folders:
HOME and PREFIX are temporary, `TERMUX_VERSION` is set in the injected
environment, the only tools are fake scripts under `$PREFIX/bin` (Termux has no
/usr/bin), and there is no systemd, D-Bus or Secret Service. Fake `systemctl` and
`secret-tool` scripts are planted there too: they leave a marker if anything
ever runs them.
"""

import dataclasses
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from multicred.core import real_env
from multicred.core.claude.credentials import FileClaudeCredentialStore
from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.engine.types import ClaudeTarget, SwitchStepKind
from multicred.core.env import SystemEnvironment
from multicred.core.file_secrets import FileSecretStore, FileVaultBackend, SecretFileError
from multicred.core.models import (
    APIKeyAccount, APIProvider, Config, GitHubAccount, GitIdentity, Profile, ProfileColor, ShellKind,
)
from multicred.core.paths import AppPaths
from multicred.core.platform import Platform, SecretBackend, detect
from multicred.core.rendering import hooks
from multicred.core.rendering.envfile import (
    APIKeyExport, SecretFileReader, SecretToolLookup, ShellEnvironment, render_fish, render_posix, secret_reader_for,
)
from multicred.core.runner import ProcessRunner
from multicred.core.secrets import EmptySecret, InvalidAccount
from multicred.core.storage import ConfigRepository
from multicred.core.tools import DEFAULT_SEARCH_DIRECTORIES, ToolLocator
from tests.support import FIXED_NOW, SafeTestCase

TERMUX_VERSION = "0.118.3"
KNOWN = "6B1F4C1E-0D7A-4E0B-9A3E-3C1C2B0F9A11"
BASH = shutil.which("bash") or "/bin/bash"


class RecordingRunner:
    """Real processes, every request remembered (to prove systemctl/secret-tool never run)."""

    def __init__(self, inner):
        self.inner = inner
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.inner.run(request)


# gh stand-in. Only shell builtins: the child PATH is $PREFIX/bin, where nothing else lives.
FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
if [ "$1 $2" = "config get" ]; then
  [ -r "{state}" ] || exit 0
  IFS= read -r user < "{state}"
  printf '%s\\n' "$user"
  exit 0
fi
if [ "$1 $2" = "auth switch" ]; then
  case "$6" in
    dev-a|dev-b) printf '%s\\n' "$6" > "{state}"; exit 0 ;;
  esac
  printf 'not logged in to github.com account %s\\n' "$6" >&2
  exit 1
fi
exit 1
"""

FORBIDDEN = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "{marker}"
exit 1
"""


class TermuxWorld(SafeTestCase):
    """A simulated Termux: temporary HOME and PREFIX, fake tools in $PREFIX/bin."""

    def setUp(self):
        super().setUp()
        root = Path(tempfile.mkdtemp(prefix="MultiCredTermux-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        self.prefix = root / "usr"
        (self.prefix / "bin").mkdir(parents=True)
        (self.prefix / "tmp").mkdir()
        self.gh_log = root / "gh.log"
        self.gh_state = root / "gh-active"
        self.forbidden = root / "forbidden.log"
        self.write_tool("gh", FAKE_GH.format(log=self.gh_log, state=self.gh_state))
        for name in ("systemctl", "secret-tool", "dbus-send"):
            self.write_tool(name, FORBIDDEN.format(marker=self.forbidden))
        self.termux_environ = {
            "TERMUX_VERSION": TERMUX_VERSION,
            "PREFIX": str(self.prefix),
            "HOME": str(self.home),
            "TMPDIR": str(self.prefix / "tmp"),
            "SHELL": str(self.prefix / "bin" / "bash"),  # absent: the login-shell PATH probe just fails
            # Real Termux sets LD_PRELOAD to termux-exec; left empty here so glibc on this
            # machine does not complain about a library that does not exist.
            "LD_PRELOAD": "",
        }
        built = real_env.make_termux(self.termux_environ)
        self.recording = RecordingRunner(built.runner)
        self.termux = dataclasses.replace(built, mode="test", clock=lambda: FIXED_NOW, runner=self.recording)
        self.paths = AppPaths(self.termux)

    def write_tool(self, name, content):
        path = self.prefix / "bin" / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def termux_tools(self, environment=None):
        environment = environment or self.termux
        return ToolLocator(environment, well_known=environment.platform.tool_search_directories)

    def assert_nothing_forbidden_ran(self):
        names = {Path(r.executable).name for r in self.recording.requests}
        self.assertFalse(names & {"systemctl", "secret-tool", "dbus-send"}, names)
        self.assertFalse(self.forbidden.exists(), self.forbidden.read_text() if self.forbidden.exists() else "")


class PlatformDetectionTests(SafeTestCase):
    def test_desktop_without_termux_variables(self):
        self.assertEqual(detect({}), Platform.desktop())
        self.assertFalse(detect({"PREFIX": "/usr/local", "HOME": "/home/x"}).is_termux)

    def test_termux_version_or_termux_prefix_or_com_termux_prefix(self):
        termux_prefix = "/data/data/com.termux/files/usr"
        for environ in ({"TERMUX_VERSION": TERMUX_VERSION, "PREFIX": termux_prefix},
                        {"TERMUX__PREFIX": termux_prefix},
                        {"PREFIX": termux_prefix}):
            platform = detect(environ)
            self.assertTrue(platform.is_termux, environ)
            self.assertEqual(platform.prefix, termux_prefix)

    def test_termux_without_prefix_uses_the_standard_one(self):
        self.assertEqual(detect({"TERMUX_VERSION": TERMUX_VERSION}).prefix, "/data/data/com.termux/files/usr")

    def test_desktop_values_are_the_ones_the_app_always_used(self):
        desktop = Platform.desktop()
        self.assertEqual(desktop.tool_search_directories, DEFAULT_SEARCH_DIRECTORIES)
        self.assertEqual((desktop.sh, desktop.true, desktop.setsid), ("/bin/sh", "/usr/bin/true", "/usr/bin/setsid"))
        self.assertEqual(desktop.temp_directory, "/tmp")
        self.assertTrue(desktop.has_graphical_session)
        self.assertEqual(desktop.secret_backend, SecretBackend.SECRET_SERVICE)
        self.assertEqual(desktop.child_environment(), {})

    def test_termux_values_come_from_the_prefix(self):
        termux = detect({"TERMUX_VERSION": TERMUX_VERSION, "PREFIX": "/p/usr", "TMPDIR": "/p/usr/tmp",
                         "LD_PRELOAD": "/p/usr/lib/libtermux-exec-ld-preload.so"})
        self.assertEqual(termux.tool_search_directories[0], "/p/usr/bin")
        self.assertEqual((termux.sh, termux.true, termux.setsid), ("/p/usr/bin/sh", "/p/usr/bin/true",
                                                                    "/p/usr/bin/setsid"))
        self.assertEqual(termux.temp_directory, "/p/usr/tmp")
        self.assertFalse(termux.has_graphical_session)
        self.assertEqual(termux.secret_backend, SecretBackend.FILES)
        self.assertEqual(termux.child_environment(), {"PREFIX": "/p/usr", "TMPDIR": "/p/usr/tmp",
                                                      "LD_PRELOAD": "/p/usr/lib/libtermux-exec-ld-preload.so"})
        for value in [*termux.tool_search_directories, termux.sh, termux.true, termux.setsid, termux.temp_directory]:
            self.assertFalse(value.startswith(("/usr/", "/bin/", "/tmp")), value)

    def test_termux_temp_directory_defaults_to_prefix_tmp(self):
        # /tmp is not writable on Android.
        self.assertEqual(detect({"TERMUX_VERSION": TERMUX_VERSION, "PREFIX": "/p/usr"}).temp_directory, "/p/usr/tmp")

    def test_system_environment_defaults_to_desktop(self):
        self.assertEqual(self.environment.platform, Platform.desktop())
        self.assertNotIn("PREFIX", self.environment.base_process_environment(["/x"]))

    def test_children_get_prefix_tmpdir_and_ld_preload_on_termux(self):
        termux = detect({"TERMUX_VERSION": TERMUX_VERSION, "PREFIX": "/p/usr", "LD_PRELOAD": "/p/usr/lib/x.so"})
        environment = dataclasses.replace(self.environment, platform=termux)
        child = environment.base_process_environment(["/p/usr/bin"])
        self.assertEqual((child["PREFIX"], child["TMPDIR"], child["LD_PRELOAD"], child["PATH"]),
                         ("/p/usr", "/p/usr/tmp", "/p/usr/lib/x.so", "/p/usr/bin"))


class RealEnvironmentTests(TermuxWorld):
    def test_make_termux_builds_file_backed_stores_without_touching_anything(self):
        environment = real_env.make_termux(self.termux_environ)
        self.assertEqual(environment.mode, "real")
        self.assertEqual(environment.home, self.home)
        self.assertTrue(environment.platform.is_termux)
        self.assertEqual(environment.platform.prefix, str(self.prefix))
        self.assertIsInstance(environment.runner, ProcessRunner)
        self.assertIsInstance(environment.secrets, FileSecretStore)
        self.assertEqual(environment.secrets.directory, self.home / ".config/multicred/secrets")
        self.assertIsInstance(environment.claude_credentials, FileClaudeCredentialStore)
        self.assertIsInstance(environment.claude_credentials.vault, FileVaultBackend)
        self.assertEqual(environment.claude_credentials.vault.directory,
                         self.home / ".config/multicred/secrets/claude-login")
        self.assertEqual(environment.login_shell, str(self.prefix / "bin" / "bash"))
        self.assertIsNone(environment.dbus_session_bus_address)
        self.assertEqual(list(self.home.iterdir()), [], "building the environment must not write")

    def test_make_real_picks_termux_when_detected(self):
        self.assertTrue(real_env.make_real(self.termux_environ).platform.is_termux)
        self.assertFalse(real_env.detect_platform({}).is_termux)


class FileSecretStoreTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.home / ".config/multicred/secrets"
        self.store = FileSecretStore(self.directory)

    def test_one_private_file_per_account(self):
        self.store.set_secret(b"sk-value", KNOWN)
        self.assertEqual(self.mode_of(self.directory), 0o700)
        self.assertEqual(self.mode_of(self.directory / KNOWN), 0o600)
        self.assertEqual((self.directory / KNOWN).read_bytes(), b"sk-value")
        self.assertTrue(self.store.contains_item(KNOWN))
        self.assertEqual(self.store.all_accounts(), [KNOWN])
        self.store.delete_item(KNOWN)
        self.assertFalse(self.store.contains_item(KNOWN))
        self.assertFalse((self.directory / KNOWN).exists())
        self.store.delete_item(KNOWN)  # deleting a missing item is not an error

    def test_overwrite_is_atomic_and_tightens_modes(self):
        self.directory.mkdir(parents=True, mode=0o755)
        self.directory.chmod(0o755)
        (self.directory / KNOWN).write_bytes(b"old")
        (self.directory / KNOWN).chmod(0o644)
        self.store.set_secret(b"new", KNOWN)
        self.assertEqual((self.directory / KNOWN).read_bytes(), b"new")
        self.assertEqual(self.mode_of(self.directory / KNOWN), 0o600)
        self.assertEqual(self.mode_of(self.directory), 0o700)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [KNOWN], "no temp file left behind")

    def test_only_uppercase_uuids_name_files(self):
        for bad in ("../escape", "claude-login", KNOWN.lower(), "", "A/B", KNOWN + "/x"):
            with self.assertRaises(InvalidAccount, msg=bad):
                self.store.set_secret(b"v", bad)
            self.assertFalse(self.store.contains_item(bad))
        self.assertFalse((self.home / ".config/multicred/escape").exists())

    def test_empty_values_and_line_breaks_are_refused(self):
        with self.assertRaises(EmptySecret):
            self.store.set_secret(b"", KNOWN)
        # The shell hook reads one line; a value with a line break would be cut short.
        for value in (b"a\nb", b"a\rb", b"a\x00b"):
            with self.assertRaises(SecretFileError):
                self.store.set_secret(value, KNOWN)
        self.assertFalse((self.directory / KNOWN).exists())

    def test_a_symlink_in_place_of_the_file_is_never_followed(self):
        self.directory.mkdir(parents=True)
        outside = self.home / "outside"
        outside.write_bytes(b"keep")
        (self.directory / KNOWN).symlink_to(outside)
        with self.assertRaises(SecretFileError):
            self.store.set_secret(b"v", KNOWN)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_all_accounts_lists_only_secret_files(self):
        other = str(uuid.uuid4()).upper()
        self.store.set_secret(b"a", KNOWN)
        self.store.set_secret(b"b", other)
        (self.directory / "claude-login").mkdir()
        (self.directory / "notes.txt").write_text("x")
        self.assertEqual(self.store.all_accounts(), sorted([KNOWN, other]))

    def test_never_returns_a_value(self):
        public = {name for name in dir(self.store) if not name.startswith("_")}
        self.assertEqual(public - {"directory"}, {"set_secret", "contains_item", "delete_item", "all_accounts"})


class FileVaultBackendTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.home / ".config/multicred/secrets/claude-login"
        self.vault = FileVaultBackend(self.directory)

    def test_store_lookup_clear_accounts(self):
        self.assertIsNone(self.vault.lookup(KNOWN))
        self.vault.store(KNOWN, b'{"claudeAiOauth":{}}', label="ignored")
        self.assertEqual(self.vault.lookup(KNOWN), b'{"claudeAiOauth":{}}')
        self.assertEqual(self.vault.accounts(), [KNOWN])
        self.assertEqual(self.mode_of(self.directory), 0o700)
        self.assertEqual(self.mode_of(self.directory.parent), 0o700)
        self.assertEqual(self.mode_of(self.directory / KNOWN), 0o600)
        self.vault.clear(KNOWN)
        self.vault.clear(KNOWN)
        self.assertIsNone(self.vault.lookup(KNOWN))
        self.assertEqual(self.vault.accounts(), [])

    def test_invalid_keys_are_refused(self):
        with self.assertRaises(InvalidAccount):
            self.vault.store("../x", b"v")
        with self.assertRaises(InvalidAccount):
            self.vault.lookup("../x")

    def test_credential_store_uses_it_as_the_login_vault(self):
        store = FileClaudeCredentialStore(self.home, vault=self.vault)
        store.write_vault(KNOWN, b"{}")
        self.assertEqual(store.read_vault(KNOWN), b"{}")
        self.assertEqual(store.vault_keys(), [KNOWN])
        store.delete_vault(KNOWN)
        self.assertEqual(store.vault_keys(), [])


class SecretFileEnvTests(SafeTestCase):
    """env.sh/env.fish on Termux read the key files with shell builtins only."""

    def setUp(self):
        super().setUp()
        self.secrets_dir = self.home / ".config/multicred/secrets"
        self.secrets_dir.mkdir(parents=True)
        self.reader = SecretFileReader()

    def shell_env(self, keys):
        return ShellEnvironment(generation=3, managed_vars=["GEMINI_API_KEY", "OPENAI_API_KEY"],
                                claude=ClaudeTarget.unchanged(), api_keys=keys)

    def source(self, text, preset=""):
        path = self.home / "env.sh"
        path.write_text(text)
        empty = self.home / "no-tools"
        empty.mkdir(exist_ok=True)
        script = f'{preset}\n. "$1"\nprintf \'%s|%s\' "${{OPENAI_API_KEY-unset}}" "${{GEMINI_API_KEY-unset}}"'
        # PATH has no programs at all: the file may use builtins only.
        return subprocess.run([BASH, "--norc", "--noprofile", "-c", script, "bash", str(path)],
                              capture_output=True, text=True, env={"HOME": str(self.home), "PATH": str(empty)})

    def test_reader_follows_the_platform(self):
        self.assertIsInstance(secret_reader_for(Platform.desktop()), SecretToolLookup)
        self.assertIsInstance(secret_reader_for(detect({"TERMUX_VERSION": "1"})), SecretFileReader)

    def test_reads_values_with_and_without_trailing_newline_literally(self):
        other = str(uuid.uuid4()).upper()
        (self.secrets_dir / KNOWN).write_bytes(b"sk a\\b$HOME`x`")  # no trailing newline
        (self.secrets_dir / other).write_bytes(b"g-value\n")
        text = render_posix(self.shell_env([APIKeyExport("OPENAI_API_KEY", KNOWN), APIKeyExport("GEMINI_API_KEY", other)]),
                            reader=self.reader)
        result = self.source(text)
        self.assertEqual((result.stdout, result.stderr), ("sk a\\b$HOME`x`|g-value", ""))

    def test_missing_or_empty_file_leaves_the_variable_unset(self):
        other = str(uuid.uuid4()).upper()
        (self.secrets_dir / other).write_bytes(b"")
        text = render_posix(self.shell_env([APIKeyExport("OPENAI_API_KEY", KNOWN), APIKeyExport("GEMINI_API_KEY", other)]),
                            reader=self.reader)
        result = self.source(text, preset="export OPENAI_API_KEY=old GEMINI_API_KEY=old")
        self.assertEqual((result.stdout, result.stderr), ("unset|unset", ""))

    def test_text_names_the_file_under_home_and_no_program(self):
        text = render_posix(self.shell_env([APIKeyExport("OPENAI_API_KEY", KNOWN)]), reader=self.reader)
        self.assertIn(f'"$HOME/.config/multicred/secrets/{KNOWN}"', text)
        fish = render_fish(self.shell_env([APIKeyExport("OPENAI_API_KEY", KNOWN)]), reader=self.reader)
        self.assertIn(f'"$HOME/.config/multicred/secrets/{KNOWN}"', fish)
        self.assertIn("read -l", fish)
        for rendered in (text, fish):
            for forbidden in ("secret-tool", "/usr/bin", "/tmp", "$("):
                self.assertNotIn(forbidden, rendered)

    def test_only_validated_accounts_reach_the_file(self):
        for render in (render_posix, render_fish):
            with self.assertRaises(InvalidAccount):
                render(self.shell_env([APIKeyExport("OPENAI_API_KEY", '"; rm -rf ~; "')]), reader=self.reader)

    def test_desktop_rendering_is_unchanged_by_default(self):
        env = self.shell_env([APIKeyExport("OPENAI_API_KEY", KNOWN)])
        self.assertEqual(render_posix(env), render_posix(env, reader=SecretToolLookup()))
        self.assertIn("/usr/bin/secret-tool", render_posix(env))
        self.assertEqual(render_fish(env), render_fish(env, reader=SecretToolLookup()))


class TermuxSwitchTests(TermuxWorld):
    """End to end: `multicred trocar` in the simulated Termux."""

    def setUp(self):
        super().setUp()
        git_a = GitIdentity(nickname="A", name="Dev A", email="dev@a.example", color=ProfileColor.INDIGO)
        git_b = GitIdentity(nickname="B", name="Dev B", email="dev@b.example", color=ProfileColor.ORANGE)
        gh_a = GitHubAccount(nickname="A", username="dev-a", color=ProfileColor.INDIGO)
        gh_b = GitHubAccount(nickname="B", username="dev-b", color=ProfileColor.ORANGE)
        self.openai = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        self.gemini = APIKeyAccount(nickname="G", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
        self.termux.secrets.set_secret(b"sk-openai $x", self.openai.secret_account)
        self.termux.secrets.set_secret(b"g-gemini", self.gemini.secret_account)
        self.a = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=git_a.id,
                         github_account_id=gh_a.id, api_key_ids=[self.openai.id])
        self.b = Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=git_b.id,
                         github_account_id=gh_b.id, api_key_ids=[self.gemini.id])
        self.config = Config(git_identities=[git_a, git_b], github_accounts=[gh_a, gh_b],
                             api_keys=[self.openai, self.gemini], profiles=[self.a, self.b])
        ConfigRepository(self.termux).save(self.config)

    def cli(self, *argv):
        import io
        from multicred import cli
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), environment=self.termux, tools=self.termux_tools(), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_secrets_live_in_private_files(self):
        directory = self.home / ".config/multicred/secrets"
        self.assertEqual(self.mode_of(directory), 0o700)
        self.assertEqual(self.mode_of(directory / self.openai.secret_account), 0o600)

    def test_switch_writes_git_switches_gh_and_skips_the_graphical_session(self):
        code, out, err = self.cli("trocar", "trabalho")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Agora em Trabalho", out)
        self.assertIn("dev@a.example", self.paths.gitconfig.read_text())
        git = shutil.which("git")
        if git:
            email = subprocess.run([git, "config", "--file", str(self.paths.gitconfig), "user.email"],
                                   capture_output=True, text=True).stdout.strip()
            self.assertEqual(email, "dev@a.example")
        self.assertIn("auth switch --hostname github.com --user dev-a", self.gh_log.read_text())
        self.assertEqual(self.gh_state.read_text().strip(), "dev-a")
        self.assertFalse((self.home / ".config/environment.d").exists())
        self.assert_nothing_forbidden_ran()

        gh_requests = [r for r in self.recording.requests if Path(r.executable).name == "gh"]
        self.assertTrue(gh_requests)
        for request in gh_requests:
            self.assertEqual(request.executable, str(self.prefix / "bin" / "gh"))
            self.assertEqual(request.environment["PREFIX"], str(self.prefix))
            self.assertEqual(request.environment["TMPDIR"], str(self.prefix / "tmp"))
            self.assertEqual(request.environment["PATH"].split(":"),
                             [str(self.prefix / "bin"), str(self.home / ".local/bin")])

        env_sh = self.paths.env_sh.read_text()
        self.assertIn(f'"$HOME/.config/multicred/secrets/{self.openai.secret_account}"', env_sh)
        for forbidden in ("secret-tool", "/usr/bin", "/tmp"):
            self.assertNotIn(forbidden, env_sh)

    def test_engine_has_no_session_step_on_termux(self):
        engine = SwitchEngine(self.termux, self.termux_tools())
        result = engine.switch_profile(self.b.id, self.config)
        self.assertNotIn(SwitchStepKind.SESSION, result.applied_steps)
        self.assertIn(SwitchStepKind.ENV_FILES, result.applied_steps)
        self.assert_nothing_forbidden_ran()

    def test_open_bash_exports_the_key_and_drops_the_previous_one_on_the_next_prompt(self):
        hook = self.paths.hook(ShellKind.BASH)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(hooks.render(ShellKind.BASH))
        self.assertEqual(self.cli("trocar", "1")[0], 0)
        script = """. "$1"
printf 'A:%s|%s\\n' "${OPENAI_API_KEY-unset}" "${GEMINI_API_KEY-unset}"
IFS= read -r _go
_multicred_apply   # what PROMPT_COMMAND runs before the next prompt
printf 'B:%s|%s\\n' "${OPENAI_API_KEY-unset}" "${GEMINI_API_KEY-unset}"
"""
        # PATH is $PREFIX/bin: only the fakes live there (a secret-tool call would leave a marker).
        process = subprocess.Popen([BASH, "--norc", "--noprofile", "-c", script, "bash", str(hook)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={"HOME": str(self.home), "PATH": str(self.prefix / "bin")})
        first = process.stdout.readline()
        code, out, err = self.cli("trocar", "Pessoal")
        rest, shell_err = process.communicate("\n", timeout=20)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(first + rest, "A:sk-openai $x|unset\nB:unset|g-gemini\n", shell_err)
        self.assertEqual(shell_err, "")
        self.assert_nothing_forbidden_ran()

    def test_missing_key_file_stops_the_switch_before_any_change(self):
        (self.home / ".config/multicred/secrets" / self.openai.secret_account).unlink()
        code, out, err = self.cli("trocar", "Trabalho")
        self.assertEqual(code, 1)
        self.assertIn("Não consegui trocar para Trabalho", err)
        self.assertIn("OPENAI_API_KEY", err)
        self.assertFalse(self.paths.gitconfig.exists())
        self.assertFalse(self.gh_log.exists() and "auth switch" in self.gh_log.read_text())

    def test_status_shows_the_profile_after_the_switch(self):
        self.cli("trocar", "Trabalho")
        code, out, err = self.cli("status")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^Trabalho · Ativo desde \d\d:\d\d$")
        self.assertIn("git dev@a.example", lines)
        self.assertIn("GitHub dev-a", lines)
        self.assertIn("Claude · ~/.claude · login da pasta", lines)
        self.assertIn("Chaves: OPENAI_API_KEY", lines)
        self.assert_nothing_forbidden_ran()


if __name__ == "__main__":
    import unittest
    unittest.main()
