"""S-14: the .deb for Termux (Android) is built with $PREFIX paths and the CLI it carries runs (R-13).

The package is built once into a temporary directory and inspected with dpkg-deb
(never installed: no apt, no dpkg -i). The CLI is then run FROM THE EXTRACTED
PACKAGE in a simulated Termux: a temporary HOME, TERMUX_VERSION set, PREFIX pointing
at a temporary prefix that mirrors the package's `data/data/com.termux/files/usr`,
PATH = $PREFIX/bin only, and in $PREFIX/bin nothing but python3 (run with -S, so
this machine's PyGObject is out of reach, as on Termux), sh and stubs that record
any attempt to run systemctl, secret-tool or dbus-send.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from multicred import VERSION
from multicred.core import real_env
from multicred.core.models import ActiveState, Config, GitIdentity, Profile, ProfileColor
from multicred.core.paths import AppPaths
from multicred.core.storage import ConfigRepository, StateStore
from tests.support import SafeTestCase
from tests.test_termux import TERMUX_VERSION

LINUX = Path(__file__).resolve().parent.parent
BUILD = LINUX / "packaging" / "build-deb-termux.sh"
TERMUX_PREFIX = "data/data/com.termux/files/usr"
SHEBANG = "#!/data/data/com.termux/files/usr/bin/sh\n"
HAS_TOOLS = all(shutil.which(tool) for tool in ("dpkg-deb", "fakeroot"))
# Modules that only the desktop window and tray use; none ships to Termux.
DESKTOP_ONLY = ("multicred/tray/", "multicred/ui/pages/", "multicred/ui/icons/", "multicred/ui/app.py",
                "multicred/ui/window.py", "multicred/ui/widgets.py", "multicred/ui/model.py")
FORBIDDEN_STUB = "#!/bin/sh\necho \"$0 $*\" >> '{marker}'\nexit 97\n"

_built = {}


def built_package():
    """Builds and extracts the package once per test run."""
    if not _built:
        out = Path(tempfile.mkdtemp(prefix="multicred-termux-deb-"))
        result = subprocess.run([str(BUILD), str(out / "dist")], capture_output=True, text=True, timeout=180)
        deb = out / "dist" / f"multicred_{VERSION}_all.deb"
        root, control = out / "root", out / "control"
        if deb.exists():
            subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
            subprocess.run(["dpkg-deb", "-e", str(deb), str(control)], check=True)
        _built.update(out=out, deb=deb, root=root, control=control, output=result.stdout + result.stderr,
                      returncode=result.returncode)
    return _built


def tearDownModule():
    if _built:
        shutil.rmtree(_built["out"], True)


def ar_members(path: Path) -> list[str]:
    """Member names of an ar archive (a .deb): debian-binary, control.tar.*, data.tar.*."""
    data = path.read_bytes()
    assert data[:8] == b"!<arch>\n", "not an ar archive"
    names, offset = [], 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        names.append(header[:16].decode().strip().rstrip("/"))
        size = int(header[48:58].decode().strip())
        offset += 60 + size + (size % 2)
    return names


@unittest.skipUnless(HAS_TOOLS, "dpkg-deb or fakeroot not installed")
class TermuxPackagingTests(SafeTestCase):
    @classmethod
    def setUpClass(cls):
        built = built_package()
        cls.deb, cls.root, cls.control = built["deb"], built["root"], built["control"]
        if not cls.deb.exists():
            raise AssertionError(f"package not built (exit {built['returncode']}):\n{built['output']}")
        cls.prefix_root = cls.root / TERMUX_PREFIX

    def listing(self):
        """(mode, owner, path) of every entry, as `dpkg-deb --contents` prints them."""
        text = subprocess.run(["dpkg-deb", "--contents", str(self.deb)], capture_output=True, text=True).stdout
        entries = []
        for line in text.splitlines():
            parts = line.split()
            entries.append((parts[0], parts[1], parts[5].lstrip(".") or "/"))
        return entries

    def contents(self):
        return [path for _mode, _owner, path in self.listing()]

    def field(self, name):
        return subprocess.run(["dpkg-deb", "--field", str(self.deb), name], capture_output=True,
                              text=True).stdout.strip()

    # MARK: the file and its control fields

    def test_named_after_the_version_with_xz_members_termux_can_read(self):
        self.assertEqual(self.deb.name, f"multicred_{VERSION}_all.deb")
        self.assertEqual(ar_members(self.deb), ["debian-binary", "control.tar.xz", "data.tar.xz"])

    def test_control_fields(self):
        self.assertEqual(self.field("Package"), "multicred")
        self.assertEqual(self.field("Version"), VERSION)
        self.assertEqual(self.field("Architecture"), "all", "pure Python: one package for every device")
        self.assertEqual([d.strip() for d in self.field("Depends").split(",")], ["python", "git"])
        # gh for the GitHub accounts, nodejs-lts for Claude Code (npm), util-linux for setsid
        # (the logins run in a pseudo-terminal through setsid).
        self.assertEqual([d.strip() for d in self.field("Recommends").split(",")], ["gh", "nodejs-lts", "util-linux"])
        self.assertRegex(self.field("Installed-Size"), r"^\d+$")
        self.assertTrue(self.field("Maintainer"))
        self.assertEqual(self.field("Homepage"), "https://github.com/Rafael-Leivas/MultiCred")
        description = self.field("Description")
        self.assertIn("Termux", description)
        for desktop_only in ("gir1.2", "python3-gi", "libsecret", "gtk"):
            self.assertNotIn(desktop_only, self.field("Depends") + self.field("Recommends"))

    def test_no_maintainer_scripts(self):
        """`apt install ./multicred_<v>_all.deb` runs nothing: `multicred configurar` does the setup."""
        self.assertEqual(sorted(p.name for p in self.control.iterdir()), ["control", "md5sums"])

    # MARK: what it carries

    def test_every_file_lives_under_the_termux_prefix(self):
        files = self.contents()
        prefix = f"/{TERMUX_PREFIX}/"
        outside = [f for f in files if not (f.startswith(prefix) or prefix.startswith(f))]
        self.assertEqual(outside, [], "only $PREFIX (and its parent directories) is installed")
        for path in ("bin/multicred", "lib/multicred/multicred/__main__.py", "lib/multicred/multicred/cli.py",
                     "lib/multicred/multicred/cli_menus.py", "lib/multicred/multicred/core/platform.py",
                     "lib/multicred/multicred/core/file_secrets.py",
                     "lib/multicred/multicred/core/claude/termux_support.py",
                     "lib/multicred/multicred/ui/messages.py", "lib/multicred/multicred/ui/feedback.py",
                     "share/doc/multicred/copyright", "share/doc/multicred/README.md"):
            self.assertIn(f"/{TERMUX_PREFIX}/{path}", files)

    def test_no_desktop_integration_tests_or_bytecode(self):
        files = self.contents()
        lib = f"/{TERMUX_PREFIX}/lib/multicred/"
        self.assertEqual([f for f in files if f.startswith(lib) and any(f[len(lib):].startswith(d)
                                                                        for d in DESKTOP_ONLY)], [])
        self.assertEqual([f for f in files if "/tests/" in f or "__pycache__" in f or f.endswith((".pyc", ".pyo"))],
                         [])
        for desktop in ("/share/applications/", "/share/icons/", "/share/metainfo/", "/etc/xdg/autostart",
                        "/usr/bin/", "/usr/lib/"):
            self.assertEqual([f for f in files if desktop in f.replace(f"/{TERMUX_PREFIX}", "")], [], desktop)

    def test_readme_is_the_termux_one(self):
        shipped = (self.prefix_root / "share/doc/multicred/README.md").read_text()
        self.assertEqual(shipped, (LINUX / "README-termux.md").read_text())

    def test_launcher_has_the_termux_shebang_and_runs_python_from_the_prefix(self):
        launcher = (self.prefix_root / "bin/multicred").read_text()
        self.assertTrue(launcher.startswith(SHEBANG), launcher[:80])
        self.assertIn("/bin/python3", launcher)
        self.assertIn("lib/multicred", launcher)
        for desktop in ("/usr/bin/", "/usr/lib/", "/tmp"):
            self.assertNotIn(desktop, launcher.replace(f"/{TERMUX_PREFIX}", ""))

    def test_permissions_and_root_ownership(self):
        wrong = []
        for mode, owner, path in self.listing():
            if owner != "root/root":
                wrong.append(f"{path} owned by {owner}")
            expected = ("drwxr-xr-x" if mode.startswith("d") else
                        "-rwxr-xr-x" if path == f"/{TERMUX_PREFIX}/bin/multicred" else "-rw-r--r--")
            if mode != expected:
                wrong.append(f"{path}: {mode}, expected {expected}")
        self.assertEqual(wrong, [])

    def test_md5sums_cover_every_file(self):
        listed = {}
        for line in (self.control / "md5sums").read_text().splitlines():
            digest, name = line.split(None, 1)
            listed[name.strip()] = digest
        regular = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(sorted(listed), regular)
        for name, digest in listed.items():
            self.assertEqual(hashlib.md5((self.root / name).read_bytes()).hexdigest(), digest, name)

    # MARK: it runs in a simulated Termux

    def simulated_termux(self):
        """A temporary prefix mirroring the package's, plus the few programs Termux would have."""
        work = Path(tempfile.mkdtemp(prefix="MultiCredTermuxPkg-")).resolve()
        self.addCleanup(shutil.rmtree, work, True)
        prefix = work / "usr"
        shutil.copytree(self.prefix_root, prefix, symlinks=True)
        (prefix / "tmp").mkdir()
        bin_dir = prefix / "bin"
        # Termux's python3; -S keeps this machine's dist-packages (PyGObject) out, as on Termux.
        (bin_dir / "python3").write_text(f"#!/bin/sh\nexec '{sys.executable}' -S \"$@\"\n")
        (bin_dir / "python3").chmod(0o755)
        (bin_dir / "sh").symlink_to(shutil.which("sh"))
        forbidden = work / "forbidden.log"
        for name in ("systemctl", "secret-tool", "dbus-send"):
            (bin_dir / name).write_text(FORBIDDEN_STUB.format(marker=forbidden))
            (bin_dir / name).chmod(0o755)
        environ = {"TERMUX_VERSION": TERMUX_VERSION, "PREFIX": str(prefix), "HOME": str(self.home),
                   "TMPDIR": str(prefix / "tmp"), "PATH": str(bin_dir), "SHELL": str(bin_dir / "bash"),
                   "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        return prefix, environ, forbidden

    def run_cli(self, prefix, environ, *argv, inp=""):
        # The shebang names the real Termux prefix, which this machine does not have: the
        # launcher goes through the simulated prefix's sh, as the kernel would through its own.
        return subprocess.run([str(prefix / "bin/sh"), str(prefix / "bin/multicred"), *argv], input=inp,
                              capture_output=True, text=True, timeout=60, env=environ, cwd=str(self.home))

    def seed_profile(self, environ):
        termux = real_env.make_termux(environ)
        identity = GitIdentity(nickname="Celular", name="Dev", email="dev@celular.example", color=ProfileColor.GREEN)
        profile = Profile(name="Celular", color=ProfileColor.GREEN, git_identity_id=identity.id)
        ConfigRepository(termux).save(Config(git_identities=[identity], profiles=[profile]))
        StateStore(AppPaths(termux), termux.clock).commit(ActiveState(profile_id=profile.id))
        return termux

    def test_extracted_cli_prints_its_version(self):
        prefix, environ, forbidden = self.simulated_termux()
        result = self.run_cli(prefix, environ, "--version")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"MultiCred {VERSION}"), result.stderr)
        self.assertFalse(forbidden.exists())

    def test_extracted_cli_shows_the_status_in_a_simulated_termux(self):
        prefix, environ, forbidden = self.simulated_termux()
        result = self.run_cli(prefix, environ, "status")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "MultiCred ainda não foi configurado.\nPara configurar: multicred configurar\n")
        self.seed_profile(environ)
        result = self.run_cli(prefix, environ, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "Celular")
        self.assertIn("git dev@celular.example", lines)
        self.assertIn("Claude · ~/.claude · login da pasta", lines)
        self.assertFalse(forbidden.exists(), forbidden.read_text() if forbidden.exists() else "")
        self.assertFalse(list(self.home.rglob("__pycache__")))

    def test_without_a_command_it_shows_the_help_not_a_window(self):
        prefix, environ, _ = self.simulated_termux()
        result = self.run_cli(prefix, environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: multicred", result.stdout)
        self.assertIn("desinstalar", result.stdout)

    def test_extracted_cli_lists_verifies_and_uninstalls_without_leaving_the_prefix(self):
        prefix, environ, forbidden = self.simulated_termux()
        self.seed_profile(environ)
        for argv in (["perfis"], ["claude"], ["contas"], ["verificar"]):
            result = self.run_cli(prefix, environ, *argv)
            self.assertNotIn("Traceback", result.stderr, argv)
            self.assertIn(result.returncode, (0, 1), (argv, result.stderr))
        self.assertIn("Celular  ✓", self.run_cli(prefix, environ, "perfis").stdout)
        result = self.run_cli(prefix, environ, "desinstalar", inp="n\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Nada foi alterado.", result.stdout)
        self.assertTrue((self.home / ".config/multicred").exists())
        result = self.run_cli(prefix, environ, "desinstalar", inp="s\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MultiCred desinstalado", result.stdout)
        self.assertIn("apt remove multicred", result.stdout)
        self.assertNotIn("sudo", result.stdout)
        self.assertFalse((self.home / ".config/multicred").exists())
        self.assertFalse(forbidden.exists(), forbidden.read_text() if forbidden.exists() else "")

    def test_no_shipped_module_needs_pygobject_at_import(self):
        """Termux has no PyGObject: every module in the package imports without it (the
        desktop's Secret Service store is the one exception, and make_termux never loads it)."""
        lib = self.prefix_root / "lib/multicred"
        modules = sorted(".".join(p.relative_to(lib).with_suffix("").parts) for p in lib.rglob("*.py"))
        modules = [m.removesuffix(".__init__") for m in modules
                   if m not in ("multicred.core.secret_service", "multicred.__main__")]
        code = ("import importlib, sys\nsys.path.insert(0, sys.argv[1])\nfor name in sys.argv[2:]:\n"
                "    importlib.import_module(name)\nprint('gi' in sys.modules)\n")
        result = subprocess.run([sys.executable, "-S", "-P", "-c", code, str(lib), *modules], capture_output=True,
                                text=True, timeout=60, env={"PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "False"), result.stderr)


if __name__ == "__main__":
    unittest.main()
