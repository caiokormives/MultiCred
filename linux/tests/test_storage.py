import json
import os

from multicred.core import storage
from multicred.core.models import ActiveState
from multicred.core.paths import AppPaths
from multicred.core.storage import (
    AtomicFileWriter, Corrupt, JSONFileStore, StateStore, UnsafeTarget, UnsupportedVersion,
)
from tests.support import FIXED_NOW, SafeTestCase


class Sample:
    current_version = 1

    def __init__(self, name, version=1):
        self.name, self.version = name, version

    def to_dict(self):
        return {"version": self.version, "name": self.name}

    @classmethod
    def from_dict(cls, data):
        return cls(data["name"], data["version"])

    def __eq__(self, other):
        return (self.name, self.version) == (other.name, other.version)


class AtomicFileWriterTests(SafeTestCase):
    def test_creates_file_with_requested_permissions(self):
        path = self.home / "a/b/file.txt"
        AtomicFileWriter.write(b"one", path, permissions=0o600)
        self.assertEqual(self.read(path), "one")
        self.assertEqual(self.mode_of(path), 0o600)

    def test_replacing_keeps_existing_permissions_and_leaves_no_temp_files(self):
        path = self.home / "rc"
        path.write_text("old")
        path.chmod(0o640)
        AtomicFileWriter.write(b"new", path, permissions=0o600)
        self.assertEqual(self.read(path), "new")
        self.assertEqual(self.mode_of(path), 0o640)
        self.assertEqual([p for p in os.listdir(self.home) if "multicred-" in p], [])

    def test_refuses_symlink_unless_asked_to_follow(self):
        real = self.home / "dotfiles/bashrc"
        real.parent.mkdir()
        real.write_text("real")
        link = self.home / ".bashrc"
        link.symlink_to(real)
        with self.assertRaises(UnsafeTarget):
            AtomicFileWriter.write(b"x", link)
        written = AtomicFileWriter.write(b"edited", link, follow_symlink=True)
        self.assertEqual(written, real.resolve())
        self.assertEqual(self.read(real), "edited")
        self.assertTrue(link.is_symlink(), "the link itself must survive")

    def test_refuses_broken_symlink_and_directories(self):
        link = self.home / ".bashrc"
        link.symlink_to(self.home / "missing")
        with self.assertRaises(UnsafeTarget):
            AtomicFileWriter.write(b"x", link, follow_symlink=True)
        directory = self.home / "dir"
        directory.mkdir()
        with self.assertRaises(UnsafeTarget):
            AtomicFileWriter.write(b"x", directory)

    def test_restore_removes_a_file_that_did_not_exist(self):
        path = self.home / "new"
        path.write_text("x")
        storage.restore_file(None, path)
        self.assertFalse(path.exists())
        storage.restore_file(b"back", path)
        self.assertEqual(self.read(path), "back")


class JSONFileStoreTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.environment.app_support_directory / "config.json"
        self.store = JSONFileStore(self.path, Sample, clock=self.environment.clock)

    def test_missing_file_loads_as_none(self):
        self.assertIsNone(self.store.load())

    def test_round_trip_with_private_permissions(self):
        document = Sample('Trabalho "NPU" — ç')
        self.store.save(document)
        self.assertEqual(self.store.load(), document)
        self.assertEqual(self.mode_of(self.path), 0o600)
        self.assertEqual(self.mode_of(self.environment.app_support_directory), 0o700)
        self.assertIn("ç", self.read(self.path), "UTF-8, not \\u escapes")

    def test_corrupt_file_is_moved_aside_and_reported(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json")
        with self.assertRaises(Corrupt) as raised:
            self.store.load()
        self.assertTrue(raised.exception.moved_to.endswith("config.json.corrupt-20260921-141320"))
        self.assertTrue(os.path.exists(raised.exception.moved_to))
        self.assertFalse(self.path.exists())

    def test_newer_version_is_never_overwritten_or_moved(self):
        self.path.parent.mkdir(parents=True)
        newer = '{"version": 2, "name": "x", "future": true}'
        self.path.write_text(newer)
        with self.assertRaises(UnsupportedVersion) as raised:
            self.store.load()
        self.assertEqual((raised.exception.found, raised.exception.supported), (2, 1))
        self.assertEqual(self.read(self.path), newer)


class StateStoreTests(SafeTestCase):
    def setUp(self):
        super().setUp()
        self.paths = AppPaths(self.environment)
        self.store = StateStore(self.paths, clock=self.environment.clock)

    def test_missing_state_is_empty(self):
        self.assertEqual(self.store.load(), ActiveState())

    def test_commit_writes_state_then_generation(self):
        state = ActiveState(activated_at=FIXED_NOW, generation=7)
        self.store.commit(state)
        self.assertEqual(self.read(self.paths.generation), "7\n")
        self.assertEqual(json.loads(self.read(self.paths.state))["generation"], 7)
        self.assertEqual(self.store.load(), state)

    def test_snapshot_and_restore_are_exact(self):
        snapshot = self.store.snapshot()
        self.store.commit(ActiveState(generation=1))
        self.store.restore(snapshot)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.generation.exists())
