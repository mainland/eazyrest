"""Every traversal follows a page link by the same checks.

``filter()``, ``all()``, ``next_page()``, ``previous_page()``, and the two
``follow_*`` helpers refuse a link to a page already read, then a link to
another origin unless the page that holds the link allows it, then a page past
``max_pages``. They record the URL that the response came from, and follow a
link by the setting of the page that holds it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests

from eazyrest import (
    API,
    Client,
    JSONObject,
    LimitExceeded,
    Page,
    PaginationError,
    Response,
    follow_link_headers,
    follow_next_links,
    json_object,
    link_headers_page,
    next_links_page,
)

BASE = "https://example.com/v1/"
ITEMS = BASE + "items/"
CDN = "https://cdn.test/items/"
EVIL = "https://evil.test/items/"


def _body(number: int, link: str | None) -> dict[str, Any]:
    """Return a page of one item that links to link."""
    return {"results": [{"id": number}], "next": link, "previous": None}


@json_object
class Plain(JSONObject):
    """Model whose pages are read by the default hook."""

    class_url = "/items/"

    id: int


Plain.register_api(API(BASE))


@json_object
class Linked(JSONObject):
    """Model whose pages are linked by headers."""

    class_url = "/items/"

    id: int

    @classmethod
    def collection_page(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Page[Any]:
        """Read a page and the pages that it links to by ``Link`` headers.

        Args:
            resp: Response for the page.
            api: The query's client.
            **kwargs: Context that later releases may pass.

        Returns:
            The page.
        """
        return link_headers_page(api, resp)


Linked.register_api(API(BASE))


class _settings:
    """Which pages of ``Varying`` allow other origins, set by each test."""

    @staticmethod
    def allow(url: str) -> bool:
        """Return whether the page at ``url`` allows other origins."""
        return False


@json_object
class Varying(JSONObject):
    """Model whose pages allow other origins by the page's own URL."""

    class_url = "/items/"

    id: int

    @classmethod
    def collection_page(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Page[Any]:
        """Read a page that allows other origins only if its URL says so.

        Args:
            resp: Response for the page.
            api: The query's client.
            **kwargs: Context that later releases may pass.

        Returns:
            The page, which allows other origins as ``_settings`` says.
        """
        return next_links_page(
            api, resp, allow_other_origins=_settings.allow(resp.url)
        )


Varying.register_api(API(BASE))


@json_object
class Renamed(JSONObject):
    """Model whose hook reports a page URL without its query."""

    class_url = "/items/"

    id: int

    @classmethod
    def collection_page(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Page[Any]:
        """Read a page and report its URL without the query.

        Args:
            resp: Response for the page.
            api: The query's client.
            **kwargs: Context that later releases may pass.

        Returns:
            The page, whose ``url`` is not the URL that was requested.
        """
        page = next_links_page(api, resp)
        page.url = page.url.split("?")[0]
        return page


Renamed.register_api(API(BASE))


def _filter(model: type[Any], api: API, **query: Any) -> list[Any]:
    """Read a collection with ``filter()``."""
    return [item.id for item in model.filter(api=api, **query)]


def _all(model: type[Any], api: API, **query: Any) -> list[Any]:
    """Read a collection with ``all()``, which takes no query."""
    return [item.id for item in model.all(api=api)]


def _next_pages(model: type[Any], api: API, **query: Any) -> list[Any]:
    """Read a collection with ``get_page()`` and ``next_page()``."""
    ids: list[Any] = []
    page: Page[Any] | None = model.get_page(api=api, **query)
    while page is not None:
        ids.extend(item.id for item in page)
        page = page.next_page()
    return ids


TRAVERSALS: dict[str, Callable[..., list[Any]]] = {
    "filter": _filter,
    "all": _all,
    "next_page": _next_pages,
}


def _urls(mock: Any) -> list[str]:
    """Return the URLs requested, without the base URL."""
    return [request.url.removeprefix(BASE) for request in mock.request_history]


by_traversal = pytest.mark.parametrize(
    "traverse", TRAVERSALS.values(), ids=TRAVERSALS
)


@by_traversal
def test_a_traversal_reads_every_page_of_a_collection(
    requests_mock: Any, traverse: Callable[[type[Any], API], list[Any]]
) -> None:
    """The links are followed in order."""
    requests_mock.get(ITEMS, json=_body(1, "?page=2"))
    requests_mock.get(ITEMS + "?page=2", json=_body(2, None))

    assert traverse(Plain, API(BASE)) == [1, 2]


@by_traversal
def test_a_traversal_refuses_a_link_to_a_page_already_read(
    requests_mock: Any, traverse: Callable[[type[Any], API], list[Any]]
) -> None:
    """A loop raises after each page was requested once."""
    requests_mock.get(ITEMS, json=_body(1, "?page=2"))
    requests_mock.get(ITEMS + "?page=2", json=_body(2, ITEMS))

    with pytest.raises(PaginationError, match="already read"):
        traverse(Plain, API(BASE))

    assert _urls(requests_mock) == ["items/", "items/?page=2"]


@by_traversal
def test_a_traversal_refuses_a_link_to_another_origin(
    requests_mock: Any, traverse: Callable[[type[Any], API], list[Any]]
) -> None:
    """The page's link is not requested, and the error names no path."""
    requests_mock.get(ITEMS, json=_body(1, EVIL + "?page=2"))
    other = requests_mock.get(EVIL + "?page=2", json=_body(2, None))

    with pytest.raises(PaginationError, match="different origin"):
        traverse(Plain, API(BASE))

    assert not other.called


@by_traversal
@pytest.mark.parametrize("pages", [2, 3])
def test_a_traversal_stops_at_max_pages(
    requests_mock: Any,
    traverse: Callable[[type[Any], API], list[Any]],
    pages: int,
) -> None:
    """Reading a page past the limit raises before it is requested."""
    for number in range(1, 5):
        url = ITEMS if number == 1 else f"{ITEMS}?page={number}"
        link = f"?page={number + 1}" if number < 4 else None
        requests_mock.get(url, json=_body(number, link))
    api = API(BASE)
    api.max_pages = pages

    with pytest.raises(LimitExceeded, match=f"more than {pages} pages"):
        traverse(Plain, api)

    assert len(requests_mock.request_history) == pages


@by_traversal
def test_a_traversal_refuses_a_link_that_requests_cannot_prepare(
    requests_mock: Any, traverse: Callable[[type[Any], API], list[Any]]
) -> None:
    """Nothing is sent for the link, and the error quotes none of it."""
    requests_mock.get(ITEMS, json=_body(1, "https://h.test:99999/SECRET"))

    with pytest.raises(PaginationError, match="cannot be parsed") as raised:
        traverse(Plain, API(BASE))

    assert "SECRET" not in str(raised.value)
    assert len(requests_mock.request_history) == 1


@by_traversal
def test_a_link_is_refused_by_the_setting_of_the_page_that_holds_it(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    traverse: Callable[[type[Any], API], list[Any]],
) -> None:
    """The first page allows another origin, and the page there does not."""
    monkeypatch.setattr(
        _settings, "allow", lambda url: urlsplit(url).hostname == "example.com"
    )
    requests_mock.get(ITEMS, json=_body(1, CDN + "?page=2"))
    requests_mock.get(CDN + "?page=2", json=_body(2, EVIL + "?page=3"))
    evil = requests_mock.get(EVIL + "?page=3", json=_body(3, None))

    with pytest.raises(PaginationError, match="different origin"):
        traverse(Varying, API(BASE))

    assert not evil.called


@by_traversal
def test_a_later_page_that_allows_other_origins_is_followed_there(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    traverse: Callable[[type[Any], API], list[Any]],
) -> None:
    """Only the second page allows it, and its link to another origin goes."""
    monkeypatch.setattr(_settings, "allow", lambda url: "page=2" in url)
    requests_mock.get(ITEMS, json=_body(1, ITEMS + "?page=2"))
    requests_mock.get(ITEMS + "?page=2", json=_body(2, CDN + "?page=3"))
    requests_mock.get(CDN + "?page=3", json=_body(3, None))

    assert traverse(Varying, API(BASE)) == [1, 2, 3]


@by_traversal
def test_a_page_hook_that_changes_the_page_url_does_not_hide_a_loop(
    requests_mock: Any, traverse: Callable[[type[Any], API], list[Any]]
) -> None:
    """A loop through a redirect is found after the same requests."""
    requests_mock.get(ITEMS, json=_body(1, "?page=2"))
    requests_mock.get(
        ITEMS + "?page=2",
        status_code=302,
        headers={"Location": BASE + "items/?page=2b"},
    )
    requests_mock.get(ITEMS + "?page=2b", json=_body(2, "?page=2b"))

    with pytest.raises(PaginationError, match="already read"):
        traverse(Renamed, API(BASE))

    assert _urls(requests_mock) == [
        "items/",
        "items/?page=2",
        "items/?page=2b",
    ]


@pytest.mark.parametrize(
    "traverse", [_filter, _next_pages], ids=["filter", "next_page"]
)
def test_the_first_page_is_known_by_the_url_that_was_requested(
    requests_mock: Any, traverse: Callable[..., list[Any]]
) -> None:
    """A link back to the first page is a loop, whatever its hook reports."""
    requests_mock.get(ITEMS + "?x=1", json=_body(1, "?page=2"))
    requests_mock.get(ITEMS + "?page=2", json=_body(2, ITEMS + "?x=1"))

    with pytest.raises(PaginationError, match="already read"):
        traverse(Renamed, API(BASE), x=1)

    assert _urls(requests_mock) == ["items/?x=1", "items/?page=2"]


@by_traversal
def test_a_link_to_another_origin_is_refused_before_max_pages_is_counted(
    requests_mock: Any, traverse: Callable[..., list[Any]]
) -> None:
    """The origin check comes before the limit on pages."""
    requests_mock.get(ITEMS, json=_body(1, EVIL + "?page=2"))
    api = API(BASE)
    api.max_pages = 1

    with pytest.raises(PaginationError, match="different origin"):
        traverse(Plain, api)


def test_previous_page_follows_the_same_checks(requests_mock: Any) -> None:
    """A loop, another origin, and ``max_pages`` hold going back."""
    requests_mock.get(
        ITEMS,
        json=[{"id": 2}],
        headers={"Link": f'<{ITEMS}?page=1>; rel="prev"'},
    )
    requests_mock.get(
        ITEMS + "?page=1",
        json=[{"id": 1}],
        headers={"Link": f'<{ITEMS}>; rel="prev"'},
    )
    page = Linked.get_page()
    first = page.previous_page()
    assert first is not None
    with pytest.raises(PaginationError, match="already read"):
        first.previous_page()

    requests_mock.get(
        ITEMS + "?back",
        json=[{"id": 3}],
        headers={"Link": f'<{EVIL}>; rel="prev"'},
    )
    other = requests_mock.get(EVIL, json=[])
    with pytest.raises(PaginationError, match="different origin"):
        Linked.get_page(url=ITEMS + "?back").previous_page()
    assert not other.called

    limited = API(BASE)
    limited.max_pages = 1
    with pytest.raises(LimitExceeded, match="more than 1 pages"):
        Linked.get_page(api=limited).previous_page()


def test_next_links_helper_follows_each_link_by_one_rule(
    requests_mock: Any,
) -> None:
    """The helpers refuse a loop, another origin, and ``max_pages``."""
    api = API(BASE)
    requests_mock.get(ITEMS + "?page=2", json=_body(2, ITEMS))

    with pytest.raises(PaginationError, match="already read"):
        list(
            follow_next_links(
                api, _body(1, "?page=2"), url=ITEMS, allow_other_origins=False
            )
        )
    api.max_pages = 1
    with pytest.raises(LimitExceeded, match="more than 1 pages"):
        list(follow_next_links(api, _body(1, "?page=2"), url=ITEMS))
    api.max_pages = 1000
    other = requests_mock.get(CDN, json=_body(2, None))
    with pytest.raises(PaginationError, match="different origin"):
        list(follow_next_links(api, _body(1, CDN), url=ITEMS))
    assert not other.called
    assert list(
        follow_next_links(
            api, _body(1, CDN), url=ITEMS, allow_other_origins=True
        )
    ) == [{"id": 1}, {"id": 2}]


def test_link_header_helper_follows_each_link_by_one_rule(
    requests_mock: Any,
) -> None:
    """A Link header's loop, other origin, and page limit are refused."""
    requests_mock.get(
        ITEMS, json=[1], headers={"Link": f'<{ITEMS}?p=2>; rel="next"'}
    )
    requests_mock.get(
        ITEMS + "?p=2", json=[2], headers={"Link": f'<{ITEMS}>; rel="next"'}
    )
    api = API(BASE)

    with pytest.raises(PaginationError, match="already read"):
        list(follow_link_headers(api, requests.get(ITEMS, timeout=1)))
    api.max_pages = 1
    with pytest.raises(LimitExceeded, match="more than 1 pages"):
        list(follow_link_headers(api, requests.get(ITEMS, timeout=1)))
    assert urlsplit(ITEMS).scheme == "https"


def test_link_header_helper_refuses_another_origin(requests_mock: Any) -> None:
    """Without ``allow_other_origins`` the link is not followed."""
    requests_mock.get(
        ITEMS, json=[1], headers={"Link": f'<{CDN}>; rel="next"'}
    )
    other = requests_mock.get(CDN, json=[2])
    api = API(BASE)

    with pytest.raises(PaginationError, match="different origin"):
        list(follow_link_headers(api, requests.get(ITEMS, timeout=1)))
    assert not other.called
    assert list(
        follow_link_headers(
            api, requests.get(ITEMS, timeout=1), allow_other_origins=True
        )
    ) == [1, 2]
