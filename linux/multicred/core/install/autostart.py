""""Abrir ao iniciar a sessão": an XDG autostart entry (the Linux counterpart of SMAppService).

GNOME, COSMIC and every XDG desktop start the `.desktop` files in
`$XDG_CONFIG_HOME/autostart` (`~/.config/autostart`) at login. MultiCred starts
there with `--background`: tray only, and the active profile is re-applied
because systemd's user environment does not survive a reboot.

Like D36 on the Mac (login item only from /Applications), the entry is offered
only when the app runs from the installed package: an entry written by a dev
checkout would point at a `multicred` command that does not exist.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from ..storage import AtomicFileWriter, restore_file

AUTOSTART_NAME = "app.multicred.MultiCred.desktop"
INSTALLED_COMMAND = "/usr/bin/multicred"

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=MultiCred
Comment=Troca de identidade de dev com um clique
Exec=multicred --background
Icon=app.multicred.MultiCred
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""


class AutostartUnavailable(Exception):
    """The app does not run from the installed package (no /usr/bin/multicred)."""

    def __init__(self, command_path: str):
        super().__init__(f"{command_path} not found: autostart needs the installed package")
        self.command_path = command_path


class Autostart:
    def __init__(self, environment, command_path: str = INSTALLED_COMMAND):
        self.environment = environment
        self.command_path = command_path

    @property
    def file(self) -> Path:
        return self.environment.config_home / "autostart" / AUTOSTART_NAME

    def is_available(self) -> bool:
        return os.path.isfile(self.command_path) and os.access(self.command_path, os.X_OK)

    def is_enabled(self) -> bool:
        """The file exists and the desktop has not switched it off (GNOME's
        "Startup Applications" writes `X-GNOME-Autostart-enabled=false` or `Hidden=true`)."""
        try:
            text = self.file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(text)
            entry = parser["Desktop Entry"]
        except (configparser.Error, KeyError):
            return False
        return (entry.get("X-GNOME-Autostart-enabled", "true").strip().lower() != "false"
                and entry.get("Hidden", "false").strip().lower() != "true")

    def enable(self) -> None:
        if not self.is_available():
            raise AutostartUnavailable(self.command_path)
        AtomicFileWriter.write(DESKTOP_ENTRY.encode(), self.file, permissions=0o644, keep_mode=False)

    def disable(self) -> None:
        """Works even when the package is gone, so uninstalling always cleans up."""
        restore_file(None, self.file)
