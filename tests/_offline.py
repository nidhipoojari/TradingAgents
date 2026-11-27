"""Offline guard for tests that run real provider SDKs: any connection that is not local fails at once.

A mistake in intercepting an SDK's HTTP layer then fails the test instead of reaching a provider.
"""

from __future__ import annotations

import socket

LOCAL = ("127.0.0.1", "::1", "localhost")


def block_network(monkeypatch) -> None:
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        if isinstance(address, (str, bytes)) or (isinstance(address, tuple) and address[0] in LOCAL):
            return real_connect(self, address)          # Unix sockets and loopback are fine
        raise OSError(f"network blocked in offline tests: {address!r}")

    def getaddrinfo(host, *args, **kwargs):
        if host in LOCAL or host is None:
            return real_getaddrinfo(host, *args, **kwargs)
        raise OSError(f"name lookup blocked in offline tests: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
