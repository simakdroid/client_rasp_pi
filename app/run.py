"""Start uvicorn; ``BACKEND_HOST=::`` listens on IPv4 and IPv6."""

from __future__ import annotations

import logging
import os
import socket

import uvicorn

LOGGER = logging.getLogger("airmon.backend")

_DUALSTACK_HOSTS = frozenset({"::", "::0", "*"})


def is_dualstack_host(host: str) -> bool:
    return host.strip() in _DUALSTACK_HOSTS


def create_listen_socket(host: str, port: int) -> socket.socket:
    """Bind a TCP socket. ``::`` / ``*`` is dual-stack IPv4+IPv6."""
    host = host.strip() or "127.0.0.1"
    if is_dualstack_host(host):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
        sock.set_inheritable(True)
        return sock

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    host = os.environ.get("BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("BACKEND_PORT", "8080"))
    sock = create_listen_socket(host, port)
    bound = sock.getsockname()
    if is_dualstack_host(host) and sock.family == socket.AF_INET6:
        v6only = sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
        LOGGER.info(
            "Listening dual-stack on [::]:%s (IPv4+IPv6, IPV6_V6ONLY=%s)",
            bound[1],
            v6only,
        )
    else:
        LOGGER.info("Listening on %s:%s", host, bound[1] if isinstance(bound, tuple) else port)
    config = uvicorn.Config(
        "app.main:app",
        ws_ping_interval=15.0,
        ws_ping_timeout=30.0,
    )
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
