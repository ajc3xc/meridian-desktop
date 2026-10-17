"""Test doubles. Nothing here touches the OS credential store or the network."""

from __future__ import annotations

import sys
from http.server import ThreadingHTTPServer

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class QuietServer(ThreadingHTTPServer):
    """A loopback stub server that drops the traceback for a client that closed early.

    Size-cap and timeout tests close the connection on purpose, so the write fails.
    Every other error still reaches the default handler.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class MemoryKeyring(KeyringBackend):
    """An in-memory keyring backend. It holds values in a dict and nothing else.

    conftest.py installs one for the whole session. Its priority is low, so keyring
    would never pick it on its own. It raises PasswordDeleteError for a missing entry,
    as the real backends do.
    """

    priority = 0.5

    def __init__(self) -> None:
        super().__init__()
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._values:
            raise PasswordDeleteError("not found")
        del self._values[(service, username)]

    def snapshot(self) -> dict[tuple[str, str], str]:
        return dict(self._values)


class FailingKeyring:
    """A backend whose every call fails. Each error message contains a secret on purpose."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(f"read failed while holding {self._secret}")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise ValueError(f"write refused for value {password}")

    def delete_password(self, service: str, username: str) -> None:
        raise OSError(f"delete failed for {self._secret}")
