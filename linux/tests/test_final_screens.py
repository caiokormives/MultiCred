"""S-14: the final screenshots of the main window, one per page, for the comparison with the Mac.

Renders (stock light Adwaita, the window's own renderer, never a screen grab) Visão
geral, Git, GitHub, Claude, Chaves de API, Verificar, Ajustes and a profile editor into
MULTICRED_SCREENS_DIR/final-*.png, from a crafted test-mode sandbox (three profiles,
two Claude folders, logins, keys, the shell integration installed). Nothing real runs:
dry-run runner, no tools found, gh and Claude statuses crafted.

Runs only under tests/run-ui-tests.sh with MULTICRED_SCREENS_DIR set.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests import test_sidebar as sidebar

PAGES = (("visao-geral", "overview"), ("git", "git"), ("github", "github"), ("claude", "claude"),
         ("chaves", "keys"), ("verificar", "verify"), ("ajustes", "settings"), ("editor-perfil", "profile"))


@unittest.skipUnless(sidebar.HAS_DISPLAY and os.environ.get("MULTICRED_SCREENS_DIR"),
                     "needs tests/run-ui-tests.sh (private display and bus) and MULTICRED_SCREENS_DIR")
class FinalScreensTests(unittest.TestCase):
    make_model = sidebar.SidebarTests.make_model
    run_steps = sidebar.SidebarTests.run_steps
    mark_active = staticmethod(sidebar.SidebarTests.mark_active)

    def setUp(self):
        from multicred.core import real_env

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-final-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.environment = real_env.make_sandbox(self.sandbox)
        (self.sandbox / ".claude-empresa").mkdir(mode=0o700)

    def test_every_page_of_the_window(self):
        from gi.repository import Adw
        from multicred.core.claude.folder_inspector import ClaudeFolderSummary
        from multicred.core.claude.status import ClaudeLoginStatus
        from multicred.core.install.installer import Installer, InstallOptions
        from multicred.core.models import ClaudeFolder, ShellKind
        from tests.test_accounts_pages import logins

        directory = Path(os.environ["MULTICRED_SCREENS_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        config = sidebar.crafted_config(self.environment)
        for key in config.api_keys:
            self.environment.secrets.set_secret(b"sk-seed", key.secret_account)
        self.environment.secrets.set_secret(b"sk-ant-oat01-seed", config.claude_accounts[1].secret_account)
        (self.sandbox / ".bashrc").write_text("alias ll='ls -l'\n")
        Installer(self.environment).apply(InstallOptions(shells=[ShellKind.BASH]))
        model = self.make_model(config)
        model.refresh_github_logins = lambda on_done: None
        model.github_logins = logins(("dev-empresa", True, True), ("dev-pessoal", False, True))
        company = str(self.sandbox / ".claude-empresa")
        model.claude_statuses[ClaudeFolder.own_login(None).id] = ClaudeLoginStatus.logged_in("eu@pessoal.example")
        model.claude_statuses[ClaudeFolder.own_login(company).id] = ClaudeLoginStatus.logged_in("dev@empresa.example")
        now = datetime.now(timezone.utc)
        model.folder_summaries[None] = ClaudeFolderSummary(None, str(self.sandbox / ".claude"), "eu@pessoal.example",
                                                           None, 3, 12, now - timedelta(hours=2), [])
        model.folder_summaries[company] = ClaudeFolderSummary(company, company, "dev@empresa.example", "Empresa", 5,
                                                              41, now - timedelta(minutes=30), [])
        tries = {}

        def setup(app):
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            app.window.set_default_size(980, 660)
            app.window.dismiss_toast()

        def wait(ticks):
            left = [ticks]

            def step(_app):
                left[0] -= 1
                return left[0] > 0
            return step

        def select(key):
            def step(app):
                row_key = f"profile:{config.profiles[0].id}" if key == "profile" else key
                app.window.sidebar.select_row(app.window.sidebar_row(row_key))
            return step

        def shot(name):
            def step(app):
                tries[name] = tries.get(name, 0) + 1
                if sidebar.save_png(app.window, directory / f"final-{name}.png"):
                    return False
                if tries[name] > 20:
                    raise AssertionError(f"nothing rendered for {name}")
                return True
            return step

        steps = [self.mark_active(model, config.profiles[0].id), setup, wait(20)]
        for name, key in PAGES:
            # Verificar runs the (dry-run) verifier when it opens: give it time to answer.
            steps += [select(key), wait(60 if key == "verify" else 20), shot(name)]
        self.run_steps(model, steps, seconds=60)
        for name, _key in PAGES:
            self.assertTrue((directory / f"final-{name}.png").exists(), name)


if __name__ == "__main__":
    unittest.main()
