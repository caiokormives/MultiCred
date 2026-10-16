"""The real Secret Service (GNOME Keyring on Ubuntu and Pop!_OS), through libsecret.

Items use the same attributes `secret-tool` uses (`service`, `account`) under the
generic schema, so the shell hook's `secret-tool lookup service … account …`
finds exactly what the app stored.
"""

from __future__ import annotations

import gi

gi.require_version("Secret", "1")
from gi.repository import GLib, Secret  # noqa: E402

from .secrets import API_KEY_SERVICE, EmptySecret, SecretServiceUnavailable, validate_account  # noqa: E402

_SCHEMA = Secret.Schema.new(
    "org.freedesktop.Secret.Generic",
    Secret.SchemaFlags.DONT_MATCH_NAME,
    {"service": Secret.SchemaAttributeType.STRING, "account": Secret.SchemaAttributeType.STRING},
)


class SecretServiceBackend:
    """Raw store/lookup/clear for one `service`. Lookup exists only for MultiCred's
    own login vault (D39); API keys go through SecretServiceStore, which cannot read."""

    def __init__(self, service: str):
        self.service = service

    def _attributes(self, account: str) -> dict[str, str]:
        return {"service": self.service, "account": account}

    def store(self, account: str, value: bytes, label: str) -> None:
        try:
            Secret.password_store_sync(_SCHEMA, self._attributes(account), Secret.COLLECTION_DEFAULT, label,
                                       value.decode("utf-8"), None)
        except GLib.Error as error:
            raise SecretServiceUnavailable(error.message) from error

    def lookup(self, account: str) -> bytes | None:
        try:
            value = Secret.password_lookup_sync(_SCHEMA, self._attributes(account), None)
        except GLib.Error as error:
            raise SecretServiceUnavailable(error.message) from error
        return None if value is None else value.encode("utf-8")

    def exists(self, account: str) -> bool:
        """Checked through attributes only; the secret is not unlocked or read."""
        try:
            items = Secret.password_search_sync(_SCHEMA, self._attributes(account), Secret.SearchFlags.NONE, None)
        except GLib.Error as error:
            raise SecretServiceUnavailable(error.message) from error
        return bool(items)

    def clear(self, account: str) -> None:
        """Deleting a missing item is not an error."""
        try:
            Secret.password_clear_sync(_SCHEMA, self._attributes(account), None)
        except GLib.Error as error:
            raise SecretServiceUnavailable(error.message) from error

    def accounts(self) -> list[str]:
        try:
            items = Secret.password_search_sync(_SCHEMA, {"service": self.service}, Secret.SearchFlags.ALL, None)
        except GLib.Error as error:
            raise SecretServiceUnavailable(error.message) from error
        return sorted({item.get_attributes().get("account") for item in items} - {None})


class SecretServiceStore:
    """API keys and Claude tokens. Writes and checks; never returns a value."""

    def __init__(self, service: str = API_KEY_SERVICE):
        self.backend = SecretServiceBackend(service)

    def set_secret(self, value: bytes, account: str) -> None:
        validate_account(account)
        if not value:
            raise EmptySecret()
        self.backend.store(account, value, label=f"MultiCred: {account}")

    def contains_item(self, account: str) -> bool:
        return self.backend.exists(account)

    def delete_item(self, account: str) -> None:
        self.backend.clear(account)

    def all_accounts(self) -> list[str]:
        return self.backend.accounts()
