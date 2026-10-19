"""Which kind of machine MultiCred runs on: a Linux desktop or Termux on Android.

Carried by SystemEnvironment (desktop by default), so the core asks the platform
for tool locations, the temporary folder, whether a graphical session exists and
where secrets live, instead of assuming a desktop. `detect` is pure: it gets the
process environment as a mapping from real_env.py, the only module allowed to
read it.

Termux facts this relies on (researched; there is no device in the test suite):
- Termux is recognised by `TERMUX_VERSION` (set by the Termux app), by
  `TERMUX__PREFIX` (newer termux-exec / app builds), or by a `PREFIX` under
  `/com.termux/` (the standard `PREFIX=/data/data/com.termux/files/usr`).
- Every tool lives in `$PREFIX/bin`; there is no /usr/bin or /bin/sh.
- /tmp is not writable; TMPDIR (normally `$PREFIX/tmp`) is.
- No systemd, no D-Bus session, no Secret Service, no graphical session.
- Child processes need `PREFIX`, `TMPDIR` and `LD_PRELOAD` (termux-exec, which
  rewrites `#!/usr/bin/env` style shebangs) from the session to behave.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

DESKTOP = "desktop"
TERMUX = "termux"

# The standard Termux prefix (app id com.termux).
TERMUX_DEFAULT_PREFIX = "/data/data/com.termux/files/usr"
TERMUX_PATH_MARKER = "/com.termux/"

# Where a desktop app finds tools (core/tools.py explains the order). This module is
# the only one in the core that names system paths (tests/test_architecture.py).
DESKTOP_SEARCH_DIRECTORIES = (
    "/usr/local/bin",
    "~/.local/bin",
    "/usr/bin",
    "/bin",
    "/snap/bin",
    "/home/linuxbrew/.linuxbrew/bin",
)
# The system's own programs, for children that need nothing else (login simulators,
# systemctl).
DESKTOP_SYSTEM_PATH = ("/usr/bin", "/bin")
# PATH the login-shell probe starts from; the rc files add the rest.
DESKTOP_PROBE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class SecretBackend(str, Enum):
    # libsecret / GNOME Keyring; the shell hook reads values with `secret-tool`.
    SECRET_SERVICE = "secretService"
    # One 0600 file per secret in ~/.config/multicred/secrets (dir 0700), read by
    # the shell hook with builtins (Termux has no Secret Service).
    FILES = "files"


@dataclass(frozen=True)
class Platform:
    kind: str = DESKTOP
    # Termux only: $PREFIX, $TMPDIR and $LD_PRELOAD of the session.
    prefix: str | None = None
    tmpdir: str | None = None
    ld_preload: str | None = None

    @classmethod
    def desktop(cls) -> "Platform":
        return cls(DESKTOP)

    @classmethod
    def termux(cls, prefix: str = TERMUX_DEFAULT_PREFIX, tmpdir: str | None = None,
               ld_preload: str | None = None) -> "Platform":
        return cls(TERMUX, prefix=prefix.rstrip("/") or TERMUX_DEFAULT_PREFIX,
                   tmpdir=tmpdir if tmpdir and tmpdir.startswith("/") else None,
                   ld_preload=ld_preload or None)

    @property
    def is_termux(self) -> bool:
        return self.kind == TERMUX

    def _bin(self, name: str) -> str:
        return f"{self.prefix}/bin/{name}"

    @property
    def tool_search_directories(self) -> list[str]:
        """Where tools are looked for, in order (`~` is expanded against the injected home).
        On Termux, npm globals (Claude Code) also land in $PREFIX/bin; ~/.local/bin is
        where pip --user and Claude Code's own installer put things."""
        if self.is_termux:
            return [f"{self.prefix}/bin", "~/.local/bin"]
        return list(DESKTOP_SEARCH_DIRECTORIES)

    @property
    def system_path(self) -> list[str]:
        """PATH entries of the system's own programs (sh, sleep, systemctl…)."""
        return [f"{self.prefix}/bin"] if self.is_termux else list(DESKTOP_SYSTEM_PATH)

    @property
    def login_shell_probe_path(self) -> str:
        """PATH the login shell starts with when asked for the user's PATH."""
        return f"{self.prefix}/bin" if self.is_termux else DESKTOP_PROBE_PATH

    def system_tool(self, name: str) -> str:
        """Where a packaged tool would be installed: /usr/bin/<name>, or $PREFIX/bin/<name>."""
        return self._bin(name) if self.is_termux else f"/usr/bin/{name}"

    @property
    def sh(self) -> str:
        return self._bin("sh") if self.is_termux else "/bin/sh"

    @property
    def true(self) -> str:
        return self._bin("true") if self.is_termux else "/usr/bin/true"

    @property
    def setsid(self) -> str:
        # util-linux setsid(1); Termux ships it in the util-linux package.
        return self._bin("setsid") if self.is_termux else "/usr/bin/setsid"

    @property
    def temp_directory(self) -> str:
        if self.is_termux:
            return self.tmpdir or f"{self.prefix}/tmp"
        return "/tmp"

    @property
    def has_graphical_session(self) -> bool:
        """Whether the `session` switch step (systemd user manager + environment.d) exists."""
        return not self.is_termux

    @property
    def secret_backend(self) -> SecretBackend:
        return SecretBackend.FILES if self.is_termux else SecretBackend.SECRET_SERVICE

    def install_command(self, package: str) -> str:
        """How the user installs a package (shown, never run): apt on the desktop, pkg on Termux."""
        return f"pkg install {package}" if self.is_termux else f"sudo apt install {package}"

    def child_environment(self) -> dict[str, str]:
        """Variables the session sets that child processes need on this platform."""
        if not self.is_termux:
            return {}
        env = {"PREFIX": self.prefix, "TMPDIR": self.temp_directory}
        if self.ld_preload:
            env["LD_PRELOAD"] = self.ld_preload
        return env


def detect(environ: Mapping[str, str]) -> Platform:
    """Termux when the environment says so (see the module docstring), else desktop."""
    prefix = environ.get("PREFIX") or ""
    termux_prefix = environ.get("TERMUX__PREFIX") or ""
    is_termux = bool(environ.get("TERMUX_VERSION")) or bool(termux_prefix) or TERMUX_PATH_MARKER in prefix
    if not is_termux:
        return Platform.desktop()
    chosen = next((p for p in (prefix, termux_prefix) if p.startswith("/")), TERMUX_DEFAULT_PREFIX)
    return Platform.termux(chosen, tmpdir=environ.get("TMPDIR"), ld_preload=environ.get("LD_PRELOAD"))


@dataclass(frozen=True)
class SecretStoreWording:
    """How the messages name the place secrets live. The desktop keeps them in the
    Secret Service ("chaveiro do sistema", the Mac's Keychain); Termux has none, so
    they are 0600 files in ~/.config/multicred/secrets and the messages say so.
    Here (not in the UI) because Verificar's texts use it too; ui/messages.py re-exports it."""

    # "A chave X não está {located}."
    located: str = "no chaveiro do sistema"
    # "Não consegui consultar {store}: …"
    store: str = "o chaveiro do sistema"
    # Verificar, when the store cannot be read: "{unavailable}: <erro>".
    unavailable: str = "chaveiro indisponível"
    # …and what to do about it.
    unavailable_hint: str = "Desbloqueie o chaveiro e verifique de novo."


SYSTEM_KEYRING = SecretStoreWording()


def secret_store_wording(platform=None, folder: str = "~/.config/multicred/secrets") -> SecretStoreWording:
    """The wording for `platform`; the desktop's when None."""
    if platform is not None and platform.secret_backend == SecretBackend.FILES:
        return SecretStoreWording(located=f"em {folder}", store=f"a pasta {folder}",
                                  unavailable="pasta de segredos indisponível",
                                  unavailable_hint=f"Confira as permissões de {folder} (pasta 0700, arquivos 0600) "
                                                   "e verifique de novo.")
    return SYSTEM_KEYRING
