"""S-10: the .deb for Ubuntu and Pop!_OS is built, lintian-friendly, and the app it carries runs.

PackagingTests build the package once into a temporary directory and inspect it
with dpkg-deb (never installed: no sudo, no dpkg -i). PackagedAppUITests start
the app from the extracted package in test mode and render the main pages; like
every display test they run only under tests/run-ui-tests.sh (private Xvfb and
private bus). With MULTICRED_SCREENS_DIR set the screenshots go there
(S-10-*.png), otherwise to a temporary directory.
"""

import configparser
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from multicred import APP_ID, VERSION

LINUX = Path(__file__).resolve().parent.parent
BUILD = LINUX / "packaging" / "build-deb.sh"
DRIVER = Path(__file__).resolve().parent / "packaged_app_driver.py"
LOG_LINE = re.compile(r"\b(Gtk|Adw|Adwaita|Gdk|Gsk|GLib-GObject|GLib|Gio)-(WARNING|CRITICAL)\b")
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
# lintian on PATH, or MULTICRED_LINTIAN=<command> (e.g. a wrapper running an unpacked lintian).
LINTIAN = os.environ.get("MULTICRED_LINTIAN") or shutil.which("lintian")
HAS_TOOLS = all(shutil.which(tool) for tool in ("dpkg-deb", "fakeroot", "dpkg-parsechangelog"))

_built = {}


def built_package():
    """Builds and extracts the package once per test run: (deb, root, control dir, build output)."""
    if not _built:
        out = Path(tempfile.mkdtemp(prefix="multicred-deb-"))
        result = subprocess.run([str(BUILD), str(out / "dist")], capture_output=True, text=True, timeout=180)
        deb = out / "dist" / f"multicred_{VERSION}_all.deb"
        root, control = out / "root", out / "control"
        if deb.exists():
            subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
            subprocess.run(["dpkg-deb", "-e", str(deb), str(control)], check=True)
        _built.update(out=out, deb=deb, root=root, control=control, output=result.stdout + result.stderr)
    return _built


def tearDownModule():
    if _built:
        shutil.rmtree(_built["out"], True)


def child_environment() -> dict:
    """This environment without PYTHONPATH (only the extracted tree may provide `multicred`)
    and without writing bytecode into the extracted tree (md5sums are checked against it)."""
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG")
    return struct.unpack(">II", data[16:24])


@unittest.skipUnless(HAS_TOOLS, "dpkg-deb, fakeroot or dpkg-parsechangelog not installed")
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        built = built_package()
        cls.deb, cls.root, cls.control = built["deb"], built["root"], built["control"]
        cls.build_output = built["output"]
        if not cls.deb.exists():
            raise AssertionError(f"package not built:\n{cls.build_output}")

    def listing(self):
        """(mode, owner, path) of every entry, as `dpkg-deb --contents` prints them."""
        text = subprocess.run(["dpkg-deb", "--contents", str(self.deb)], capture_output=True, text=True).stdout
        entries = []
        for line in text.splitlines():
            parts = line.split()
            path = parts[5].lstrip(".") or "/"
            entries.append((parts[0], parts[1], path))
        return entries

    def contents(self):
        return [path for _mode, _owner, path in self.listing()]

    def field(self, name):
        return subprocess.run(["dpkg-deb", "--field", str(self.deb), name], capture_output=True, text=True).stdout.strip()

    # MARK: what it carries

    def test_package_carries_the_app_and_the_command(self):
        files = self.contents()
        self.assertIn("/usr/bin/multicred", files)
        self.assertIn("/usr/lib/multicred/multicred/__main__.py", files)
        self.assertIn("/usr/lib/multicred/multicred/core/engine/switch_engine.py", files)
        self.assertIn("/usr/lib/multicred/multicred/tray/controller.py", files)
        self.assertIn("/usr/lib/multicred/multicred/ui/window.py", files)
        for icon in ("branch", "code", "sparkle"):  # S-01: the app's own sidebar icons ship
            self.assertIn(f"/usr/lib/multicred/multicred/ui/icons/hicolor/scalable/actions/multicred-{icon}-symbolic.svg",
                          files)

    def test_no_tests_bytecode_or_build_leftovers(self):
        files = self.contents()
        self.assertEqual([f for f in files if "/tests/" in f or "__pycache__" in f or f.endswith((".pyc", ".pyo"))],
                         [], "no tests or bytecode")
        self.assertEqual([f for f in files if not f.startswith("/usr")], ["/"], "only /usr is installed")

    def test_icon_at_every_launcher_size_and_scalable(self):
        files = self.contents()
        self.assertIn(f"/usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg", files)
        for size in ICON_SIZES:
            path = f"/usr/share/icons/hicolor/{size}x{size}/apps/{APP_ID}.png"
            self.assertIn(path, files)
            self.assertEqual(png_size(self.root / path.lstrip("/")), (size, size), path)

    def test_icon_is_drawn_not_blank(self):
        """The 48 px icon is drawn: transparent corner, opaque indigo background."""
        try:
            import cairo
        except ImportError:
            self.skipTest("pycairo not installed")
        surface = cairo.ImageSurface.create_from_png(str(self.root / f"usr/share/icons/hicolor/48x48/apps/{APP_ID}.png"))
        data, stride = bytes(surface.get_data()), surface.get_stride()

        def pixel(x, y):
            b, g, r, a = data[y * stride + 4 * x: y * stride + 4 * x + 4]
            return r, g, b, a

        self.assertEqual(pixel(0, 0)[3], 0, "transparent corner")
        r, g, b, a = pixel(6, 24)  # left edge inside the rounded square
        self.assertEqual(a, 255)
        self.assertTrue(b > r and b > g, f"indigo background, got {(r, g, b)}")

    def test_desktop_entry(self):
        path = self.root / f"usr/share/applications/{APP_ID}.desktop"
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "MultiCred")
        self.assertEqual(entry["Comment"], "Troca de identidade de dev com um clique")
        self.assertEqual(entry["Exec"], "multicred")
        self.assertEqual(entry["Icon"], APP_ID)
        self.assertEqual(entry["StartupWMClass"], APP_ID, "the GTK application id")
        self.assertEqual(entry["Terminal"], "false")
        self.assertIn("Development", entry["Categories"])
        if shutil.which("desktop-file-validate"):
            result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("error:", result.stdout + result.stderr)

    def test_appstream_metainfo(self):
        path = self.root / f"usr/share/metainfo/{APP_ID}.metainfo.xml"
        component = ET.parse(path).getroot()
        self.assertEqual(component.tag, "component")
        self.assertEqual(component.get("type"), "desktop-application")
        self.assertEqual(component.findtext("id"), APP_ID)
        self.assertEqual(component.findtext("name"), "MultiCred")
        self.assertTrue(component.findtext("summary"))
        self.assertTrue(component.find("description").findall("p"))
        launchable = component.find("launchable")
        self.assertEqual((launchable.get("type"), launchable.text), ("desktop-id", f"{APP_ID}.desktop"))
        self.assertEqual(component.find("content_rating").get("type"), "oars-1.1")
        self.assertIsNone(component.find("screenshots"))
        self.assertEqual([r.get("version") for r in component.find("releases").findall("release")][0], VERSION)
        if shutil.which("appstreamcli"):
            result = subprocess.run(["appstreamcli", "validate", "--no-net", "--explain", str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_copyright_and_changelog(self):
        doc = self.root / "usr/share/doc/multicred"
        copyright_text = (doc / "copyright").read_text()
        self.assertTrue(copyright_text.startswith(
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"))
        self.assertIn("Files: *", copyright_text)
        # Same license as the repository's LICENSE file.
        self.assertIn("License: MIT", copyright_text)
        self.assertIn("Permission is hereby granted, free of charge", copyright_text)
        # A native version (0.1.0) gets changelog.gz, as lintian expects.
        changelog = gzip.decompress((doc / "changelog.gz").read_bytes()).decode()
        self.assertTrue(changelog.startswith(f"multicred ({VERSION}) "), changelog[:80])
        self.assertRegex(changelog, r"\n -- .+ <.+>  \w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}\n")
        self.assertEqual(sorted(p.name for p in doc.iterdir()), ["changelog.gz", "copyright"])

    def test_metainfo_declares_the_repository_license(self):
        component = ET.parse(self.root / f"usr/share/metainfo/{APP_ID}.metainfo.xml").getroot()
        self.assertEqual(component.findtext("project_license"), "MIT")

    def test_md5sums_cover_every_file(self):
        md5sums = (self.control / "md5sums").read_text().splitlines()
        listed = {}
        for line in md5sums:
            digest, name = line.split(None, 1)
            listed[name.strip()] = digest
        regular = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(sorted(listed), regular)
        for name, digest in listed.items():
            self.assertEqual(hashlib.md5((self.root / name).read_bytes()).hexdigest(), digest, name)

    def test_permissions_and_root_ownership(self):
        wrong = []
        for mode, owner, path in self.listing():
            if owner != "root/root":
                wrong.append(f"{path} owned by {owner}")
            if mode.startswith("d"):
                expected = "drwxr-xr-x"
            elif path == "/usr/bin/multicred":
                expected = "-rwxr-xr-x"
            else:
                expected = "-rw-r--r--"
            if mode != expected:
                wrong.append(f"{path}: {mode}, expected {expected}")
        self.assertEqual(wrong, [])

    def test_no_maintainer_scripts(self):
        """Per-user setup is the app's onboarding: nothing runs as root at install time."""
        self.assertEqual(sorted(p.name for p in self.control.iterdir()), ["control", "md5sums"])

    # MARK: control fields

    def test_dependencies_cover_gtk_adwaita_and_secrets(self):
        depends = [d.strip() for d in self.field("Depends").split(",")]
        self.assertEqual(depends, ["python3 (>= 3.12)", "python3-gi", "python3-gi-cairo", "gir1.2-gtk-4.0",
                                   "gir1.2-adw-1 (>= 1.5)", "gir1.2-secret-1", "libsecret-tools", "git"])
        self.assertEqual(self.field("Recommends"), "gh")
        self.assertEqual(self.field("Architecture"), "all")
        self.assertEqual(self.field("Version"), VERSION)
        self.assertEqual(self.field("Package"), "multicred")
        self.assertRegex(self.field("Installed-Size"), r"^\d+$")

    # MARK: it runs

    def test_command_runs_from_the_extracted_package(self):
        launcher = (self.root / "usr/bin/multicred").read_text()
        self.assertTrue(launcher.startswith("#!/bin/sh\n"))
        self.assertIn("/usr/lib/multicred", launcher)
        environment = child_environment()
        result = subprocess.run(["python3", "-m", "multicred", "--version"], cwd=self.root / "usr/lib/multicred",
                                capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.stdout.strip(), f"MultiCred {VERSION}", result.stderr)

    @unittest.skipUnless(LINTIAN, "lintian not installed")
    def test_lintian_reports_no_errors(self):
        result = subprocess.run([LINTIAN, "--no-tag-display-limit", str(self.deb)], capture_output=True, text=True,
                                timeout=300)
        print(f"\nlintian:\n{result.stdout}", end="")
        errors = [line for line in result.stdout.splitlines() if line.startswith("E:")]
        self.assertIn(result.returncode, (0, 1), result.stderr)  # 2 = lintian itself failed
        self.assertEqual(errors, [], result.stdout)


class IconRenderingTests(unittest.TestCase):
    """packaging/render-icons.py without librsvg: the cairo drawing of the same design."""

    def test_cairo_fallback_draws_every_size(self):
        try:
            import cairo
        except ImportError:
            self.skipTest("pycairo not installed")
        import importlib.util
        spec = importlib.util.spec_from_file_location("render_icons", LINUX / "packaging" / "render-icons.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._rsvg_renderer = lambda _svg: None
        with tempfile.TemporaryDirectory() as out:
            written = module.render(LINUX / "packaging" / f"{APP_ID}.svg", Path(out))
            self.assertEqual([png_size(p) for p in written], [(n, n) for n in ICON_SIZES])
            surface = cairo.ImageSurface.create_from_png(str(Path(out) / f"64x64/apps/{APP_ID}.png"))
            data, stride = bytes(surface.get_data()), surface.get_stride()
            b, g, r, a = data[32 * stride + 4 * 6: 32 * stride + 4 * 6 + 4]
            self.assertEqual(a, 255)
            self.assertTrue(b > r and b > g, f"indigo background, got {(r, g, b)}")
            self.assertEqual(data[3], 0, "transparent corner")


@unittest.skipUnless(HAS_TOOLS and os.environ.get("MULTICRED_PRIVATE_BUS") == "1",
                     "needs dpkg-deb and tests/run-ui-tests.sh (private display and bus)")
class PackagedAppUITests(unittest.TestCase):
    """Starts the app FROM THE EXTRACTED PACKAGE (not this checkout) in test mode, as a child
    process inside the runner's private Xvfb and bus, and renders each main page."""

    PAGES = ("perfis", "contas", "verificar", "ajustes", "sem-bandeja", "perfis-escuro")

    def setUp(self):
        built = built_package()
        if not built["deb"].exists():
            self.fail(f"package not built:\n{built['output']}")
        self.lib = built["root"] / "usr/lib/multicred"
        self.work = Path(tempfile.mkdtemp(prefix="multicred-packaged-ui-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.screens = Path(os.environ.get("MULTICRED_SCREENS_DIR") or self.work / "screens")

    def test_extracted_app_opens_in_test_mode_and_every_page_renders(self):
        sandbox = self.work / "sandbox"
        environment = child_environment()
        # Screenshots compare with the stock Adwaita designs: keep the desktop's own
        # ~/.config/gtk-4.0/gtk.css out of the child (as the S-08 screenshots do).
        environment["XDG_CONFIG_HOME"] = str(self.work / "xdg-config")
        result = subprocess.run(
            ["python3", str(DRIVER), "--lib", str(self.lib), "--sandbox", str(sandbox), "--screens", str(self.screens)],
            capture_output=True, text=True, timeout=120, env=environment, cwd=self.work)
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(lines[-1])

        self.assertTrue(report["module"].startswith(str(self.lib)), "the app came from the extracted package")
        self.assertTrue(report["test_mode"])
        self.assertTrue(report["sandbox_home"].startswith(str(sandbox.resolve())))
        self.assertTrue(report["mapped"], "the window was on screen")
        self.assertEqual(report["prgname"], APP_ID, "X11 WM_CLASS matches the .desktop's StartupWMClass")
        self.assertEqual(report["sidebar"], ["Visão geral", "Trabalho", "Pessoal", "Cliente X", "Novo perfil", "Git",
                                             "GitHub", "Claude", "Chaves de API", "Verificar", "Ajustes"])
        self.assertTrue(report["tray_banner"], "no StatusNotifierWatcher on the private bus: the banner shows")
        self.assertEqual(report["titles"], {"perfis": "Visão geral", "contas": "Claude", "verificar": "Verificar",
                                            "ajustes": "Ajustes"})
        self.assertEqual(report["active"], "Trabalho", "the first profile was switched to (dry run)")
        warnings = [line for line in result.stderr.splitlines() if LOG_LINE.search(line)]
        self.assertEqual(warnings, [], "no GTK/libadwaita warnings")
        for page in self.PAGES:
            path = self.screens / f"S-10-{page}.png"
            self.assertTrue(path.exists(), path)
            width, height = png_size(path)
            self.assertGreaterEqual(width, 800, path)
            self.assertGreaterEqual(height, 600, path)
        self.assertFalse(any(re.search(r"Traceback", line) for line in result.stderr.splitlines()), result.stderr)


if __name__ == "__main__":
    unittest.main()
