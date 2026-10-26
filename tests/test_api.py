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


@pytest.mark.parametrize(
    ("method", "status"),
    [("post", 301), ("post", 302), ("patch", 302), ("delete", 303)],
)
def test_api_rejects_redirect_that_changes_method(
    requests_mock: Any, method: str, status: int
) -> None:
    """A write that a redirect would resend as a GET raises."""
    api = API("https://example.com/v1/")
    requests_mock.request(
        method.upper(),
        "https://example.com/v1/users/",
        status_code=status,
        headers={"Location": "https://example.com/v2/users/"},
    )
    requests_mock.get("https://example.com/v2/users/", json=[{"id": 1}])

    with pytest.raises(requests.HTTPError, match="was not performed") as info:
        getattr(api, method)("/users/", json={"name": "Ada"})

    assert info.value.response is not None
    assert info.value.response.status_code == status


@pytest.mark.parametrize("status", [307, 308])
def test_api_follows_redirect_that_keeps_method(
    requests_mock: Any, status: int
) -> None:
    """Redirects that keep the method and body are followed."""
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


def test_api_mounts_adapter_with_retries() -> None:
    """Extra keyword arguments configure the base URL's adapter."""
    api = API("https://example.com/", max_retries=3)

    adapter = cast(
        requests.adapters.HTTPAdapter,
        api.session.get_adapter("https://example.com/users/"),
    )
    assert adapter.max_retries.total == 3


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
