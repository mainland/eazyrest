"""Tests for the HTTP API client wrapper."""

from __future__ import annotations

import http.server
import json
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

from eazyrest import API, LimitExceeded, RedirectError
from eazyrest.api import _canonical_url, _redirect_destination


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


@pytest.mark.parametrize("status", [301, 302, 303])
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_api_refuses_redirect_that_drops_write(
    requests_mock: Any, method: str, status: int
) -> None:
    """A write that requests would resend without its body raises.

    A 301 keeps ``PATCH``, ``PUT``, and ``DELETE`` but drops the body, and
    the other redirects change the method to ``GET``. Either way the server
    would never receive the write, so nothing is sent to the destination.

    Args:
        method: Name of the ``API`` method that sends the write, such as
            ``"post"``.
        status: Redirect status code that the server returns for the write.
    """
    api = API("https://example.com/v1/")
    requests_mock.request(
        method.upper(),
        "https://example.com/v1/users/1",
        status_code=status,
        headers={"Location": "https://example.com/v1/users/1/"},
    )
    destination = requests_mock.request(
        requests_mock_ANY, "https://example.com/v1/users/1/", json={"id": 1}
    )

    with pytest.raises(
        requests.HTTPError,
        match=(
            "status 303, which says that the server performed it"
            if status == 303
            else "not be performed"
        ),
    ) as info:
        getattr(api, method)("/users/1", json={"name": "Ada"})

    assert info.value.response is not None
    assert info.value.response.status_code == status
    assert not destination.called


def test_api_follows_moved_bodyless_delete(requests_mock: Any) -> None:
    """A 301 resends a bodyless ``DELETE`` unchanged, so it is followed."""
    api = API("https://example.com/v1/")
    requests_mock.delete(
        "https://example.com/v1/users/1",
        status_code=301,
        headers={"Location": "https://example.com/v1/users/1/"},
    )
    deleted = requests_mock.delete(
        "https://example.com/v1/users/1/", status_code=204
    )

    api.delete("/users/1")

    assert deleted.called


def test_api_check_redirects_rejects_followed_body_loss() -> None:
    """The final-response check catches a body lost after the hook ran."""
    first = requests.Response()
    first.status_code = 301
    first.url = "https://example.com/v1/users/1"
    first.headers["Location"] = "https://example.com/v1/users/1/"
    first.request = requests.Request(
        "PATCH", first.url, json={"name": "Ada"}
    ).prepare()
    final = requests.Response()
    final.status_code = 200
    final.url = "https://example.com/v1/users/1/"
    final.request = requests.Request("PATCH", final.url).prepare()
    final.history = [first]

    with pytest.raises(requests.HTTPError, match="not performed"):
        API._check_redirects(final)


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

    with pytest.raises(RedirectError, match="cannot be parsed"):
        api.get("/r", allow_redirects=False)
    with pytest.raises(RedirectError, match="cannot be parsed"):
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

    with pytest.raises(RedirectError, match="cannot be parsed"):
        api.get("/r", allow_redirects=False)
    with pytest.raises(RedirectError, match="cannot be parsed"):
        api._scoped({"allow_redirects": False}).get("/r")


def test_api_check_redirects_ignores_a_resent_401() -> None:
    """A 401 that an authentication handler resent is not a redirect.

    requests records it in the history, as ``HTTPDigestAuth`` does, and the
    resent request keeps its method and body, which the server performs.
    """
    challenge = requests.Response()
    challenge.status_code = 401
    challenge.url = "https://example.com/v1/users/1/"
    challenge.headers["WWW-Authenticate"] = 'Digest realm="r", nonce="n"'
    challenge.request = requests.Request(
        "PATCH", challenge.url, json={"name": "Ada"}
    ).prepare()
    final = requests.Response()
    final.status_code = 200
    final.url = challenge.url
    final.request = requests.Request(
        "PATCH", final.url, json={"name": "Ada"}
    ).prepare()
    final.history = [challenge]

    API._check_redirects(final)


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


@pytest.mark.parametrize("status", [307, 308])
def test_api_follows_redirect_that_keeps_method(
    requests_mock: Any, status: int
) -> None:
    """Redirects that keep the method and body are followed.

    Args:
        status: Redirect status code that keeps the method and body.
    """
    api = API("https://example.com/v1/")
    requests_mock.post(
        "https://example.com/v1/users/",
        status_code=status,
        headers={"Location": "https://example.com/v2/users/"},
    )
    created = requests_mock.post(
        "https://example.com/v2/users/", status_code=201, json={"id": 1}
    )

    response = api.post("/users/", json={"name": "Ada"})

    assert response.json() == {"id": 1}
    assert created.last_request.json() == {"name": "Ada"}


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
        ("post", 307, "https://other.example.net/users/"),
        ("put", 308, "https://example.com:8443/v1/users/"),
        ("get", 301, "http://example.com/v1/users/1"),
    ],
)
def test_api_refuses_redirect_to_other_origin(
    requests_mock: Any, method: str, status: int, target: str
) -> None:
    """A redirect to another origin raises before anything is sent there.

    requests would send custom credentials and a preserved body along.

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
        getattr(api, method)("/users/1", json={"password": "secret"})

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
    requests_mock.post(
        "https://example.com/v1/users/",
        status_code=307,
        headers={"Location": target},
    )
    evil = requests_mock.post(
        re.compile("https://.*evil\\.example\\.net/.*"), json={}
    )

    with pytest.raises(requests.HTTPError, match="redirect_origins"):
        api.post("/users/", json={"password": "secret"})

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


def test_api_refuses_a_later_hop_that_drops_the_body(
    requests_mock: Any,
) -> None:
    """Each hop of a chain is checked, not only the first.

    A 307 resends a ``PATCH`` with its body, but a 301 after it would drop
    the body, so the chain stops there, before the last URL is requested.
    """
    api = API("https://example.com/v1/")
    first = requests_mock.patch(
        "https://example.com/v1/a",
        status_code=307,
        headers={"Location": "/v1/b"},
    )
    second = requests_mock.patch(
        "https://example.com/v1/b",
        status_code=301,
        headers={"Location": "/v1/c"},
    )
    last = requests_mock.patch("https://example.com/v1/c", json={})

    with pytest.raises(RedirectError, match="without the request body"):
        api.patch("a", json={"name": "Ada"})

    assert first.call_count == 1
    assert second.last_request.json() == {"name": "Ada"}
    assert not last.called


def test_api_follows_a_chain_that_keeps_the_request(
    requests_mock: Any,
) -> None:
    """A chain of 307 and 308 redirects delivers the request unchanged."""
    api = API("https://example.com/v1/")
    api.redirect_origins = ["https://files.example.com"]
    requests_mock.post(
        "https://example.com/v1/a",
        status_code=307,
        headers={"Location": "https://files.example.com/b"},
    )
    requests_mock.post(
        "https://files.example.com/b",
        status_code=308,
        headers={"Location": "https://example.com/v1/c"},
    )
    created = requests_mock.post(
        "https://example.com/v1/c", status_code=201, json={"id": 1}
    )

    assert api.post("a", json={"name": "Ada"}).json() == {"id": 1}
    assert created.last_request.json() == {"name": "Ada"}


def test_redirect_guard_without_a_budget_checks_only_redirects(
    requests_mock: Any,
) -> None:
    """The guard's hooks check origins and writes even without a budget.

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


@settings(max_examples=150, deadline=None)
@given(
    method=st.sampled_from(["GET", "POST", "PATCH", "PUT", "DELETE"]),
    with_body=st.booleans(),
    chain=_REDIRECT_CHAINS,
)
def test_redirect_chains_deliver_the_request_or_stop_before_harm(
    method: str, with_body: bool, chain: list[Any]
) -> None:
    """The client sends what requests would, up to the first unsafe hop.

    Plain requests follows every hop of a generated chain. The client must
    send exactly the requests that plain requests sends until the first one
    that would go to an origin that the API does not allow, or that would
    have another method or lack the original body. There it raises
    ``RedirectError`` without sending that request. A chain without such a
    request is followed to the end.

    Args:
        method: HTTP method of the original request.
        with_body: Whether the original request has a JSON body.
        chain: Status and location kind of each redirect, in order.
    """
    body = {"name": "Ada"} if with_body else None
    url = "https://example.com/v1/hop0"
    session = requests.Session()
    plain, _ = _send_through_chain(
        lambda m, **kw: session.request(m, url, **kw), method, body, chain
    )
    api = API("https://example.com/v1/")
    api.redirect_origins = ["https://files.example.com"]
    guarded, refused = _send_through_chain(
        lambda m, **kw: getattr(api, m.lower())("hop0", **kw),
        method,
        body,
        chain,
    )

    original = plain[0][2]
    safe = 0
    while safe < len(plain) and (
        _chain_origin(plain[safe][1]) in _ALLOWED_ORIGINS
        and plain[safe][0] == method
        and plain[safe][2] == original
    ):
        safe += 1

    assert guarded == plain[:safe]
    assert refused == (safe < len(plain))
    assert all(
        _chain_origin(hop_url) in _ALLOWED_ORIGINS for _, hop_url, _ in guarded
    )


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
        def raise_for_response(self, resp: requests.Response) -> None:
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
        def raise_for_response(self, resp: requests.Response) -> None:
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
        def raise_for_response(self, resp: requests.Response) -> None:
            if resp.status_code != 404:
                resp.raise_for_status()

    api = LenientAPI("https://example.com/")
    requests_mock.get("https://example.com/missing", status_code=404)

    assert api.get("/missing").status_code == 404


def test_redirect_with_unreadable_port_is_refused(requests_mock: Any) -> None:
    """A redirect whose authority cannot be parsed is not followed."""
    api = API("https://example.com/")
    requests_mock.get(
        "https://example.com/a",
        status_code=302,
        headers={"Location": "https://example.com:99999/b"},
    )

    with pytest.raises(requests.HTTPError, match="redirect_origins"):
        api.get("/a")


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
    """A request-scoped copy adds its options, and a call's own options win."""
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
    """The copy shares the session and leaves the original's options alone."""
    api = API("https://example.com/v1/")

    scoped = api._scoped({"headers": {"X-Tenant": "a"}})

    assert scoped.session is api.session
    assert scoped._request_scoped
    assert not api._request_scoped
    assert dict(api._request_options) == {}
    assert api._scoped({})._scoped({"timeout": 1})._request_options == {
        "timeout": 1
    }


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
def test_scoped_client_refuses_a_final_redirect(
    requests_mock: Any, status: int
) -> None:
    """A scoped request expects a response to read, not an unfollowed redirect.

    Args:
        status: Final redirect status.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/",
        status_code=status,
        headers={"Location": "/elsewhere/"},
    )

    with pytest.raises(RedirectError, match=f"status {status}"):
        api._scoped({"allow_redirects": False}).get("/items/")

    assert api.get("/items/", allow_redirects=False).status_code == status


@pytest.mark.parametrize(
    ("method", "status", "location"),
    [
        ("get", 302, "https://elsewhere.example.net/"),
        ("post", 302, "/elsewhere/"),
        ("post", 303, "/elsewhere/"),
        ("patch", 301, "/elsewhere/"),
    ],
)
def test_unfollowed_redirect_of_a_raw_request_is_returned(
    requests_mock: Any, method: str, status: int, location: str
) -> None:
    """A raw request that follows nothing returns any redirect as it is.

    The redirect guard refuses a redirect to another origin and one that
    changes the method, but with ``allow_redirects=False`` neither happens. The
    same redirect still raises for a request-scoped client, which expects a
    final response.

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
    with pytest.raises(RedirectError, match=f"returned status {status}"):
        getattr(api._scoped({"allow_redirects": False}), method)("/items/")


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
