"""The .deb for Ubuntu and Pop!_OS is built and carries the app and its dependencies."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from multicred import VERSION

LINUX = Path(__file__).resolve().parent.parent
BUILD = LINUX / "packaging" / "build-deb.sh"


@unittest.skipUnless(shutil.which("dpkg-deb") and shutil.which("fakeroot"), "dpkg-deb/fakeroot not installed")
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="multicred-deb-"))
        result = subprocess.run([str(BUILD), str(cls.out)], capture_output=True, text=True, timeout=120)
        cls.build_output = result.stdout + result.stderr
        cls.deb = cls.out / f"multicred_{VERSION}_all.deb"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True)

    def contents(self):
        listing = subprocess.run(["dpkg-deb", "--contents", str(self.deb)], capture_output=True, text=True).stdout
        return [line.split()[-1].lstrip(".") for line in listing.splitlines()]

    def field(self, name):
        return subprocess.run(["dpkg-deb", "--field", str(self.deb), name], capture_output=True, text=True).stdout.strip()

    def test_package_is_built(self):
        self.assertTrue(self.deb.exists(), self.build_output)

    def test_package_carries_the_app_and_the_command(self):
        files = self.contents()
        self.assertIn("/usr/bin/multicred", files)
        self.assertIn("/usr/lib/multicred/multicred/__main__.py", files)
        self.assertIn("/usr/lib/multicred/multicred/core/engine/switch_engine.py", files)
        self.assertFalse([f for f in files if "/tests/" in f or "__pycache__" in f], "no tests or bytecode")

    def test_dependencies_cover_gtk_adwaita_and_secrets(self):
        depends = self.field("Depends")
        for package in ["python3", "python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1", "gir1.2-secret-1",
                        "libsecret-tools", "git"]:
            self.assertIn(package, depends)
        self.assertIn("gh", self.field("Recommends"))
        self.assertEqual(self.field("Architecture"), "all")
        self.assertEqual(self.field("Version"), VERSION)

    def test_command_runs_from_the_extracted_package(self):
        root = self.out / "root"
        subprocess.run(["dpkg-deb", "-x", str(self.deb), str(root)], check=True)
        launcher = (root / "usr/bin/multicred").read_text()
        self.assertIn("/usr/lib/multicred", launcher)
        result = subprocess.run(["python3", "-m", "multicred", "--version"], cwd=root / "usr/lib/multicred",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout.strip(), f"MultiCred {VERSION}")
