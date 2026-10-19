"""S-03: tray icon (StatusNotifierItem) with the profile menu (com.canonical.dbusmenu).
S-08 (R-08): the menu follows the Mac's (AppDelegate.swift:71-194, MenuHeaderView.swift).

The menu tree and the icon are pure and always run. The D-Bus tests need a private
bus with nothing else on it and run only under tests/run-ui-tests.sh
(MULTICRED_PRIVATE_BUS=1): they own a fake org.kde.StatusNotifierWatcher, start
the tray, and talk to it as a panel would. The app tests also need the private display.
"""

import os
import struct
import threading
import time
import unittest
import uuid
import zlib
from datetime import timedelta, timezone

from multicred.core.engine.types import RollbackOutcome, StepFailed, SwitchResult, SwitchStepKind
from multicred.core.models import (ActiveState, ClaudeAccount, ClaudeAccountKind, ClaudeFolder, Config,
                                   GitHubAccount, GitIdentity, Profile, ProfileColor, ProfileIcon)
from tests.support import FIXED_NOW, SafeTestCase

PRIVATE_BUS = os.environ.get("MULTICRED_PRIVATE_BUS") == "1"
HAS_DISPLAY = False
if PRIVATE_BUS:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk
        HAS_DISPLAY = Gtk.init_check()
    except (ImportError, ValueError):
        HAS_DISPLAY = False

BRT = timezone(timedelta(hours=-3))
WATCHER = "org.kde.StatusNotifierWatcher"
SNI = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"


WORK_FOLDER = "/home/dev/.claude-trabalho"


def sample_world():
    """Three profiles, two Claude accounts; Trabalho is active with its own account and folder."""
    work_git = GitIdentity(nickname="Trabalho", name="Dev", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
    work_gh = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    empresa = ClaudeAccount(nickname="Empresa", color=ProfileColor.INDIGO, email="dev@empresa.com.br",
                            kind=ClaudeAccountKind.LOGIN)
    pessoal_claude = ClaudeAccount(nickname="Pessoal", color=ProfileColor.ORANGE)
    work = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work_git.id,
                   github_account_id=work_gh.id, claude_account_id=empresa.id, claude_config_dir=WORK_FOLDER)
    personal = Profile(name="Pessoal", color=ProfileColor.ORANGE)
    client = Profile(name="Cliente X", color=ProfileColor.TEAL, icon=ProfileIcon("letter", "X"))
    config = Config(git_identities=[work_git], github_accounts=[work_gh], claude_accounts=[empresa, pessoal_claude],
                    profiles=[work, personal, client])
    state = ActiveState(profile_id=work.id, applied_claude_account_id=empresa.id, activated_at=FIXED_NOW,
                        generation=3)
    return config, state


def abbreviate(config_dir):
    return "~/" + config_dir.removeprefix("/home/dev/") if config_dir else "~/.claude"


def report(*statuses, checked_at=FIXED_NOW):
    from multicred.core.engine.verifier import Check, CheckStatus, VerifyReport
    titles = {CheckStatus.ERROR: "Identidade do git sobrescrita", CheckStatus.WARNING: "Claude Code · conta",
              CheckStatus.OK: "git", CheckStatus.INFO: "1 sessão do Claude Code aberta"}
    checks = [Check(f"c{n}", f"{titles[s]} {n}" if s == CheckStatus.ERROR else titles[s], s)
              for n, s in enumerate(map(CheckStatus, statuses), start=1)]
    return VerifyReport(checks=checks, checked_at=checked_at)


def logged_in(email=None):
    from multicred.core.claude.status import ClaudeLoginStatus
    return ClaudeLoginStatus.logged_in(email)


def logged_out():
    from multicred.core.claude.status import ClaudeLoginStatus
    return ClaudeLoginStatus.logged_out()


def visible(items):
    return [item for item in items if not item.separator]


def labels(items):
    return [item.label for item in visible(items)]


def find(items, label):
    return next(i for i in items if i.label == label)


def claude_submenu(items):
    return find(items, "Trocar só a conta Claude").children


class MenuModelTests(unittest.TestCase):
    """The menu of AppDelegate.swift:71-194 as dbusmenu lines (R-08)."""

    def build(self, config, state, **flags):
        from multicred.tray.menu_model import build_menu
        flags.setdefault("tz", BRT)
        flags.setdefault("folder_name", abbreviate)
        return build_menu(config, state, **flags)

    # MARK: header (MenuHeaderView.swift)

    def test_header_lines_have_icons_and_the_verify_status(self):
        config, state = sample_world()
        items = self.build(config, state, icon=lambda profile: b"png:" + profile.name.encode())
        since = FIXED_NOW.astimezone(BRT).strftime("%H:%M")
        self.assertEqual([i.label for i in items[:5]], [
            f"Trabalho · Ativo desde {since}",
            "Não verificado",
            "dev@empresa.com.br",
            "dev-empresa",
            "Claude · ~/.claude-trabalho · Empresa",
        ])
        self.assertEqual(items[0].icon_png, b"png:Trabalho")
        self.assertEqual([i.icon_name for i in items[1:5]], [
            "dialog-question-symbolic", "multicred-branch-symbolic", "multicred-code-symbolic",
            "multicred-sparkle-symbolic"])
        self.assertTrue(all(not i.enabled and i.action is None for i in items[:5]))
        self.assertTrue(items[5].separator)

    def test_status_line_follows_the_last_verification(self):
        config, state = sample_world()
        cases = [
            (dict(verification=report("ok", "warning", "info")), "Tudo certo", "emblem-ok-symbolic"),
            (dict(verification=report("ok", "error")), "Inconsistente", "dialog-warning-symbolic"),
            (dict(verifying=True), "Verificando", "emblem-synchronizing-symbolic"),
            (dict(), "Não verificado", "dialog-question-symbolic"),
            # The Linux half-switched state (failed rollback) is inconsistent too.
            (dict(inconsistent=True, verification=report("ok")), "Inconsistente", "dialog-warning-symbolic"),
        ]
        for flags, label, icon in cases:
            with self.subTest(label=label, flags=list(flags)):
                status = self.build(config, state, **flags)[1]
                self.assertEqual((status.label, status.icon_name, status.enabled), (label, icon, False))

    def test_header_credential_is_the_folder_login_with_its_email_and_override(self):
        config, state = sample_world()
        state.profile_id = config.profiles[1].id
        state.applied_claude_account_id = None
        state.claude_override_id = ClaudeFolder.OWN_LOGIN_OVERRIDE
        items = self.build(config, state, folder_status=lambda d: logged_in("eu@pessoal.com") if d is None else None)
        self.assertEqual(labels(items)[:3], ["Pessoal · Ativo desde " + FIXED_NOW.astimezone(BRT).strftime("%H:%M"),
                                             "Não verificado",
                                             "Claude · ~/.claude · login da pasta (eu@pessoal.com) (só Claude)"])
        plain = self.build(config, state, folder_status=lambda d: logged_in(None))
        self.assertEqual(plain[2].label, "Claude · ~/.claude · login da pasta (só Claude)")

    def test_no_active_profile_header(self):
        config, _ = sample_world()
        items = self.build(config, ActiveState())
        self.assertEqual([i.label for i in items[:2]], ["Nenhum perfil ativo", "Escolha um perfil abaixo."])
        self.assertFalse(items[0].enabled or items[1].enabled)
        self.assertTrue(items[2].separator)

    # MARK: problems (MenuHeaderView.swift:40-52, AppDelegate.swift:94-97)

    def test_first_problem_and_count_then_corrigir(self):
        config, state = sample_world()
        items = self.build(config, state, verification=report("ok", "error", "warning", "error", "error"))
        first_separator = next(n for n, i in enumerate(items) if i.separator)
        problem, more, fix = items[first_separator - 3:first_separator]
        self.assertEqual((problem.label, problem.icon_name, problem.enabled),
                         ("Identidade do git sobrescrita 2", "dialog-warning-symbolic", False))
        self.assertEqual((more.label, more.enabled), ("e mais 2", False))
        self.assertEqual((fix.label, fix.action, fix.icon_name, fix.enabled),
                         ("Corrigir…", ("open", "verify"), "dialog-warning-symbolic", True))

    def test_one_problem_has_no_count(self):
        config, state = sample_world()
        items = self.build(config, state, verification=report("error"))
        self.assertNotIn("e mais 0", labels(items))
        fix = items.index(find(items, "Corrigir…"))
        self.assertEqual(items[fix - 1].label, "Identidade do git sobrescrita 1")

    def test_no_corrigir_without_problems(self):
        config, state = sample_world()
        for flags in (dict(), dict(verification=report("ok", "warning", "info"))):
            with self.subTest(flags=list(flags)):
                self.assertNotIn("Corrigir…", labels(self.build(config, state, **flags)))

    def test_half_switched_state_offers_corrigir_without_a_problem_line(self):
        config, state = sample_world()
        items = self.build(config, state, inconsistent=True)
        fix = items.index(find(items, "Corrigir…"))
        self.assertEqual(items[fix - 1].label, "Claude · ~/.claude-trabalho · Empresa")

    def test_problems_show_without_an_active_profile(self):
        config, _ = sample_world()
        items = self.build(config, ActiveState(), verification=report("error"))
        self.assertEqual(labels(items)[:4], ["Nenhum perfil ativo", "Escolha um perfil abaixo.",
                                             "Identidade do git sobrescrita 1", "Corrigir…"])

    # MARK: profiles (AppDelegate.swift:100-120)

    def test_perfis_label_before_the_profiles(self):
        config, state = sample_world()
        items = self.build(config, state)
        label = find(items, "Perfis")
        self.assertFalse(label.enabled)
        self.assertTrue(items[items.index(label) - 1].separator)
        self.assertEqual(items[items.index(label) + 1].action, ("profile", config.profiles[0].id))

    def test_profiles_with_icon_check_and_ctrl_number(self):
        config, state = sample_world()
        items = self.build(config, state, icon=lambda profile: b"png:" + profile.name.encode())
        profiles = [i for i in items if i.action and i.action[0] == "profile"]
        self.assertEqual([p.label for p in profiles], ["Trabalho", "Pessoal", "Cliente X"])
        self.assertEqual([p.action[1] for p in profiles], [p.id for p in config.profiles])
        self.assertTrue(all(p.toggle == "checkmark" for p in profiles))
        self.assertEqual([p.toggled for p in profiles], [True, False, False])
        self.assertEqual(profiles[2].icon_png, b"png:Cliente X")
        self.assertEqual([p.shortcut for p in profiles], [("Control", "1"), ("Control", "2"), ("Control", "3")])
        self.assertTrue(all(p.enabled for p in profiles))

    def test_only_the_first_nine_profiles_have_a_shortcut(self):
        config, state = sample_world()
        config.profiles = [Profile(name=f"P{n}", color=ProfileColor.BLUE) for n in range(1, 12)]
        profiles = [i for i in self.build(config, state) if i.action and i.action[0] == "profile"]
        self.assertEqual([p.shortcut for p in profiles],
                         [("Control", str(n)) for n in range(1, 10)] + [None, None])

    def test_profile_that_needs_claude_login_says_so(self):
        config, state = sample_world()
        token_profile = Profile(name="Token", color=ProfileColor.BLUE, claude_account_id=config.claude_accounts[1].id)
        config.profiles.append(token_profile)
        items = self.build(config, state, folder_status=lambda d: logged_out() if d is None else logged_in(),
                           has_token=lambda account: account.nickname != "Pessoal")
        profiles = [i.label for i in items if i.action and i.action[0] == "profile"]
        self.assertEqual(profiles, ["Trabalho", "Pessoal  ⚠︎ Claude precisa entrar",
                                    "Cliente X  ⚠︎ Claude precisa entrar", "Token  ⚠︎ Claude precisa entrar"])
        # Unknown status is never "logged out".
        unknown = self.build(config, state, has_token=lambda account: True)
        self.assertNotIn("⚠︎", " ".join(labels(unknown)))

    def test_profiles_are_disabled_while_switching(self):
        config, state = sample_world()
        profiles = [i for i in self.build(config, state, switching=True) if i.action and i.action[0] == "profile"]
        self.assertTrue(profiles and not any(p.enabled for p in profiles))

    def test_without_profiles_criar_um_perfil(self):
        config, state = sample_world()
        config.profiles = []
        items = self.build(config, ActiveState())
        create = find(items, "Criar um perfil…")
        self.assertEqual((create.action, create.icon_name), (("open", "new-profile"), "list-add-symbolic"))
        self.assertEqual(items[items.index(create) - 1].label, "Perfis")

    # MARK: "Trocar só a conta Claude" (AppDelegate.swift:142-194)

    def test_claude_submenu_like_the_mac(self):
        config, state = sample_world()
        items = self.build(config, state, folder_status=lambda d: logged_in("dev@pasta.com"),
                           has_token=lambda account: account.nickname != "Pessoal",
                           account_icon=lambda account: b"png:" + account.nickname.encode())
        entry = find(items, "Trocar só a conta Claude")
        self.assertEqual(entry.icon_name, "multicred-sparkle-symbolic")
        submenu = entry.children
        self.assertEqual(labels(submenu), [
            "Conta Claude · pasta ~/.claude-trabalho (Trabalho) fica",
            "Login da pasta · dev@pasta.com",
            "Empresa · dev@empresa.com.br · do perfil",
            "Pessoal · sem token · conta Claude",
            "Adicionar conta Claude…",
            "Agents, skills e conversas não mudam.",
            "Vale para novos terminais; sessões abertas continuam.",
        ])
        header, own, empresa, pessoal, add, note1, note2 = visible(submenu)
        self.assertFalse(header.enabled or note1.enabled or note2.enabled)
        self.assertEqual((own.action, own.icon_name, own.toggle, own.toggled, own.enabled),
                         (("claude", None), "folder-symbolic", "checkmark", False, True))
        self.assertEqual((empresa.action, empresa.icon_png, empresa.toggle, empresa.toggled, empresa.enabled),
                         (("claude", config.claude_accounts[0].id), b"png:Empresa", "checkmark", True, True))
        self.assertFalse(pessoal.enabled)  # no token
        self.assertEqual((add.action, add.icon_name), (("open", "add-claude"), "list-add-symbolic"))
        separators = [n for n, i in enumerate(submenu) if i.separator]
        self.assertEqual(separators, [submenu.index(pessoal) + 1, submenu.index(add) + 1])

    def test_claude_submenu_folder_login_states(self):
        config, state = sample_world()
        state.profile_id, state.applied_claude_account_id = config.profiles[1].id, None
        cases = [
            (logged_in("eu@pessoal.com"), "Login da pasta · eu@pessoal.com · do perfil", ("claude", None)),
            (logged_in(None), "Login da pasta · Logada · do perfil", ("claude", None)),
            (logged_out(), "Login da pasta · Entrar…", ("folder-login", None)),
            (None, "Login da pasta · do perfil", ("claude", None)),
        ]
        for status, label, action in cases:
            with self.subTest(label=label):
                own = visible(claude_submenu(self.build(config, state, folder_status=lambda d: status)))[1]
                self.assertEqual((own.label, own.action, own.toggled), (label, action, True))
        from multicred.core.claude.status import ClaudeLoginStatus
        own = visible(claude_submenu(self.build(config, state,
                                                folder_status=lambda d: ClaudeLoginStatus.unknown("x"))))[1]
        self.assertEqual(own.label, "Login da pasta · Status indisponível · do perfil")

    def test_claude_submenu_folder_login_of_another_folder_asks_for_that_folder(self):
        config, state = sample_world()
        own = visible(claude_submenu(self.build(config, state, folder_status=lambda d: logged_out())))[1]
        self.assertEqual(own.action, ("folder-login", WORK_FOLDER))

    def test_claude_submenu_is_disabled_while_switching(self):
        config, state = sample_world()
        submenu = claude_submenu(self.build(config, state, switching=True))
        self.assertFalse(any(i.enabled for i in submenu if i.action and i.action[0] == "claude"))

    def test_claude_submenu_without_active_profile(self):
        config, _ = sample_world()
        submenu = claude_submenu(self.build(config, ActiveState()))
        self.assertEqual(labels(submenu), ["Conta Claude", "Ative um perfil primeiro"])
        self.assertFalse(any(i.enabled for i in submenu))

    # MARK: tail, unconfigured, test mode

    def test_tail_actions_icons_and_shortcuts(self):
        config, state = sample_world()
        items = self.build(config, state)
        tail = items[items.index(find(items, "Trocar só a conta Claude")) + 1:]
        self.assertTrue(tail[0].separator and tail[-2].separator)
        self.assertEqual([(i.label, i.action, i.icon_name, i.shortcut) for i in visible(tail)], [
            ("Verificar…", ("open", "verify"), "security-high-symbolic", ("Control", "r")),
            ("Contas…", ("open", "claude"), "avatar-default-symbolic", None),
            ("Ajustes…", ("open", "settings"), "emblem-system-symbolic", ("Control", "comma")),
            ("Abrir MultiCred", ("open", "main"), None, None),
            ("Sair do MultiCred", ("quit", None), None, ("Control", "q")),
        ])

    def test_unconfigured_menu(self):
        items = self.build(None, ActiveState())
        self.assertEqual(labels(items), ["Configurar MultiCred…", "Sair do MultiCred"])
        configure, quit_item = visible(items)
        self.assertEqual((configure.action, configure.icon_name), (("open", "onboarding"), "system-run-symbolic"))
        self.assertEqual((quit_item.action, quit_item.shortcut), (("quit", None), ("Control", "q")))

    def test_test_mode_line_on_top(self):
        config, state = sample_world()
        items = self.build(config, state, test_mode=True)
        self.assertEqual(items[0].label, "MODO DE TESTE · nada no sistema real muda")
        self.assertFalse(items[0].enabled)
        self.assertTrue(items[1].separator)


class DBusMenuPropertiesTests(unittest.TestCase):
    def test_item_properties_omit_defaults_and_escape_mnemonics(self):
        from multicred.tray.dbusmenu import properties
        from multicred.tray.menu_model import MenuItem
        self.assertEqual(properties(None), {"children-display": "submenu"})
        self.assertEqual(properties(MenuItem(separator=True)), {"type": "separator"})
        self.assertEqual(properties(MenuItem("git dev_x@empresa.com.br", enabled=False)),
                         {"label": "git dev__x@empresa.com.br", "enabled": False})
        self.assertEqual(properties(MenuItem("Pessoal", ("profile", 1), toggle="checkmark", toggled=True,
                                             icon_png=b"png")),
                         {"label": "Pessoal", "toggle-type": "checkmark", "toggle-state": 1, "icon-data": b"png"})
        self.assertEqual(properties(MenuItem("Sub", children=[MenuItem("x")]))["children-display"], "submenu")

    def test_icon_name_and_shortcut(self):
        from multicred.tray.dbusmenu import properties
        from multicred.tray.menu_model import MenuItem
        self.assertEqual(properties(MenuItem("Verificar…", ("open", "verify"), icon_name="security-high-symbolic",
                                             shortcut=("Control", "r"))),
                         {"label": "Verificar…", "icon-name": "security-high-symbolic",
                          "shortcut": [["Control", "r"]]})

    def test_icon_theme_path_holds_the_apps_own_icons(self):
        from pathlib import Path

        from multicred.tray.dbusmenu import ICON_THEME_PATH
        for name in ("branch", "code", "sparkle"):
            self.assertTrue((Path(ICON_THEME_PATH) / "hicolor" / "scalable" / "actions"
                             / f"multicred-{name}-symbolic.svg").is_file())


def pixel(pixmap, x, y):
    width, height, data = pixmap
    offset = (y * width + x) * 4
    return tuple(data[offset:offset + 4])  # A, R, G, B (network byte order)


def rgb(hex_color):
    value = int(hex_color[1:], 16)
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


class TrayIconTests(unittest.TestCase):
    def test_pixmaps_are_argb32_in_several_sizes(self):
        from multicred.tray.icon import sni_pixmaps
        pixmaps = sni_pixmaps("#5856D6", "T")
        self.assertEqual([(w, h) for w, h, _ in pixmaps], [(22, 22), (24, 24), (32, 32), (48, 48)])
        self.assertTrue(all(len(data) == w * h * 4 for w, h, data in pixmaps))

    def test_square_has_the_profile_color_and_the_corner_is_transparent(self):
        from multicred.tray.icon import sni_pixmaps
        for pixmap in sni_pixmaps(ProfileColor.ORANGE.hex, "P"):
            size = pixmap[0]
            self.assertEqual(pixel(pixmap, 2, size // 2), (255, *rgb(ProfileColor.ORANGE.hex)))
            self.assertEqual(pixel(pixmap, 0, 0)[0], 0)

    def test_letter_is_drawn_in_white(self):
        from multicred.tray.icon import sni_pixmaps
        width, height, data = sni_pixmaps(ProfileColor.INDIGO.hex, "T")[-1]
        pixels = [tuple(data[i:i + 4]) for i in range(0, len(data), 4)]
        self.assertTrue(any(a == 255 and r > 240 and g > 240 and b > 240 for a, r, g, b in pixels))

    def test_orange_dot_when_inconsistent(self):
        from multicred.tray.icon import ALERT_HEX, sni_pixmaps
        plain = sni_pixmaps(ProfileColor.INDIGO.hex, "T")
        alert = sni_pixmaps(ProfileColor.INDIGO.hex, "T", alert=True)
        for normal, flagged in zip(plain, alert):
            size = normal[0]
            x, y = int(size * 0.81), int(size * 0.19)
            self.assertEqual(pixel(normal, x, y), (255, *rgb(ProfileColor.INDIGO.hex)))
            self.assertEqual(pixel(flagged, x, y), (255, *rgb(ALERT_HEX)))

    def test_appearance_of_profile_and_no_profile(self):
        from multicred.tray.icon import appearance
        self.assertEqual(appearance(None), (ProfileColor.GRAPHITE.hex, "M"))
        self.assertEqual(appearance(Profile(name="Cliente", color=ProfileColor.TEAL, icon=ProfileIcon("letter", "x"))),
                         (ProfileColor.TEAL.hex, "X"))
        # S-05: one of the editor's 8 symbols is drawn as that symbol (StatusIcon.swift:22-29).
        self.assertEqual(appearance(Profile(name="Casa", color=ProfileColor.RED, icon=ProfileIcon("symbol", "house"))),
                         (ProfileColor.RED.hex, "house"))
        # A symbol this port does not draw (e.g. typed on the Mac) falls back to the name's letter.
        self.assertEqual(appearance(Profile(name="Casa", color=ProfileColor.RED, icon=ProfileIcon("symbol", "bolt"))),
                         (ProfileColor.RED.hex, "C"))

    def test_the_eight_symbols_are_drawn_in_white_and_differ(self):
        """The editor's symbols (Components.swift:65) are drawn in the tray icon, not a letter."""
        from multicred.tray.icon import SYMBOLS, sni_pixmaps
        self.assertEqual(SYMBOLS, ("briefcase", "house", "building.2", "laptopcomputer", "star", "person", "flask",
                                   "globe"))
        masks = {}
        for symbol in SYMBOLS + ("C",):
            width, height, data = sni_pixmaps(ProfileColor.TEAL.hex, symbol)[-1]
            pixels = [tuple(data[i:i + 4]) for i in range(0, len(data), 4)]
            white = frozenset(i for i, (a, r, g, b) in enumerate(pixels) if a == 255 and min(r, g, b) > 200)
            self.assertGreater(len(white), 40, f"{symbol} draws something white")
            self.assertEqual(pixel((width, height, data), 2, height // 2), (255, *rgb(ProfileColor.TEAL.hex)),
                             f"{symbol} keeps the colored square")
            masks[symbol] = white
        self.assertEqual(len(set(masks.values())), len(SYMBOLS) + 1, "every symbol looks different (and not a letter)")

    def test_menu_png_of_a_symbol_profile_shows_the_symbol(self):
        from multicred.tray.icon import png_bytes, profile_png
        profile = Profile(name="Casa", color=ProfileColor.RED, icon=ProfileIcon("symbol", "house"))
        self.assertEqual(profile_png(profile), png_bytes(ProfileColor.RED.hex, "house", size=16))
        self.assertNotEqual(profile_png(profile), png_bytes(ProfileColor.RED.hex, "C", size=16))

    def test_symbol_sheet_screenshot(self):
        """With MULTICRED_SCREENS_DIR set: every symbol in each color, at the panel sizes, plus the alert dot."""
        directory = os.environ.get("MULTICRED_SCREENS_DIR")
        if not directory:
            self.skipTest("MULTICRED_SCREENS_DIR not set")
        from pathlib import Path

        import cairo

        from multicred.tray.icon import SYMBOLS, render
        sizes, gap = (22, 32, 48), 12
        glyphs = ("T",) + SYMBOLS
        colors = list(ProfileColor) + [None]  # None: no profile, with the alert dot
        band = sum(size + 4 for size in sizes) + gap
        sheet = cairo.ImageSurface(cairo.FORMAT_ARGB32, gap + len(glyphs) * (48 + gap), gap + len(colors) * band)
        context = cairo.Context(sheet)
        context.set_source_rgb(0.18, 0.18, 0.2)  # a dark panel
        context.paint()
        y = gap
        for color in colors:
            for size in sizes:
                for column, glyph in enumerate(glyphs):
                    icon = render(color.hex if color else ProfileColor.GRAPHITE.hex, glyph, size,
                                  alert=color is None)
                    context.set_source_surface(icon, gap + column * (48 + gap) + (48 - size) / 2, y)
                    context.paint()
                y += size + 4
            y += gap
        Path(directory).mkdir(parents=True, exist_ok=True)
        path = Path(directory) / "S-05-bandeja-simbolos.png"
        sheet.write_to_png(str(path))
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_png_for_menu_items(self):
        from multicred.tray.icon import png_bytes
        data = png_bytes(ProfileColor.GREEN.hex, "G", size=16)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (16, 16))
        self.assertEqual(zlib.crc32(data[12:29]) & 0xFFFFFFFF, struct.unpack(">I", data[29:33])[0])


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.error = None

    def _result(self, profile_id, override):
        return SwitchResult(state=ActiveState(profile_id=profile_id, claude_override_id=override), claude_target=None)

    def switch_profile(self, profile_id, config):
        self.calls.append(("profile", profile_id))
        if self.error:
            raise self.error
        return self._result(profile_id, None)

    def switch_claude_only(self, account_id, config):
        self.calls.append(("claude", account_id))
        if self.error:
            raise self.error
        return self._result(None, account_id)


class AppModelTrayTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        from multicred.ui.model import AppModel
        self.config, self.state = sample_world()
        self.model = AppModel(self.environment, tools=None)
        self.model.config, self.model.state = self.config, self.state
        self.engine = self.model.engine = FakeEngine()

    def run_switch(self, start):
        done = threading.Event()
        outcome = {}

        def on_done(result, error):
            outcome.update(result=result, error=error)
            done.set()

        start(on_done)
        self.assertTrue(done.wait(5))
        return outcome

    def test_switch_claude_only_uses_override_semantics(self):
        empresa, pessoal = self.config.claude_accounts
        self.run_switch(lambda cb: self.model.switch_claude_only(pessoal.id, cb))
        self.model.state = self.state
        self.run_switch(lambda cb: self.model.switch_claude_only(empresa.id, cb))
        self.model.state = self.state
        self.run_switch(lambda cb: self.model.switch_claude_only(None, cb))
        self.assertEqual(self.engine.calls, [("claude", pessoal.id), ("claude", None),
                                             ("claude", ClaudeFolder.OWN_LOGIN_OVERRIDE)])

    def test_inconsistent_after_incomplete_rollback_and_cleared_by_success(self):
        self.engine.error = StepFailed(SwitchStepKind.SESSION, "boom", RollbackOutcome((SwitchStepKind.GIT,)))
        outcome = self.run_switch(lambda cb: self.model.switch_profile(self.config.profiles[1].id, cb))
        self.assertIsNotNone(outcome["error"])
        self.assertTrue(self.model.inconsistent)
        self.engine.error = None
        self.run_switch(lambda cb: self.model.switch_profile(self.config.profiles[1].id, cb))
        self.assertFalse(self.model.inconsistent)

    def test_complete_rollback_is_not_inconsistent(self):
        self.engine.error = StepFailed(SwitchStepKind.SESSION, "boom", RollbackOutcome(()))
        self.run_switch(lambda cb: self.model.switch_profile(self.config.profiles[1].id, cb))
        self.assertFalse(self.model.inconsistent)


# MARK: D-Bus

def spin_until(predicate, timeout=5.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting on the main loop")
        if not context.iteration(False):
            time.sleep(0.002)


def new_connection(test):
    address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    connection = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)
    test.addCleanup(connection.close_sync, None)
    return connection


def call(connection, destination, path, interface, method, params=None):
    box = {}

    def done(conn, result):
        try:
            box["value"] = conn.call_finish(result)
        except GLib.Error as error:
            box["error"] = error

    connection.call(destination, path, interface, method, params, None, Gio.DBusCallFlags.NONE, 3000, None, done)
    spin_until(lambda: box)
    if "error" in box:
        raise box["error"]
    return box["value"].unpack()


WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
  <method name="RegisterStatusNotifierHost"><arg name="service" type="s" direction="in"/></method>
  <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
  <property name="ProtocolVersion" type="i" access="read"/>
</interface></node>"""


class FakeWatcher:
    """Owns org.kde.StatusNotifierWatcher on its own connection and records registrations."""

    def __init__(self, test):
        self.connection = new_connection(test)
        self.registrations = []
        node = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML)
        registration = self.connection.register_object("/StatusNotifierWatcher", node.interfaces[0],
                                                       self._method, self._property, None)
        test.addCleanup(self.connection.unregister_object, registration)
        reply = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          "RequestName", GLib.Variant("(su)", (WATCHER, 4)), None,
                                          Gio.DBusCallFlags.NONE, -1, None)
        assert reply.unpack() == (1,), "the private bus already has a watcher"

    def _method(self, connection, sender, path, interface, method, params, invocation):
        if method == "RegisterStatusNotifierItem":
            self.registrations.append((sender, params.unpack()[0]))
        invocation.return_value(None)

    def _property(self, connection, sender, path, interface, name):
        return GLib.Variant("b", True) if name == "IsStatusNotifierHostRegistered" else GLib.Variant("i", 0)


class FakeModel:
    """What the tray reads from AppModel, with recording switch, status and verify methods."""

    def __init__(self, config, state):
        self.config, self.state = config, state
        self.inconsistent = False
        self.switching = False
        self.is_test_mode = False
        self.verification = None
        self.statuses = {}
        self.tokens = {}
        self.switched, self.claude_switched = [], []
        self.status_refreshes, self.verifies = [], []
        self._listeners = []

    @property
    def active_profile(self):
        return self.config.profile(self.state.profile_id) if self.config else None

    def add_listener(self, listener):
        self._listeners.append(listener)

    def notify(self):
        for listener in list(self._listeners):
            listener()

    def folder_name(self, config_dir):
        return abbreviate(config_dir)

    def folder_status(self, config_dir):
        return self.statuses.get(config_dir)

    def has_token(self, account):
        return self.tokens.get(account.nickname, True)

    def needs_claude_login(self, profile):
        account = self.config.claude_account(profile.claude_account_id)
        if account is not None:
            return not self.has_token(account)
        status = self.folder_status(profile.claude_config_dir)
        return status is not None and status.kind == "loggedOut"

    def refresh_claude_statuses(self, on_done=None):
        self.status_refreshes.append(on_done)

    def verify(self, on_done=None, deep=False):
        self.verifies.append((on_done, deep))

    def switch_profile(self, profile_id, on_done):
        self.switched.append(profile_id)
        self.state = ActiveState(profile_id=profile_id, activated_at=FIXED_NOW)
        on_done(object(), None)
        self.notify()

    def switch_claude_only(self, account_id, on_done):
        self.claude_switched.append(account_id)
        on_done(object(), None)


def walk(layout):
    item_id, props, children = layout
    yield item_id, props
    for child in children:
        yield from walk(child)


def shown(children):
    return [c[1].get("label") for c in children if c[1].get("type") != "separator"]


@unittest.skipUnless(PRIVATE_BUS, "needs tests/run-ui-tests.sh (private bus)")
class TrayTests(unittest.TestCase):
    def setUp(self):
        self.config, self.state = sample_world()
        self.model = FakeModel(self.config, self.state)
        self.opened, self.quits, self.availability, self.logins = [], [], [], []
        self.now = FIXED_NOW
        self.client = new_connection(self)

    def start_tray(self, with_login=True):
        from multicred.tray.controller import TrayController
        tray = TrayController(self.model, on_open=self.opened.append, on_quit=lambda: self.quits.append(True),
                              on_login=(lambda kind, target: self.logins.append((kind, target))) if with_login
                              else None,
                              on_availability=self.availability.append, connection=new_connection(self), tz=BRT,
                              clock=lambda: self.now)
        tray.start()
        self.addCleanup(tray.stop)
        spin_until(lambda: self.availability)
        return tray

    def layout(self, tray, parent=0, depth=-1, names=()):
        return call(self.client, tray.bus_name, "/MenuBar", MENU, "GetLayout",
                    GLib.Variant("(iias)", (parent, depth, list(names))))

    def item_id(self, tray, label):
        _, layout = self.layout(tray)
        return next(i for i, props in walk(layout) if props.get("label") == label)

    def click(self, tray, item_id):
        call(self.client, tray.bus_name, "/MenuBar", MENU, "Event",
             GLib.Variant("(isvu)", (item_id, "clicked", GLib.Variant("s", ""), 0)))

    def about_to_show(self, tray, item_id=0):
        return call(self.client, tray.bus_name, "/MenuBar", MENU, "AboutToShow", GLib.Variant("(i)", (item_id,)))

    def test_registers_with_the_watcher(self):
        watcher = FakeWatcher(self)
        tray = self.start_tray()
        self.assertEqual(self.availability, [True])
        self.assertTrue(tray.available)
        self.assertRegex(tray.bus_name, rf"^org\.kde\.StatusNotifierItem-{os.getpid()}-\d+$")
        self.assertEqual(len(watcher.registrations), 1)
        sender, service = watcher.registrations[0]
        self.assertEqual(service, tray.bus_name)
        owner = call(self.client, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                     "GetNameOwner", GLib.Variant("(s)", (tray.bus_name,)))[0]
        self.assertEqual(sender, owner)

    def test_layout_like_the_mac_menu(self):
        FakeWatcher(self)
        self.model.statuses[WORK_FOLDER] = logged_in("dev@pasta.com")
        self.model.tokens["Pessoal"] = False
        tray = self.start_tray()
        revision, layout = self.layout(tray)
        root_id, root_props, children = layout
        self.assertEqual(root_id, 0)
        self.assertEqual(root_props.get("children-display"), "submenu")
        since = FIXED_NOW.astimezone(BRT).strftime("%H:%M")
        self.assertEqual(shown(children), [
            f"Trabalho · Ativo desde {since}", "Não verificado", "dev@empresa.com.br", "dev-empresa",
            "Claude · ~/.claude-trabalho · Empresa",
            "Perfis", "Trabalho", "Pessoal", "Cliente X",
            "Trocar só a conta Claude",
            "Verificar…", "Contas…", "Ajustes…", "Abrir MultiCred",
            "Sair do MultiCred"])
        by_label = {c[1].get("label"): c for c in children}
        self.assertIs(by_label["dev@empresa.com.br"][1].get("enabled"), False)
        work, personal = by_label["Trabalho"][1], by_label["Pessoal"][1]
        self.assertEqual((work.get("toggle-type"), work.get("toggle-state")), ("checkmark", 1))
        self.assertEqual((personal.get("toggle-type"), personal.get("toggle-state")), ("checkmark", 0))
        self.assertEqual(bytes(personal["icon-data"][:4]), b"\x89PNG")
        claude = by_label["Trocar só a conta Claude"]
        self.assertEqual(claude[1].get("children-display"), "submenu")
        self.assertEqual(shown(claude[2]), [
            "Conta Claude · pasta ~/.claude-trabalho (Trabalho) fica", "Login da pasta · dev@pasta.com",
            "Empresa · dev@empresa.com.br · do perfil", "Pessoal · sem token · conta Claude",
            "Adicionar conta Claude…", "Agents, skills e conversas não mudam.",
            "Vale para novos terminais; sessões abertas continuam."])
        sub = {c[1].get("label"): c[1] for c in claude[2]}
        self.assertIs(sub["Pessoal · sem token · conta Claude"].get("enabled"), False)
        self.assertEqual(bytes(sub["Empresa · dev@empresa.com.br · do perfil"]["icon-data"][:4]), b"\x89PNG")
        ids = [i for i, _ in walk(layout)]
        self.assertEqual(len(ids), len(set(ids)))
        version = call(self.client, tray.bus_name, "/MenuBar", "org.freedesktop.DBus.Properties", "Get",
                       GLib.Variant("(ss)", (MENU, "Version")))[0]
        self.assertEqual(version, 3)

    def test_icon_name_and_shortcut_go_over_the_bus(self):
        FakeWatcher(self)
        tray = self.start_tray()
        _, layout = self.layout(tray)
        props = {p.get("label"): p for _, p in walk(layout)}
        self.assertEqual(props["Verificar…"]["icon-name"], "security-high-symbolic")
        self.assertEqual(props["Verificar…"]["shortcut"], [["Control", "r"]])
        self.assertEqual(props["Ajustes…"]["shortcut"], [["Control", "comma"]])
        self.assertEqual(props["Sair do MultiCred"]["shortcut"], [["Control", "q"]])
        self.assertEqual(props["Pessoal"]["shortcut"], [["Control", "2"]])
        self.assertEqual(props["dev@empresa.com.br"]["icon-name"], "multicred-branch-symbolic")
        self.assertEqual(props["Trocar só a conta Claude"]["icon-name"], "multicred-sparkle-symbolic")
        verify_id = self.item_id(tray, "Verificar…")
        (value,) = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetProperty",
                        GLib.Variant("(is)", (verify_id, "shortcut")))
        self.assertEqual(value, [["Control", "r"]])
        box = []
        self.client.call(tray.bus_name, "/MenuBar", MENU, "GetGroupProperties",
                         GLib.Variant("(aias)", ([verify_id], ["icon-name", "shortcut"])), None,
                         Gio.DBusCallFlags.NONE, 3000, None, lambda conn, res: box.append(conn.call_finish(res)))
        spin_until(lambda: box)
        group = box[0].get_child_value(0).get_child_value(0).get_child_value(1)
        self.assertEqual(group.lookup_value("shortcut", None).get_type_string(), "aas")
        self.assertEqual(group.lookup_value("icon-name", None).get_type_string(), "s")
        (theme_path,) = call(self.client, tray.bus_name, "/MenuBar", "org.freedesktop.DBus.Properties", "Get",
                             GLib.Variant("(ss)", (MENU, "IconThemePath")))
        from multicred.tray.dbusmenu import ICON_THEME_PATH
        self.assertEqual(theme_path, [ICON_THEME_PATH])

    def test_sub_layout_and_group_properties(self):
        FakeWatcher(self)
        tray = self.start_tray()
        claude_id = self.item_id(tray, "Trocar só a conta Claude")
        _, sub = self.layout(tray, claude_id, 1, ["label"])
        self.assertEqual(sub[0], claude_id)
        self.assertEqual(sub[2][0][1], {"label": "Conta Claude · pasta ~/.claude-trabalho (Trabalho) fica"})
        quit_id = self.item_id(tray, "Sair do MultiCred")
        (props,) = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetGroupProperties",
                        GLib.Variant("(aias)", ([quit_id], ["label"])))
        self.assertEqual(props, [(quit_id, {"label": "Sair do MultiCred"})])
        (label,) = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetProperty",
                        GLib.Variant("(is)", (quit_id, "label")))
        self.assertEqual(label, "Sair do MultiCred")

    def test_about_to_show_refreshes_statuses_then_verifies_when_older_than_120_s(self):
        FakeWatcher(self)
        tray = self.start_tray()
        self.assertEqual(self.about_to_show(tray), (False,))  # the layout follows with LayoutUpdated
        self.assertEqual(len(self.model.status_refreshes), 1)
        self.assertEqual(self.model.verifies, [])  # after the statuses, like the Mac's await
        self.about_to_show(tray)
        self.assertEqual(len(self.model.status_refreshes), 1)  # one refresh at a time
        self.model.status_refreshes[0]()
        self.assertEqual(len(self.model.verifies), 1)
        on_done, deep = self.model.verifies[0]
        self.assertFalse(deep)  # Mac: verify(checkTokens: false)
        _, layout = self.layout(tray)
        self.assertIn("Verificando", shown(layout[2]))
        fresh = report("ok", "error", checked_at=FIXED_NOW)
        self.model.verification = fresh
        on_done(fresh, None)
        self.model.notify()
        _, layout = self.layout(tray)
        self.assertIn("Inconsistente", shown(layout[2]))
        self.assertIn("Corrigir…", shown(layout[2]))

        # Opened again within 120 s: statuses only.
        self.now = FIXED_NOW + timedelta(seconds=120)
        self.about_to_show(tray)
        self.model.status_refreshes[-1]()
        self.assertEqual(len(self.model.status_refreshes), 2)
        self.assertEqual(len(self.model.verifies), 1)
        # Later: verifies again.
        self.now = FIXED_NOW + timedelta(seconds=121)
        (updates, errors) = call(self.client, tray.bus_name, "/MenuBar", MENU, "AboutToShowGroup",
                                 GLib.Variant("(ai)", ([0, 9999],)))
        self.assertEqual((updates, errors), ([], [9999]))
        self.model.status_refreshes[-1]()
        self.assertEqual(len(self.model.verifies), 2)

    def test_about_to_show_of_a_submenu_does_nothing(self):
        FakeWatcher(self)
        tray = self.start_tray()
        self.assertEqual(self.about_to_show(tray, self.item_id(tray, "Trocar só a conta Claude")), (False,))
        self.assertEqual((self.model.status_refreshes, self.model.verifies), ([], []))

    def test_clicked_on_a_profile_switches_it(self):
        FakeWatcher(self)
        tray = self.start_tray()
        revision, _ = self.layout(tray)
        updates = []
        self.client.signal_subscribe(tray.bus_name, MENU, "LayoutUpdated", "/MenuBar", None,
                                     Gio.DBusSignalFlags.NONE, lambda *a: updates.append(a[5].unpack()))
        self.click(tray, self.item_id(tray, "Pessoal"))
        spin_until(lambda: self.model.switched and updates)
        self.assertEqual(self.model.switched, [self.config.profiles[1].id])
        self.assertEqual(self.logins, [])
        new_revision, layout = self.layout(tray)
        self.assertGreater(new_revision, revision)
        toggled = [props.get("label") for _, props in walk(layout) if props.get("toggle-state") == 1]
        self.assertIn("Pessoal", toggled)

    def test_switching_to_a_profile_that_needs_claude_login_opens_the_login(self):
        """AppDelegate.swift:198-211: the account's login again, or the folder's own login."""
        FakeWatcher(self)
        self.model.statuses[None] = logged_out()
        tray = self.start_tray()
        self.click(tray, self.item_id(tray, "Pessoal  ⚠︎ Claude precisa entrar"))
        spin_until(lambda: self.logins)
        self.assertEqual(self.logins, [("folder", None)])
        self.model.tokens["Empresa"] = False
        self.model.notify()
        self.click(tray, self.item_id(tray, "Trabalho  ⚠︎ Claude precisa entrar"))
        spin_until(lambda: len(self.logins) == 2)
        self.assertEqual(self.logins[1], ("login", self.config.claude_accounts[0]))

    def test_other_actions(self):
        FakeWatcher(self)
        tray = self.start_tray()
        claude_id = self.item_id(tray, "Trocar só a conta Claude")
        _, sub = self.layout(tray, claude_id, 1)
        own = next(c[0] for c in sub[2] if c[1].get("label") == "Login da pasta")
        self.click(tray, own)
        self.click(tray, self.item_id(tray, "Contas…"))
        self.click(tray, self.item_id(tray, "Sair do MultiCred"))
        spin_until(lambda: self.model.claude_switched and self.opened and self.quits)
        self.assertEqual(self.model.claude_switched, [None])
        self.assertEqual(self.opened, ["claude"])  # AppDelegate.swift:229
        call(self.client, tray.bus_name, "/StatusNotifierItem", SNI, "Activate", GLib.Variant("(ii)", (0, 0)))
        spin_until(lambda: len(self.opened) == 2)
        self.assertEqual(self.opened[-1], "main")

    def test_folder_login_entrar_opens_the_folder_login(self):
        FakeWatcher(self)
        self.model.statuses[WORK_FOLDER] = logged_out()
        tray = self.start_tray()
        self.click(tray, self.item_id(tray, "Login da pasta · Entrar…"))
        spin_until(lambda: self.logins)
        self.assertEqual(self.logins, [("folder", WORK_FOLDER)])
        self.assertEqual(self.model.claude_switched, [])

    def test_without_a_login_handler_logins_open_the_claude_page(self):
        FakeWatcher(self)
        self.model.statuses[WORK_FOLDER] = logged_out()
        tray = self.start_tray(with_login=False)
        self.click(tray, self.item_id(tray, "Login da pasta · Entrar…"))
        spin_until(lambda: self.opened)
        self.assertEqual(self.opened, ["claude"])

    def test_item_properties_icon_and_status(self):
        FakeWatcher(self)
        tray = self.start_tray()
        (props,) = call(self.client, tray.bus_name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                        "GetAll", GLib.Variant("(s)", (SNI,)))
        self.assertEqual(props["Category"], "ApplicationStatus")
        self.assertEqual(props["Status"], "Active")
        self.assertEqual(props["Menu"], "/MenuBar")
        self.assertEqual(props["IconName"], "")
        self.assertIs(props["ItemIsMenu"], False)
        self.assertEqual(props["Title"], "MultiCred")
        self.assertEqual([(w, h) for w, h, _ in props["IconPixmap"]], [(22, 22), (24, 24), (32, 32), (48, 48)])
        w, h, data = props["IconPixmap"][0]
        self.assertEqual(pixel((w, h, bytes(data)), 2, h // 2), (255, *rgb(ProfileColor.INDIGO.hex)))
        self.assertIn("Trabalho", props["ToolTip"][2] + props["ToolTip"][3])

        signals = []
        for name in ("NewIcon", "NewStatus"):
            self.client.signal_subscribe(tray.bus_name, SNI, name, "/StatusNotifierItem", None,
                                         Gio.DBusSignalFlags.NONE, lambda *a: signals.append(a[4]))
        self.model.inconsistent = True
        self.model.notify()
        spin_until(lambda: {"NewIcon", "NewStatus"} <= set(signals))
        (status,) = call(self.client, tray.bus_name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                         "Get", GLib.Variant("(ss)", (SNI, "Status")))
        self.assertEqual(status, "NeedsAttention")
        _, layout = self.layout(tray)
        self.assertIn("Corrigir…", [p.get("label") for _, p in walk(layout)])

    def test_verification_problems_mark_the_icon(self):
        """AppDelegate.swift:59: the orange dot follows `consistency`, i.e. the report's problems."""
        FakeWatcher(self)
        tray = self.start_tray()
        self.model.verification = report("ok", "warning")
        self.model.notify()
        (status,) = call(self.client, tray.bus_name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                         "Get", GLib.Variant("(ss)", (SNI, "Status")))
        self.assertEqual(status, "Active")
        self.model.verification = report("ok", "error")
        self.model.notify()
        (status,) = call(self.client, tray.bus_name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                         "Get", GLib.Variant("(ss)", (SNI, "Status")))
        self.assertEqual(status, "NeedsAttention")
        _, layout = self.layout(tray)
        self.assertIn("Identidade do git sobrescrita 2", shown(layout[2]))

    def test_without_a_watcher_the_tray_is_unavailable(self):
        tray = self.start_tray()
        self.assertEqual(self.availability, [False])
        self.assertFalse(tray.available)

    def test_registers_again_when_a_watcher_appears_later(self):
        tray = self.start_tray()
        watcher = FakeWatcher(self)
        spin_until(lambda: watcher.registrations and tray.available)
        self.assertEqual(self.availability, [False, True])
        self.assertEqual(watcher.registrations[0][1], tray.bus_name)


class FixedTools:
    def locate(self, tool):
        return None

    def search_directories(self):
        return ["/usr/bin", "/bin"]


@unittest.skipUnless(HAS_DISPLAY, "needs tests/run-ui-tests.sh (private display and bus)")
class TrayAppTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        from multicred.core import real_env
        from multicred.core.storage import ConfigRepository
        from multicred.ui.model import AppModel

        self.sandbox = Path(tempfile.mkdtemp(prefix="multicred-tray-"))
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        environment = real_env.make_sandbox(self.sandbox)
        ConfigRepository(environment).save(real_env.seed_sandbox(environment))
        self.model = AppModel(environment, FixedTools(), dispatch=GLib.idle_add)

    def run_app(self, drive):
        from multicred.ui.app import MultiCredApplication
        app = MultiCredApplication(self.model, background=True, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.connect("startup", lambda *_: GLib.timeout_add(20, drive, app))
        GLib.timeout_add_seconds(15, app.quit)  # never hang the suite
        app.run([])
        return app

    def test_background_without_tray_presents_window_with_banner(self):
        seen = {}

        def drive(app):
            if app.tray_available is None:
                return True
            seen["available"] = app.tray_available
            seen["visible"] = app.window is not None and app.window.get_visible()
            seen["banner"] = app.window.tray_banner.get_revealed() if app.window else None
            seen["title"] = app.window.tray_banner.get_title() if app.window else None
            app.quit()
            return False

        self.run_app(drive)
        self.assertIs(seen.get("available"), False)
        self.assertTrue(seen.get("visible"))
        self.assertTrue(seen.get("banner"))
        self.assertIn("AppIndicator", seen.get("title"))
        self.assertIn("COSMIC", seen.get("title"))

    def test_background_with_tray_stays_hidden_and_survives_closing_the_window(self):
        FakeWatcher(self)
        client = new_connection(self)
        seen = {"steps": []}

        def drive(app):
            if app.tray_available is None:
                return True
            seen["available"] = app.tray_available
            seen["window_at_start"] = app.window
            bus_name = app.tray.bus_name

            def activate():
                client.call(bus_name, "/StatusNotifierItem", SNI, "Activate", GLib.Variant("(ii)", (0, 0)),
                            None, Gio.DBusCallFlags.NONE, 3000, None, None)
                GLib.timeout_add(200, after_activate)
                return False

            def after_activate():
                seen["visible_after_activate"] = app.window is not None and app.window.get_visible()
                seen["banner"] = app.window.tray_banner.get_revealed()
                app.window.close()
                GLib.timeout_add(200, after_close)
                return False

            def after_close():
                seen["still_running"] = True
                seen["visible_after_close"] = app.window is not None and app.window.get_visible()
                app.quit()
                return False

            GLib.idle_add(activate)
            return False

        self.run_app(drive)
        self.assertIs(seen.get("available"), True)
        self.assertIsNone(seen.get("window_at_start"))
        self.assertTrue(seen.get("visible_after_activate"))
        self.assertFalse(seen.get("banner"))
        self.assertTrue(seen.get("still_running"))
        self.assertFalse(seen.get("visible_after_close"))

    def test_tray_logins_open_the_login_page_in_the_window(self):
        """"Login da pasta · Entrar…" in the tray (Mac :155-168) lands on the window's
        "Entrar no Claude" page, not just the Claude page."""
        FakeWatcher(self)
        folder = str(self.sandbox / ".claude")
        seen = {}

        def drive(app):
            if app.tray_available is None:
                return True
            app.tray._login("folder", folder)
            GLib.timeout_add(200, after_login, app)
            return False

        def after_login(app):
            from multicred.ui.pages.claude import ClaudeFolderLoginPage
            page = app.window.nav.get_visible_page() if app.window else None
            seen["visible"] = app.window is not None and app.window.get_visible()
            seen["page"] = type(page).__name__
            seen["section"] = app.window.current_section if app.window else None
            if isinstance(page, ClaudeFolderLoginPage):
                app.window.nav.pop()  # leaving the page stops the (simulated) login
            app.quit()
            return False

        self.run_app(drive)
        self.assertTrue(seen.get("visible"))
        self.assertEqual(seen.get("section"), "claude")
        self.assertEqual(seen.get("page"), "ClaudeFolderLoginPage")


if __name__ == "__main__":
    unittest.main()
