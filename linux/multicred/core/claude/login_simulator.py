"""Test-mode stand-ins for `claude auth login` and `claude setup-token` (D30).

Port of LoginSimulator.swift and SetupTokenSimulator. They are `/bin/sh`
scripts that print what the real CLI printed (Phase 1 spike) and wait for a
code. Codes starting with "erro" or containing "expir" fail.

On Linux the login simulator writes what Claude Code itself writes after a
login: a fake `claudeAiOauth` in `<folder>/.credentials.json` (0600) and the
displayed `oauthAccount` in the folder's `.claude.json`, so the capture into the
vault runs exactly as with the real CLI. The folder is `$CLAUDE_CONFIG_DIR`
(`$HOME/.claude` when unset). It refuses, with exit code 3, any folder that is
not inside `$HOME`, which in test mode is the sandbox: paths with `..` or `.`
components and folders whose real location (symlinks resolved) is outside.
"""

from __future__ import annotations

SIMULATED_ACCESS_TOKEN = "sk-ant-oat01-SIMULADO-modo-de-teste"


class LoginSimulator:
    EXECUTABLE = "/bin/sh"
    SIMULATED_EMAIL = "simulado@example.com"
    REFUSED_EXIT_CODE = 3

    SCRIPT = r"""
printf '[MODO DE TESTE: simulação do claude auth login]\r\n'
printf 'Opening browser to sign in...\r\n'
sleep 1
printf "If the browser didn't open, visit: https://example.invalid/oauth/authorize?simulado=1\r\n"
printf 'Paste code here if prompted > '
IFS= read -r code
sleep 1
case "$code" in
  erro*|*expir*) printf '\r\nError: Invalid code or expired\r\n'; exit 1 ;;
esac
refuse() { printf 'Refusing to write outside the test home\r\n'; exit 3; }
case "$HOME" in /?*) ;; *) refuse ;; esac
dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if [ -n "$CLAUDE_CONFIG_DIR" ]; then state="$CLAUDE_CONFIG_DIR/.claude.json"; else state="$HOME/.claude.json"; fi
case "$dir" in
  */../*|*/..|*/./*|*/.) refuse ;;
  "$HOME"/*) ;;
  *) refuse ;;
esac
home_real=$(cd "$HOME" && pwd -P) || refuse
if [ -d "$dir" ]; then check="$dir"; else check=$(dirname "$dir"); fi
check_real=$(cd "$check" && pwd -P) || refuse
case "$check_real" in "$home_real"|"$home_real"/*) ;; *) refuse ;; esac
umask 077
[ -d "$dir" ] || mkdir "$dir" || refuse
printf '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-SIMULADO-modo-de-teste","refreshToken":"sk-ant-ort01-SIMULADO-modo-de-teste","expiresAt":4102444800000,"scopes":["user:inference","user:profile"],"subscriptionType":"pro"}}' > "$dir/.credentials.json"
printf '{"oauthAccount":{"emailAddress":"simulado@example.com"}}' > "$state"
printf 'Login successful.\r\n'
"""

    @classmethod
    def arguments(cls) -> list[str]:
        return ["-c", cls.SCRIPT]


class SetupTokenSimulator:
    EXECUTABLE = "/bin/sh"
    FAKE_TOKEN = "sk-ant-oat01-SIMULADO-modo-de-teste-0000000000"

    SCRIPT = r"""
printf '[MODO DE TESTE: simulação do claude setup-token]\r\n'
printf 'Opening browser to sign in...\r\n'
sleep 1
printf 'Paste code here if prompted > '
IFS= read -r code
case "$code" in
  erro*|*expir*) printf '\r\nOAuth error: Request failed with status code 400\r\n'; exit 1 ;;
esac
printf '\r\nLogin successful.\r\n'
printf 'Your OAuth token (valid for 1 year):\r\n\r\nsk-ant-oat01-SIMULADO-modo-de-teste-0000000000\r\n'
"""

    @classmethod
    def arguments(cls) -> list[str]:
        return ["-c", cls.SCRIPT]
