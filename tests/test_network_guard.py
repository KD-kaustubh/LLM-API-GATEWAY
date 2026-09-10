import socket

import pytest


def test_outbound_connections_are_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(RuntimeError, match="must not make network calls"):
            sock.connect(("203.0.113.1", 443))
