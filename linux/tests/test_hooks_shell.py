"""Shell hooks: terminals already open follow a switch on their next prompt."""

import shutil
import subprocess

from multicred.core.engine.switch_engine import SwitchEngine
from multicred.core.models import APIKeyAccount, APIProvider, Config, Profile, ProfileColor, ShellKind
from multicred.core.paths import AppPaths
from multicred.core.rendering import hooks
from tests.support import SafeTestCase
from tests.test_engine import FakeSystem, FakeTools


class ShellHookTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.paths = AppPaths(self.environment)

    def run_shell(self, argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=20,
                              env={"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})

    def prepare(self, shell, claude_dir, generation):
        for path, text in ((self.paths.hook(shell), hooks.render(shell)),
                           (self.paths.env_sh, f"export CLAUDE_CONFIG_DIR='{claude_dir}'\n"),
                           (self.paths.generation, f"{generation}\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_bash_hook_with_string_prompt_command(self):
        self.prepare(ShellKind.BASH, "/a", 1)
        script = """PROMPT_COMMAND='history -a'
. "$1"
. "$1"
printf 'A=%s\\n' "${CLAUDE_CONFIG_DIR-unset}"
printf 'PC=%s\\n' "$PROMPT_COMMAND"
printf "export CLAUDE_CONFIG_DIR='/b'\\n" > "$2/env.sh"
_multicred_apply; printf 'same-generation=%s\\n' "$CLAUDE_CONFIG_DIR"
printf '2' > "$2/generation"
_multicred_apply
printf 'B=%s\\n' "$CLAUDE_CONFIG_DIR"
"""
        result = self.run_shell(["/bin/bash", "--norc", "--noprofile", "-c", script, "bash",
                                 str(self.paths.hook(ShellKind.BASH)), str(self.paths.root)])
        self.assertEqual(result.stdout, "A=/a\nPC=_multicred_apply;history -a\nsame-generation=/a\nB=/b\n", result.stderr)

    def test_bash_hook_with_array_prompt_command(self):
        # bash 5.1+ (Ubuntu 24.04 ships 5.2) accepts PROMPT_COMMAND as an array.
        self.prepare(ShellKind.BASH, "/a", 1)
        script = "PROMPT_COMMAND=('history -a')\n. \"$1\"; . \"$1\"\nprintf '%s\\n' \"${PROMPT_COMMAND[@]}\""
        result = self.run_shell(["/bin/bash", "--norc", "--noprofile", "-c", script, "bash",
                                 str(self.paths.hook(ShellKind.BASH))])
        self.assertEqual(result.stdout, "_multicred_apply\nhistory -a\n", result.stderr)

    def test_bash_hook_is_silent_without_files(self):
        hook = self.paths.hook(ShellKind.BASH)
        hook.parent.mkdir(parents=True)
        hook.write_text(hooks.render(ShellKind.BASH))
        result = self.run_shell(["/bin/bash", "--norc", "--noprofile", "-c", '. "$1"; _multicred_apply; echo ok', "bash",
                                 str(hook)])
        self.assertEqual((result.stdout, result.stderr), ("ok\n", ""))

    def test_zsh_hook_follows_generation_changes_when_installed(self):
        zsh = shutil.which("zsh")
        if zsh is None:
            self.skipTest("zsh is not installed")
        self.prepare(ShellKind.ZSH, "/a", 1)
        script = """source "$1"
print -r -- "A=${CLAUDE_CONFIG_DIR-unset}"
print -r -- "export CLAUDE_CONFIG_DIR='/b'" > "$2/env.sh"
print 2 > "$2/generation"
_multicred_apply; print -r -- "B=$CLAUDE_CONFIG_DIR"
source "$1"
print -r -- "registered=${#${(@M)precmd_functions:#_multicred_apply}}"
"""
        result = self.run_shell([zsh, "-f", "-c", script, "zsh", str(self.paths.hook(ShellKind.ZSH)), str(self.paths.root)])
        self.assertEqual(result.stdout, "A=/a\nB=/b\nregistered=1\n", result.stderr)

    def test_hooks_use_home_relative_paths_only(self):
        for shell in ShellKind:
            text = hooks.render(shell)
            self.assertIn("$HOME/.config/multicred", text, shell)
            self.assertNotIn("/home/", text, shell)


class OpenTerminalFollowsSwitchTests(SafeTestCase):
    """R-07: a bash that is already open exports the new profile's key and drops the old one."""

    def test_open_bash_follows_a_profile_switch_on_its_next_prompt(self):
        system = FakeSystem(self.runner)
        openai = APIKeyAccount(nickname="O", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
        gemini = APIKeyAccount(nickname="G", provider=APIProvider.GEMINI, color=ProfileColor.ORANGE)
        self.secrets.set_secret(b"sk-openai", openai.secret_account)
        self.secrets.set_secret(b"g-gemini", gemini.secret_account)
        a = Profile(name="A", color=ProfileColor.INDIGO, api_key_ids=[openai.id])
        b = Profile(name="B", color=ProfileColor.ORANGE, api_key_ids=[gemini.id])
        config = Config(api_keys=[openai, gemini], profiles=[a, b])

        # A secret-tool that answers from the in-memory store, like the real one would.
        lines = [f'[ "$5" = {acc} ] && printf %s {self.secrets.stored_value(acc).decode()} && exit 0'
                 for acc in self.secrets.all_accounts()]
        tool = self.make_executable("bin/secret-tool", "#!/bin/sh\n" + "\n".join(lines) + "\nexit 1\n")
        engine = SwitchEngine(self.environment, FakeTools(), secret_tool=str(tool))
        paths = AppPaths(self.environment)
        hook = paths.hook(ShellKind.BASH)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(hooks.render(ShellKind.BASH))

        engine.switch_profile(a.id, config)
        # One bash process: prompt, switch (done by a helper between prompts), prompt.
        switch_b = self.home / "switch-to-b"
        script = f""". "$1"
printf 'A:%s|%s\\n' "${{OPENAI_API_KEY-unset}}" "${{GEMINI_API_KEY-unset}}"
while [ ! -e "{switch_b}.done" ]; do sleep 0.05; done
_multicred_apply   # what PROMPT_COMMAND runs before the next prompt
printf 'B:%s|%s\\n' "${{OPENAI_API_KEY-unset}}" "${{GEMINI_API_KEY-unset}}"
"""
        process = subprocess.Popen(["/bin/bash", "--norc", "--noprofile", "-c", script, "bash", str(hook)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        first = process.stdout.readline()
        engine.switch_profile(b.id, config)
        (self.home / "switch-to-b.done").touch()
        rest, err = process.communicate(timeout=20)
        self.assertEqual(first + rest, "A:sk-openai|unset\nB:unset|g-gemini\n", err)
        self.assertEqual(system.gh_active, "dev-empresa", "profiles without GitHub do not touch gh")
