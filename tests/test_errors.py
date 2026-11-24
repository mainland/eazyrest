"""Tests for the exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import http.server
import pickle
import socket
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import requests
from requests.auth import AuthBase

from eazyrest import (
    API,
    APIError,
    DoesNotExist,
    EazyrestError,
    FieldConversionError,
    JSONObject,
    LimitExceeded,
    MultipleObjectsReturned,
    NotFound,
    PaginationError,
    PrefetchNotSupported,
    RedirectError,
    ResponseShapeError,
    UsageError,
    follow_next_links,
    json_object,
)
from eazyrest._errors import _response_name
from eazyrest._urls import _prepare_redirect
from eazyrest.api import _check_final_response


@pytest.mark.parametrize(
    ("error", "bases"),
    [
        (RedirectError, (requests.HTTPError,)),
        (PaginationError, (ValueError,)),
        (ResponseShapeError, (TypeError, ValueError)),
        (UsageError, (RuntimeError,)),
        (APIError, (requests.HTTPError,)),
        (NotFound, (requests.HTTPError, DoesNotExist)),
        (PrefetchNotSupported, ()),
        (DoesNotExist, ()),
        (MultipleObjectsReturned, ()),
        (LimitExceeded, ()),
        (FieldConversionError, (TypeError, ValueError)),
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


def test_a_body_that_is_not_json_is_refused_with_its_response(
    requests_mock: Any,
) -> None:
    """The error holds the response, whose body the message does not quote."""
    requests_mock.get(
        "https://example.com/v1/notes/1/", text="<html>secret page</html>"
    )

    with pytest.raises(ResponseShapeError, match="does not hold JSON") as info:
        _ = Note(id=1).text

    assert info.value.response is not None
    assert info.value.response.text == "<html>secret page</html>"
    assert info.value.response.url == "https://example.com/v1/notes/1/"


def test_a_hook_that_refuses_decoded_json_holds_no_response(
    requests_mock: Any,
) -> None:
    """A refusal raised from decoded JSON has no response to hold."""
    requests_mock.get("https://example.com/v1/notes/", json="not a page")

    with pytest.raises(ResponseShapeError, match="must be lists") as info:
        list(Note.filter())

    assert info.value.response is None
    assert ResponseShapeError("refused").response is None


def test_a_response_shape_error_pickles_with_its_response(
    requests_mock: Any,
) -> None:
    """The response reaches another process with the error."""
    requests_mock.get("https://example.com/v1/notes/1/", text="not json")

    with pytest.raises(ResponseShapeError) as info:
        _ = Note(id=1).text
    copy = pickle.loads(pickle.dumps(info.value))

    assert type(copy) is ResponseShapeError
    assert str(copy) == str(info.value)
    assert copy.response is not None
    assert copy.response.status_code == 200
    assert copy.response.text == "not json"


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


@pytest.mark.parametrize(
    ("names", "shown", "tail"),
    [
        (["x" * 40], "'" + "x" * 40 + "'", None),
        (["x" * 41], "'" + "x" * 40 + "...'", None),
        ([f"n{i}" for i in range(10)], "'n9'", None),
        ([f"n{i}" for i in range(11)], "'n8'", "and 1 more"),
    ],
    ids=["40_characters", "41_characters", "ten_names", "eleven_names"],
)
def test_error_body_names_at_the_bounds(
    requests_mock: Any, names: list[str], shown: str, tail: str | None
) -> None:
    """A name of 40 characters is whole, and ten names leave nothing out.

    Args:
        names: Names of the error body.
        shown: What the message must show of the last name that it names.
        tail: What it says of the names left out, or ``None`` for nothing.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/x",
        status_code=400,
        json=dict.fromkeys(names, 1),
    )

    with pytest.raises(APIError) as raised:
        api.get("/x")

    message = str(raised.value)
    assert shown in message
    if tail is None:
        assert "more" not in message
    else:
        assert tail in message


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
    collect(
        lambda: _check_final_response(
            api.get("/redirect", allow_redirects=False), "the query is empty"
        )
    )
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
    assert plain + "fail?..." in str(errors[0])
    assert plain + "html?..." in str(errors[1])
    assert isinstance(errors[0], APIError)
    assert "dummy-pass" in errors[0].response.url
    assert "dummy-pass" in str(errors[0].request.url)


def test_error_messages_of_a_url_without_credentials_show_its_path(
    requests_mock: Any,
) -> None:
    """A URL that has no user and password shows its path and a marker."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/x?a=b", status_code=500)

    with pytest.raises(APIError) as raised:
        api.get("/x?a=b")

    assert "for GET https://example.com/v1/x?..." in str(raised.value)


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
    "location",
    [
        # Requests prepares the percent-encoded destination, but not the target
        # joined onto the request's URL as it was given.
        "https://%evil.test",
        # Requests prepares the joined target, but not the destination, whose
        # leading tab urlparse() removes.
        "\t//evil.test",
    ],
)
def test_followed_redirect_is_refused_if_either_reading_cannot_be_prepared(
    requests_mock: Any, location: str
) -> None:
    """A followed redirect needs both readings of its target to be prepared.

    Args:
        location: The redirect's ``Location`` header.
    """
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="requests cannot prepare") as err:
        api.get("/a")

    assert "evil.test" not in str(err.value)


@pytest.mark.parametrize(
    "location",
    [
        "https://alice:hunter2@evil.test/p?q=SECRET#SECRET",
        "https://alice:hunter2\\@evil.test/p?q=SECRET",
        "https://evil.test\\@example.com/p?q=SECRET",
        "https://example.com\\@evil.test/p?q=SECRET",
        "https://alice:hunter2#@evil.test/p?q=SECRET",
        "https://evil.test?@example.com/p#SECRET",
        "https://evil.test/p?@example.com#SECRET",
        "\x01https://alice:hunter2@evil.test/p?q=SECRET",
        "https://alice:hunter2%40evil.test/p?q=SECRET",
        "https://evil.test/p\x7f\n?q=SECRET",
        "//alice:hunter2@evil.test/p?q=SECRET",
        "https://[2001:DB8::1]:8443/p?q=SECRET",
        "http://Alice:hunter2@H.TEST:8080/x[1]",
        "https://b\u00fccher.test/p?q=SECRET",
        "https://files.test/obj?X-Amz-Signature=SECRETSIG#SECRETFRAG",
        "mailto:alice:hunter2@evil.test?subject=SECRET",
        "ftp://alice:hunter2@evil.test/x",
        "ftp:///alice:hunter2?@[v1.x/a b",
        "\x01mailto:alice:hunter2@evil.test/x@y",
        "MailTo:alice:hunter2@evil.test:abc/x",
    ],
)
def test_refused_redirect_quotes_no_part_of_the_location(
    requests_mock: Any, location: str
) -> None:
    """A redirect that is not followed is named by the request alone.

    The message holds no origin, user, password, path, query, or fragment of
    the target, however requests reads it. The error's response holds the
    header.

    Args:
        location: The redirect's ``Location`` header.
    """
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        # requests decodes a header's bytes as Latin-1, then as UTF-8.
        headers={"Location": location.encode("utf-8").decode("latin-1")},
    )

    with pytest.raises(RedirectError) as raised:
        api.get("/a")

    response = raised.value.response
    assert response is not None
    target = api.session.get_redirect_target(response)
    assert target is not None
    if _prepare_redirect(api.session, response, target) is None:
        outcome = (
            "to a location that requests cannot prepare, so it was not "
            "followed. The response of this error holds the Location header."
        )
    else:
        outcome = (
            "to another origin, so it was not followed. The response of this "
            "error holds the Location header. Add its origin to "
            "API.redirect_origins to follow it."
        )
    assert str(raised.value) == (
        f"GET https://example.com/a was redirected with status 302 {outcome}"
    )
    for text in _printed(raised.value):
        for secret in (
            "SECRET",
            "hunter2",
            "alice",
            "evil",
            "files",
            "h.test",
        ):
            assert secret not in text
    assert response.headers["Location"] == location.encode("utf-8").decode(
        "latin-1"
    )


def _printed(error: BaseException) -> list[str]:
    """Return every text that logging an error prints from it.

    Args:
        error: The error.

    Returns:
        Its message, representation, arguments, and formatted traceback.
    """
    return [
        str(error),
        repr(error),
        repr(error.args),
        "".join(traceback.format_exception(error)),
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://alice:hunter2@Example.COM:443/v1/items/?token=SECRETQ#SECRETF",
        "//alice:hunter2@example.com/v1/items/?token=SECRETQ",
        "https://u:hunter2@example.com\\@e.test/x?token=SECRETQ",
        "https://other.example.net/v1/items/?token=SECRETQ",
        "/v1/items/\x1b[2Jforged?token=SECRETQ",
        "mailto:alice:hunter2@example.com?token=SECRETQ",
        "http://[::1/?token=SECRETQ",
        "https://example.com:99999/?token=SECRETQ",
    ],
)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_write_redirect_quotes_the_url_that_was_sent_and_not_the_location(
    requests_mock: Any, method: str, location: str
) -> None:
    """A write redirect names the request, and no part of its ``Location``.

    The error's response holds the header.

    Args:
        method: Lower-case method of the write.
        location: The redirect's ``Location`` header.
    """
    api = API("https://example.com/v1/")
    getattr(requests_mock, method)(
        "https://example.com/v1/items",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError) as raised:
        getattr(api, method)("items", json={"name": "Ada"})

    assert str(raised.value).startswith(
        f"{method.upper()} https://example.com/v1/items was answered with "
        "status 302, a redirect, which a write does not follow, so it was not "
        "resent."
    )
    for text in _printed(raised.value):
        for secret in ("alice", "hunter2", "SECRETQ", "e.test", "forged"):
            assert secret not in text
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == location


def test_write_redirect_to_an_allowed_origin_leaves_out_the_signed_target(
    requests_mock: Any,
) -> None:
    """An origin in ``redirect_origins`` does not make a write follow it."""
    api = API("https://api.test/")
    api.redirect_origins = ["https://files.test"]
    requests_mock.post(
        "https://api.test/w/",
        status_code=307,
        headers={"Location": f"https://files.test/o{_SIGNED}"},
    )
    target = requests_mock.post("https://files.test/o", json={})

    with pytest.raises(RedirectError) as raised:
        api.post("w/", json={"a": 1})

    assert str(raised.value).startswith(
        "POST https://api.test/w/ was answered with status 307"
    )
    assert not target.called
    _assert_unsigned(raised.value)


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


_SIGNED = "?X-Amz-Signature=SECRETSIG#SECRETFRAG"


def _download_api(requests_mock: Any) -> API:
    """Return an API whose download redirects to an allowed host.

    ``https://api.test/dl/`` redirects to ``https://files.test/o`` with a
    signed query and a fragment.

    Args:
        requests_mock: Mock of the HTTP adapter.

    Returns:
        The API, which allows ``https://files.test``.
    """
    api = API("https://api.test/")
    api.redirect_origins = ["https://files.test"]
    requests_mock.get(
        "https://api.test/dl/",
        status_code=302,
        headers={"Location": f"https://files.test/o{_SIGNED}"},
    )
    return api


def _assert_unsigned(error: BaseException) -> None:
    """Check that nothing printed from an error holds a signature.

    Args:
        error: The error.
    """
    for text in _printed(error):
        for secret in ("SECRETSIG", "SECRETFRAG", "X-Amz", "?"):
            assert secret not in text


def test_http_error_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A failure after a redirect names the URL that was asked for."""
    api = _download_api(requests_mock)
    requests_mock.get("https://files.test/o", status_code=403)

    with pytest.raises(APIError) as raised:
        api.get("dl/")

    assert str(raised.value) == (
        "403 Client Error: None for GET https://api.test/dl/"
    )
    _assert_unsigned(raised.value)
    assert raised.value.response is not None
    assert raised.value.response.url == f"https://files.test/o{_SIGNED}"


def test_size_limit_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """The response that a redirect reached is named by the URL asked for."""
    api = _download_api(requests_mock)
    api.max_response_bytes = 4
    requests_mock.get("https://files.test/o", content=b"0123456789")

    with pytest.raises(LimitExceeded, match="max_response_bytes") as raised:
        api.get("dl/")

    assert str(raised.value).startswith(
        "The response to GET https://api.test/dl/ is larger than 4 bytes"
    )
    _assert_unsigned(raised.value)


def test_size_limit_of_a_second_redirect_names_the_url_that_was_asked_for(
    requests_mock: Any,
) -> None:
    """A second redirect's body is named by the URL that was asked for."""
    api = _download_api(requests_mock)
    api.max_response_bytes = 4
    requests_mock.get(
        "https://files.test/o",
        status_code=302,
        headers={"Location": f"https://files.test/p{_SIGNED}"},
        content=b"0123456789",
    )

    with pytest.raises(LimitExceeded, match="max_response_bytes") as raised:
        api.get("dl/")

    assert str(raised.value).startswith(
        "The response to GET https://api.test/dl/ is larger than 4 bytes"
    )
    _assert_unsigned(raised.value)


def test_total_timeout_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline of a request names the URL that was asked for."""
    api = _download_api(requests_mock)
    api.total_timeout = 100.0
    requests_mock.get("https://files.test/o", content=b"body")
    clock = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 500.0, 500.0, 500.0, 500.0])
    monkeypatch.setattr("eazyrest.api.time.monotonic", lambda: next(clock))

    with pytest.raises(LimitExceeded, match="total_timeout") as raised:
        api.get("dl/")

    assert str(raised.value).startswith(
        "GET https://api.test/dl/ took at least 100.0 seconds"
    )
    _assert_unsigned(raised.value)


def test_unfollowed_final_redirect_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A final 3xx that a redirect reached is named by the URL asked for."""
    api = _download_api(requests_mock)
    requests_mock.get("https://files.test/o", status_code=300)
    resp = api.get("dl/")

    with pytest.raises(RedirectError) as raised:
        _check_final_response(resp, "the query has no collection to read")

    assert str(raised.value) == (
        "GET https://api.test/dl/ returned status 300, so the query has no "
        "collection to read"
    )
    _assert_unsigned(raised.value)


def test_shape_error_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A body that is not JSON is named by the URL that was asked for."""
    api = _download_api(requests_mock)
    requests_mock.get("https://files.test/o", content=b"not json")
    resp = api.get("dl/")

    with pytest.raises(ResponseShapeError) as raised:
        api.json(resp)

    assert str(raised.value) == (
        "The response to GET https://api.test/dl/, with status 200, does "
        "not hold JSON"
    )
    _assert_unsigned(raised.value)


def test_depth_limit_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """The JSON depth limit names the URL that was asked for."""
    api = _download_api(requests_mock)
    api.max_json_depth = 1
    requests_mock.get("https://files.test/o", json=[[[1]]])
    resp = api.get("dl/")

    with pytest.raises(LimitExceeded, match="max_json_depth") as raised:
        api.json(resp)

    assert str(raised.value).startswith(
        "The response to GET https://api.test/dl/, with status 200, holds "
        "JSON nested more than 1 levels deep"
    )
    _assert_unsigned(raised.value)


def test_redirect_refused_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """The guard names the URL that was asked for, not the one it reached."""
    api = _download_api(requests_mock)
    requests_mock.get(
        "https://files.test/o",
        status_code=302,
        headers={"Location": f"https://evil.test/x{_SIGNED}"},
    )

    with pytest.raises(RedirectError) as raised:
        api.get("dl/")

    assert str(raised.value).startswith(
        "GET https://api.test/dl/ was redirected with status 302 to "
        "another origin, so it was not followed."
    )
    _assert_unsigned(raised.value)


@pytest.mark.parametrize("location", ["http://[::1/p", "https://a:b@h:x/p"])
def test_unquotable_redirect_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any, location: str
) -> None:
    """A target that cannot be read is reported at the URL that was asked for.

    Args:
        location: The second redirect's ``Location`` header.
    """
    api = _download_api(requests_mock)
    requests_mock.get(
        "https://files.test/o",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError) as raised:
        api.get("dl/")

    assert str(raised.value).startswith(
        "GET https://api.test/dl/ was redirected with status 302 to a "
        "location that "
    )
    _assert_unsigned(raised.value)


def test_caller_url_shows_a_marker_for_its_query_in_each_message(
    requests_mock: Any,
) -> None:
    """A request that no redirect reached shows its origin and path.

    A query shows as ``?...``.
    """
    api = API("https://api.test/")
    requests_mock.get("https://api.test/a", status_code=403)
    requests_mock.get("https://api.test/b", content=b"not json")
    requests_mock.get("https://api.test/c", content=b"0123456789")

    with pytest.raises(APIError) as failed:
        api.get("a?key=GIVEN")
    with pytest.raises(ResponseShapeError) as shaped:
        api.json(api.get("b?key=GIVEN"))
    api.max_response_bytes = 4
    with pytest.raises(LimitExceeded) as large:
        api.get("c?key=GIVEN")

    assert str(failed.value).endswith("for GET https://api.test/a?...")
    assert str(shaped.value).startswith(
        "The response to GET https://api.test/b?..., with status 200"
    )
    assert str(large.value).startswith(
        "The response to GET https://api.test/c?... is larger"
    )


def test_page_redirect_to_another_origin_names_no_origin(
    requests_mock: Any,
) -> None:
    """A next-page request's redirect is refused without naming its target."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://evil.test\\@api.test/p?q=SECRET"},
    )

    with pytest.raises(PaginationError) as raised:
        list(
            follow_next_links(
                api, {"next": "https://api.test/p", "results": []}
            )
        )

    assert str(raised.value) == (
        "The request for page 2 of 'https://api.test' was redirected to a "
        "different origin from the API's base URL 'https://api.test/'. Pass "
        "allow_other_origins=True to follow it."
    )
    for text in _printed(raised.value):
        assert "SECRET" not in text
        assert "evil" not in text


@pytest.fixture
def redirecting_server() -> Iterator[Callable[[Callable[[Any], None]], str]]:
    """Serve a signed redirect and its target on a local port.

    A request for ``/dl`` is answered with a redirect to ``/o`` with a signed
    query on the same origin, and any other request is answered by the handler
    that the test installs.

    Yields:
        Function that installs the handler of the target, which receives the
        request handler object, and returns the server's base URL.
    """
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(handle: Callable[[Any], None]) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.startswith("/dl"):
                    self.send_response(302)
                    self.send_header("Location", f"/o{_SIGNED[:-12]}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
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


class _LoginHandler(AuthBase):
    """Authentication handler whose hook sends to the login URL a 401 names."""

    def __call__(
        self, r: requests.PreparedRequest
    ) -> requests.PreparedRequest:
        """Register the hook, as an authentication handler does.

        Args:
            r: Request that requests is preparing.

        Returns:
            The request.
        """
        # requests does not annotate register_hook().
        r.register_hook("response", self._login)  # type: ignore[no-untyped-call]
        return r

    @staticmethod
    def _login(resp: requests.Response, **kwargs: Any) -> requests.Response:
        """Answer a 401 with the response for the URL that it names.

        Args:
            resp: Response, which is replaced if it is a 401.
            kwargs: Arguments that requests passes to response hooks.

        Returns:
            The response for the login URL, or ``resp``.
        """
        if resp.status_code != 401:
            return resp
        login = requests.Request("GET", resp.headers["X-Login"]).prepare()
        return resp.connection.send(login)


def test_error_after_an_authentication_hook_names_the_url_asked_for(
    redirecting_server: Callable[[Callable[[Any], None]], str],
) -> None:
    """An authentication hook's URL is not the one that was asked for."""

    def login(handler: Any) -> None:
        if handler.path.startswith("/a"):
            handler.send_response(401)
            handler.send_header(
                "X-Login",
                f"http://{handler.headers['Host']}/login{_SIGNED[:-12]}",
            )
        else:
            handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    base = redirecting_server(login)
    api = API(base)
    api.session.auth = _LoginHandler()

    with pytest.raises(APIError) as raised:
        api.get("a?CALLERQ=1")

    assert str(raised.value) == (
        f"403 Client Error: Forbidden for GET {base}a?..."
    )
    _assert_no_signature(raised.value)


def test_size_limit_of_a_redirected_response_names_the_url_asked_for(
    redirecting_server: Callable[[Callable[[Any], None]], str],
) -> None:
    """The adapter names the URL that was asked for, not the target."""

    def large(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"x" * 100)

    base = redirecting_server(large)
    api = API(base)
    api.max_response_bytes = 10

    with pytest.raises(LimitExceeded, match="max_response_bytes") as raised:
        api.get("dl")

    assert str(raised.value).startswith(
        f"The response to GET {base}dl is larger than 10 bytes"
    )
    _assert_unsigned(raised.value)


def test_total_timeout_of_a_redirected_response_names_the_url_asked_for(
    redirecting_server: Callable[[Callable[[Any], None]], str],
) -> None:
    """The adapter names the URL that was asked for, not a stalled target."""

    def stalled(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.write(b"01")
        handler.wfile.flush()
        time.sleep(0.6)

    base = redirecting_server(stalled)
    api = API(base)
    api.timeout = None
    api.total_timeout = 0.2

    with pytest.raises(LimitExceeded, match="total_timeout") as raised:
        api.get("dl")

    assert str(raised.value).startswith(
        f"GET {base}dl took at least 0.2 seconds"
    )
    _assert_unsigned(raised.value)


def test_size_limit_of_a_caller_url_shows_a_marker_for_its_query(
    redirecting_server: Callable[[Callable[[Any], None]], str],
) -> None:
    """The adapter names the caller's URL, with ?... for a query."""

    def large(handler: Any) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"x" * 100)

    base = redirecting_server(large)
    api = API(base)
    api.max_response_bytes = 10

    with pytest.raises(LimitExceeded, match="max_response_bytes") as raised:
        api.get("o?CALLERQ=1")

    assert str(raised.value).startswith(
        f"The response to GET {base}o?... is larger than 10 bytes"
    )


def _resend_with_a_new_token(
    resp: requests.Response, **kwargs: Any
) -> requests.Response:
    """Answer a 401 by sending the request again, as a session hook may.

    Args:
        resp: Response, which is replaced if it is a 401.
        kwargs: Arguments that requests passes to response hooks.

    Returns:
        The response to the resent request, or ``resp``.
    """
    if resp.status_code != 401:
        return resp
    prepared = resp.request.copy()
    prepared.headers["Authorization"] = "Bearer new"
    resent = resp.connection.send(prepared, **kwargs)
    resent.history.append(resp)
    resent.request = prepared
    return resent


def test_redirect_refused_after_a_hook_resend_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A hook's new response is named by the URL that was asked for."""
    api = _download_api(requests_mock)
    api.session.hooks["response"] = [_resend_with_a_new_token]
    requests_mock.get(
        "https://files.test/o",
        [
            {"status_code": 401},
            {
                "status_code": 302,
                "headers": {"Location": "https://evil.test/z"},
            },
        ],
    )

    with pytest.raises(RedirectError) as raised:
        api.get("dl/")

    assert str(raised.value).startswith(
        "GET https://api.test/dl/ was redirected with status 302 to "
        "another origin, so it was not followed."
    )
    _assert_unsigned(raised.value)


def test_redirect_refused_after_a_hook_resent_the_given_url_shows_its_marker(
    requests_mock: Any,
) -> None:
    """The URL that the caller gave is named with ?... after a resend."""
    api = API("https://api.test/")
    api.session.hooks["response"] = [_resend_with_a_new_token]
    requests_mock.get(
        "https://api.test/x",
        [
            {"status_code": 401},
            {
                "status_code": 302,
                "headers": {"Location": "https://evil.test/z"},
            },
        ],
    )

    with pytest.raises(RedirectError) as raised:
        api.get("x?CALLERQ=1")

    assert str(raised.value).startswith(
        "GET https://api.test/x?... was redirected with status 302 to "
        "another origin, so it was not followed."
    )


def test_page_link_refused_after_a_hook_resent_it_is_named_by_position(
    requests_mock: Any,
) -> None:
    """A page link is named by its position after a resend."""
    api = API("https://api.test/")
    api.session.hooks["response"] = [_resend_with_a_new_token]
    requests_mock.get(
        "https://api.test/p",
        [
            {"status_code": 401},
            {
                "status_code": 302,
                "headers": {"Location": "https://evil.test/z"},
            },
        ],
    )

    with pytest.raises(PaginationError) as raised:
        list(
            follow_next_links(
                api, {"next": "https://api.test/p?cursor=KEPT", "results": []}
            )
        )

    assert str(raised.value).startswith(
        "The request for page 2 of 'https://api.test' was redirected to "
        "a different origin"
    )


def _assert_no_signature(error: BaseException) -> None:
    """Check that nothing printed from an error holds the signed query.

    Args:
        error: The error.
    """
    for text in _printed(error):
        assert "SECRETSIG" not in text
        assert "SECRETFRAG" not in text


def _answer_with_another_url(
    resp: requests.Response, **kwargs: Any
) -> requests.Response:
    """Answer a response with one for a signed URL that a hook chose.

    Args:
        resp: Response, which is replaced unless it is the hook's own.
        kwargs: Arguments that requests passes to response hooks.

    Returns:
        The response to the signed URL, which has no history.
    """
    if resp.url.startswith("https://files.test/"):
        return resp
    return resp.connection.send(
        requests.Request(
            "GET", f"https://files.test/hooked{_SIGNED}"
        ).prepare()
    )


def test_error_of_a_response_that_a_hook_substituted_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A response for a URL that a hook chose is named by the URL asked for."""
    api = API("https://api.test/")
    api.session.hooks["response"] = [_answer_with_another_url]
    requests_mock.get("https://api.test/h", status_code=200)
    requests_mock.get("https://files.test/hooked", status_code=403)

    with pytest.raises(APIError) as raised:
        api.get("h?CALLERQ=1")

    assert str(raised.value) == (
        "403 Client Error: None for GET https://api.test/h?..."
    )
    _assert_no_signature(raised.value)


def test_messages_of_one_request_agree_on_a_url_that_a_hook_chose(
    requests_mock: Any,
) -> None:
    """A size error and an HTTP error quote the same URL for one request."""
    api = API("https://api.test/")
    api.max_response_bytes = 10
    api.session.hooks["response"] = [_answer_with_another_url]
    requests_mock.get("https://api.test/h", status_code=200)
    requests_mock.get("https://files.test/hooked", content=b"x" * 100)

    with pytest.raises(LimitExceeded) as raised:
        api.get("h?CALLERQ=1")

    assert str(raised.value).startswith(
        "The response to GET https://api.test/h?... is larger"
    )
    _assert_no_signature(raised.value)


def test_error_of_a_response_whose_url_a_hook_rewrote_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A hook that changes a response's URL does not change the name."""

    def rewrite(resp: requests.Response, **kwargs: Any) -> requests.Response:
        resp.url = f"https://files.test/rw{_SIGNED}"
        return resp

    api = API("https://api.test/")
    api.session.hooks["response"] = [rewrite]
    requests_mock.get("https://api.test/rw", status_code=403)

    with pytest.raises(APIError) as raised:
        api.get("rw?CALLERQ=1")

    assert str(raised.value) == (
        "403 Client Error: None for GET https://api.test/rw?..."
    )
    _assert_no_signature(raised.value)


def test_hook_error_after_a_redirect_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A hook's error for a followed redirect does not quote its signed URL.

    The hook runs while the request is in flight, before the response is
    recorded as the answer to the URL asked for.
    """
    api = API("https://api.test/")

    def decode(resp: requests.Response, **kwargs: Any) -> requests.Response:
        if not resp.is_redirect:
            api.json(resp)
        return resp

    api.session.hooks["response"] = [decode]
    requests_mock.get(
        "https://api.test/dl/",
        status_code=302,
        headers={"Location": f"https://api.test/blob{_SIGNED}"},
    )
    requests_mock.get(f"https://api.test/blob{_SIGNED}", text="not json")

    with pytest.raises(ResponseShapeError) as raised:
        api.get("dl/")

    assert "GET https://api.test/dl/" in str(raised.value)
    _assert_no_signature(raised.value)


@pytest.mark.parametrize(
    ("url", "named"),
    [
        ("https://alice:hunter2@api.test/a?key=GIVEN", "https://api.test"),
        ("https://api.test/b", "https://api.test"),
    ],
)
def test_response_that_no_api_request_returned_is_named_by_its_own_url(
    url: str, named: str
) -> None:
    """A response with no recorded request is named by its URL's origin.

    Args:
        url: URL of the response.
        named: What the name shows of the URL.
    """
    resp = requests.Response()
    resp.status_code = 500
    resp.url = url
    resp.request = requests.Request("GET", url).prepare()

    assert _response_name(resp) == f"GET {named}"


def test_page_redirect_is_checked_however_the_subclasss_get_sends(
    requests_mock: Any,
) -> None:
    """A get() that sends through the session does not exempt a page."""

    class Direct(API):
        def get(self, uri: str, *args: Any, **kwargs: Any) -> Any:
            return self.session.get(self.resolve_url(uri), *args, **kwargs)

    api = Direct("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://other.test/z"},
    )
    other = requests_mock.get(
        "https://other.test/z", json={"results": [{"id": 1}]}
    )

    with pytest.raises(PaginationError, match="different origin"):
        list(
            follow_next_links(
                api,
                {"next": "https://api.test/p?cursor=KEPT", "results": []},
            )
        )

    assert not other.called


def test_http_error_of_a_url_that_redirected_to_itself_names_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A redirect from ``/o`` to ``/o`` does not change the URL named."""
    api = _download_api(requests_mock)
    requests_mock.get(
        "https://files.test/o",
        [
            {
                "status_code": 302,
                "headers": {"Location": f"https://files.test/o{_SIGNED}"},
            },
            {"status_code": 403},
        ],
    )

    with pytest.raises(APIError) as raised:
        api.get("dl/")

    assert str(raised.value) == (
        "403 Client Error: None for GET https://api.test/dl/"
    )


def _response_with_status(status_code: int) -> requests.Response:
    """Return a response of the given status for a mocked request."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://example.com/items/1"
    response.reason = "Reason"
    response.request = requests.Request(
        "GET", "https://example.com/items/1"
    ).prepare()
    return response


@pytest.mark.parametrize(
    ("status_code", "kind"),
    [
        (400, "Client"),
        (499, "Client"),
        (500, "Server"),
        (599, "Server"),
    ],
)
def test_the_edges_of_the_error_statuses_raise_as_client_or_server_errors(
    status_code: int, kind: str
) -> None:
    """Statuses 400 and 499 are client errors, 500 and 599 server errors."""
    api = API("https://example.com/")

    with pytest.raises(APIError, match=rf"^{status_code} {kind} Error"):
        api.raise_for_response(_response_with_status(status_code))


@pytest.mark.parametrize("status_code", [399, 600])
def test_the_statuses_beside_the_error_range_do_not_raise(
    status_code: int,
) -> None:
    """A status below 400 or above 599 is not a failure."""
    api = API("https://example.com/")

    api.raise_for_response(_response_with_status(status_code))
