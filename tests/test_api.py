"""Tests for the HTTP API client wrapper."""

from __future__ import annotations

import copy
import http.server
import json
import os
import pickle
import re
import threading
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import requests
import requests.auth
import urllib3
from hypothesis import given, settings
from hypothesis import strategies as st
from requests_mock import ANY as requests_mock_ANY
from requests_mock import Mocker, create_response

from eazyrest import API, JSONObject, LimitExceeded, RedirectError, Response
from eazyrest.api import (
    _canonical_url,
    _prepare_server_url,
    _redirect_destination,
    _ScopedAPI,
)


def test_api_preserves_base_path_prefix_in_relative_requests(
    requests_mock: Any,
) -> None:
    """Relative resource paths keep the base URL path prefix."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/users/1", json={"id": 1})

    response = api.get("/users/1")

    assert response.json() == {"id": 1}


def test_api_allows_absolute_request_urls(requests_mock: Any) -> None:
    """Absolute URLs bypass base URL resolution."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://other.example.com/users/1", json={"id": 1})

    response = api.get("https://other.example.com/users/1")

    assert response.json() == {"id": 1}


def test_api_put_uses_resolved_url(requests_mock: Any) -> None:
    """PUT requests should use the same URL resolution as other methods."""
    api = API("https://example.com/v1/")
    requests_mock.put("https://example.com/v1/users/1", json={"id": 1})

    response = api.put("/users/1", json={"name": "Ada"})

    assert response.json() == {"id": 1}


@pytest.mark.parametrize("allow_redirects", [None, True])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_api_never_follows_a_redirect_of_a_write(
    requests_mock: Any,
    method: str,
    status: int,
    allow_redirects: bool | None,
) -> None:
    """A write answered with a redirect raises and sends nothing further.

    The ``Location`` is on the error's response and not in its message, and an
    explicit ``allow_redirects=True`` does not make the write follow it.

    Args:
        method: Name of the ``API`` method that sends the write, such as
            ``"post"``.
        status: Redirect status code that the server returns for the write.
        allow_redirects: Value that the caller passes, or ``None`` for none.
    """
    api = API("https://example.com/v1/")
    location = "https://example.com/v1/users/1/?signature=abc123"
    source = requests_mock.request(
        method.upper(),
        "https://example.com/v1/users/1",
        status_code=status,
        headers={"Location": location},
    )
    destination = requests_mock.request(
        requests_mock_ANY, "https://example.com/v1/users/1/", json={"id": 1}
    )
    options = {} if allow_redirects is None else {"allow_redirects": True}

    with pytest.raises(RedirectError) as info:
        getattr(api, method)("/users/1", json={"name": "Ada"}, **options)

    message = str(info.value)
    assert f"{method.upper()} https://example.com/v1/users/1 was" in message
    assert f"status {status}" in message
    assert "abc123" not in message
    assert "users/1/" not in message
    assert info.value.response is not None
    assert info.value.response.status_code == status
    assert info.value.response.headers["Location"] == location
    assert source.call_count == 1
    assert not destination.called


@pytest.mark.parametrize("allow_redirects", [None, True])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_api_never_follows_a_redirect_of_a_request_with_a_body(
    requests_mock: Any,
    status: int,
    allow_redirects: bool | None,
) -> None:
    """A ``GET`` with a body raises and sends nothing further.

    requests drops the body of a 301, 302, or 303 when it follows one, so the
    server would answer a different request from the one that was made.

    Args:
        status: Redirect status code that the server returns.
        allow_redirects: Value that the caller passes, or ``None`` for none.
    """
    api = API("https://example.com/v1/")
    source = requests_mock.get(
        "https://example.com/v1/users/1",
        status_code=status,
        headers={"Location": "https://example.com/v1/users/1/?sig=abc123"},
    )
    destination = requests_mock.request(
        requests_mock_ANY, "https://example.com/v1/users/1/", json={"id": 1}
    )
    options = {} if allow_redirects is None else {"allow_redirects": True}

    with pytest.raises(RedirectError) as info:
        api.get("/users/1", json={"q": 1}, **options)

    message = str(info.value)
    assert "GET https://example.com/v1/users/1 was" in message
    assert "which a request with a body does not follow" in message
    assert "abc123" not in message
    assert info.value.response is not None
    assert info.value.response.status_code == status
    assert source.call_count == 1
    assert not destination.called


@pytest.mark.parametrize(
    "body",
    [
        {"json": {}},
        {"json": []},
        {"json": 0},
        {"data": b"x"},
        {"data": {"a": "1"}},
        {"data": iter([b"x"])},
        {"files": {"f": b"x"}},
    ],
)
def test_api_get_with_any_body_follows_no_redirect(
    requests_mock: Any, body: dict[str, Any]
) -> None:
    """Each argument that makes requests send a body stops a redirect.

    Args:
        body: Keyword arguments of a ``GET`` that carry a body.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/a",
        status_code=302,
        headers={"Location": "https://example.com/v1/b"},
    )
    other = requests_mock.get("https://example.com/v1/b", json={})

    with pytest.raises(RedirectError):
        api.get("/a", **body)

    assert not other.called


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"json": None},
        {"data": None},
        {"data": {}},
        {"data": b""},
        {"files": {}},
    ],
)
def test_api_get_without_a_body_follows_a_same_origin_redirect(
    requests_mock: Any, body: dict[str, Any]
) -> None:
    """A ``GET`` that sends no body still follows a same-origin redirect.

    Args:
        body: Keyword arguments of a ``GET`` that carry no body.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/a",
        status_code=301,
        headers={"Location": "https://example.com/v1/b"},
    )
    requests_mock.get("https://example.com/v1/b", json={"id": 1})

    response = api.get("/a", **body)

    assert response.json() == {"id": 1}


@pytest.mark.parametrize("status", [301, 307])
def test_api_refuses_a_redirect_of_a_write_to_another_origin(
    requests_mock: Any, status: int
) -> None:
    """A write's redirect to another origin sends no header or body there.

    Args:
        status: Redirect status code that the server returns.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "secret"
    requests_mock.post(
        "https://example.com/v1/users/",
        status_code=status,
        headers={"Location": "https://other.example.net/users/"},
    )
    other = requests_mock.post("https://other.example.net/users/", json={})

    with pytest.raises(RedirectError) as info:
        api.post("/users/", json={"password": "secret"})

    assert "other.example.net" not in str(info.value)
    assert not other.called


@pytest.mark.parametrize(
    "location", ["http://[::1/p", "http://example.com\uff0f.evil.test/p"]
)
def test_unparsable_redirect_raises_redirect_error(
    requests_mock: Any, location: str
) -> None:
    """A redirect to a location that cannot be parsed raises RedirectError.

    Args:
        location: The redirect's ``Location`` header.
    """
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/r",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="cannot be parsed"):
        api.get("/r")


UNPARSABLE_LOCATIONS = ["/n\x85x", "http://[bad/", "//[::1"]


@pytest.mark.parametrize("location", UNPARSABLE_LOCATIONS)
def test_unfollowed_unparsable_redirect_raises_redirect_error(
    requests_mock: Any, location: str
) -> None:
    """A ``Location`` that cannot be parsed raises, though nothing follows it.

    Requests parses the ``Location`` of an unfollowed redirect too, and raises
    ``UnicodeDecodeError`` or ``ValueError`` out of ``send()``, where no caller
    expects either.

    Args:
        location: The redirect's ``Location`` header.
    """
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/r",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="cannot prepare"):
        api.get("/r", allow_redirects=False)
    with pytest.raises(RedirectError, match="cannot prepare"):
        api._scoped({"allow_redirects": False}).get("/r")


# A port that urlsplit() accepts until it is read. requests reads it when it
# compares the redirect's host with the request's to decide whether to strip
# credentials, or to decide whether the host bypasses proxies.
UNREADABLE_PORT_CASES = [
    ("http://127.0.0.1:99999/x", "authorization"),
    ("http://127.0.0.1:abc/x", "authorization"),
    ("http://localhost:99999/x", "no_proxy"),
    ("http://localhost:abc/x", "no_proxy"),
]


@pytest.mark.parametrize(("location", "setup"), UNREADABLE_PORT_CASES)
def test_unfollowed_redirect_with_an_unreadable_port_raises_redirect_error(
    redirect_server: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    setup: str,
) -> None:
    """Requests cannot prepare the next request, so the redirect is refused.

    Args:
        location: The redirect's ``Location`` header.
        setup: ``authorization`` for a session that sends credentials, or
            ``no_proxy`` for an environment that lists hosts to reach directly.
    """
    api = API(redirect_server(location))
    if setup == "authorization":
        api.session.headers["Authorization"] = "Token dummy"
    else:
        monkeypatch.setenv("NO_PROXY", "example.invalid")

    with pytest.raises(RedirectError, match="cannot prepare"):
        api.get("/r", allow_redirects=False)
    with pytest.raises(RedirectError, match="cannot prepare"):
        api._scoped({"allow_redirects": False}).get("/r")


def test_unfollowed_redirect_passes_on_the_other_failures_of_requests(
    requests_mock: Any,
) -> None:
    """Only ``ValueError`` and ``UnicodeError`` become ``RedirectError``.

    Requests raises ``TooManyRedirects`` when ``max_redirects`` is 0, and
    ``UnrewindableBodyError`` for a 307 that resends a body that cannot rewind.
    Both reach the caller as they do without eazyrest.
    """
    requests_mock.get(
        "https://example.com/r",
        status_code=302,
        headers={"Location": "/elsewhere"},
    )
    requests_mock.post(
        "https://example.com/up",
        status_code=307,
        headers={"Location": "/elsewhere"},
    )
    api = API("https://example.com/")
    api.session.max_redirects = 0
    with pytest.raises(requests.TooManyRedirects):
        api.get("/r", allow_redirects=False)

    read_end, write_end = os.pipe()
    os.write(write_end, b"dummy")
    os.close(write_end)
    with (
        os.fdopen(read_end, "rb") as body,
        pytest.raises(requests.exceptions.UnrewindableBodyError),
    ):
        API("https://example.com/").post(
            "/up", data=body, allow_redirects=False
        )


def test_unfollowed_redirect_keeps_the_cookies_of_an_authentication_challenge(
    digest_redirect_server: Callable[[str], str],
) -> None:
    """The session stores the cookie of a challenge that a handler answered."""
    api = API(digest_redirect_server("/elsewhere"))
    api.session.auth = requests.auth.HTTPDigestAuth("user", "dummy")

    resp = api.get("/", allow_redirects=False)

    assert resp.status_code == 302
    assert {cookie.name for cookie in api.session.cookies} == {
        "challenge",
        "redirect",
    }


def test_unfollowed_redirect_after_digest_authentication_returns_a_bad_port(
    digest_redirect_server: Callable[[str], str],
) -> None:
    """A port that requests prepares without error is not refused.

    requests compares ports for a redirect that keeps the host only when the
    request it resent carries an ``Authorization`` header. It prepares the next
    request from the original request, which has none.
    """
    api = API(digest_redirect_server("http://127.0.0.1:99999/x"))
    api.session.auth = requests.auth.HTTPDigestAuth("user", "dummy")

    resp = api.get("/", allow_redirects=False)

    assert resp.status_code == 302


@pytest.mark.parametrize(
    "location", ["http://127.0.0.1:99999/x", "http://localhost:abc/x"]
)
def test_unfollowed_redirect_that_requests_prepares_is_returned(
    redirect_server: Callable[[str], str], location: str
) -> None:
    """Without credentials or a proxy bypass list, requests reads no port.

    Args:
        location: A ``Location`` whose port cannot be read.
    """
    api = API(redirect_server(location))

    resp = api.get("/r", allow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == location


def test_unfollowed_redirect_to_another_host_with_a_bad_port_is_returned(
    redirect_server: Callable[[str], str],
) -> None:
    """A redirect that leaves the host makes requests read no port."""
    api = API(redirect_server("http://other.test:99999/x"))
    api.session.headers["Authorization"] = "Token dummy"

    assert api.get("/r", allow_redirects=False).status_code == 302


def test_unfollowed_redirect_uses_the_no_proxy_list_of_the_request(
    redirect_server: Callable[[str], str],
) -> None:
    """The request's own ``proxies`` reach requests, which reads the port."""
    api = API(redirect_server("http://localhost:99999/x"))
    options: dict[str, Any] = {
        "allow_redirects": False,
        "proxies": {"no_proxy": "example.invalid"},
    }

    with pytest.raises(RedirectError, match="cannot prepare") as raised:
        api.get("/r", **options)
    with pytest.raises(RedirectError, match="cannot prepare"):
        api._scoped(options).get("/r")

    message = str(raised.value)
    assert "GET http://127.0.0.1:" in message
    assert "status 302" in message
    assert "localhost" not in message
    assert "99999" not in message


def test_unfollowed_redirect_error_leaves_out_a_control_character() -> None:
    """The message has no control character of the ``Location``."""
    api = API("https://example.com/")
    with Mocker() as mock:
        mock.get(
            "https://example.com/r",
            status_code=302,
            headers={"Location": "http://[bad/\x85"},
        )
        with pytest.raises(RedirectError) as raised:
            api.get("/r", allow_redirects=False)

    assert "bad" not in str(raised.value)
    assert "\x85" not in str(raised.value)
    assert "%C2%85" not in str(raised.value)
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == "http://[bad/\x85"


@pytest.mark.parametrize("status", [200, 302])
def test_value_error_of_a_response_hook_propagates(
    requests_mock: Mocker, status: int
) -> None:
    """Only an error of requests' own preparation becomes ``RedirectError``.

    Args:
        status: Status of the response that the hook sees.
    """
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/r",
        status_code=status,
        headers={"Location": "/elsewhere"},
    )

    def hook(resp: requests.Response, **kwargs: Any) -> None:
        raise ValueError("from the hook")

    with pytest.raises(ValueError, match="from the hook") as raised:
        api.get("/r", allow_redirects=False, hooks={"response": [hook]})

    assert not isinstance(raised.value, RedirectError)


def test_digest_authentication_writes_once() -> None:
    """A write answered by a digest challenge is resent and succeeds."""
    received: list[Any] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PATCH(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
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
            received.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with API(f"http://127.0.0.1:{server.server_port}/") as api:
            api.session.auth = requests.auth.HTTPDigestAuth("user", "dummy")

            response = api.patch("/notes/1/", json={"title": "new"})
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 200
    assert received == [{"title": "new"}]


def test_api_follows_get_redirect(requests_mock: Any) -> None:
    """A redirected GET is still a GET, so it is followed."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/users/1",
        status_code=301,
        headers={"Location": "https://example.com/v1/users/1/"},
    )
    requests_mock.get("https://example.com/v1/users/1/", json={"id": 1})

    assert api.get("/users/1").json() == {"id": 1}


@pytest.mark.parametrize(
    ("method", "status", "target"),
    [
        ("get", 302, "https://other.example.net/users/1"),
        ("get", 308, "https://example.com:8443/v1/users/1"),
        ("get", 301, "http://example.com/v1/users/1"),
    ],
)
def test_api_refuses_redirect_to_other_origin(
    requests_mock: Any, method: str, status: int, target: str
) -> None:
    """A redirect to another origin raises before anything is sent there.

    requests would send custom credentials along.

    Args:
        method: Name of the ``API`` method that sends the request.
        status: Redirect status code that the server returns.
        target: Redirect destination, with a different scheme, host, or port.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "secret"
    requests_mock.request(
        method.upper(),
        "https://example.com/v1/users/1",
        status_code=status,
        headers={"Location": target},
    )
    other = requests_mock.request(method.upper(), target, json={})

    with pytest.raises(requests.HTTPError, match="redirect_origins") as info:
        getattr(api, method)("/users/1")

    assert info.value.response is not None
    assert info.value.response.status_code == status
    assert not other.called


def test_api_follows_redirect_to_listed_origin(requests_mock: Any) -> None:
    """``redirect_origins`` admits an origin, whatever the URL's path."""
    api = API("https://example.com/v1/")
    api.redirect_origins = ["https://FILES.example.com:443/ignored/path"]
    requests_mock.get(
        "https://example.com/v1/files/1",
        status_code=302,
        headers={"Location": "https://files.example.com/1"},
    )
    requests_mock.get("https://files.example.com/1", json={"id": 1})

    assert api.get("/files/1").json() == {"id": 1}


def test_api_follows_redirect_within_absolute_request_origin(
    requests_mock: Any,
) -> None:
    """An absolute request may be redirected within its own origin."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://other.example.com/users/1",
        status_code=301,
        headers={"Location": "/users/1/"},
    )
    requests_mock.get("https://other.example.com/users/1/", json={"id": 1})

    assert api.get("https://other.example.com/users/1").json() == {"id": 1}


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com\\@evil.example.net/steal",
        "https://example.com:443\\@evil.example.net/steal",
        "//example.com\\@evil.example.net/steal",
        # A browser reads the host before the backslash, and requests the one
        # after it, so the target must differ from the request's origin under
        # the reading that is not requests'.
        "https://evil.example.net\\@example.com/steal",
    ],
)
def test_api_refuses_backslash_authority_redirect(
    requests_mock: Any, target: str
) -> None:
    """A backslash in a redirect cannot hide another host from the guard.

    requests percent-encodes the backslash before following the redirect, so
    the text before ``@`` becomes userinfo and the request goes to the host
    after it, with every session header.

    Args:
        target: Redirect target whose host differs once requests encodes it.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "secret"
    requests_mock.get(
        "https://example.com/v1/users/",
        status_code=307,
        headers={"Location": target},
    )
    evil = requests_mock.get(
        re.compile("https://.*evil\\.example\\.net/.*"), json={}
    )

    with pytest.raises(requests.HTTPError, match="redirect_origins"):
        api.get("/users/")

    assert not evil.called


_LOCATION_TEXT = st.lists(
    st.sampled_from(
        [
            *"abc.:/\\@%?#[]-_ 019\t\u00e9",
            *["example.com", "evil.net", "//", "https:", "http:", ":443"],
        ]
    ),
    max_size=12,
).map("".join)


@given(location=_LOCATION_TEXT)
def test_redirect_destination_matches_requests(location: str) -> None:
    """The guard computes the same redirect URL that requests follows.

    Args:
        location: Generated ``Location`` header value.
    """
    session = requests.Session()
    resp = requests.Response()
    resp.status_code = 307
    resp.url = "https://example.com/v1/users/?page=1"
    resp.headers["Location"] = location
    resp.request = requests.Request("GET", resp.url).prepare()
    resp._content = b""
    resp._content_consumed = True  # type: ignore[attr-defined]
    try:
        target = session.get_redirect_target(resp)
    except UnicodeDecodeError:
        # requests cannot read the header either, so it follows nothing.
        return
    if not target:
        return

    try:
        followed = next(
            session.resolve_redirects(resp, resp.request, yield_requests=True)
        )
    except (ValueError, requests.RequestException):
        return

    assert _redirect_destination(resp, target) == followed.url


@pytest.mark.parametrize("on_request", [False, True])
def test_api_redirect_guard_keeps_response_hooks(
    requests_mock: Any, on_request: bool
) -> None:
    """Session or request response hooks still run beside the guard.

    Args:
        on_request: Whether the hook is passed with the request rather than
            installed on the session.
    """
    api = API("https://example.com/v1/")
    seen: list[int] = []

    def record(resp: requests.Response, **kwargs: Any) -> None:
        seen.append(resp.status_code)

    kwargs: dict[str, Any] = {}
    if on_request:
        kwargs["hooks"] = {"response": record}
    else:
        api.session.hooks["response"].append(record)
    requests_mock.get(
        "https://example.com/v1/users/1",
        status_code=302,
        headers={"Location": "https://other.example.net/users/1"},
    )

    with pytest.raises(requests.HTTPError, match="redirect_origins"):
        api.get("/users/1", **kwargs)

    assert seen == [302]


def test_redirect_guard_without_a_budget_checks_only_redirects(
    requests_mock: Any,
) -> None:
    """The guard's hooks check origins even without a budget.

    ``API`` passes each request's budget, which also limits the body of a
    redirect, as ``api.get()`` shows. Hooks made without one, for a request
    sent through the session directly, still refuse an unsafe redirect but
    leave the body alone.
    """
    api = API("https://example.com/v1/")
    api.max_response_bytes = 10
    requests_mock.get(
        "https://example.com/v1/a",
        status_code=302,
        headers={"Location": "/v1/b"},
        text="x" * 100,
    )
    requests_mock.get("https://example.com/v1/b", json={"id": 1})
    requests_mock.get(
        "https://example.com/v1/elsewhere",
        status_code=302,
        headers={"Location": "https://evil.example.net/"},
    )
    evil = requests_mock.get("https://evil.example.net/", json={})

    with pytest.raises(LimitExceeded):
        api.get("a")

    url = "https://example.com/v1/a"
    resp = api.session.get(url, hooks=api._guard_redirects(url, None))
    assert resp.json() == {"id": 1}

    url = "https://example.com/v1/elsewhere"
    with pytest.raises(RedirectError, match="redirect_origins"):
        api.session.get(url, hooks=api._guard_redirects(url, None))
    assert not evil.called


# Origins of the generated redirect chains: the API's own, one that the API
# allows with redirect_origins, and one that it does not.
_CHAIN_LOCATIONS = {
    "same": "https://example.com/v1/hop{}",
    "relative": "/v1/hop{}",
    "listed": "https://files.example.com/hop{}",
    "other": "https://evil.example.net/hop{}",
}
_ALLOWED_ORIGINS = {
    ("https", "example.com", 443),
    ("https", "files.example.com", 443),
}

_REDIRECT_CHAINS = st.lists(
    st.tuples(
        st.sampled_from([301, 302, 303, 307, 308]),
        st.sampled_from(sorted(_CHAIN_LOCATIONS)),
    ),
    min_size=1,
    max_size=4,
)


def _chain_origin(url: str) -> tuple[str, str | None, int | None]:
    """Return a URL's scheme, host, and port, with the default port filled in.

    Args:
        url: Absolute URL.

    Returns:
        The URL's origin.
    """
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or 443


def _send_through_chain(
    send: Any, method: str, body: dict[str, str] | None, chain: list[Any]
) -> tuple[list[tuple[str, str, bytes | None]], bool]:
    """Send a request through a mocked redirect chain and record each hop.

    Args:
        send: Callable that takes a method and keyword arguments and sends
            the request to ``https://example.com/v1/hop0``.
        method: HTTP method of the request.
        body: JSON body of the request, if any.
        chain: Status and location kind of each redirect, in order.

    Returns:
        The method, URL, and body of each request sent, and whether the
        sending raised ``RedirectError``.
    """

    def respond(request: Any) -> Any:
        """Answer hop ``i`` with its redirect, or the last hop with 200.

        Args:
            request: Request that requests_mock received.

        Returns:
            The mocked response.
        """
        hop = int(urlsplit(request.url).path.rsplit("hop", 1)[1])
        if hop == len(chain):
            return create_response(request, json={})
        status, kind = chain[hop]
        location = _CHAIN_LOCATIONS[kind].format(hop + 1)
        return create_response(
            request, status_code=status, headers={"Location": location}
        )

    with Mocker() as mock:
        mock.add_matcher(respond)
        refused = False
        try:
            send(method, **({} if body is None else {"json": body}))
        except RedirectError:
            refused = True
        sent = [
            (
                request.method,
                request.url,
                request.body
                if request.body is None or isinstance(request.body, bytes)
                else request.body.encode(),
            )
            for request in mock.request_history
        ]
    return sent, refused


@settings(max_examples=100, deadline=None)
@given(method=st.sampled_from(["GET", "HEAD"]), chain=_REDIRECT_CHAINS)
def test_redirect_chains_of_a_read_stop_before_another_origin(
    method: str, chain: list[Any]
) -> None:
    """A read follows what requests follows, up to an origin not allowed.

    Plain requests follows every hop of a generated chain. The client must
    send exactly the requests that plain requests sends until the first one
    that would go to an origin that the API does not allow. There it raises
    ``RedirectError`` without sending that request. A chain without such a
    request is followed to the end.

    Args:
        method: HTTP method of the original request.
        chain: Status and location kind of each redirect, in order.
    """
    url = "https://example.com/v1/hop0"
    session = requests.Session()
    plain, _ = _send_through_chain(
        lambda m, **kw: session.request(m, url, **kw), method, None, chain
    )
    api = API("https://example.com/v1/")
    api.redirect_origins = ["https://files.example.com"]
    guarded, refused = _send_through_chain(
        lambda m, **kw: api._send(
            m,
            lambda url, **opts: api.session.request(m, url, **opts),
            "hop0",
            **kw,
        ),
        method,
        None,
        chain,
    )

    safe = 0
    while (
        safe < len(plain) and _chain_origin(plain[safe][1]) in _ALLOWED_ORIGINS
    ):
        safe += 1

    assert guarded == plain[:safe]
    assert refused == (safe < len(plain))


@settings(max_examples=100, deadline=None)
@given(
    method=st.sampled_from(["POST", "PATCH", "PUT", "DELETE"]),
    with_body=st.booleans(),
    chain=_REDIRECT_CHAINS,
)
def test_redirect_chains_of_a_write_stop_at_the_first_response(
    method: str, with_body: bool, chain: list[Any]
) -> None:
    """A write sends one request, whatever status and Location answer it.

    Args:
        method: HTTP method of the write.
        with_body: Whether the write has a JSON body.
        chain: Status and location kind of each redirect, in order.
    """
    body = {"name": "Ada"} if with_body else None
    api = API("https://example.com/v1/")
    api.redirect_origins = ["https://files.example.com"]
    sent, refused = _send_through_chain(
        lambda m, **kw: getattr(api, m.lower())("hop0", **kw),
        method,
        body,
        chain,
    )

    assert [(m, u) for m, u, _ in sent] == [
        (method, "https://example.com/v1/hop0")
    ]
    assert refused


def test_api_closes_session_as_context_manager() -> None:
    """The API client should close its session on context exit."""
    api = API("https://example.com/")
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    api.session.close = close  # type: ignore[method-assign]

    with api as entered:
        assert entered is api

    assert closed


def test_api_rejects_invalid_default_write_mode() -> None:
    """API defaults should validate supported write modes."""
    with pytest.raises(ValueError, match="Unsupported write mode"):
        API(
            "https://example.com/",
            default_write_mode=cast(Any, "later"),
        )


def test_api_raise_for_response_allows_success_statuses() -> None:
    """The public hook should not raise for successful responses."""
    api = API("https://example.com/")
    response = requests.Response()
    response.status_code = 204
    response.url = "https://example.com/items/1"
    response.reason = "No Content"

    api.raise_for_response(response)


def test_api_raise_for_response_raises_http_error_for_failures() -> None:
    """The public hook should preserve default requests error behavior."""
    api = API("https://example.com/")
    response = requests.Response()
    response.status_code = 404
    response.url = "https://example.com/items/1"
    response.reason = "Not Found"
    response.request = requests.Request(
        "GET", "https://example.com/items/1"
    ).prepare()

    with pytest.raises(requests.HTTPError):
        api.raise_for_response(response)


def test_api_uses_raise_for_response_hook_in_requests(
    requests_mock: Any,
) -> None:
    """Request methods should route response validation through the hook."""

    class HookedAPI(API):
        def raise_for_response(self, resp: Response, **kwargs: Any) -> None:
            if resp.status_code == 418:
                raise RuntimeError("teapot")

            super().raise_for_response(resp)

    api = HookedAPI("https://example.com/")
    requests_mock.get("https://example.com/brew", status_code=418)

    with pytest.raises(RuntimeError, match="teapot"):
        api.get("/brew")


def test_api_skips_raise_for_response_hook_for_successes(
    requests_mock: Any,
) -> None:
    """Successful request paths should return before calling the hook."""

    class HookedAPI(API):
        def raise_for_response(self, resp: Response, **kwargs: Any) -> None:
            raise AssertionError("raise_for_response() should not be called")

    api = HookedAPI("https://example.com/")
    requests_mock.get(
        "https://example.com/ok", status_code=200, json={"ok": True}
    )

    response = api.get("/ok")

    assert response.json() == {"ok": True}


def test_api_proxy_takes_precedence_over_environment(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit proxy wins over proxy environment variables."""
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.com:9999")
    api = API("https://example.com/", proxy="http://proxy.example.com:3128")
    requests_mock.get("https://example.com/users/1", json={"id": 1})

    api.get("/users/1")

    assert requests_mock.last_request.proxies["https"] == (
        "http://proxy.example.com:3128"
    )


def test_api_reset_session_keeps_client_configuration() -> None:
    """Resetting the session keeps the session's configuration."""
    api = API("https://example.com/", proxy="http://proxy.example.com:3128")
    old_session = api.session
    old_session.headers["Authorization"] = "Bearer token"
    old_session.auth = ("user", "password")
    old_session.cookies.set("session", "abc")
    old_session.params = {"format": "json"}
    old_session.cert = "/path/to/client.pem"
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    old_session.close = close  # type: ignore[method-assign]

    api.reset_session(pool_maxsize=4)

    assert api.session is not old_session
    assert closed
    assert api.session.headers["Authorization"] == "Bearer token"
    assert api.session.auth == ("user", "password")
    assert api.session.cookies.get("session") == "abc"
    assert api.session.proxies["https"] == "http://proxy.example.com:3128"
    assert api.session.params == {"format": "json"}
    assert api.session.cert == "/path/to/client.pem"


def test_api_reset_session_keeps_disabled_verification() -> None:
    """Resetting without ``verify`` keeps ``verify=False``."""
    api = API("https://example.com/", verify=False)

    api.reset_session()

    assert api.session.verify is False


def test_api_reset_session_can_enable_verification() -> None:
    """An explicit ``verify=True`` replaces ``verify=False``."""
    api = API("https://example.com/", verify=False)

    api.reset_session(verify=True)

    assert api.session.verify is True


def test_api_accepts_ca_bundle_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CA bundle path verifies certificates and keeps warnings."""
    disabled: list[type[Warning]] = []
    monkeypatch.setattr(urllib3, "disable_warnings", disabled.append)

    api = API("https://example.com/", verify="/etc/ssl/certs/internal.pem")

    assert api.session.verify == "/etc/ssl/certs/internal.pem"
    assert disabled == []


@pytest.mark.parametrize("verify", ["/etc/ssl/certs/internal.pem", False])
def test_api_verify_wins_over_environment_ca_bundle(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch, verify: bool | str
) -> None:
    """A configured ``verify`` is not replaced by an environment CA bundle.

    With ``trust_env``, requests replaces a request's ``verify`` of ``True``
    with ``REQUESTS_CA_BUNDLE`` and prefers it over the session's setting.

    Args:
        verify: Configured CA bundle path, or ``False``.
    """
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/public.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/etc/ssl/certs/public.pem")
    api = API("https://example.com/", verify=verify)
    requests_mock.get("https://example.com/users/1", json={"id": 1})

    api.get("/users/1")

    assert requests_mock.last_request.verify == verify


def test_api_default_verify_honors_environment_ca_bundle(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default ``verify=True`` uses requests' environment CA bundle."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/corporate.pem")
    api = API("https://example.com/")
    requests_mock.get("https://example.com/users/1", json={"id": 1})

    api.get("/users/1")

    assert requests_mock.last_request.verify == "/etc/ssl/certs/corporate.pem"


def test_api_mounts_adapter_with_retries() -> None:
    """Extra keyword arguments configure the base URL's adapter."""
    api = API("https://example.com/", max_retries=3)

    adapter = cast(
        requests.adapters.HTTPAdapter,
        api.session.get_adapter("https://example.com/users/"),
    )
    assert adapter.max_retries.total == 3


def test_api_cookie_setter_replaces_cookies(requests_mock: Any) -> None:
    """Assigning a jar replaces the session's cookies with its cookies."""
    api = API("https://example.com/")
    api.cookies.set("stale", "old", domain="example.com", path="/")
    jar = requests.cookies.RequestsCookieJar()
    jar.set("session", "abc", domain="example.com", path="/")
    requests_mock.get("https://example.com/me", json={})

    api.cookies = jar
    jar.set("later", "x", domain="example.com", path="/")
    api.get("/me")

    assert requests_mock.last_request.headers["Cookie"] == "session=abc"


def test_api_without_verification_keeps_warnings_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verify=False`` leaves urllib3's warnings to the application.

    Suppressing them would hide them for every client in the process.
    """
    disabled: list[type[Warning]] = []
    monkeypatch.setattr(urllib3, "disable_warnings", disabled.append)

    api = API("https://example.com/", verify=False)

    assert api.session.verify is False
    assert disabled == []


def test_package_reports_its_version() -> None:
    """``eazyrest.__version__`` is the installed distribution's version."""
    import importlib.metadata

    import eazyrest

    assert eazyrest.__version__ == importlib.metadata.version("eazyrest")


def test_raise_for_response_override_may_accept_a_failure(
    requests_mock: Any,
) -> None:
    """An override that returns lets the failed response through."""

    class LenientAPI(API):
        def raise_for_response(self, resp: Response, **kwargs: Any) -> None:
            if resp.status_code != 404:
                super().raise_for_response(resp)

    api = LenientAPI("https://example.com/")
    requests_mock.get("https://example.com/missing", status_code=404)

    assert api.get("/missing").status_code == 404


def test_redirect_with_unreadable_port_is_refused(requests_mock: Any) -> None:
    """A redirect whose authority cannot be read is not followed."""
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": "https://example.com:99999/b"},
    )

    with pytest.raises(RedirectError, match="cannot prepare") as raised:
        api.get("/a")

    assert "99999" not in str(raised.value)


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [(None, 5.0), (2.0, 2.0), (9.0, 5.0), ((1.0, None), (1.0, 5.0))],
)
def test_total_timeout_caps_request_timeout(
    timeout: Any, expected: Any
) -> None:
    """Each part of a request's timeout ends by the request's deadline.

    Args:
        timeout: Timeout that the request would otherwise use.
        expected: Timeout with the remaining time, about 5 seconds, as its
            cap.
    """
    from eazyrest.api import _Budget

    budget = _Budget(None, 5.0)

    assert budget.timeout(timeout) == pytest.approx(expected, abs=0.5)
    assert _Budget(None, None).timeout(timeout) == timeout


def test_scoped_client_sends_its_options_with_every_request(
    requests_mock: Any,
) -> None:
    """A request-scoped view adds its options, and a call's own options win."""
    api = API("https://example.com/v1/")
    get = requests_mock.get("https://example.com/v1/items/", json=[])
    scoped = api._scoped(
        {"headers": {"X-Tenant": "a", "Accept": "text/plain"}, "timeout": 3}
    )

    scoped.get("/items/", headers={"accept": "application/json"})

    headers = get.last_request.headers
    assert headers["X-Tenant"] == "a"
    assert headers["Accept"] == "application/json"
    assert get.last_request.timeout == 3

    api.get("/items/")

    assert "X-Tenant" not in get.last_request.headers
    assert get.last_request.timeout == api.timeout


def test_scoped_client_shares_its_session() -> None:
    """The view shares the session and leaves the API's options alone."""
    api = API("https://example.com/v1/")

    scoped = api._scoped({"headers": {"X-Tenant": "a"}})

    assert scoped.session is api.session
    assert dict(api._request_options) == {}
    assert api._scoped({})._scoped({"timeout": 1})._request_options == {
        "timeout": 1
    }


def test_nested_scopes_merge_their_options_over_one_api(
    requests_mock: Any,
) -> None:
    """A view of a view sends both option sets through the one API."""
    api = API("https://example.com/v1/")
    get = requests_mock.get("https://example.com/v1/items/", json=[])
    outer = api._scoped({"headers": {"X-A": "1", "X-B": "1"}, "timeout": 3})

    outer._scoped({"headers": {"X-B": "2"}}).get("/items/")

    headers = get.last_request.headers
    assert (headers["X-A"], headers["X-B"]) == ("1", "2")
    assert get.last_request.timeout == 3
    assert outer._scoped({"timeout": 9})._api is api


def test_scoped_view_sends_every_request_through_the_apis_methods(
    requests_mock: Any,
) -> None:
    """A subclass that overrides a request method sees a scoped request."""

    class Counting(API):
        """An API that counts the requests that reach its methods."""

        calls: tuple[str, ...] = ()
        """The methods so far, kept on the instance."""

        def get(
            self, uri: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            self.calls += ("get",)
            return super().get(uri, *args, **kwargs)

        def post(
            self, uri: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            self.calls += ("post",)
            return super().post(uri, *args, **kwargs)

    api = Counting("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/", json=[])
    requests_mock.post("https://example.com/v1/items/", json={})
    scoped = api._scoped({"headers": {"X-Tenant": "a"}})

    scoped.get("/items/")
    scoped.post("/items/", json={})

    assert api.calls == ("get", "post")
    assert requests_mock.last_request.headers["X-Tenant"] == "a"


def test_scoped_view_reads_other_attributes_from_the_api() -> None:
    """The view reads the API's attributes, and a subclass's helpers."""

    class Signed(API):
        """An API with a helper of its own."""

        def request_headers(self) -> dict[str, str]:
            return {"X-Signed": "yes"}

    api = Signed("https://example.com/v1/")
    api.timeout = 7
    scoped = api._scoped({"timeout": 1})

    assert scoped.request_headers() == {"X-Signed": "yes"}
    assert scoped.timeout == 7
    assert scoped.base_url == api.base_url
    assert scoped.resolve_url("/items/") == api.resolve_url("/items/")
    assert scoped.max_items == api.max_items
    assert not isinstance(cast(Any, scoped), API)


def test_scoped_view_refuses_assignment() -> None:
    """Assigning an attribute on the view raises, so it never diverges."""
    api = API("https://example.com/v1/")
    scoped = api._scoped({"timeout": 1})

    with pytest.raises(AttributeError, match="cannot assign 'timeout'"):
        scoped.timeout = 2
    with pytest.raises(AttributeError, match="cannot assign 'extra'"):
        scoped.extra = 1
    assert api.timeout != 2


@pytest.mark.parametrize(
    "duplicate",
    [
        copy.copy,
        copy.deepcopy,
        lambda view: pickle.loads(pickle.dumps(view)),
    ],
    ids=["copy", "deepcopy", "pickle"],
)
def test_scoped_view_can_be_copied_and_pickled_with_its_options(
    duplicate: Callable[[Any], Any], requests_mock: Any
) -> None:
    """A duplicate of a view keeps its options and still sends them.

    Args:
        duplicate: Copies the view, or round-trips it through pickle.
    """
    get = requests_mock.get("https://example.com/v1/items/", json=[])
    view = API("https://example.com/v1/")._scoped(
        {"headers": {"X-Tenant": "a"}, "timeout": 3}
    )

    again = duplicate(view)

    assert again is not view
    assert dict(again._request_options) == dict(view._request_options)
    again.get("/items/")
    assert get.last_request.headers["X-Tenant"] == "a"
    assert get.last_request.timeout == 3


def test_scoped_view_without_an_api_raises_attribute_error() -> None:
    """Reading an attribute of a view that has no API does not recurse."""
    bare = _ScopedAPI.__new__(_ScopedAPI)

    with pytest.raises(AttributeError):
        _ = bare.anything


def test_scoped_view_cannot_be_registered_as_an_api() -> None:
    """The registry holds an ``API``, so a view raises ``TypeError``."""

    class Registered(JSONObject):
        pass

    scoped = API("https://example.com/v1/")._scoped({"timeout": 1})

    with pytest.raises(TypeError, match="only through an API"):
        Registered.register_api(scoped)  # type: ignore[arg-type]


def test_scoped_client_runs_option_hooks_before_its_guard(
    requests_mock: Any,
) -> None:
    """Option hooks run first, then the call's, then the redirect guard."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/",
        status_code=302,
        headers={"Location": "https://other.example/items/"},
    )
    other = requests_mock.get("https://other.example/items/", json=[])
    seen: list[str] = []
    scoped = api._scoped(
        {"hooks": {"response": [lambda resp, **_: seen.append("option")]}}
    )

    with pytest.raises(RedirectError, match="origin"):
        scoped.get(
            "/items/",
            hooks={"response": [lambda resp, **_: seen.append("call")]},
        )

    assert seen == ["option", "call"]
    assert not other.called


@pytest.mark.parametrize("status", [300, 302, 304, 307])
def test_scoped_client_returns_a_redirect_that_it_does_not_follow(
    requests_mock: Any, status: int
) -> None:
    """A scoped client adds its options and nothing else.

    A request that does not follow redirects gets the 3xx back, as it does from
    the client that the scope was made from.

    Args:
        status: Final redirect status.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/",
        status_code=status,
        headers={"Location": "/elsewhere/"},
    )

    scoped = api._scoped({"allow_redirects": False}).get("/items/")

    assert scoped.status_code == status
    assert api.get("/items/", allow_redirects=False).status_code == status


@pytest.mark.parametrize(
    ("method", "status", "location"),
    [
        ("get", 302, "https://elsewhere.example.net/"),
        ("get", 303, "/elsewhere/"),
        ("get", 301, "/elsewhere/"),
    ],
)
def test_unfollowed_redirect_of_a_raw_request_is_returned(
    requests_mock: Any, method: str, status: int, location: str
) -> None:
    """A raw request that follows nothing returns any redirect as it is.

    The redirect guard refuses a redirect to another origin, but with
    ``allow_redirects=False`` it follows none. A request-scoped client returns
    the same redirect.

    Args:
        method: Request method.
        status: Redirect status.
        location: Redirect target.
    """
    api = API("https://example.com/v1/")
    requests_mock.register_uri(
        method.upper(),
        "https://example.com/v1/items/",
        status_code=status,
        headers={"Location": location},
    )

    resp = getattr(api, method)("/items/", allow_redirects=False)

    assert resp.status_code == status
    assert resp.headers["Location"] == location
    scoped = getattr(api._scoped({"allow_redirects": False}), method)(
        "/items/"
    )
    assert scoped.status_code == status
    assert scoped.headers["Location"] == location


def test_redirect_error_of_a_write_leaves_out_the_location() -> None:
    """A ``Location`` with control characters reaches no message.

    The header stays on the error's response, as the server sent it.
    """
    api = API("https://example.com/v1/")
    location = "/next\x1b[2J\x07forged line"

    with Mocker() as mock:
        mock.post(
            "https://example.com/v1/items/",
            status_code=302,
            headers={"Location": location},
        )
        with pytest.raises(RedirectError) as info:
            api.post("/items/", json={})

    message = str(info.value)
    assert message.isprintable()
    assert "forged" not in message
    assert info.value.response is not None
    assert info.value.response.headers["Location"] == location


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("HTTPS://Example.COM:443/a/?b=1#c", "https://example.com/a/?b=1"),
        ("http://example.com:8080", "http://example.com:8080/"),
        ("https://[::1]:443/x", "https://[::1]/x"),
        ("https://user@Example.com/x", "https://user@example.com/x"),
    ],
)
def test_canonical_url_spells_one_resource_one_way(
    url: str, canonical: str
) -> None:
    """Spellings of one URL that differ in case, port, or fragment agree.

    Args:
        url: URL to canonicalize.
        canonical: Expected canonical spelling.
    """
    assert _canonical_url(url) == canonical


def test_raise_for_response_without_kwargs_is_refused() -> None:
    """An override of ``raise_for_response()`` must accept ``**kwargs``."""
    with pytest.raises(TypeError, match=r"must accept \*\*kwargs"):

        class Strict(API):
            def raise_for_response(self, resp: Response) -> None:  # type: ignore[override]
                pass

    class ErrorsMixin:
        def raise_for_response(self, resp: Response) -> None:
            pass

    with pytest.raises(TypeError, match=r"ErrorsMixin\.raise_for_response"):

        class Mixed(ErrorsMixin, API):  # type: ignore[misc]
            pass


def test_raise_for_response_may_be_a_classmethod() -> None:
    """A ``classmethod`` override that accepts ``**kwargs`` is valid."""

    class ClassLevel(API):
        @classmethod
        def raise_for_response(cls, resp: Response, **kwargs: Any) -> None:
            pass

    assert ClassLevel("https://example.com/") is not None


def test_requests_accept_json(requests_mock: Any) -> None:
    """A request asks for JSON, also after the session is reset."""
    api = API("https://example.com/")
    requests_mock.get("https://example.com/a", json={})

    api.get("/a")
    api.reset_session()
    api.get("/a")
    api.get("/a", headers={"Accept": "text/csv"})

    accepts = [
        request.headers["Accept"] for request in requests_mock.request_history
    ]
    assert accepts == ["application/json", "application/json", "text/csv"]


@pytest.mark.parametrize(
    ("url", "prepared", "origin"),
    [
        ("https://h.test/p", "https://h.test/p", ("https", "h.test", 443)),
        (
            "HTTP://H.test:8080",
            "http://h.test:8080/",
            ("http", "h.test", 8080),
        ),
        (
            "https://alice:hunter2@h.test/p?q=1",
            "https://alice:hunter2@h.test/p?q=1",
            ("https", "h.test", 443),
        ),
        (
            "https://h.test/a b\\c",
            "https://h.test/a%20b%5Cc",
            ("https", "h.test", 443),
        ),
        (
            "https://a.test\\@b.test/",
            "https://a.test/%5C@b.test/",
            ("https", "a.test", 443),
        ),
    ],
)
def test_server_url_is_read_as_requests_prepares_it(
    url: str, prepared: str, origin: tuple[str, str, int]
) -> None:
    """The scheme, host, and port come from the URL that requests sends.

    Args:
        url: URL that the server supplied.
        prepared: The URL after requests prepares it.
        origin: Scheme, host, and port of the prepared URL.
    """
    result = _prepare_server_url(requests.Session(), url)

    assert result is not None
    assert result.url == prepared
    assert result.origin == origin
    assert result.is_http
    assert result.sendable


@pytest.mark.parametrize(
    "url",
    [
        "ftp://alice:hunter2@h.test/p2",
        "mailto:alice:hunter2@h.test",
        "FTP:///alice:hunter2@h",
        "ftp://alice:hunter2#@h.test/",
        "alice:hunter2@h.test",
        "x:",
    ],
)
def test_server_url_with_another_scheme_has_no_host(url: str) -> None:
    """A URL that is not HTTP or HTTPS names no host or port.

    Args:
        url: URL that the server supplied.
    """
    result = _prepare_server_url(requests.Session(), url)

    assert result is not None
    assert result.scheme == urlsplit(url).scheme.lower()
    assert result.origin == (result.scheme, None, None)
    assert not result.is_http
    assert not result.sendable


def test_server_url_is_sendable_when_the_session_has_an_adapter() -> None:
    """A URL of another scheme is sendable if an adapter is mounted for it."""
    session = requests.Session()
    session.mount("ftp://", requests.adapters.HTTPAdapter())

    result = _prepare_server_url(session, "ftp://h.test/x")

    assert result is not None
    assert not result.is_http
    assert result.sendable


@pytest.mark.parametrize(
    "url",
    [
        # Requests leaves a URL that does not begin with http unprepared, so it
        # has no adapter, but urlsplit() ignores the control character and
        # reads an HTTP URL.
        "\x01http://alice:hunter2#@h.test/",
        "\x01http://alice:hunter2\\@h.test/",
        "\x08http://alice:hunter2@h.test/",
        "https://h.test:hunter2/",
        "https://alice:hunter2#@h.test:hunter2/",
        "https://alice:hunter2\u2215@api.test/p2",
        "https://[bad/",
        "http://alice:hunter2@/x",
        "ftp://[bad/",
        ":alice:hunter2@h.test",
    ],
)
def test_server_url_that_requests_cannot_prepare_is_refused(url: str) -> None:
    """A URL that requests cannot prepare or send has no reading.

    Args:
        url: URL that the server supplied.
    """
    assert _prepare_server_url(requests.Session(), url) is None


def test_server_url_needs_an_adapter_for_an_http_url() -> None:
    """A session with no adapter for HTTP cannot send an HTTP URL."""
    session = requests.Session()
    session.adapters.clear()

    assert _prepare_server_url(session, "https://h.test/") is None
