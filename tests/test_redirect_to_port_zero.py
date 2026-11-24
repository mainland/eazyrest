"""A redirect to port 0 of the request's host leads to another origin.

requests drops port 0 when it prepares a URL, but it sends a redirect's
destination as it stands, so the connection goes to port 0. The origin check
must read the port from the destination as it is sent, so that an API on its
scheme's default port does not take the destination for its own origin.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, PaginationError, RedirectError, link_headers_page
from eazyrest._urls import _ServerURL

TARGETS = {
    "http port 0": ("http://a.test/", "http://a.test:0/z"),
    "https port 0": ("https://a.test/", "https://a.test:0/z"),
    "port 00 without a scheme": ("http://a.test/", "//a.test:00/z"),
    "another port": ("http://a.test/", "http://a.test:8080/z"),
}


@pytest.mark.parametrize(("base", "target"), TARGETS.values(), ids=TARGETS)
def test_a_redirect_to_port_zero_is_refused_before_anything_is_sent(
    base: str, target: str
) -> None:
    """The error is the guide's, and nothing is asked of port 0."""
    api = API(base)
    with requests_mock.Mocker() as mocker:
        mocker.get(base + "x", status_code=302, headers={"Location": target})
        with pytest.raises(RedirectError, match="another origin"):
            api.get("/x")
        assert mocker.call_count == 1


def test_a_redirect_to_the_default_port_is_followed() -> None:
    """A port that the scheme implies is the origin's own port."""
    api = API("http://a.test/")
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "http://a.test/x",
            status_code=302,
            headers={"Location": "http://a.test:80/z"},
        )
        mocker.get("http://a.test:80/z", json={"ok": True})
        assert api.get("/x").json() == {"ok": True}


def test_a_next_link_to_port_zero_is_refused() -> None:
    """A page link has another origin when its port is 0."""
    api = API("http://a.test/")
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "http://a.test/items/",
            json=[],
            headers={"Link": '<http://a.test:0/items/?p=2>; rel="next"'},
        )
        page = link_headers_page(api, api.get("/items/"))
        with pytest.raises(PaginationError, match="different origin"):
            page.next_page()
        assert mocker.call_count == 1


def test_the_origin_of_a_url_that_names_port_zero_has_port_zero() -> None:
    """Both readings of a URL keep the port that preparing it drops."""
    zero: Any = _ServerURL.from_prepared(
        "http://a.test/z", sent="http://a.test:0/z"
    )
    assert zero.origin == ("http", "a.test", 0)
    plain: Any = _ServerURL.from_prepared("http://a.test/z")
    assert plain.origin == ("http", "a.test", 80)
