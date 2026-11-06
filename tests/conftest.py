"""Fixtures shared by several test modules."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest


@pytest.fixture
def redirect_server() -> Iterator[Callable[[str], str]]:
    """Serve a 302 redirect to a given ``Location`` for every request.

    Yields:
        Function that takes the ``Location`` to send and returns the server's
        base URL.
    """
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(location: str) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def respond(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = respond

            def log_message(self, *args: Any) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
