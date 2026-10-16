"""Storage for API key values and Claude account tokens.

There is intentionally no method that returns a stored value: the app only
writes secrets and checks that they exist. The value is read by the user's
shell hook through `secret-tool`, never by the app.
"""

from __future__ import annotations

import threading
import uuid
from typing import Protocol

# Secret Service attributes of every API key / Claude token item. The shell hook
# reads `secret-tool lookup service app.multicred.apikey account <uuid>`.
API_KEY_SERVICE = "app.multicred.apikey"


class SecretError(Exception):
    pass


class EmptySecret(SecretError):
    def __init__(self):
        super().__init__("The key is empty")


class InvalidAccount(SecretError):
    def __init__(self, account):
        super().__init__(f"Invalid secret account name: {account}")
        self.account = account


class SecretServiceUnavailable(SecretError):
    pass


def validate_account(account: str) -> None:
    """Accounts are always app-generated UUIDs, safe to write into shell files."""
    try:
        uuid.UUID(account)
    except (ValueError, AttributeError, TypeError):
        raise InvalidAccount(account) from None
    if account != account.upper():
        raise InvalidAccount(account)


class SecretStore(Protocol):
    def set_secret(self, value: bytes, account: str) -> None: ...
    def contains_item(self, account: str) -> bool: ...
    def delete_item(self, account: str) -> None: ...
    def all_accounts(self) -> list[str]: ...


class InMemorySecretStore:
    """For tests and the app's test mode."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, bytes] = {}

    def set_secret(self, value: bytes, account: str) -> None:
        validate_account(account)
        if not value:
            raise EmptySecret()
        with self._lock:
            self._items[account] = value

    def contains_item(self, account: str) -> bool:
        with self._lock:
            return account in self._items

    def delete_item(self, account: str) -> None:
        with self._lock:
            self._items.pop(account, None)

    def all_accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._items)

    def stored_value(self, account: str) -> bytes | None:
        """Test-only peek; not part of SecretStore."""
        with self._lock:
            return self._items.get(account)
