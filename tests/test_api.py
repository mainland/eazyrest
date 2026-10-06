"""Tests for the HTTP API client wrapper."""

from __future__ import annotations

import warnings
from typing import Any, cast

import pytest
import requests
import urllib3

from eazyrest import API


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
    with pytest.raises(ValueError):
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
    """Resetting the session keeps headers, auth, cookies, and proxies."""
    api = API("https://example.com/", proxy="http://proxy.example.com:3128")
    old_session = api.session
    old_session.headers["Authorization"] = "Bearer token"
    old_session.auth = ("user", "password")
    old_session.cookies.set("session", "abc")
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


def test_api_cookie_setter_merges_cookies(requests_mock: Any) -> None:
    """Assigned cookies are sent with later requests."""
    api = API("https://example.com/")
    jar = requests.cookies.RequestsCookieJar()
    jar.set("session", "abc", domain="example.com", path="/")
    requests_mock.get("https://example.com/me", json={})

    api.cookies = jar
    api.get("/me")

    assert requests_mock.last_request.headers["Cookie"] == "session=abc"


def test_api_without_verification_suppresses_only_insecure_warnings() -> None:
    """``verify=False`` leaves other urllib3 warnings visible."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        api = API("https://example.com/", verify=False)
        warnings.warn(
            "insecure", urllib3.exceptions.InsecureRequestWarning, stacklevel=1
        )
        warnings.warn(
            "clock", urllib3.exceptions.SystemTimeWarning, stacklevel=1
        )

    assert api.session.verify is False
    assert [type(w.message) for w in caught] == [
        urllib3.exceptions.SystemTimeWarning
    ]
