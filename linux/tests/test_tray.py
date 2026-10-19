"""S-03: tray icon (StatusNotifierItem) with the profile menu (com.canonical.dbusmenu).

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


def sample_world():
    """Three profiles, two Claude accounts; Trabalho is active with its own account."""
    work_git = GitIdentity(nickname="Trabalho", name="Dev", email="dev@empresa.com.br", color=ProfileColor.INDIGO)
    work_gh = GitHubAccount(nickname="Empresa", username="dev-empresa", color=ProfileColor.INDIGO)
    empresa = ClaudeAccount(nickname="Empresa", color=ProfileColor.INDIGO, kind=ClaudeAccountKind.LOGIN)
    pessoal_claude = ClaudeAccount(nickname="Pessoal", color=ProfileColor.ORANGE)
    work = Profile(name="Trabalho", color=ProfileColor.INDIGO, git_identity_id=work_git.id,
                   github_account_id=work_gh.id, claude_account_id=empresa.id)
    personal = Profile(name="Pessoal", color=ProfileColor.ORANGE)
    client = Profile(name="Cliente X", color=ProfileColor.TEAL, icon=ProfileIcon("letter", "X"))
    config = Config(git_identities=[work_git], github_accounts=[work_gh], claude_accounts=[empresa, pessoal_claude],
                    profiles=[work, personal, client])
    state = ActiveState(profile_id=work.id, applied_claude_account_id=empresa.id, activated_at=FIXED_NOW,
                        generation=3)
    return config, state


def visible(items):
    return [item for item in items if not item.separator]


def labels(items):
    return [item.label for item in visible(items)]


class MenuModelTests(unittest.TestCase):
    def build(self, config, state, **flags):
        from multicred.tray.menu_model import build_menu
        flags.setdefault("tz", BRT)
        return build_menu(config, state, **flags)

    def test_header_lines_are_disabled_text(self):
        config, state = sample_world()
        items = self.build(config, state)
        since = FIXED_NOW.astimezone(BRT).strftime("%H:%M")
        self.assertEqual([i.label for i in items[:3]], [
            f"Trabalho · ativo desde {since}",
            "git dev@empresa.com.br",
            "GitHub dev-empresa · Claude Empresa",
        ])
        self.assertTrue(all(not i.enabled and i.action is None for i in items[:3]))
        self.assertTrue(items[3].separator)

    def test_header_without_github_and_with_override_and_folder_login(self):
        config, state = sample_world()
        state.profile_id = config.profiles[1].id
        state.applied_claude_account_id = None
        state.claude_override_id = ClaudeFolder.OWN_LOGIN_OVERRIDE
        items = self.build(config, state)
        self.assertEqual(items[1].label, "Claude login da pasta (só Claude)")

    def test_no_active_profile_header(self):
        config, _ = sample_world()
        items = self.build(config, ActiveState())
        self.assertEqual(items[0].label, "Nenhum perfil ativo")
        self.assertFalse(items[0].enabled)

    def test_profiles_in_config_order_with_radio_on_the_active_one_and_icons(self):
        config, state = sample_world()
        items = self.build(config, state, icon=lambda profile: b"png:" + profile.name.encode())
        profiles = [i for i in items if i.action and i.action[0] == "profile"]
        self.assertEqual([p.label for p in profiles], ["Trabalho", "Pessoal", "Cliente X"])
        self.assertEqual([p.action[1] for p in profiles], [p.id for p in config.profiles])
        self.assertTrue(all(p.toggle == "radio" for p in profiles))
        self.assertEqual([p.toggled for p in profiles], [True, False, False])
        self.assertEqual(profiles[2].icon_png, b"png:Cliente X")
        self.assertTrue(all(p.enabled for p in profiles))

    def test_profiles_are_disabled_while_switching(self):
        config, state = sample_world()
        profiles = [i for i in self.build(config, state, switching=True) if i.action and i.action[0] == "profile"]
        self.assertTrue(profiles and not any(p.enabled for p in profiles))

    def test_inconsistent_item_only_when_inconsistent(self):
        config, state = sample_world()
        self.assertNotIn("⚠ Inconsistente — Corrigir", labels(self.build(config, state)))
        items = self.build(config, state, inconsistent=True)
        fix = next(i for i in items if i.label == "⚠ Inconsistente — Corrigir")
        self.assertEqual(fix.action, ("open", "verify"))
        self.assertTrue(fix.enabled)
        self.assertLess(items.index(fix), next(n for n, i in enumerate(items) if i.separator))

    def test_claude_submenu_marks_applied_account_and_profile_default(self):
        config, state = sample_world()
        items = self.build(config, state)
        submenu = next(i for i in items if i.label == "Trocar só a conta Claude")
        self.assertEqual(labels(submenu.children),
                         ["Login da pasta", "Empresa · do perfil", "Pessoal", "Adicionar conta Claude…"])
        own, empresa, pessoal, add = visible(submenu.children)
        self.assertEqual(own.action, ("claude", None))
        self.assertEqual(empresa.action, ("claude", config.claude_accounts[0].id))
        self.assertEqual([own.toggled, empresa.toggled, pessoal.toggled], [False, True, False])
        self.assertTrue(all(i.toggle == "radio" for i in (own, empresa, pessoal)))
        self.assertEqual(add.action, ("open", "add-claude"))
        self.assertTrue(submenu.children[-2].separator)

    def test_claude_submenu_folder_login_is_the_profile_default(self):
        config, state = sample_world()
        state.profile_id, state.applied_claude_account_id = config.profiles[1].id, None
        submenu = next(i for i in self.build(config, state) if i.label == "Trocar só a conta Claude")
        own = submenu.children[0]
        self.assertEqual(own.label, "Login da pasta · do perfil")
        self.assertTrue(own.toggled)

    def test_claude_submenu_without_active_profile(self):
        config, _ = sample_world()
        submenu = next(i for i in self.build(config, ActiveState()) if i.label == "Trocar só a conta Claude")
        self.assertEqual(labels(submenu.children), ["Ative um perfil primeiro"])
        self.assertFalse(submenu.children[0].enabled)

    def test_tail_actions_and_quit(self):
        config, state = sample_world()
        items = self.build(config, state)
        tail = items[items.index(next(i for i in items if i.label == "Trocar só a conta Claude")) + 1:]
        self.assertEqual(labels(tail), ["Verificar…", "Abrir MultiCred", "Contas…", "Ajustes…", "Sair do MultiCred"])
        self.assertEqual([i.action for i in visible(tail)], [
            ("open", "verify"), ("open", "main"), ("open", "accounts"), ("open", "settings"), ("quit", None)])
        self.assertTrue(tail[-2].separator)

    def test_unconfigured_menu(self):
        items = self.build(None, ActiveState())
        self.assertEqual(labels(items), ["Configurar MultiCred…", "Sair do MultiCred"])
        self.assertEqual([i.action for i in visible(items)], [("open", "onboarding"), ("quit", None)])

    def test_test_mode_line_on_top(self):
        config, state = sample_world()
        items = self.build(config, state, test_mode=True)
        self.assertEqual(items[0].label, "MODO DE TESTE · nada no sistema real muda")
        self.assertFalse(items[0].enabled)


class DBusMenuPropertiesTests(unittest.TestCase):
    def test_item_properties_omit_defaults_and_escape_mnemonics(self):
        from multicred.tray.dbusmenu import properties
        from multicred.tray.menu_model import MenuItem
        self.assertEqual(properties(None), {"children-display": "submenu"})
        self.assertEqual(properties(MenuItem(separator=True)), {"type": "separator"})
        self.assertEqual(properties(MenuItem("git dev_x@empresa.com.br", enabled=False)),
                         {"label": "git dev__x@empresa.com.br", "enabled": False})
        self.assertEqual(properties(MenuItem("Pessoal", ("profile", 1), toggle="radio", toggled=True,
                                             icon_png=b"png")),
                         {"label": "Pessoal", "toggle-type": "radio", "toggle-state": 1, "icon-data": b"png"})
        self.assertEqual(properties(MenuItem("Sub", children=[MenuItem("x")]))["children-display"], "submenu")


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
    """What the tray reads from AppModel, with recording switch methods."""

    def __init__(self, config, state):
        self.config, self.state = config, state
        self.inconsistent = False
        self.switching = False
        self.is_test_mode = False
        self.switched, self.claude_switched = [], []
        self._listeners = []

    @property
    def active_profile(self):
        return self.config.profile(self.state.profile_id) if self.config else None

    def add_listener(self, listener):
        self._listeners.append(listener)

    def notify(self):
        for listener in list(self._listeners):
            listener()

    def switch_profile(self, profile_id, on_done):
        self.switched.append(profile_id)
        self.state = ActiveState(profile_id=profile_id, activated_at=FIXED_NOW)
        on_done(None, None)
        self.notify()

    def switch_claude_only(self, account_id, on_done):
        self.claude_switched.append(account_id)
        on_done(None, None)


def walk(layout):
    item_id, props, children = layout
    yield item_id, props
    for child in children:
        yield from walk(child)


@unittest.skipUnless(PRIVATE_BUS, "needs tests/run-ui-tests.sh (private bus)")
class TrayTests(unittest.TestCase):
    def setUp(self):
        self.config, self.state = sample_world()
        self.model = FakeModel(self.config, self.state)
        self.opened, self.quits, self.availability = [], [], []
        self.client = new_connection(self)

    def start_tray(self):
        from multicred.tray.controller import TrayController
        tray = TrayController(self.model, on_open=self.opened.append, on_quit=lambda: self.quits.append(True),
                              on_availability=self.availability.append, connection=new_connection(self), tz=BRT)
        tray.start()
        self.addCleanup(tray.stop)
        return tray

    def layout(self, tray):
        revision, layout = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetLayout",
                                GLib.Variant("(iias)", (0, -1, [])))
        return revision, layout

    def item_id(self, tray, label):
        _, layout = self.layout(tray)
        return next(i for i, props in walk(layout) if props.get("label") == label)

    def click(self, tray, item_id):
        call(self.client, tray.bus_name, "/MenuBar", MENU, "Event",
             GLib.Variant("(isvu)", (item_id, "clicked", GLib.Variant("s", ""), 0)))

    def test_registers_with_the_watcher(self):
        watcher = FakeWatcher(self)
        tray = self.start_tray()
        spin_until(lambda: self.availability)
        self.assertEqual(self.availability, [True])
        self.assertTrue(tray.available)
        self.assertRegex(tray.bus_name, rf"^org\.kde\.StatusNotifierItem-{os.getpid()}-\d+$")
        self.assertEqual(len(watcher.registrations), 1)
        sender, service = watcher.registrations[0]
        self.assertEqual(service, tray.bus_name)
        owner = call(self.client, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                     "GetNameOwner", GLib.Variant("(s)", (tray.bus_name,)))[0]
        self.assertEqual(sender, owner)

    def test_layout_has_header_profiles_claude_submenu_and_quit(self):
        FakeWatcher(self)
        tray = self.start_tray()
        spin_until(lambda: self.availability)
        revision, layout = self.layout(tray)
        root_id, root_props, children = layout
        self.assertEqual(root_id, 0)
        self.assertEqual(root_props.get("children-display"), "submenu")
        top = [c[1].get("label") for c in children if c[1].get("type") != "separator"]
        since = FIXED_NOW.astimezone(BRT).strftime("%H:%M")
        self.assertEqual(top, [f"Trabalho · ativo desde {since}", "git dev@empresa.com.br",
                               "GitHub dev-empresa · Claude Empresa", "Trabalho", "Pessoal", "Cliente X",
                               "Trocar só a conta Claude", "Verificar…", "Abrir MultiCred", "Contas…", "Ajustes…",
                               "Sair do MultiCred"])
        by_label = {c[1].get("label"): c for c in children}
        self.assertIs(by_label["git dev@empresa.com.br"][1].get("enabled"), False)
        work, personal = by_label["Trabalho"][1], by_label["Pessoal"][1]
        self.assertEqual((work.get("toggle-type"), work.get("toggle-state")), ("radio", 1))
        self.assertEqual((personal.get("toggle-type"), personal.get("toggle-state")), ("radio", 0))
        self.assertEqual(bytes(personal["icon-data"][:4]), b"\x89PNG")
        claude = by_label["Trocar só a conta Claude"]
        self.assertEqual(claude[1].get("children-display"), "submenu")
        self.assertEqual([c[1].get("label") for c in claude[2] if c[1].get("type") != "separator"],
                         ["Login da pasta", "Empresa · do perfil", "Pessoal", "Adicionar conta Claude…"])
        self.assertTrue(any(c[1].get("type") == "separator" for c in children))
        ids = [i for i, _ in walk(layout)]
        self.assertEqual(len(ids), len(set(ids)))
        version = call(self.client, tray.bus_name, "/MenuBar", "org.freedesktop.DBus.Properties", "Get",
                       GLib.Variant("(ss)", (MENU, "Version")))[0]
        self.assertEqual(version, 3)

    def test_sub_layout_and_group_properties(self):
        FakeWatcher(self)
        tray = self.start_tray()
        spin_until(lambda: self.availability)
        claude_id = self.item_id(tray, "Trocar só a conta Claude")
        _, sub = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetLayout",
                      GLib.Variant("(iias)", (claude_id, 1, ["label"])))
        self.assertEqual(sub[0], claude_id)
        self.assertEqual(sub[2][0][1], {"label": "Login da pasta"})
        quit_id = self.item_id(tray, "Sair do MultiCred")
        (props,) = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetGroupProperties",
                        GLib.Variant("(aias)", ([quit_id], [])))
        self.assertEqual(props, [(quit_id, {"label": "Sair do MultiCred"})])
        (label,) = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetProperty",
                        GLib.Variant("(is)", (quit_id, "label")))
        self.assertEqual(label, "Sair do MultiCred")
        self.assertEqual(call(self.client, tray.bus_name, "/MenuBar", MENU, "AboutToShow",
                              GLib.Variant("(i)", (0,))), (False,))

    def test_clicked_on_a_profile_switches_it(self):
        FakeWatcher(self)
        tray = self.start_tray()
        spin_until(lambda: self.availability)
        revision, _ = self.layout(tray)
        updates = []
        self.client.signal_subscribe(tray.bus_name, MENU, "LayoutUpdated", "/MenuBar", None,
                                     Gio.DBusSignalFlags.NONE, lambda *a: updates.append(a[5].unpack()))
        self.click(tray, self.item_id(tray, "Pessoal"))
        spin_until(lambda: self.model.switched and updates)
        self.assertEqual(self.model.switched, [self.config.profiles[1].id])
        new_revision, layout = self.layout(tray)
        self.assertGreater(new_revision, revision)
        toggled = [props.get("label") for _, props in walk(layout) if props.get("toggle-state") == 1]
        self.assertIn("Pessoal", toggled)

    def test_other_actions(self):
        FakeWatcher(self)
        tray = self.start_tray()
        spin_until(lambda: self.availability)
        claude_id = self.item_id(tray, "Trocar só a conta Claude")
        _, sub = call(self.client, tray.bus_name, "/MenuBar", MENU, "GetLayout",
                      GLib.Variant("(iias)", (claude_id, 1, [])))
        own = next(c[0] for c in sub[2] if c[1].get("label") == "Login da pasta")
        self.click(tray, own)
        self.click(tray, self.item_id(tray, "Contas…"))
        self.click(tray, self.item_id(tray, "Sair do MultiCred"))
        spin_until(lambda: self.model.claude_switched and self.opened and self.quits)
        self.assertEqual(self.model.claude_switched, [None])
        self.assertEqual(self.opened, ["accounts"])
        call(self.client, tray.bus_name, "/StatusNotifierItem", SNI, "Activate", GLib.Variant("(ii)", (0, 0)))
        spin_until(lambda: len(self.opened) == 2)
        self.assertEqual(self.opened[-1], "main")

    def test_item_properties_icon_and_status(self):
        FakeWatcher(self)
        tray = self.start_tray()
        spin_until(lambda: self.availability)
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
        self.assertIn("⚠ Inconsistente — Corrigir", [p.get("label") for _, p in walk(layout)])

    def test_without_a_watcher_the_tray_is_unavailable(self):
        tray = self.start_tray()
        spin_until(lambda: self.availability)
        self.assertEqual(self.availability, [False])
        self.assertFalse(tray.available)

    def test_registers_again_when_a_watcher_appears_later(self):
        tray = self.start_tray()
        spin_until(lambda: self.availability)
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


if __name__ == "__main__":
    unittest.main()
