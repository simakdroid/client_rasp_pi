import socket

import pytest

from app.run import create_listen_socket, is_dualstack_host


def test_dualstack_host_names() -> None:
    assert is_dualstack_host("::")
    assert is_dualstack_host("::0")
    assert is_dualstack_host(" * ")
    assert not is_dualstack_host("0.0.0.0")
    assert not is_dualstack_host("127.0.0.1")
    assert not is_dualstack_host("::1")


def test_ipv4_socket_family() -> None:
    sock = create_listen_socket("127.0.0.1", 0)
    try:
        assert sock.family == socket.AF_INET
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()


def _ipv6_available() -> bool:
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _ipv6_available(), reason="IPv6 unavailable")
def test_colon_host_disables_v6only() -> None:
    try:
        sock = create_listen_socket("::", 0)
    except OSError as exc:
        pytest.skip(f"cannot bind [::]: {exc}")
    try:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
        sock.listen(1)
        port = sock.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            assert client.getpeername()[1] == port
    except OSError as exc:
        pytest.skip(f"IPv4-mapped connect failed: {exc}")
    finally:
        sock.close()
