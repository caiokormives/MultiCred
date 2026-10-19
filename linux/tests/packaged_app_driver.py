"""Test helper for S-10 (not a test module): runs the MultiCred carried by an extracted .deb.

usage: python3 tests/packaged_app_driver.py --lib <root>/usr/lib/multicred --sandbox DIR --screens DIR

Imports `multicred` from `--lib` only (this checkout is kept off sys.path), builds the
app exactly as `multicred --test-mode --sandbox DIR` does, seeds the sandbox with the
demo accounts of the approved designs, activates the first profile, opens Perfis,
Contas (Claude tab), Verificar and Ajustes and renders each into `--screens` as
S-10-<page>.png (light, like the approved designs), a grab of the whole private X
screen (S-10-sem-bandeja.png: the window with the "no tray area" banner, as Xvfb
shows it) and Perfis in dark (S-10-perfis-escuro.png). Prints one JSON line
with what it saw. Refuses to run outside tests/run-ui-tests.sh.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGES = (("perfis", "main"), ("contas", "accounts"), ("verificar", "verify"), ("ajustes", "settings"))


def parse():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lib", type=Path, required=True)
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--screens", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=60)
    return parser.parse_args()


def seeded_config(environment):
    """The accounts of designs A and E (three profiles, two of each account kind)."""
    from multicred.core.models import (APIKeyAccount, APIProvider, ClaudeAccount, ClaudeAccountKind, Config,
                                       GitHubAccount, GitIdentity, Profile, ProfileColor)
    work = GitIdentity(nickname="Trabalho", name="Dev Trabalho", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
    personal = GitIdentity(nickname="Pessoal", name="Dev Pessoal", email="dev@pessoal.dev", color=ProfileColor.ORANGE)
    gh_work = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    gh_personal = GitHubAccount(nickname="Pessoal", username="dev-pessoal", color=ProfileColor.ORANGE)
    empresa = ClaudeAccount(nickname="Empresa", email="dev@empresa.com.br", color=ProfileColor.INDIGO,
                            kind=ClaudeAccountKind.LOGIN)
    cliente = ClaudeAccount(nickname="Cliente X", color=ProfileColor.TEAL, kind=ClaudeAccountKind.TOKEN)
    openai = APIKeyAccount(nickname="Empresa", provider=APIProvider.OPENAI, color=ProfileColor.INDIGO)
    gemini = APIKeyAccount(nickname="Empresa", provider=APIProvider.GEMINI, color=ProfileColor.INDIGO)
    folder = str(environment.home / ".claude-empresa")
    return Config(
        git_identities=[work, personal], github_accounts=[gh_work, gh_personal],
        claude_accounts=[empresa, cliente], api_keys=[openai, gemini],
        profiles=[
            Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work.id, github_account_id=gh_work.id,
                    claude_config_dir=folder, api_key_ids=[openai.id, gemini.id]),
            Profile(name="Pessoal", color=ProfileColor.ORANGE, git_identity_id=personal.id,
                    github_account_id=gh_personal.id),
            Profile(name="Cliente X", color=ProfileColor.TEAL, git_identity_id=work.id, claude_config_dir=folder,
                    claude_account_id=cliente.id),
        ])


def main() -> int:
    args = parse()
    if os.environ.get("MULTICRED_PRIVATE_BUS") != "1":
        print("packaged_app_driver: only under tests/run-ui-tests.sh", file=sys.stderr)
        return 2
    lib = args.lib.resolve()
    # Only the extracted package may provide `multicred`.
    sys.path[:] = [str(lib)] + [p for p in sys.path if p and Path(p).resolve() not in (HERE, HERE.parent)]

    import multicred
    from multicred import __main__ as cli
    from multicred.core import real_env
    from multicred.core.storage import ConfigRepository

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, GLib, Graphene, Gtk

    sandbox = args.sandbox
    environment = real_env.make_sandbox(sandbox)
    (environment.home / ".claude-empresa").mkdir(mode=0o700, exist_ok=True)
    config = seeded_config(environment)
    ConfigRepository(environment).save(config)

    model = cli.build_model(cli.parse_args(["--test-mode", "--sandbox", str(sandbox)]))
    for key in config.api_keys:  # the in-memory store of this process
        model.environment.secrets.set_secret(b"sk-exemplo", key.secret_account)
    model.dispatch = GLib.idle_add

    from multicred.ui.app import MultiCredApplication
    app = MultiCredApplication(model)
    args.screens.mkdir(parents=True, exist_ok=True)
    report = {"module": multicred.__file__, "test_mode": model.is_test_mode,
              "sandbox_home": str(model.environment.home), "titles": {}, "shots": [], "errors": []}

    def render(window, name) -> bool:
        width, height = window.get_width(), window.get_height()
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            return False
        rect = Graphene.Rect()
        rect.init(0, 0, width, height)
        path = args.screens / f"S-10-{name}.png"
        window.get_renderer().render_texture(node, rect).save_to_png(str(path))
        report["shots"].append(str(path))
        return True

    def grab_screen(name) -> None:
        """The private X screen as Xvfb shows it (never the user's: DISPLAY is the runner's)."""
        path = args.screens / f"S-10-{name}.png"
        if shutil.which("import"):
            subprocess.run(["import", "-window", "root", str(path)], check=True, timeout=30)
            report["shots"].append(str(path))

    def wait_ticks(count):
        left = [count]

        def step():
            left[0] -= 1
            return left[0] > 0
        return step

    steps = []

    def window_ready():
        window = app.window
        if window is None or not window.get_mapped() or app.tray_available is None:
            return True
        window.dismiss_toast()
        report["mapped"] = window.get_mapped()
        report["prgname"] = GLib.get_prgname()  # X11 WM_CLASS; the .desktop's StartupWMClass
        report["sidebar"] = window.sidebar_titles()
        report["tray_banner"] = window.tray_banner.get_revealed()
        # Light, like the approved designs (A, E, F, G), so the screenshots compare 1:1;
        # a dark Perfis is rendered at the end.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        window.activate_profile_number(1)
        return False

    def switched():
        if model.switching:
            return True
        report["active"] = model.active_profile.name if model.active_profile else None
        return False

    steps += [window_ready, wait_ticks(5), switched, wait_ticks(30)]

    def screen_grab():
        grab_screen("sem-bandeja")
        return False

    steps += [lambda: app.window.dismiss_toast(), wait_ticks(20), screen_grab]

    for name, page in PAGES:
        def opened(page=page, name=name):
            window = app.window
            window.dismiss_toast()
            window.open_page(page)
            report["titles"][name] = window.content_page.get_title()
            return False

        def settled(page=page):
            section = app.window._section_pages.get(page)
            return bool(getattr(section, "busy", False))

        def shot(name=name):
            app.window.dismiss_toast()
            return not render(app.window, name)

        steps += [opened, wait_ticks(15), settled, wait_ticks(15), shot]

    def dark():
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        app.window.open_page("main")
        return False

    steps += [dark, wait_ticks(15), lambda: not render(app.window, "perfis-escuro")]

    def tick():
        if not steps:
            app.quit()
            return False
        try:
            again = steps[0]()
        except Exception:  # reported after the loop ends
            import traceback
            report["errors"].append(traceback.format_exc())
            app.quit()
            return False
        if not again:
            steps.pop(0)
        return True

    GLib.timeout_add(60, tick)
    GLib.timeout_add_seconds(args.seconds, app.quit)  # never hang the suite
    status = app.run([])
    report["steps_left"] = len(steps)
    report["status"] = status
    print(json.dumps(report))
    return 0 if not report["errors"] and not steps else 1


if __name__ == "__main__":
    sys.exit(main())
