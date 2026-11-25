"""A URL whose scheme is not http or https has no origin.

No origin matches anything, not even another URL with none, so a base URL or a
request URL of another scheme with an adapter mounted for it refuses a link or
a redirect to another host, and every refusal of a base URL with a host stays.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests
import requests_mock

from eazyrest import (
    API,
    JSONObject,
    PaginationError,
    RedirectError,
    follow_link_headers,
    follow_next_links,
)
from eazyrest._models import _Query
from eazyrest._urls import (
    _base_origins,
    _origin,
    _origin_matches,
    _read_url,
    _redirect_origin,
)

EVIL = "mock://evil.test/items/?page=2"


def _make(base: str) -> tuple[API, Any]:
    """Return an API at base, with a mock adapter on every scheme used."""
    api = API(base)
    adapter = requests_mock.Adapter()
    for prefix in ("https://", "http://", "mock:"):
        api.session.mount(prefix, adapter)
    api.session.headers["X-Api-Key"] = "KEY"
    return api, adapter


def _urls(adapter: Any) -> list[str]:
    """Return the URLs that the adapter answered."""
    return [request.url for request in adapter.request_history]


def _next_links(api: API, adapter: Any, first: str, link: str) -> None:
    """Follow a next-page link of a body from the page at first."""
    page = {"results": [1], "next": link}
    list(follow_next_links(api, page, url=first))


def _link_headers(api: API, adapter: Any, first: str, link: str) -> None:
    """Follow a Link header from the page at first."""
    adapter.register_uri(
        "GET", first, json=[1], headers={"Link": f'<{link}>; rel="next"'}
    )
    list(follow_link_headers(api, api.get(first)))


FOLLOWERS: dict[str, Callable[[API, Any, str, str], None]] = {
    "next links": _next_links,
    "link headers": _link_headers,
}

BASES = {
    "hostless base": ("mock:/", "mock:/items/"),
    "hostless base with a path": ("mock:/v1/", "mock:/v1/items/"),
    "base with a host": ("mock://api.test/", "mock://api.test/items/"),
    "base with a host and a port": (
        "mock://api.test:8000/",
        "mock://api.test:8000/items/",
    ),
}


@pytest.mark.parametrize("follow", FOLLOWERS.values(), ids=FOLLOWERS)
@pytest.mark.parametrize(("base", "first"), BASES.values(), ids=BASES)
def test_a_base_of_another_scheme_refuses_a_link_to_another_host(
    base: str, first: str, follow: Callable[[API, Any, str, str], None]
) -> None:
    """The link is refused and nothing is sent to the other host."""
    api, adapter = _make(base)
    adapter.register_uri("GET", EVIL, json=[2])

    with pytest.raises(PaginationError, match="different origin"):
        follow(api, adapter, first, EVIL)

    assert not any("evil.test" in url for url in _urls(adapter))


@pytest.mark.parametrize("follow", FOLLOWERS.values(), ids=FOLLOWERS)
@pytest.mark.parametrize(("base", "first"), BASES.values(), ids=BASES)
def test_a_base_of_another_scheme_refuses_a_link_to_its_own_host(
    base: str, first: str, follow: Callable[[API, Any, str, str], None]
) -> None:
    """A URL with no origin matches nothing, its own base's included."""
    link = first + "?page=2"
    api, adapter = _make(base)
    adapter.register_uri("GET", link, json=[2])

    with pytest.raises(PaginationError, match="different origin"):
        follow(api, adapter, first, link)

    assert link not in _urls(adapter)


@pytest.mark.parametrize(("base", "first"), BASES.values(), ids=BASES)
def test_a_base_of_another_scheme_follows_a_link_that_allows_other_origins(
    base: str, first: str
) -> None:
    """Only ``allow_other_origins=True`` lets such a link leave."""
    api, adapter = _make(base)
    adapter.register_uri("GET", EVIL, json={"results": [2]})
    page = {"results": [1], "next": EVIL}

    assert list(
        follow_next_links(api, page, url=first, allow_other_origins=True)
    ) == [1, 2]


@pytest.mark.parametrize(("base", "start"), BASES.values(), ids=BASES)
@pytest.mark.parametrize(
    "target",
    ["mock://evil.test/b", "mock:/b", "mock://api.test/b"],
    ids=["other host", "hostless", "same host"],
)
def test_a_base_of_another_scheme_refuses_a_redirect(
    base: str, start: str, target: str
) -> None:
    """The redirect is not followed, so no header reaches its target."""
    api, adapter = _make(base)
    adapter.register_uri(
        "GET", start, status_code=302, headers={"Location": target}
    )
    adapter.register_uri("GET", target, json=["there"])

    with pytest.raises(RedirectError, match="another origin"):
        api.get(start)

    assert _urls(adapter) == [start]


@pytest.mark.parametrize("start", ["mock:/a", "mock:///a"])
def test_a_request_url_of_another_scheme_refuses_a_redirect(
    start: str,
) -> None:
    """A caller's hostless URL under an https base names no origin."""
    api, adapter = _make("https://api.test/")
    adapter.register_uri(
        "GET",
        start,
        status_code=302,
        headers={"Location": "mock://evil.test/b"},
    )
    adapter.register_uri("GET", "mock://evil.test/b", json=["there"])

    with pytest.raises(RedirectError, match="another origin"):
        api.get(start)

    assert not any("evil.test" in url for url in _urls(adapter))


def test_a_redirect_of_a_page_link_of_another_scheme_is_refused() -> None:
    """With other origins allowed, a link's redirect still needs an origin."""
    api, adapter = _make("https://api.test/")
    adapter.register_uri(
        "GET",
        "mock:/items/",
        status_code=302,
        headers={"Location": "mock://evil.test/p"},
    )
    adapter.register_uri("GET", "mock://evil.test/p", json={"results": [3]})
    page = {"results": [1], "next": "mock:/items/"}

    with pytest.raises(RedirectError):
        list(
            follow_next_links(
                api,
                page,
                url="https://api.test/items/",
                allow_other_origins=True,
            )
        )

    assert not any("evil.test" in url for url in _urls(adapter))


@pytest.mark.parametrize(
    "target",
    ["mock://evil.test/items/?page=2", "mock:/items/?page=2"],
    ids=["host", "hostless"],
)
def test_a_link_of_another_scheme_from_an_https_base_is_refused(
    target: str,
) -> None:
    """A server's response alone never leaves the base URL's origin."""
    api, adapter = _make("https://api.test/")
    adapter.register_uri("GET", target, json={"results": [2]})

    with pytest.raises(PaginationError, match="different origin"):
        _next_links(api, adapter, "https://api.test/items/", target)

    assert _urls(adapter) == []


@pytest.mark.parametrize(
    "text",
    [
        "mock:/a",
        "mock://api.test/x",
        "MOCK://API.test:8000/x",
        "ftp://api.test/x",
        "file:///etc/passwd",
        "http+unix://%2Ftmp%2Fapp.sock/x",
        "mock:///x",
    ],
)
def test_a_url_of_another_scheme_has_no_origin(text: str) -> None:
    """The origin is read once, and only http and https URLs have one."""
    reading = _read_url(text)

    assert reading is not None
    assert reading.origin is None
    assert _origin(text) is None
    assert _base_origins(text) == set()


def test_no_origin_matches_anything() -> None:
    """A missing origin is in no set, whatever the set holds."""
    origin = ("https", "h.test", 443)

    assert _origin_matches(origin, {origin})
    assert not _origin_matches(origin, set())
    assert not _origin_matches(None, {origin})
    assert not _origin_matches(None, set())
    assert not _origin_matches(None, {None})  # type: ignore[arg-type]


def test_a_redirect_origin_of_another_scheme_is_refused() -> None:
    """An entry of the allowed origins needs an http or https scheme."""
    with pytest.raises(ValueError, match="scheme is not http or https"):
        _redirect_origin(0, "mock://cdn.test")


@pytest.mark.parametrize("base", ["mock:/", "mock://api.test/"])
def test_a_related_lookup_of_another_scheme_gets_no_request_options(
    base: str,
) -> None:
    """The query's options go only to a client with the query's origin."""
    client = API(base)
    related = API(base)
    query = _Query(
        api=related, client=client, options={"headers": {"X-Trace": "1"}}
    )

    assert query.related_client(JSONObject) is related


def test_a_related_lookup_of_the_same_origin_gets_the_request_options() -> (
    None
):
    """The check still passes options to a client of the query's origin."""
    client = API("https://api.test/")
    related = API("https://api.test/other/")
    query = _Query(
        api=related, client=client, options={"headers": {"X-Trace": "1"}}
    )

    assert query.related_client(JSONObject) is not related


@pytest.mark.parametrize(
    "base",
    [
        "https://h.test/",
        "\xa0https://h.test/",
        chr(0x2003) + "https://h.test/",
    ],
    ids=["plain", "no-break space", "em space"],
)
def test_adapter_arguments_follow_the_scheme_that_requests_reads(
    base: str,
) -> None:
    """A base URL led by white space that requests strips keeps its retries."""
    api = API(base, max_retries=7)

    adapter = api.session.get_adapter("https://h.test/")
    assert isinstance(adapter, requests.adapters.HTTPAdapter)
    assert adapter.max_retries.total == 7


@pytest.mark.parametrize("base", ["h.test/v1", "http://", "https://:x/"])
def test_a_base_url_that_requests_cannot_prepare_is_refused(
    base: str,
) -> None:
    """The error is the one requests raises, and it quotes none of the URL."""
    with pytest.raises(requests.exceptions.RequestException) as raised:
        API(base)

    assert base not in str(raised.value)
