"""Tests for the API's resource limits."""

from __future__ import annotations

import gzip
import http.server
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests
from urllib3.util import Retry

from eazyrest import (
    API,
    JSONObject,
    LimitExceeded,
    json_object,
)


def test_limit_defaults() -> None:
    """The limits are finite by default."""
    api = API("https://example.com/")

    assert api.max_response_bytes == 16 * 1024 * 1024
    assert api.total_timeout == 300.0
    assert api.max_json_depth == 128


@pytest.mark.parametrize("declared", [True, False])
def test_large_response_raises(requests_mock: Any, declared: bool) -> None:
    """A body larger than ``max_response_bytes`` raises before decoding.

    Args:
        declared: Whether the response declares its length.
    """
    api = API("https://example.com/")
    api.max_response_bytes = 100
    headers = {"Content-Length": "200"} if declared else {}
    requests_mock.get(
        "https://example.com/big", content=b"x" * 200, headers=headers
    )

    with pytest.raises(LimitExceeded, match="max_response_bytes") as info:
        api.get("/big")

    assert info.value.limit == "max_response_bytes"


def test_response_within_limit_is_read(requests_mock: Any) -> None:
    """A body no larger than the limit reads as usual."""
    api = API("https://example.com/")
    api.max_response_bytes = 9
    requests_mock.get("https://example.com/ok", json={"a": 1})

    assert api.get("/ok").json() == {"a": 1}


def test_streamed_response_is_not_limited(requests_mock: Any) -> None:
    """A request that streams reads its own body, without the size limit."""
    api = API("https://example.com/")
    api.max_response_bytes = 10
    requests_mock.get("https://example.com/big", content=b"x" * 200)

    resp = api.get("/big", stream=True)

    assert resp.raw.read() == b"x" * 200


def test_large_redirect_body_raises(requests_mock: Any) -> None:
    """A redirect's body counts too, because requests reads it."""
    api = API("https://example.com/")
    api.max_response_bytes = 100
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": "/b"},
        content=b"x" * 200,
    )
    after = requests_mock.get("https://example.com/b", json={})

    with pytest.raises(LimitExceeded, match="max_response_bytes"):
        api.get("/a")

    assert not after.called


def _nested(depth: int) -> str:
    """Return JSON text of arrays nested ``depth`` levels deep.

    Args:
        depth: Number of nested arrays.

    Returns:
        JSON text.
    """
    return "[" * depth + "]" * depth


class LimitBase(JSONObject):
    """Base class of the models of these tests."""


LimitBase.register_api(API("https://example.com/v1/"))


@json_object
class Box(LimitBase):
    """Model whose responses these tests make deep."""

    class_url = "/boxes/"

    id: int
    contents: list[Any]


@pytest.mark.parametrize("depth", [130, 100_000])
def test_deep_json_raises(requests_mock: Any, depth: int) -> None:
    """JSON nested deeper than ``max_json_depth`` raises.

    Args:
        depth: Nesting depth of the field's value. The second is deeper than
            Python's decoder can read.
    """
    requests_mock.get(
        "https://example.com/v1/boxes/1/",
        text='{"id": 1, "contents": ' + _nested(depth) + "}",
    )

    with pytest.raises(LimitExceeded, match="max_json_depth"):
        _ = Box(id=1).contents


def test_json_within_depth_is_read(requests_mock: Any) -> None:
    """JSON as deep as the limit allows reads as usual."""
    requests_mock.get(
        "https://example.com/v1/boxes/1/",
        text='{"id": 1, "contents": ' + _nested(127) + "}",
    )

    assert Box(id=1).id == 1
    assert Box(id=1).contents is not None


@pytest.fixture
def local_server() -> Iterator[Callable[[Any], str]]:
    """Serve requests with a handler function on a local port.

    Yields:
        Function that installs a handler, which receives the request handler
        object, and returns the server's base URL.
    """
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(handle: Any) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                handle(self)

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


def test_trickling_body_hits_total_timeout(
    local_server: Callable[[Any], str],
) -> None:
    """A body that arrives a byte at a time cannot outlast ``total_timeout``.

    Each byte arrives well within the read timeout, so only the total limit
    stops the request.
    """
    stop = threading.Event()

    def trickle(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        for _ in range(1000):
            if stop.is_set():
                return
            try:
                handler.wfile.write(b"x")
                handler.wfile.flush()
            except OSError:
                return
            time.sleep(0.02)

    api = API(local_server(trickle))
    api.timeout = 5
    api.total_timeout = 0.3
    started = time.monotonic()

    try:
        with pytest.raises(LimitExceeded, match="total_timeout"):
            api.get("/slow")
    finally:
        stop.set()

    assert time.monotonic() - started < 2


@pytest.mark.parametrize("compress", [False, True])
def test_chunked_body_is_limited_after_decoding(
    local_server: Callable[[Any], str], compress: bool
) -> None:
    """A chunked or compressed body counts its decoded size.

    Args:
        compress: Whether the body is sent gzip-compressed.
    """
    body = b"[" + b"0," * 5000 + b"0]"

    def send(handler: Any) -> None:
        data = gzip.compress(body) if compress else body
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Connection", "close")
        if compress:
            handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        for start in range(0, len(data), 1000):
            chunk = data[start : start + 1000]
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        handler.wfile.write(b"0\r\n\r\n")

    api = API(local_server(send))
    assert len(api.get("/items").json()) == 5001

    api.max_response_bytes = len(body) - 1
    with pytest.raises(LimitExceeded, match="max_response_bytes"):
        api.get("/items")


def test_retry_after_cannot_outlast_the_request_budget(
    local_server: Callable[[Any], str],
) -> None:
    """Retry sleeps stop at the deadline before another attempt is sent."""
    attempts = []

    def unavailable(handler: Any) -> None:
        attempts.append(handler.path)
        handler.send_response(503)
        handler.send_header("Retry-After", "1")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    api = API(
        local_server(unavailable),
        max_retries=Retry(total=1, status_forcelist=[503]),
    )
    api.total_timeout = 0.1
    started = time.monotonic()

    with pytest.raises(LimitExceeded, match="total_timeout"):
        api.get("/retry")

    assert time.monotonic() - started < 0.8
    assert attempts == ["/retry"]


@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_retried_response_body_obeys_the_size_limit(
    local_server: Callable[[Any], str], compress: bool, declared: bool
) -> None:
    """An intermediate failure is bounded before urllib3 drains its body.

    Args:
        compress: Whether the body is gzip-compressed.
        declared: Whether the response declares its encoded length.
    """
    attempts = []

    def unavailable(handler: Any) -> None:
        attempts.append(handler.path)
        body = gzip.compress(b"x" * 200) if compress else b"x" * 200
        handler.send_response(503)
        if declared:
            handler.send_header("Content-Length", str(len(body)))
        if compress:
            handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        handler.wfile.write(body)

    api = API(
        local_server(unavailable),
        max_retries=Retry(total=1, status_forcelist=[503]),
    )
    api.max_response_bytes = 100

    with pytest.raises(LimitExceeded, match="max_response_bytes"):
        api.get("/retry")

    assert attempts == ["/retry"]


def test_truncated_response_keeps_requests_exception_type(
    local_server: Callable[[Any], str],
) -> None:
    """Bounded reads translate urllib3 failures as Requests normally does."""

    def truncated(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.write(b"part")

    api = API(local_server(truncated))

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        api.get("/truncated")


def test_retry_backoff_cannot_outlast_the_request_budget(
    local_server: Callable[[Any], str],
) -> None:
    """Exponential backoff stops before the next attempt at the deadline."""
    attempts = []

    def unavailable(handler: Any) -> None:
        attempts.append(handler.path)
        handler.send_response(503)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    api = API(
        local_server(unavailable),
        max_retries=Retry(total=3, status_forcelist=[503], backoff_factor=1),
    )
    api.total_timeout = 0.1

    with pytest.raises(LimitExceeded, match="total_timeout"):
        api.get("/retry")

    assert attempts == ["/retry", "/retry"]


def test_invalid_compression_keeps_requests_exception_type(
    local_server: Callable[[Any], str],
) -> None:
    """A bad compressed body still raises Requests' decoding exception."""

    def invalid(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        handler.wfile.write(b"invalid gzip")

    api = API(local_server(invalid))

    with pytest.raises(requests.exceptions.ContentDecodingError):
        api.get("/invalid")


def test_body_timeout_keeps_requests_exception_type(
    local_server: Callable[[Any], str],
) -> None:
    """A socket timeout while reading a body remains a ConnectionError."""

    def stalled(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.write(b"part")
        handler.wfile.flush()
        time.sleep(0.2)

    api = API(local_server(stalled))
    api.timeout = 0.03

    with pytest.raises(requests.ConnectionError):
        api.get("/stalled")
