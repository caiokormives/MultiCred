"""Secret storage: the in-memory contract, and the real Secret Service on a private keyring.

The real test starts its own gnome-keyring-daemon (temporary HOME) on the private
bus that tests/run-ui-tests.sh creates, and only runs when that bus is in use
(MULTICRED_PRIVATE_BUS=1). It never talks to the user's keyring.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from multicred.core.secrets import API_KEY_SERVICE, EmptySecret, InMemorySecretStore, InvalidAccount


def new_account():
    return str(uuid.uuid4()).upper()


class InMemorySecretStoreTests(unittest.TestCase):
    def test_contract(self):
        store, account = InMemorySecretStore(), new_account()
        self.assertFalse(store.contains_item(account))
        store.set_secret(b"sk-1", account)
        store.set_secret(b"sk-2", account)
        self.assertTrue(store.contains_item(account))
        self.assertEqual(store.stored_value(account), b"sk-2")
        self.assertEqual(store.all_accounts(), [account])
        store.delete_item(account)
        store.delete_item(account)
        self.assertEqual(store.all_accounts(), [])

    def test_rejects_empty_secrets_and_non_uuid_accounts(self):
        store = InMemorySecretStore()
        with self.assertRaises(EmptySecret):
            store.set_secret(b"", new_account())
        with self.assertRaises(InvalidAccount):
            store.set_secret(b"x", "x; rm -rf ~")
        with self.assertRaises(InvalidAccount):
            store.set_secret(b"x", str(uuid.uuid4()))  # lower case is not what the app generates


@unittest.skipUnless(os.environ.get("MULTICRED_PRIVATE_BUS") == "1" and shutil.which("gnome-keyring-daemon"),
                     "needs tests/run-ui-tests.sh (private bus) and gnome-keyring-daemon")
class RealSecretServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="multicred-keyring-")
        run = os.path.join(cls.home, "run")
        os.makedirs(run, mode=0o700)
        env = dict(os.environ, HOME=cls.home, XDG_DATA_HOME=os.path.join(cls.home, ".local/share"), XDG_RUNTIME_DIR=run)
        cls.daemon = subprocess.Popen(
            ["gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets", f"--control-directory={run}"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        cls.daemon.stdin.write(b"test-password")
        cls.daemon.stdin.close()
        from gi.repository import Gio
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        deadline = time.time() + 10
        while time.time() < deadline:
            owned = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "NameHasOwner", __import__("gi.repository.GLib", fromlist=["Variant"]).Variant(
                                      "(s)", ("org.freedesktop.secrets",)), None, Gio.DBusCallFlags.NONE, -1, None)
            if owned.unpack()[0]:
                break
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.daemon.terminate()
        cls.daemon.wait(5)
        shutil.rmtree(cls.home, True)

    def test_store_check_and_delete_with_the_attributes_secret_tool_uses(self):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        from multicred.core.secret_service import SecretServiceStore

        store, account = SecretServiceStore(), new_account()
        self.assertFalse(store.contains_item(account))
        store.set_secret(b"sk-real", account)
        store.set_secret(b"sk-real-2", account)
        self.assertTrue(store.contains_item(account))
        self.assertIn(account, store.all_accounts())
        # What `secret-tool lookup service app.multicred.apikey account <id>` does: a schema-less lookup.
        found = Secret.password_lookup_sync(None, {"service": API_KEY_SERVICE, "account": account}, None)
        self.assertEqual(found, "sk-real-2")
        store.delete_item(account)
        store.delete_item(account)
        self.assertFalse(store.contains_item(account))

    def test_vault_backend_reads_back_what_it_stores(self):
        from multicred.core.secret_service import SecretServiceBackend
        backend, key = SecretServiceBackend("app.multicred.claude-login"), new_account()
        self.assertIsNone(backend.lookup(key))
        backend.store(key, b'{"claudeAiOauth": {"accessToken": "a"}}', label="MultiCred: login")
        self.assertEqual(backend.lookup(key), b'{"claudeAiOauth": {"accessToken": "a"}}')
        backend.clear(key)
        self.assertIsNone(backend.lookup(key))
