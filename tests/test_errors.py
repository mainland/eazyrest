"""Tests for the exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import http.server
import pickle
import socket
import threading
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import requests

from eazyrest import (
    API,
    APIError,
    DoesNotExist,
    EazyrestError,
    FieldConversionError,
    JSONObject,
    LimitExceeded,
    NotFound,
    PaginationError,
    RedirectError,
    ResponseShapeError,
    UsageError,
    follow_next_links,
    json_object,
)
from eazyrest._errors import _authority_less_location, _redact_url


@pytest.mark.parametrize(
    ("error", "bases"),
    [
        (RedirectError, (requests.HTTPError,)),
        (PaginationError, (ValueError,)),
        (ResponseShapeError, (TypeError, ValueError)),
        (UsageError, (RuntimeError,)),
        (APIError, (requests.HTTPError,)),
        (NotFound, (requests.HTTPError, DoesNotExist)),
    ],
)
def test_policy_errors_keep_the_types_they_replace(
    error: type[Exception], bases: tuple[type[Exception], ...]
) -> None:
    """Code that caught the replaced types still catches each error.

    Args:
        error: eazyrest exception class.
        bases: Exception types that ``error`` replaces.
    """
    assert issubclass(error, EazyrestError)
    for base in bases:
        assert issubclass(error, base)


class ErrorBase(JSONObject):
    """Base class for the models of these tests."""


ErrorBase.register_api(API("https://example.com/v1/"))


@json_object
class Note(ErrorBase):
    """Model whose responses these tests break."""

    class_url = "/notes/"

    id: int
    text: str


def test_refused_redirect_is_a_redirect_error(requests_mock: Any) -> None:
    """A refused redirect raises ``RedirectError`` with the redirect."""
    requests_mock.patch(
        "https://example.com/v1/notes/1/",
        status_code=301,
        headers={"Location": "https://example.com/v1/notes/1/x/"},
    )
    note = Note(json={"id": 1, "text": "a"})
    note.text = "b"

    with pytest.raises(RedirectError) as info:
        note.save()

    assert info.value.response is not None
    assert info.value.response.status_code == 301


def test_non_json_write_response_keeps_pending_updates(
    requests_mock: Any,
) -> None:
    """A write response that is not JSON raises without quoting the body."""
    requests_mock.patch(
        "https://example.com/v1/notes/1/", text="<html>secret page</html>"
    )
    note = Note(json={"id": 1, "text": "a"})
    note.text = "b"

    with pytest.raises(ResponseShapeError, match="does not hold JSON") as info:
        note.save()

    assert "secret" not in str(info.value)
    assert note._pending_updates == {"text": "b"}


def test_page_cycle_is_a_pagination_error(requests_mock: Any) -> None:
    """A next link to a page already read raises ``PaginationError``."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/notes/?page=2",
        json={"next": "/v1/notes/?page=2", "results": []},
    )

    with pytest.raises(PaginationError, match="already read"):
        list(
            follow_next_links(
                api, {"next": "/v1/notes/?page=2", "results": []}
            )
        )


def test_model_without_api_is_a_usage_error() -> None:
    """Using a model whose class has no registered API raises."""

    @json_object
    class Orphan(JSONObject):
        class_url = "/orphans/"

        id: int

    with pytest.raises(UsageError, match="No API registered"):
        _ = Orphan.api


@pytest.mark.parametrize(
    ("body", "names"),
    [
        ({"email": ["Enter a valid email address: 'x'."]}, "'email'"),
        ({"name": ["Required."], "age": ["Too young."]}, "'age', 'name'"),
        ({"detail": "You do not have permission: 'secret'."}, None),
        ("<html>secret</html>", None),
    ],
)
def test_failed_request_raises_api_error_with_its_body(
    requests_mock: Any, body: Any, names: str | None
) -> None:
    """An error status raises ``APIError``, which holds the parsed body.

    The message names the members of a JSON object body but quotes none of
    its text, which can quote the values that were sent.

    Args:
        body: Error response body, JSON or text.
        names: Member names that the message should list, or ``None``.
    """
    api = API("https://example.com/v1/")
    if isinstance(body, str):
        requests_mock.post(
            "https://example.com/v1/users/", status_code=400, text=body
        )
    else:
        requests_mock.post(
            "https://example.com/v1/users/", status_code=400, json=body
        )

    with pytest.raises(APIError) as info:
        api.post("/users/", json={"email": "x"})

    error = info.value
    assert isinstance(error, requests.HTTPError)
    assert error.response is not None
    assert error.response.status_code == 400
    assert error.data == (None if isinstance(body, str) else body)
    message = str(error)
    assert message.startswith(
        "400 Client Error: None for POST https://example.com/v1/users/"
    )
    assert "secret" not in message
    assert "'x'" not in message
    if names is None:
        assert "body names" not in message
    else:
        assert message.endswith(f"(body names {names})")


def test_deleted_object_raises_not_found(requests_mock: Any) -> None:
    """A 404 on a lazy load is ``NotFound``, a ``DoesNotExist``."""
    requests_mock.get(
        "https://example.com/v1/notes/7/",
        status_code=404,
        json={"detail": "Not found."},
    )

    with pytest.raises(DoesNotExist) as info:
        _ = Note(id=7).text

    assert isinstance(info.value, NotFound)
    assert isinstance(info.value, requests.HTTPError)
    assert info.value.data == {"detail": "Not found."}


def test_server_error_is_an_api_error(requests_mock: Any) -> None:
    """A 5xx status raises ``APIError`` but not ``NotFound``."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/x", status_code=503)

    with pytest.raises(APIError, match=r"^503 Server Error") as info:
        api.get("/x")

    assert not isinstance(info.value, NotFound)
    assert info.value.data is None


@pytest.mark.parametrize(
    "error",
    [
        LimitExceeded("max_pages", "The query read 3 pages"),
        FieldConversionError(int, "count", ValueError("bad")),
        PaginationError("bad page"),
        DoesNotExist("gone"),
    ],
    ids=["limit", "field", "pagination", "missing"],
)
def test_errors_pickle(error: EazyrestError) -> None:
    """An error reaches another process with its type, message, and fields.

    Args:
        error: Error to pickle.
    """
    copy = pickle.loads(pickle.dumps(error))

    assert type(copy) is type(error)
    assert str(copy) == str(error)
    assert vars(copy) == vars(error)


def test_http_errors_pickle_without_their_guards() -> None:
    """An error that holds a response pickles, without eazyrest's guards.

    The guards that check redirects are local functions, which the
    response's request would otherwise keep.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "15")
            self.end_headers()
            self.wfile.write(b'{"detail": "x"}')

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with (
            API(f"http://127.0.0.1:{server.server_port}/") as api,
            pytest.raises(APIError) as raised,
        ):
            api.get("/x")
    finally:
        server.shutdown()
        server.server_close()

    copy = pickle.loads(pickle.dumps(raised.value))

    assert type(copy) is APIError
    assert copy.data == {"detail": "x"}
    assert copy.response.status_code == 500


def test_error_of_a_request_with_a_stream_body_does_not_pickle(
    requests_mock: Any, tmp_path: Path
) -> None:
    """An error pickles unless its request's body is a stream.

    The request keeps an open file that was sent as the body, and a file does
    not pickle. The same request with a body of bytes does.

    Args:
        tmp_path: Directory for the file that the request sends.
    """
    api = API("https://example.com/v1/")
    requests_mock.post("https://example.com/v1/upload/", status_code=400)
    path = tmp_path / "upload.bin"
    path.write_bytes(b"dummy")

    with pytest.raises(APIError) as sent_bytes:
        api.post("/upload/", data=b"dummy")
    with path.open("rb") as stream, pytest.raises(APIError) as sent_stream:
        api.post("/upload/", data=stream)

    assert type(pickle.loads(pickle.dumps(sent_bytes.value))) is APIError
    with pytest.raises(TypeError, match="pickle"):
        pickle.dumps(sent_stream.value)


def test_errors_of_digest_authentication_pickle() -> None:
    """An error of a request sent with digest authentication pickles.

    requests keeps the handler's response hooks, which hold the thread-local
    state of ``HTTPDigestAuth``, on the request.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if "Authorization" not in self.headers:
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    'Digest realm="r", nonce="abc", qop="auth", '
                    'algorithm="MD5"',
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "15")
            self.end_headers()
            self.wfile.write(b'{"detail": "x"}')

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with API(f"http://127.0.0.1:{server.server_port}/") as api:
            api.session.auth = requests.auth.HTTPDigestAuth("user", "dummy")
            with pytest.raises(APIError) as raised:
                api.get("/x")
    finally:
        server.shutdown()
        server.server_close()

    copy = pickle.loads(pickle.dumps(raised.value))

    assert copy.response.status_code == 500
    assert copy.response.history[0].status_code == 401


def test_errors_after_redirects_pickle_without_their_guards(
    requests_mock: Any,
) -> None:
    """The requests of every hop, and an error's own request, lose the guards.

    An error that a followed redirect leads to holds the redirect in its
    response's history, and requests raises ``TooManyRedirects`` from
    ``send()``, with the request that it would have sent next.
    """
    api = API("https://example.com/v1/")
    api.session.max_redirects = 2
    requests_mock.get(
        "https://example.com/v1/a",
        status_code=307,
        headers={"Location": "https://example.com/v1/b"},
    )
    requests_mock.get(
        "https://example.com/v1/b", status_code=500, json={"detail": "x"}
    )
    requests_mock.get(
        "https://example.com/v1/loop",
        status_code=307,
        headers={"Location": "https://example.com/v1/loop"},
    )

    with pytest.raises(APIError) as failed:
        api.get("/a")
    with pytest.raises(requests.TooManyRedirects) as looped:
        api.get("/loop")

    copy = pickle.loads(pickle.dumps(failed.value))
    assert copy.response.history[0].status_code == 307
    loop = pickle.loads(pickle.dumps(looped.value))
    assert type(loop) is requests.TooManyRedirects


@pytest.mark.parametrize("digest", [False, True])
def test_unfollowed_redirect_that_requests_cannot_prepare_pickles(
    requests_mock: Any, digest: bool
) -> None:
    """The error keeps no response hook, so it reaches another process.

    Args:
        digest: Whether the session authenticates with a digest handler, which
            also puts hooks on the request.
    """
    api = API("https://example.com/v1/")
    if digest:
        api.session.auth = requests.auth.HTTPDigestAuth("user", "dummy")
    requests_mock.get(
        "https://example.com/v1/x",
        status_code=302,
        headers={"Location": "http://[::1/"},
    )

    with pytest.raises(RedirectError, match="cannot prepare") as raised:
        api.get("/x", allow_redirects=False)

    assert raised.value.response is not None
    assert raised.value.response.request.hooks["response"] == []
    copy = pickle.loads(pickle.dumps(raised.value))
    assert type(copy) is RedirectError
    assert "cannot prepare" in str(copy)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://user:dummy@host/x?a=b", "http://host/x?a=b"),
        ("//user:dummy@host/x", "//host/x"),
        ("//user@host:99999/x", "//host:99999/x"),
        ("//host/x@y", "//host/x@y"),
        ("/x@y", "/x@y"),
        ("http://host/x", "http://host/x"),
        (
            "http://user:dummy-pass@host.test\\@evil.test/x",
            "http://host.test\\@evil.test/x",
        ),
        (
            "https://user:dummy-pass@host.test\\@evil.test/x",
            "https://host.test\\@evil.test/x",
        ),
        (
            "HTTPS://user:dummy-pass@host.test\\@evil.test/x",
            "HTTPS://host.test\\@evil.test/x",
        ),
        (
            "http://host.test\\user@evil.test/x",
            "http://host.test\\user@evil.test/x",
        ),
        (
            "//user:dummy-pass@host.test\\@evil.test/x",
            "//host.test\\@evil.test/x",
        ),
        (
            "ftp://user:dummy-pass@host.test\\@evil.test/x",
            "ftp://evil.test/x",
        ),
        ("\x0chttp://user:dummy@host/x", "http://host/x"),
        (" \x01//user:dummy@host/x", "//host/x"),
        ("ht\ttp://user:dummy@host/x", "http://host/x"),
        ("http:/\t/user:dummy@host/x", "http://host/x"),
        ("http://user:dummy@host/\nx", "http://host/x"),
        ("\xa0http://user:dummy@host/x", "http://host/x"),
        ("\u3000http://user:dummy@host/x", "http://host/x"),
        ("\x85http://user:dummy@host/x", "http://host/x"),
        ("\u2028//user:dummy@host/x", "//host/x"),
        (" \xa0\x01\u3000http://user:dummy@host/x", "http://host/x"),
        ("\xa0http://host/x", "\xa0http://host/x"),
        ("\x0chttp://host/x\t", "\x0chttp://host/x\t"),
        (b"http://user:dummy@host/x?a=b", "http://host/x?a=b"),
        (b"http://host/x", "http://host/x"),
        ("http://user:dummy@h\u00f6st/x".encode(), "http://h\u00f6st/x"),
    ],
)
def test_redacted_url_leaves_out_a_user_and_password(
    url: str | bytes, expected: str
) -> None:
    """A URL without a scheme, as a ``Location`` may be, is redacted too.

    Args:
        url: URL to quote, as requests accepts it, also as ``bytes``.
        expected: The URL without its user and password, as text.
    """
    assert _redact_url(url) == expected


@pytest.mark.parametrize("status", [301, 302, 303])
@pytest.mark.parametrize(
    "location",
    ["https://user:dummy@example.com/x", "//user:dummy@example.com/x"],
)
def test_write_redirect_message_leaves_out_the_location_credentials(
    requests_mock: Any, status: int, location: str
) -> None:
    """A write that requests would follow as another method quotes no user.

    Args:
        status: The redirect's status.
        location: The redirect's ``Location`` header.
    """
    api = API("https://example.com/")
    requests_mock.post(
        "https://example.com/c",
        status_code=status,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError) as raised:
        api.post("/c", data=b"x")

    assert "dummy@" not in str(raised.value)
    assert "dummy@" not in repr(raised.value)
    assert "example.com/x" in str(raised.value)


@pytest.mark.parametrize("status", [302, 303])
def test_write_redirect_message_names_the_host_that_requests_follows(
    requests_mock: Any, status: int
) -> None:
    """A write redirect quotes the host after a backslash, as requests does.

    requests percent-encodes the backslash, which ends the userinfo later, so
    it connects to the host after ``@``.

    Args:
        status: The redirect's status.
    """
    api = API("https://h.test/v1/")
    api.redirect_origins = ["https://e.test"]
    requests_mock.patch(
        "https://h.test/v1/things/1/",
        status_code=status,
        headers={"Location": "https://u:dummy-pass@h.test\\@e.test/x"},
    )

    with pytest.raises(RedirectError) as raised:
        api.patch("things/1/", json={"a": 1})

    assert "dummy-pass" not in str(raised.value)
    assert "https://e.test/x" in str(raised.value)
    assert "h.test\\" not in str(raised.value)


def test_error_body_names_are_bounded_and_quoted(requests_mock: Any) -> None:
    """A server's names reach the message bounded and escaped.

    A newline or an escape sequence in a name, or in the reason phrase,
    would otherwise reach a log as written.
    """
    api = API("https://example.com/v1/")
    body = {"a\nb": 1, "b" * 1000: 2, **{f"n{i:02}": i for i in range(20)}}
    requests_mock.get(
        "https://example.com/v1/x",
        status_code=400,
        reason="Bad\x1b[31m",
        json=body,
    )

    with pytest.raises(APIError) as raised:
        api.get("/x")

    message = str(raised.value)
    assert "\n" not in message
    assert "\x1b" not in message
    assert "'a\\nb'" in message
    # Ten sorted names are shown, each of at most 40 characters.
    assert "'" + "b" * 40 + "...'" in message
    assert "b" * 41 not in message
    assert "'n07'" in message
    assert "'n08'" not in message
    assert "and 12 more" in message
    assert len(message) < 600


@pytest.fixture
def credentialed_api() -> Iterator[tuple[API, str]]:
    """Return an API whose base URL has a user and password.

    The server answers by path: ``/fail`` with a 500, ``/html`` with a body
    that is not JSON, ``/redirect`` with a 302, and ``/elsewhere`` with a 307
    to another origin whose URL has a user and password too.

    Yields:
        The API, and the user and password text that no message may quote.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def handle_any(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = self.path.split("?")[0]
            body = b"<html>"
            status = 200
            headers: list[tuple[str, str]] = []
            if path == "/fail":
                status, body = 500, b'{"detail": "x"}'
            elif path == "/redirect":
                status, body = 302, b""
                headers = [("Location", "/next")]
            elif path == "/elsewhere":
                status, body = 307, b""
                headers = [
                    ("Location", "http://u2:dummy-pass2@other.invalid/")
                ]
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = handle_any

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://user:dummy-pass@127.0.0.1:{server.server_port}/"
    try:
        with API(base) as api:
            yield api, base
    finally:
        server.shutdown()
        server.server_close()


def _quoted(error: BaseException) -> list[str]:
    """Return every text of an error that can quote a URL.

    Args:
        error: Exception to inspect.

    Returns:
        Its message, its ``repr()``, and its ``args`` as text.
    """
    return [str(error), repr(error), *map(str, error.args)]


def test_error_messages_leave_out_the_user_and_password_of_a_url(
    credentialed_api: tuple[API, str],
) -> None:
    """An error quotes a URL's query, but not its user and password.

    Args:
        credentialed_api: API with a user and password in its base URL, and its
            base URL.
    """
    api, base = credentialed_api
    plain = base.replace("user:dummy-pass@", "")
    query = "?api_key=dummy-key"
    errors: list[BaseException] = []

    def collect(action: Callable[[], object]) -> None:
        with pytest.raises(Exception, match=r"127\.0\.0\.1") as raised:
            action()
        errors.append(raised.value)

    collect(lambda: api.get(f"/fail{query}"))
    collect(lambda: api.json(api.get(f"/html{query}")))
    collect(lambda: api.post(f"/redirect{query}", json={}))
    collect(lambda: api.get(f"/elsewhere{query}"))
    collect(lambda: api._scoped({"allow_redirects": False}).get("/redirect"))
    api.max_response_bytes = 1
    collect(lambda: api.get(f"/html{query}"))
    api.max_response_bytes = None
    api.total_timeout = 0.0
    collect(lambda: api.get(f"/html{query}"))
    api.total_timeout = None
    page = f"{base}fail{query}"
    collect(
        lambda: list(
            follow_next_links(api, {"next": page, "results": []}, url=page)
        )
    )
    other = "http://u3:dummy-pass3@other.invalid/p"
    collect(
        lambda: list(
            follow_next_links(api, {"next": other, "results": []}, url=page)
        )
    )

    assert len(errors) == 9
    for error in errors:
        for text in _quoted(error):
            assert "dummy-pass" not in text, text
            assert "u2:" not in text, text
            assert "u3:" not in text, text
    assert plain + f"fail{query}" in str(errors[0])
    assert plain + f"html{query}" in str(errors[1])
    assert isinstance(errors[0], APIError)
    assert "dummy-pass" in errors[0].response.url
    assert "dummy-pass" in str(errors[0].request.url)


def test_error_messages_of_a_url_without_credentials_are_unchanged(
    requests_mock: Any,
) -> None:
    """A URL that has no user and password is quoted as it is."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/x?a=b", status_code=500)

    with pytest.raises(APIError) as raised:
        api.get("/x?a=b")

    assert "for GET https://example.com/v1/x?a=b" in str(raised.value)


# Locations that requests cannot prepare, with the parts of each after its
# scheme, which no message may quote.
UNPREPARABLE_LOCATIONS = [
    ("http://[::1/x", ["[::1", "/x"]),
    ("http://[::1/x?email=a@b.test", ["email=a", "b.test", "[::1"]),
    ("http://[::1/x@evil.test/y", ["evil.test", "[::1", "/y"]),
    ("//[::1/#a@b", ["[::1", "a@b"]),
    ("http://alice:hunter2@[::1/x@evil.test", ["alice", "hunter2", "evil"]),
    ("http://user:hunter2\\@[::1/x", ["user", "hunter2", "[::1"]),
    ("http://x\uff20alice:hunter2@h.test/p", ["alice", "hunter2", "h.test"]),
    ("http://x\ufe6balice:hunter2@h.test/p", ["alice", "hunter2", "h.test"]),
    ("\x01http://alice:hunter2@[::1/x@evil.test", ["hunter2", "evil.test"]),
    ("\x0c//alice:hunter2@[::1/x", ["alice", "hunter2", "[::1"]),
    (" \t\x01http://a\tlice:hunter2@[::1/x", ["alice", "hunter2", "[::1"]),
    (
        "http://alice:hunter2@h.test:abc/p\u00e5th",
        ["alice", "hunter2", "h.test"],
    ),
    ("http://alice:hunter2#@[::1/x", ["alice", "hunter2", "[::1"]),
    ("http://alice:hunter2?@h.test/x", ["alice", "hunter2", "h.test"]),
    ("http://alice:hunter2/@h.test/x", ["alice", "hunter2", "h.test"]),
    ("http://alice:hunter2@h.test:abc/p", ["alice", "hunter2", "h.test"]),
    ("http://h.test:abc/p", ["h.test", "abc"]),
    ("//alice:hunter2@h.test:99999/x", ["alice", "hunter2", "h.test"]),
    ("http://alice:hunter2#@h.test/x", ["alice", "hunter2", "h.test"]),
]


@pytest.mark.parametrize(("location", "parts"), UNPREPARABLE_LOCATIONS)
def test_unpreparable_redirect_message_quotes_no_part_of_the_location(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    parts: list[str],
) -> None:
    """A ``Location`` that requests cannot prepare is left out of the message.

    No rule says which of its text is a host and which is a password, so the
    message quotes none of it. The error's response holds the header.

    Args:
        location: The redirect's ``Location`` header.
        parts: Text of the header after its scheme, which the message must not
            quote.
    """
    # A no-proxy list makes requests read the port of the redirect's host.
    monkeypatch.setenv("NO_PROXY", "example.invalid")
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="cannot prepare") as raised:
        api.get("/a", allow_redirects=False)

    for text in (str(raised.value), repr(raised.value)):
        assert (
            "GET https://example.com/a was redirected with status 302 to a "
            "location that requests cannot prepare, so it was not followed."
        ) in text
        assert "The response of this error holds the Location header" in text
        for part in parts:
            assert part not in text
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == location


def _followed_problem(location: str) -> str:
    """Return how a followed redirect's error says that it cannot be followed.

    Args:
        location: The redirect's ``Location`` header.

    Returns:
        ``cannot be parsed`` for a target that requests cannot decode or
        ``urlparse()`` rejects, and ``requests cannot prepare`` for one that it
        accepts but whose origin cannot be read.
    """
    try:
        # Requests decodes the header's Latin-1 text as UTF-8.
        urlparse(location.encode("latin1").decode("utf-8"))
    except ValueError:
        return "cannot be parsed"
    return "requests cannot prepare"


@pytest.mark.parametrize(("location", "parts"), UNPREPARABLE_LOCATIONS)
def test_followed_redirect_that_requests_cannot_prepare_quotes_no_part(
    requests_mock: Any, location: str, parts: list[str]
) -> None:
    """A followed redirect leaves out a ``Location`` that requests rejects.

    The target may parse, as one with a port that is not a number does, and
    still have no origin to read. The message is then the one of an unfollowed
    redirect, and quotes none of the header.

    Args:
        location: The redirect's ``Location`` header.
        parts: Text of the header after its scheme, which the message must not
            quote.
    """
    problem = _followed_problem(location)
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="location that") as raised:
        api.get("/a")

    for text in (str(raised.value), repr(raised.value)):
        assert (
            "GET https://example.com/a was redirected with status 302 to a "
            f"location that {problem}, so it was not followed."
        ) in text
        assert "origin differs" not in text
        for part in parts:
            assert part not in text
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == location


@pytest.mark.parametrize(
    ("location", "scheme"),
    [
        ("mailto:alice:hunter2@h.test/x", "mailto"),
        ("data:alice:hunter2@x", "data"),
        ("javascript:alice:hunter2@h", "javascript"),
        ("file:alice:hunter2@h/x", "file"),
        ("ftp:///alice:hunter2?@[v1.x/a b", "ftp"),
        ("ftp://?@alice:hunter2/x", "ftp"),
        ("\x01mailto:alice:hunter2@h.test/x@y", "mailto"),
        (" \t\x01mai\tlto:alice:hunter2@[::1]/x", "mailto"),
        ("MailTo:alice:hunter2@h.test:abc/x", "mailto"),
    ],
)
def test_followed_redirect_to_a_target_without_an_authority_quotes_its_scheme(
    requests_mock: Any, location: str, scheme: str
) -> None:
    """A target with no ``//`` after its scheme is quoted by its scheme alone.

    Such a target has no user and password to leave out, yet its text can read
    as one. The error's response holds the header.

    Args:
        location: The redirect's ``Location`` header.
        scheme: The scheme that the message names, in lower case as requests
            follows it.
    """
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="origin differs") as raised:
        api.get("/a")

    for text in (str(raised.value), repr(raised.value)):
        assert (
            f"was redirected with status 302 to a {scheme}: location, whose "
            "origin differs from the request's, so it was not followed."
        ) in text
        assert "alice" not in text
        assert "hunter2" not in text
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == location


@pytest.mark.parametrize(
    ("url", "described"),
    [
        ("mailto:alice:hunter2@h.test", "a mailto: location"),
        (b"mailto:alice:hunter2@h.test", "a mailto: location"),
        ("\x01\x0c \u00a0mailto:a:b@h", "a mailto: location"),
        (" \t\x01mai\tlto:\r\na:b@h", "a mailto: location"),
        ("x+y.z:a:b@h", "a x+y.z: location"),
        ("ftp://alice:hunter2@h.test/x", None),
        ("ftp:///alice:hunter2@h", "a ftp: location"),
        ("ftp://?@alice:hunter2/x", "a ftp: location"),
        ("ftp://", "a ftp: location"),
        ("ftp://#alice:hunter2@h", "a ftp: location"),
        ("x-y:alice:hunter2@h", "a x-y: location"),
        ("ftp://h", None),
        ("\x01ftp:/\t/alice:hunter2@h.test/x", None),
        ("//alice:hunter2@h.test/x", None),
        ("/a:b@h", None),
        ("a:b@h", "a a: location"),
        ("", None),
    ],
)
def test_authority_less_location_reads_the_url_as_the_redaction_does(
    url: str | bytes, described: str | None
) -> None:
    """Only a URL with a scheme and no ``//`` after it is described.

    Args:
        url: The URL, which may begin with characters that requests ignores.
        described: The description, or ``None`` for a URL that has none.
    """
    assert _authority_less_location(url) == described


def test_followed_redirect_to_a_target_with_an_authority_is_still_quoted(
    requests_mock: Any,
) -> None:
    """A target with an authority is quoted, without its user and password."""
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": "ftp://alice:hunter2@h.test/x"},
    )

    with pytest.raises(RedirectError, match="origin differs") as raised:
        api.get("/a")

    assert "redirected with status 302 to ftp://h.test/x, whose" in str(
        raised.value
    )


def test_followed_redirect_to_a_readable_origin_leaves_out_its_credentials(
    requests_mock: Any,
) -> None:
    """A target with a readable origin that differs is quoted without them."""
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": "http://alice:hunter2@other.test:8080/x"},
    )

    with pytest.raises(RedirectError, match="origin differs") as raised:
        api.get("/a")

    message = str(raised.value)
    assert "http://other.test:8080/x, whose origin differs" in message
    assert "alice" not in message
    assert "hunter2" not in message


def test_redirect_that_cannot_be_parsed_closes_the_response(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redirect that requests would follow is closed before it is refused."""
    closed: list[requests.Response] = []
    original = requests.Response.close

    def spy(self: requests.Response) -> None:
        closed.append(self)
        original(self)

    monkeypatch.setattr(requests.Response, "close", spy)
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": "http://[::1/x"},
    )

    with pytest.raises(RedirectError, match="cannot be parsed") as raised:
        api.get("/a")

    assert raised.value.response in closed


@pytest.mark.parametrize(
    "header",
    [
        b"http://[::1/x?email=a@b.test",
        b"http://[::1/x@evil.test/y",
        b"//[::1/#a@b",
        b"http://alice:hunter2@[::1/x@evil.test",
        b"http://alice:hunter2#@[::1/x",
        b"http://alice:hunter2@h.test:abc/p",
        b"http://x\xef\xbc\xa0alice:hunter2@h.test/p",
        b"http://x\xef\xb9\xabalice:hunter2@h.test/p",
        b"\x01http://alice:hunter2@[::1/x@evil.test",
        b"\x0bhttp://alice:hunter2@[::1/x",
        b"\x1fhttp://alice:hunter2\\@[::1/x",
        b"\x0c//alice:hunter2@[::1/x",
        b"\x01http://alice:hunter2@h.test:abc/p\xe5th",
        b"http://[::1/p\xc3\xa5th",
    ],
)
def test_unpreparable_redirect_message_quotes_no_raw_location_bytes(
    monkeypatch: pytest.MonkeyPatch, header: bytes
) -> None:
    """A server's raw ``Location`` bytes are left out of the message.

    http.client strips only a leading space or tab from a header value, so a
    real server can send a leading control character. The bytes may be UTF-8,
    which requests decodes, or not.

    Args:
        header: The bytes of the ``Location`` header.
    """
    monkeypatch.setenv("NO_PROXY", "*")

    def serve(listener: socket.socket) -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: "
                + header
                + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        # A request that never connects ends the server's wait, not the run.
        listener.settimeout(10)
        thread = threading.Thread(target=serve, args=(listener,))
        thread.start()
        api = API(f"http://127.0.0.1:{listener.getsockname()[1]}/")
        try:
            with pytest.raises(
                RedirectError, match="cannot prepare"
            ) as raised:
                api.get("/a", allow_redirects=False)
        finally:
            thread.join()

    for text in (str(raised.value), repr(raised.value)):
        assert "location that requests cannot prepare" in text
        for part in ("hunter2", "alice", "evil", "[::1", "a@b", "h.test"):
            assert part not in text
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == header.decode(
        "latin1"
    ).lstrip(" \t")


def _assert_error_holds_no_header(
    error: RedirectError, header: bytes, parts: list[str]
) -> None:
    """Check that nothing a log prints for a redirect error quotes a header.

    Args:
        error: The error that refused the redirect.
        header: The bytes of the ``Location`` header.
        parts: Text of the header that no printed text may hold.
    """
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__
    texts = [
        "".join(traceback.format_exception(error)),
        str(error),
        repr(error),
    ]
    for text in texts:
        assert "location that" in text
        for part in parts:
            assert part not in text
    assert error.response is not None
    assert error.response.headers["Location"] == header.decode("latin1")


CHAINED_HEADERS = [
    (
        "http://alice:hunter2@h℀/x".encode(),
        ["alice", "hunter2", "h℀"],
    ),
    (b"http://alice:hunter2@h\xe5/x", ["alice", "hunter2", "xe5"]),
]


@pytest.mark.parametrize("follow", [False, True])
@pytest.mark.parametrize(("header", "parts"), CHAINED_HEADERS)
def test_redirect_error_does_not_chain_the_error_that_rejected_the_header(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    header: bytes,
    parts: list[str],
    follow: bool,
) -> None:
    """A redirect error has no cause that quotes the ``Location``.

    The error that rejected the header can quote its host and its password, and
    a traceback prints it. The redirect error keeps no cause or context, and
    its traceback quotes nothing, on a followed and an unfollowed redirect.

    Args:
        header: The bytes of the ``Location`` header.
        parts: Text of the header that no printed text may hold.
        follow: Whether the request allows redirects.
    """
    monkeypatch.setenv("NO_PROXY", "*")
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": header.decode("latin1")},
    )

    with pytest.raises(RedirectError) as raised:
        api.get("/a", allow_redirects=follow)

    _assert_error_holds_no_header(raised.value, header, parts)


@pytest.mark.parametrize("follow", [False, True])
@pytest.mark.parametrize(("header", "parts"), CHAINED_HEADERS)
def test_redirect_error_of_raw_header_bytes_does_not_chain_a_cause(
    monkeypatch: pytest.MonkeyPatch,
    header: bytes,
    parts: list[str],
    follow: bool,
) -> None:
    """A header's bytes from a socket appear in no traceback of its error.

    Args:
        header: The bytes of the ``Location`` header.
        parts: Text of the header that no printed text may hold.
        follow: Whether the request allows redirects.
    """
    monkeypatch.setenv("NO_PROXY", "*")

    def serve(listener: socket.socket) -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: "
                + header
                + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        # A request that never connects ends the server's wait, not the run.
        listener.settimeout(10)
        thread = threading.Thread(target=serve, args=(listener,))
        thread.start()
        api = API(f"http://127.0.0.1:{listener.getsockname()[1]}/")
        try:
            with pytest.raises(RedirectError) as raised:
                api.get("/a", allow_redirects=follow)
        finally:
            thread.join()

    _assert_error_holds_no_header(raised.value, header, parts)
