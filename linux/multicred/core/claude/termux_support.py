"""Claude Code on Termux (Android): does it run, and what to do when it does not (R-12).

Facts this relies on (anthropics/claude-code issue #50270; there is no device in the
test suite):
- Up to 2.1.112 the npm package `@anthropic-ai/claude-code` is JavaScript (cli.js)
  and runs on Termux's node.
- From 2.1.113 on, the package is a stub that looks for a platform-native binary.
  Node reports the platform as `android`, which has none, so every run, `--version`
  included, prints "Error: claude native binary not installed." and exits.
- What works: pinning 2.1.112 with the auto-updater off (DISABLE_AUTOUPDATER=1, in
  the shell or in the `env` of the folder's settings.json, plus the package folder
  made read-only so an update cannot replace it), or running Claude Code inside a
  proot-distro Ubuntu (slower; the MultiCred of Termux does not reach in there).

Fixing Claude Code on Android is out of scope: `diagnose` only reads (`claude
--version`, the package's package.json, the folder's settings) and the guidance is
text with the commands for the user to run.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .. import claude_paths
from ..runner import CommandRequest
from ..tools import Tool
from .attribution import settings_file

NPM_PACKAGE = "@anthropic-ai/claude-code"
LAST_WORKING_VERSION = "2.1.112"
FIRST_BROKEN_VERSION = (2, 1, 113)
ISSUE = "issue #50270"
STUB_MARKER = "native binary not installed"

PIN_COMMAND = f"DISABLE_AUTOUPDATER=1 npm install -g {NPM_PACKAGE}@{LAST_WORKING_VERSION}"
LOCK_COMMAND = f"chmod -R a-w $PREFIX/lib/node_modules/{NPM_PACKAGE}"
SETTINGS_ENV = '"env": {"DISABLE_AUTOUPDATER": "1"}'
SHELL_EXPORT = "export DISABLE_AUTOUPDATER=1"
PROOT_COMMAND = "pkg install proot-distro && proot-distro install ubuntu && proot-distro login ubuntu"

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_FALSE = {"", "0", "false", "no", "off"}


class ClaudeCodeState(str, Enum):
    MISSING = "missing"
    # Runs (version up to 2.1.112).
    WORKING = "working"
    # The npm stub without its native binary: does not run on Android.
    BROKEN_STUB = "brokenStub"
    # Answered with a version from 2.1.113 on, which plain Termux cannot run.
    BROKEN_VERSION = "brokenVersion"
    # `claude --version` failed for another reason.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClaudeCodeDiagnosis:
    state: ClaudeCodeState
    path: str | None = None
    # "2.1.112"; from `--version`, or from the package's package.json when it cannot answer.
    version: str | None = None
    # What `claude --version` printed when it failed.
    output: str | None = None
    # Whether the folder in use turns the auto-updater off (None when not looked at).
    autoupdater_disabled: bool | None = None
    # The settings.json of the folder in use, for display (~/.claude/settings.json).
    settings_display: str = "~/.claude/settings.json"

    @property
    def works(self) -> bool:
        return self.state == ClaudeCodeState.WORKING

    @property
    def is_broken(self) -> bool:
        return self.state in (ClaudeCodeState.BROKEN_STUB, ClaudeCodeState.BROKEN_VERSION)

    @property
    def needs_attention(self) -> bool:
        """Broken, unreadable, or working with an updater that will install a broken version."""
        if self.state == ClaudeCodeState.WORKING:
            return self.autoupdater_disabled is False
        return self.state != ClaudeCodeState.MISSING


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def is_broken_on_android(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= FIRST_BROKEN_VERSION


def installed_package_version(path: str) -> str | None:
    """Version in the package.json of the npm package `path` (the `claude` link) belongs to."""
    current = Path(os.path.realpath(path)).parent
    for directory in (current, *current.parents):
        manifest = directory / "package.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == NPM_PACKAGE:
            version = data.get("version")
            return version if isinstance(version, str) else None
    return None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def autoupdater_disabled(config_dir: str | None, environment) -> bool:
    """DISABLE_AUTOUPDATER in the `env` of the folder's settings.json, or `autoUpdates: false`
    in its `.claude.json` (the global config). The shell's own exports cannot be seen here."""
    env = _read_json(settings_file(config_dir, environment)).get("env")
    if isinstance(env, dict) and str(env.get("DISABLE_AUTOUPDATER", "")).strip().lower() not in _FALSE:
        return True
    return _read_json(claude_paths.state_file(config_dir, environment)).get("autoUpdates") is False


def diagnose(environment, tools, config_dir: str | None = None) -> ClaudeCodeDiagnosis:
    """Read-only: is Claude Code installed, does it run, will it keep running.
    `config_dir` is the Claude folder in use (None = ~/.claude)."""
    settings_display = environment.abbreviate(str(settings_file(config_dir, environment)))
    path = tools.locate(Tool.CLAUDE)
    if path is None:
        return ClaudeCodeDiagnosis(ClaudeCodeState.MISSING, settings_display=settings_display)
    request = CommandRequest(path, ["--version"],
                             environment.base_process_environment(tools.search_directories()), timeout=10)
    try:
        result = environment.runner.run(request)
        succeeded, output = result.succeeded, (result.stdout + result.stderr).strip()
    except Exception as error:  # could not start, timed out: report it, never crash Verificar
        succeeded, output = False, str(error)
    first_line = output.splitlines()[0] if output else ""
    if succeeded and parse_version(first_line):
        version = ".".join(str(part) for part in parse_version(first_line))
        if is_broken_on_android(version):
            return ClaudeCodeDiagnosis(ClaudeCodeState.BROKEN_VERSION, path, version,
                                       settings_display=settings_display)
        return ClaudeCodeDiagnosis(ClaudeCodeState.WORKING, path, version,
                                   autoupdater_disabled=autoupdater_disabled(config_dir, environment),
                                   settings_display=settings_display)
    version = installed_package_version(path)
    state = ClaudeCodeState.BROKEN_STUB if STUB_MARKER in output else ClaudeCodeState.UNKNOWN
    return ClaudeCodeDiagnosis(state, path, version, output=output or None, settings_display=settings_display)


def summary(diagnosis: ClaudeCodeDiagnosis) -> str:
    """One line: what was found."""
    named = f"Claude Code {diagnosis.version}" if diagnosis.version else "Claude Code"
    state = diagnosis.state
    if state == ClaudeCodeState.MISSING:
        return "Claude Code não está instalado"
    if state == ClaudeCodeState.WORKING:
        updater = "desligada" if diagnosis.autoupdater_disabled else "ligada"
        return f"{named} · atualização automática {updater}"
    if state == ClaudeCodeState.BROKEN_STUB:
        return f"{named} não roda no Android: binário nativo ausente (“{STUB_MARKER}”)"
    if state == ClaudeCodeState.BROKEN_VERSION:
        return f"{named}: a partir da 2.1.113 o Claude Code não roda no Android"
    first = (diagnosis.output or "").splitlines()
    return f"{named}: não consegui ler a versão" + (f" ({first[0]})" if first else "")


def _autoupdater_lines(diagnosis: ClaudeCodeDiagnosis) -> list[str]:
    return [f"Desligue a atualização automática: {SETTINGS_ENV} no {diagnosis.settings_display} "
            f"(ou {SHELL_EXPORT} no ~/.bashrc), e proteja a pasta do pacote: {LOCK_COMMAND}"]


def _proot_line() -> str:
    return (f"Ou use o Claude Code num Ubuntu dentro do Termux: {PROOT_COMMAND} (mais lento, e a troca de pasta "
            "e conta do MultiCred não vale lá dentro).")


def guidance_lines(diagnosis: ClaudeCodeDiagnosis) -> list[str]:
    """What to do, with the commands; empty when nothing needs doing."""
    state = diagnosis.state
    if state == ClaudeCodeState.WORKING:
        if diagnosis.autoupdater_disabled is not False:
            return []
        return [f"A próxima atualização instala uma versão que não roda no Android ({ISSUE} da Anthropic).",
                *_autoupdater_lines(diagnosis)]
    if state == ClaudeCodeState.MISSING:
        intro = f"Para instalar a {LAST_WORKING_VERSION}, a última versão que roda no Android:"
        commands = ["pkg install nodejs-lts", PIN_COMMAND]
    elif state == ClaudeCodeState.UNKNOWN:
        intro = (f"Se o Claude Code não abrir, instale a {LAST_WORKING_VERSION}, a última versão que roda no "
                 "Android:")
        commands = [PIN_COMMAND]
    else:
        intro = (f"A partir da 2.1.113 o Claude Code não roda no Termux ({ISSUE} da Anthropic). Instale a "
                 f"{LAST_WORKING_VERSION}, a última que roda:")
        commands = [PIN_COMMAND]
    return [intro, *commands, *_autoupdater_lines(diagnosis), _proot_line()]
