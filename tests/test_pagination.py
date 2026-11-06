"""Tests for collection pagination helpers."""

from __future__ import annotations

import re
from typing import Any

import pytest
import requests

from eazyrest import (
    API,
    JSONObject,
    LimitExceeded,
    Page,
    PaginationError,
    RedirectError,
    ResponseShapeError,
    Results,
    UsageError,
    follow_link_headers,
    follow_next_links,
    json_object,
    link_headers_page,
    next_links_page,
)


class PagedBase(JSONObject):
    """Base object for models with paginated collection responses."""

    @classmethod
    def collection_items(cls, payload: Any, *, api: API) -> Any:
        """Read every page of a collection response.

        Args:
            payload: Decoded JSON body of the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following next links.
        """
        return follow_next_links(api, payload)


PagedBase.register_api(API("https://example.com/v1/"))


@json_object
class Item(PagedBase):
    """Model whose collection endpoint is paginated."""

    class_url = "/items/"

    id: int


class ResponsePagedBase(JSONObject):
    """Base object whose collection hook reads the response."""

    @classmethod
    def collection_items_from_response(
        cls, resp: requests.Response, *, api: API
    ) -> Any:
        """Read every page, resolving links against the first page's URL.

        Args:
            resp: Response for the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following next links.
        """
        return follow_next_links(api, resp.json(), url=resp.url)


ResponsePagedBase.register_api(API("https://example.com/v1/"))


@json_object
class Entry(ResponsePagedBase):
    """Model whose collection pages link to each other by query only."""

    class_url = "/entries/"

    id: int


class LinkPagedBase(JSONObject):
    """Base object whose collection pages are linked by Link headers."""

    @classmethod
    def collection_items_from_response(
        cls, resp: requests.Response, *, api: API
    ) -> Any:
        """Read every page named by a Link header.

        Args:
            resp: Response for the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following ``Link``
            headers.
        """
        return follow_link_headers(api, resp)


LinkPagedBase.register_api(API("https://example.com/v1/"))


@json_object
class LinkedItem(LinkPagedBase):
    """Model whose collection endpoint names its next page in a header."""

    class_url = "/linked/"

    id: int


LINKED = "https://example.com/v1/linked/"


@json_object
class Owner(PagedBase):
    """Related model whose bulk lookup sends no request."""

    class_url = "/owners/"

    id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: API | None = None
    ) -> dict[Any, Owner]:
        """Return an object for each primary key.

        Args:
            pks: Primary keys to look up.
            api: API to look the objects up with, which is ignored.

        Returns:
            A new ``Owner`` for each key in ``pks``, keyed by primary key.
        """
        del api
        return {pk: cls(json={"id": pk}) for pk in pks}


@json_object
class OwnedItem(PagedBase):
    """Paginated model with a prefetchable related field."""

    class_url = "/owned-items/"
    prefetch_batch_size = 2

    id: int
    owner: Owner


def test_list_payload_is_one_unpaginated_page(requests_mock: Any) -> None:
    """A list payload yields its items without further requests."""
    api = API("https://example.com/v1/")

    items = list(follow_next_links(api, [{"id": 1}, {"id": 2}]))

    assert items == [{"id": 1}, {"id": 2}]
    assert requests_mock.call_count == 0


def test_next_page_is_requested_only_when_reached(requests_mock: Any) -> None:
    """Each next page is requested after the previous page is consumed."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 3}]},
    )
    first_page = {
        "next": "https://example.com/v1/items/?page=2",
        "results": [{"id": 1}, {"id": 2}],
    }

    items = follow_next_links(api, first_page)
    first_two = [next(items), next(items)]
    calls_before_next_page = requests_mock.call_count
    rest = list(items)

    assert first_two == [{"id": 1}, {"id": 2}]
    assert calls_before_next_page == 0
    assert rest == [{"id": 3}]
    assert requests_mock.call_count == 1


def test_path_next_link_resolves_against_origin(requests_mock: Any) -> None:
    """A next-page path replaces the page's path, as in RFC 3986."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


def test_query_next_link_resolves_against_page_url(
    requests_mock: Any,
) -> None:
    """A query-only link keeps the path of the page that holds it."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": "?page=3", "results": [{"id": 2}]},
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=3",
        json={"next": None, "results": [{"id": 3}]},
    )

    items = list(
        follow_next_links(
            api,
            {"next": "?page=2", "results": [{"id": 1}]},
            url="/items/",
        )
    )

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_next_link_after_redirect_resolves_against_final_url(
    requests_mock: Any,
) -> None:
    """A redirected page's links are relative to the URL it moved to."""
    api = API("https://example.com/v1/")
    responses: list[str] = []

    def record_response(resp: requests.Response, **kwargs: Any) -> None:
        """Record the responses seen by the existing session hook.

        Args:
            resp: Response that the session received.
            kwargs: Other arguments that Requests passes to response hooks,
                which are ignored.
        """
        responses.append(resp.url)

    api.session.hooks["response"].append(record_response)
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=301,
        headers={"Location": "https://example.com/v2/items/?page=2"},
    )
    requests_mock.get(
        "https://example.com/v2/items/?page=2",
        json={"next": "?page=3", "results": [{"id": 2}]},
    )
    requests_mock.get(
        "https://example.com/v2/items/?page=3",
        json={"next": None, "results": [{"id": 3}]},
    )

    items = list(
        follow_next_links(
            api,
            {
                "next": "https://example.com/v1/items/?page=2",
                "results": [{"id": 1}],
            },
        )
    )

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert responses == [
        "https://example.com/v1/items/?page=2",
        "https://example.com/v2/items/?page=2",
        "https://example.com/v2/items/?page=3",
    ]


def test_first_page_links_resolve_against_base_url_by_default(
    requests_mock: Any,
) -> None:
    """Without ``url``, the base URL stands in for the first page's URL."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


def test_custom_page_keys(requests_mock: Any) -> None:
    """Pages may name their items and next link differently."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?cursor=b",
        json={"data": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api,
            {"data": [{"id": 1}], "links": "/v1/items/?cursor=b"},
            results_key="data",
            next_key="links",
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("last", [None, ""])
def test_empty_next_link_ends_iteration(
    requests_mock: Any, last: str | None
) -> None:
    """A null or empty next link marks the last page.

    Args:
        last: Value of the first page's ``"next"`` key, ``None`` or the empty
            string.
    """
    api = API("https://example.com/v1/")

    items = list(follow_next_links(api, {"results": [1], "next": last}))

    assert items == [1]
    assert requests_mock.call_count == 0


def test_next_link_to_current_page_raises_value_error(
    requests_mock: Any,
) -> None:
    """A page that links to itself is not read again."""
    api = API("https://example.com/v1/")
    page = {"results": [1], "next": "?page=1"}

    items = follow_next_links(api, page, url="/items/?page=1")

    assert next(items) == 1
    with pytest.raises(ValueError, match="already read"):
        next(items)
    assert requests_mock.call_count == 0


def test_next_link_cycle_raises_value_error(requests_mock: Any) -> None:
    """Pages that link back to an earlier page are not read again."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [2], "next": "?page=1"},
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=1",
        json={"results": [1], "next": "?page=2"},
    )

    items = follow_next_links(
        api, {"results": [1], "next": "?page=2"}, url="/items/"
    )

    assert [next(items), next(items), next(items)] == [1, 2, 1]
    with pytest.raises(ValueError, match="already read"):
        next(items)
    assert requests_mock.call_count == 2


def test_page_without_results_raises_value_error() -> None:
    """A page mapping must hold its items under the results key."""
    api = API("https://example.com/v1/")

    with pytest.raises(ValueError, match="'results'"):
        list(follow_next_links(api, {"next": None}))


@pytest.mark.parametrize("results", ["abc", {"name": 1}, None, 3])
def test_page_results_must_be_a_list(results: Any) -> None:
    """A page whose results are not a list raises before yielding items.

    Args:
        results: Malformed value of the page's ``results`` member.
    """
    api = API("https://example.com/v1/")

    with pytest.raises(TypeError, match="'results' member"):
        list(follow_next_links(api, {"next": None, "results": results}))


def test_other_payload_raises_type_error() -> None:
    """Only lists and page mappings are collection responses."""
    api = API("https://example.com/v1/")

    with pytest.raises(TypeError, match="str"):
        list(follow_next_links(api, "items"))


def test_filter_reads_every_page_lazily(requests_mock: Any) -> None:
    """``filter()`` with the helper streams objects across pages."""
    requests_mock.get(
        "https://example.com/v1/items/",
        json={
            "next": "https://example.com/v1/items/?page=2",
            "results": [{"id": 1}],
        },
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = Item.filter()
    first = next(items)
    calls_after_first = requests_mock.call_count

    assert first.id == 1
    assert calls_after_first == 1
    assert [item.id for item in items] == [2]
    assert requests_mock.call_count == 2


@pytest.mark.parametrize(
    "next_url",
    [
        # TLS terminated at a proxy that Django does not trust.
        "http://example.com/v1/items/?page=2",
        # A proxy that does not pass the original Host header through.
        "http://127.0.0.1:8000/v1/items/?page=2",
        "https://example.com:8443/v1/items/?page=2",
    ],
)
def test_next_link_to_other_origin_raises_value_error(
    requests_mock: Any, next_url: str
) -> None:
    """A next-page URL on another origin is rejected before any request.

    Args:
        next_url: Absolute next-page URL whose scheme, host, or port differs
            from the API's.
    """
    api = API("https://example.com/v1/")

    with pytest.raises(ValueError, match="different origin"):
        list(follow_next_links(api, {"next": next_url, "results": []}))

    assert requests_mock.call_count == 0


def test_next_link_origin_ignores_explicit_default_port(
    requests_mock: Any,
) -> None:
    """``https://host:443`` has the same origin as ``https://host``."""
    api = API("https://Example.com/v1/")
    requests_mock.get(
        "https://example.com:443/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api,
            {
                "next": "https://example.com:443/v1/items/?page=2",
                "results": [{"id": 1}],
            },
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize(
    "target",
    [
        r"https://other.example.net\@example.com/items/?page=2",
        r"//other.example.net\@example.com/items/?page=2",
    ],
)
def test_ambiguous_authority_cannot_bypass_origin_check(
    requests_mock: Any, target: str, redirect: bool
) -> None:
    """Check the destination Requests uses before sending credentials.

    Args:
        target: URL with an ambiguous authority that Requests sends to another
            origin.
        redirect: Whether ``target`` arrives as a redirect from a same-origin
            page rather than as the next link itself.
    """
    api = API("https://example.com/v1/")
    api.session.headers.update(
        {"Authorization": "Bearer test-token", "X-API-Key": "test-token"}
    )
    # Catch any escaped request, including one whose URL Requests normalizes.
    requests_mock.get(re.compile(".*"), json={"next": None, "results": []})
    if redirect:
        requests_mock.get(
            "https://example.com/v1/items/?page=2",
            status_code=302,
            headers={"Location": target},
        )
        next_url = "/v1/items/?page=2"
    else:
        next_url = target

    with pytest.raises(ValueError, match="different origin"):
        list(follow_next_links(api, {"next": next_url, "results": []}))

    assert requests_mock.call_count == int(redirect)
    if redirect:
        assert requests_mock.last_request.url == (
            "https://example.com/v1/items/?page=2"
        )


@pytest.mark.parametrize("redirect", [False, True])
def test_allow_other_origins_follows_next_link(
    requests_mock: Any, redirect: bool
) -> None:
    """The opt-in permits both links and redirects to another server.

    A redirect also needs the API to allow its origin, as every redirect does.

    Args:
        redirect: Whether the other server is reached by a redirect from a
            same-origin page rather than by the next link itself.
    """
    api = API("https://example.com/v1/")
    next_url = "https://cdn.example.net/items/?page=2"
    if redirect:
        api.redirect_origins = ["https://cdn.example.net"]
        requests_mock.get(
            "https://example.com/v1/items/?page=2",
            status_code=302,
            headers={"Location": next_url},
        )
        next_url = "https://example.com/v1/items/?page=2"
    requests_mock.get(
        "https://cdn.example.net/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api,
            {
                "next": next_url,
                "results": [{"id": 1}],
            },
            allow_other_origins=True,
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


def test_allow_other_origins_still_checks_redirects(
    requests_mock: Any,
) -> None:
    """The opt-in does not admit a redirect to an origin the API refuses."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=302,
        headers={"Location": "https://cdn.example.net/items/?page=2"},
    )
    cdn = requests_mock.get(
        "https://cdn.example.net/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = follow_next_links(
        api,
        {
            "next": "https://example.com/v1/items/?page=2",
            "results": [{"id": 1}],
        },
        allow_other_origins=True,
    )

    assert next(items) == {"id": 1}
    with pytest.raises(requests.HTTPError, match="redirect_origins"):
        next(items)
    assert not cdn.called


@pytest.mark.parametrize(
    "target",
    [
        "https://other.example.net/items/?page=2",
        "http://example.com/v1/items/?page=2",
        "https://example.com:8443/v1/items/?page=2",
    ],
)
def test_redirect_to_other_origin_is_rejected_before_sending_credentials(
    requests_mock: Any, target: str
) -> None:
    """Every redirect hop must pass the origin check before it is sent.

    Args:
        target: URL on another origin that the second redirect names.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "test-token"
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=302,
        headers={"Location": "/v2/items/?page=2"},
    )
    requests_mock.get(
        "https://example.com/v2/items/?page=2",
        status_code=302,
        headers={"Location": target},
    )
    other_origin = requests_mock.get(
        target, json={"next": None, "results": []}
    )

    with pytest.raises(ValueError, match="different origin"):
        list(
            follow_next_links(
                api, {"next": "/v1/items/?page=2", "results": []}
            )
        )

    assert requests_mock.call_count == 2
    assert not other_origin.called


def test_filter_with_api_reads_every_page_with_it(requests_mock: Any) -> None:
    """``collection_items()`` reads later pages with the query's API."""
    tenant = API("https://tenant.example.com/v1/")
    requests_mock.get(
        "https://tenant.example.com/v1/items/",
        json={
            "results": [{"id": 1}],
            "next": "https://tenant.example.com/v1/items/?page=2",
        },
    )
    requests_mock.get(
        "https://tenant.example.com/v1/items/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    items = list(Item.filter(api=tenant))

    assert [item.id for item in items] == [1, 2]
    assert all(item.api is tenant for item in items)
    assert requests_mock.call_count == 2
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example.com"
    }
    assert Item.api is not tenant


def test_collection_hook_receives_the_response(requests_mock: Any) -> None:
    """A query-only next link resolves against the first page's URL."""
    requests_mock.get(
        "https://example.com/v1/entries/?kind=a",
        json={"results": [{"id": 1}], "next": "?kind=a&page=2"},
    )
    second = requests_mock.get(
        "https://example.com/v1/entries/?kind=a&page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    entries = list(Entry.filter(kind="a"))

    assert [entry.id for entry in entries] == [1, 2]
    assert second.call_count == 1


def test_link_headers_are_followed_lazily(requests_mock: Any) -> None:
    """Absolute and relative next links are followed only when reached."""
    requests_mock.get(
        LINKED,
        json=[{"id": 1}],
        headers={
            "Link": f'<{LINKED}?page=2>; rel="next", '
            f'<{LINKED}?page=3>; rel="last"'
        },
    )
    requests_mock.get(
        LINKED + "?page=2",
        json=[{"id": 2}],
        headers={"Link": '</v1/linked/?page=3>; rel="next"'},
    )
    requests_mock.get(LINKED + "?page=3", json=[{"id": 3}])

    items = LinkedItem.filter()
    first = next(items)

    assert first.id == 1
    assert requests_mock.call_count == 1
    assert [item.id for item in items] == [2, 3]
    assert requests_mock.call_count == 3


@pytest.mark.parametrize(
    "rel", ['"next last"', '"last next"', "next", '"NEXT"', '"Next prev"']
)
def test_link_headers_find_next_among_relation_types(
    requests_mock: Any, rel: str
) -> None:
    """A link is followed when ``next`` is one of its relation types.

    Args:
        rel: Value of the link's ``rel`` parameter.
    """
    requests_mock.get(
        LINKED,
        json=[{"id": 1}],
        headers={"Link": f"<{LINKED}?page=2>; rel={rel}"},
    )
    requests_mock.get(LINKED + "?page=2", json=[{"id": 2}])

    assert [item.id for item in LinkedItem.filter()] == [1, 2]
    assert requests_mock.call_count == 2


@pytest.mark.parametrize(
    ("link", "error", "match"),
    [
        (
            '<https://other.example.com/linked/?page=2>; rel="next"',
            ValueError,
            "different origin",
        ),
        (f'<{LINKED}>; rel="next"', ValueError, "already read"),
    ],
)
def test_link_headers_reject_unsafe_links(
    requests_mock: Any, link: str, error: type[Exception], match: str
) -> None:
    """A link to another origin or to a page already read raises.

    Args:
        link: ``Link`` header of the first page.
        error: Exception type that reading the collection raises.
        match: Pattern that the exception's message matches.
    """
    requests_mock.get(LINKED, json=[{"id": 1}], headers={"Link": link})

    with pytest.raises(error, match=match):
        list(LinkedItem.filter())

    assert requests_mock.call_count == 1


def test_link_header_pages_must_be_lists(requests_mock: Any) -> None:
    """A page body that is not a list raises ``TypeError``."""
    requests_mock.get(LINKED, json={"results": []})

    with pytest.raises(TypeError, match="must be JSON lists"):
        list(LinkedItem.filter())


def test_prefetch_reads_ahead_across_pages(requests_mock: Any) -> None:
    """A prefetch batch fills from later pages before the first object."""
    requests_mock.get(
        "https://example.com/v1/owned-items/",
        json={
            "next": "https://example.com/v1/owned-items/?page=2",
            "results": [{"id": 1, "owner": 10}],
        },
    )
    requests_mock.get(
        "https://example.com/v1/owned-items/?page=2",
        json={"next": None, "results": [{"id": 2, "owner": 20}]},
    )

    items = OwnedItem.filter(prefetch="owner")
    first = next(items)

    assert requests_mock.call_count == 2
    assert first.owner.pk == 10
    assert [item.owner.pk for item in items] == [20]
    assert requests_mock.call_count == 2


def test_single_callable_session_hook_is_kept(requests_mock: Any) -> None:
    """A session hook given as one callable, as requests allows, still runs."""
    api = API("https://example.com/v1/")
    seen: list[str] = []

    def record(resp: requests.Response, **kwargs: Any) -> None:
        seen.append(resp.url)

    api.session.hooks["response"] = record
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]
    assert seen == ["https://example.com/v1/items/?page=2"]


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com\\@evil.example.net/items/?page=2",
        "//example.com\\@evil.example.net/items/?page=2",
    ],
)
def test_backslash_redirect_cannot_leave_api_origin(
    requests_mock: Any, target: str
) -> None:
    """A page redirect cannot hide another host behind a backslash.

    Args:
        target: Redirect target that requests sends to another host.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "test-token"
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=302,
        headers={"Location": target},
    )
    evil = requests_mock.get(
        re.compile(r"https://.*evil\.example\.net/.*"),
        json={"next": None, "results": []},
    )

    with pytest.raises(ValueError, match="different origin"):
        list(
            follow_next_links(
                api, {"next": "/v1/items/?page=2", "results": []}
            )
        )

    assert not evil.called


# Responses in the shapes that Django REST Framework's paginators produce, for
# a collection of three items in pages of two.
_PAGE_NUMBER = [
    (
        "https://example.com/v1/items/",
        {
            "count": 3,
            "next": "https://example.com/v1/items/?page=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        },
    ),
    (
        "https://example.com/v1/items/?page=2",
        {
            "count": 3,
            "next": None,
            "previous": "https://example.com/v1/items/",
            "results": [{"id": 3}],
        },
    ),
]
_LIMIT_OFFSET = [
    (
        "https://example.com/v1/items/?limit=2",
        {
            "count": 3,
            "next": "https://example.com/v1/items/?limit=2&offset=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        },
    ),
    (
        "https://example.com/v1/items/?limit=2&offset=2",
        {
            "count": 3,
            "next": None,
            "previous": "https://example.com/v1/items/?limit=2",
            "results": [{"id": 3}],
        },
    ),
]
_CURSOR = [
    (
        "https://example.com/v1/items/",
        {
            "next": "https://example.com/v1/items/?cursor=cD0y",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        },
    ),
    (
        "https://example.com/v1/items/?cursor=cD0y",
        {
            "next": None,
            "previous": "https://example.com/v1/items/?cursor=cj0xJnA9Mw%3D%3D",
            "results": [{"id": 3}],
        },
    ),
]


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param(_PAGE_NUMBER, id="PageNumberPagination"),
        pytest.param(_LIMIT_OFFSET, id="LimitOffsetPagination"),
        pytest.param(_CURSOR, id="CursorPagination"),
    ],
)
def test_drf_paginators_are_read_in_full(
    requests_mock: Any, pages: list[tuple[str, dict[str, Any]]]
) -> None:
    """Every page shape that DRF's paginators produce reads every item.

    Args:
        pages: URL and body of each page, in order.
    """
    api = API("https://example.com/v1/")
    for url, body in pages[1:]:
        requests_mock.get(url, json=body, complete_qs=True)
    (first_url, first_page) = pages[0]

    items = list(follow_next_links(api, first_page, url=first_url))

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert requests_mock.call_count == len(pages) - 1


def test_filter_reports_the_collection_count(requests_mock: Any) -> None:
    """``filter()`` passes on the first page's ``count``, and stays lazy."""
    requests_mock.get(
        "https://example.com/v1/items/",
        json={
            "count": 3,
            "next": "https://example.com/v1/items/?page=2",
            "results": [{"id": 1}, {"id": 2}],
        },
    )
    second = requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"count": 3, "next": None, "results": [{"id": 3}]},
    )

    items = Item.filter()

    assert isinstance(items, Results)
    assert items.count == 3
    assert not second.called
    assert [item.id for item in items] == [1, 2, 3]


@pytest.mark.parametrize(
    "payload", [[{"id": 1}], {"next": None, "results": []}, {"count": "3"}]
)
def test_count_is_none_when_the_response_has_none(payload: Any) -> None:
    """A list, a page without ``count``, or a non-integer count gives None.

    Args:
        payload: First response of the collection.
    """
    api = API("https://example.com/v1/")

    assert follow_next_links(api, payload).count is None


def test_count_comes_from_a_custom_hook(requests_mock: Any) -> None:
    """Any iterable with an integer ``count`` gives ``filter()`` its count."""

    class Counted:
        count = 42

        def __init__(self, items: list[Any]) -> None:
            self.items = items

        def __iter__(self) -> Any:
            return iter(self.items)

    @json_object
    class Custom(PagedBase):
        class_url = "/custom/"

        id: int

        @classmethod
        def collection_items(cls, payload: Any, *, api: API) -> Any:
            return Counted(payload["data"])

    requests_mock.get(
        "https://example.com/v1/custom/", json={"data": [{"id": 1}]}
    )

    items = Custom.filter()

    assert items.count == 42
    assert [item.id for item in items] == [1]
    assert Custom.all().count == 42


def test_scoped_option_hooks_replace_session_hooks_on_every_page(
    requests_mock: Any,
) -> None:
    """Option hooks replace the session's, on later pages as on the first."""
    api = API("https://example.com/v1/")
    seen: list[str] = []
    api.session.hooks["response"] = [lambda resp, **_: seen.append("session")]
    scoped = api._scoped(
        {"hooks": {"response": [lambda resp, **_: seen.append("option")]}}
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            scoped, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]
    assert seen == ["option"]


def test_unfollowed_redirect_of_a_later_page_raises(
    requests_mock: Any,
) -> None:
    """A page answered with a redirect that is not followed has no items."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=304,
        json={"next": None, "results": [{"id": 2}]},
    )

    items = follow_next_links(
        api, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
    )

    assert next(items) == {"id": 1}
    with pytest.raises(RedirectError, match="status 304"):
        next(items)


def test_query_api_is_the_class_api_during_the_hook(
    requests_mock: Any,
) -> None:
    """While a hook runs, ``cls.api`` is the query's API, as ``api`` is."""
    tenant = API("https://tenant.example.com/v1/")
    seen: list[tuple[API, API]] = []

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_items(cls, payload: Any, *, api: API) -> Any:
            seen.append((cls.api, api))
            return payload

    requests_mock.get("https://tenant.example.com/v1/probes/", json=[])

    list(Probe.filter(api=tenant))

    assert seen == [(tenant, tenant)]
    assert Probe.api is not tenant


def test_query_request_options_reach_later_pages(requests_mock: Any) -> None:
    """A query's options reach every page that its hook requests."""
    first = requests_mock.get(
        "https://example.com/v1/items/",
        json={"results": [{"id": 1}], "next": "/v1/items/?page=2"},
    )
    second = requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    items = list(Item.filter(request_options={"headers": {"X-Trace": "t"}}))

    assert [item.id for item in items] == [1, 2]
    assert first.last_request.headers["X-Trace"] == "t"
    assert second.last_request.headers["X-Trace"] == "t"
    assert all(item._api_override is None for item in items)


@pytest.mark.parametrize("link", [5, ["/v1/items/?page=2"], {"href": "x"}])
def test_next_link_that_is_not_a_string_is_a_shape_error(link: Any) -> None:
    """A next link that is not a URL fails as a misshapen page.

    Args:
        link: Next-page member that is neither a URL nor empty.
    """
    api = API("https://example.com/v1/")
    items = follow_next_links(api, {"next": link, "results": [{"id": 1}]})

    assert next(items) == {"id": 1}
    with pytest.raises(ResponseShapeError, match="must be a URL"):
        next(items)


def _page(api: API, requests_mock: Any, url: str, **body: Any) -> Page[Any]:
    """Return the page that a mocked ``url`` answers with ``body``.

    Args:
        api: Client to read the page with.
        requests_mock: Mocker that serves the page.
        url: Absolute URL of the page.
        **body: Members of the JSON page.

    Returns:
        The page, read with ``next_links_page()``.
    """
    requests_mock.get(url, json=body)
    return next_links_page(api, api.get(url))


def test_next_links_page_navigates_both_ways(requests_mock: Any) -> None:
    """A page's links are resolved, followed, and followed back."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={
            "count": 3,
            "results": [{"id": 3}],
            "next": None,
            "previous": "?page=1",
        },
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=1",
        json={
            "count": 3,
            "results": [{"id": 1}, {"id": 2}],
            "next": "/v1/items/?page=2",
            "previous": "",
        },
    )

    first = next_links_page(api, api.get("/items/?page=1"))

    assert list(first) == [{"id": 1}, {"id": 2}]
    assert len(first) == 2
    assert first.count == 3
    assert first.previous_url is None
    assert first.previous_page() is None
    assert first.next_url == "https://example.com/v1/items/?page=2"
    second = first.next_page()
    assert second is not None
    assert second.items == [{"id": 3}]
    assert second.next_page() is None
    back = second.previous_page()
    assert back is not None
    assert back.items == first.items
    assert "items=2" in repr(back)


def test_list_payload_is_a_page_without_links(requests_mock: Any) -> None:
    """A list payload is one page with no links and no count."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/", json=[{"id": 1}])

    page = next_links_page(api, api.get("/items/"))

    assert page.items == [{"id": 1}]
    assert page.count is None
    assert page.next_page() is None
    assert page.previous_page() is None


def test_page_navigation_refuses_cycles_and_too_many_pages(
    requests_mock: Any,
) -> None:
    """A next link back to a page read going forward, or too many, raises."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [], "next": "/v1/items/?page=1"},
    )
    first = _page(
        api,
        requests_mock,
        "https://example.com/v1/items/?page=1",
        results=[],
        next="/v1/items/?page=2",
    )
    second = first.next_page()
    assert second is not None

    with pytest.raises(PaginationError, match="already read"):
        second.next_page()

    api.max_pages = 1
    with pytest.raises(LimitExceeded, match="more than 1 pages"):
        first.next_page()


def test_page_link_to_another_origin_is_refused(requests_mock: Any) -> None:
    """A link to another origin is refused before anything is sent there."""
    api = API("https://example.com/v1/")
    other = requests_mock.get("https://other.example/items/", json=[])
    page = _page(
        api,
        requests_mock,
        "https://example.com/v1/items/",
        results=[],
        next="https://other.example/items/",
    )

    with pytest.raises(PaginationError, match="different origin"):
        page.next_page()

    assert not other.called


def test_page_larger_than_max_items_is_refused(requests_mock: Any) -> None:
    """A single page with more items than ``max_items`` raises."""
    api = API("https://example.com/v1/")
    api.max_items = 1

    with pytest.raises(LimitExceeded, match="more than 1 items"):
        _page(
            api,
            requests_mock,
            "https://example.com/v1/items/",
            results=[{"id": 1}, {"id": 2}],
        )


def test_built_page_cannot_read_other_pages() -> None:
    """A page that a hook builds itself has no way to read its links."""
    page = Page([1], url="https://example.com/v1/items/", next_url="x")

    with pytest.raises(UsageError, match="cannot read other pages"):
        page.next_page()


def test_link_headers_page_reads_next_and_previous(requests_mock: Any) -> None:
    """``rel="next"`` and ``rel="prev"`` name the pages around a page."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json=[{"id": 2}],
        headers={"Link": '</v1/items/?page=1>; rel="prev first"'},
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=1",
        json=[{"id": 1}],
        headers={"Link": '<?page=2>; rel="next", <>; rel="previous"'},
    )

    first = link_headers_page(api, api.get("/items/?page=1"))

    assert first.items == [{"id": 1}]
    assert first.count is None
    assert first.previous_url is None
    second = first.next_page()
    assert second is not None
    assert second.items == [{"id": 2}]
    assert second.previous_url == "https://example.com/v1/items/?page=1"
    assert second.next_url is None


def test_link_headers_page_must_be_a_list(requests_mock: Any) -> None:
    """A Link-header page whose body is not a JSON list is misshapen."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/", json={"results": []})

    with pytest.raises(ResponseShapeError, match="JSON lists"):
        link_headers_page(api, api.get("/items/"))


def _serve_two_pages(requests_mock: Any, path: str) -> None:
    """Serve a two-page DRF collection at ``path``.

    Args:
        requests_mock: Mocker that serves the pages.
        path: Path of the collection under the API's base URL.
    """
    base = f"https://example.com/v1{path}"
    requests_mock.get(
        base,
        json={
            "count": 3,
            "results": [{"id": 1, "owner": 7}, {"id": 2, "owner": 8}],
            "next": f"{base}?page=2",
            "previous": None,
        },
    )
    requests_mock.get(
        f"{base}?page=2",
        json={
            "count": 3,
            "results": [{"id": 3, "owner": 7}],
            "next": None,
            "previous": base,
        },
    )


def test_get_page_reads_and_navigates_pages_of_objects(
    requests_mock: Any,
) -> None:
    """``get_page()`` returns objects, and its links return objects too."""
    _serve_two_pages(requests_mock, "/items/")

    first = Item.get_page(request_options={"headers": {"X-Trace": "t"}})

    assert [item.id for item in first] == [1, 2]
    assert all(isinstance(item, Item) for item in first)
    assert first.count == 3
    assert first.previous_page() is None
    second = first.next_page()
    assert second is not None
    assert [item.id for item in second] == [3]
    assert all(item._api_override is None for item in second)
    back = second.previous_page()
    assert back is not None
    assert [item.id for item in back] == [1, 2]
    assert all(
        request.headers["X-Trace"] == "t"
        for request in requests_mock.request_history
    )


def test_get_page_prefetches_each_page(requests_mock: Any) -> None:
    """Prefetch applies to each page that navigation reads."""
    _serve_two_pages(requests_mock, "/owned-items/")

    first = OwnedItem.get_page(prefetch="owner")
    second = first.next_page()

    assert second is not None
    assert [item.owner.id for item in first] == [7, 8]
    assert [item.owner.id for item in second] == [7]
    assert requests_mock.call_count == 2


def test_get_page_fetches_a_page_url(requests_mock: Any) -> None:
    """A saved page URL fetches that page, but not with query parameters."""
    _serve_two_pages(requests_mock, "/items/")
    other = requests_mock.get("https://other.example/items/", json=[])

    page = Item.get_page(url="/items/?page=2")

    assert [item.id for item in page] == [3]
    with pytest.raises(TypeError, match="not both"):
        Item.get_page(url="/items/?page=2", id=3)
    with pytest.raises(PaginationError, match="different origin"):
        Item.get_page(url="https://other.example/items/")
    assert not other.called


def test_collection_page_hook_reads_link_headers(requests_mock: Any) -> None:
    """A model reads Link-header pages by returning ``link_headers_page()``."""
    seen: list[tuple[API, API]] = []
    tenant = API("https://example.com/v1/")

    @json_object
    class Linked(PagedBase):
        class_url = "/linked/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: requests.Response, *, api: API
        ) -> Page[Any]:
            seen.append((cls.api, api))
            return link_headers_page(api, resp)

    requests_mock.get(
        "https://example.com/v1/linked/",
        json=[{"id": 1}],
        headers={"Link": '<?page=2>; rel="next"'},
    )
    requests_mock.get(
        "https://example.com/v1/linked/?page=2",
        json=[{"id": 2}],
        headers={"Link": '</v1/linked/>; rel="prev"'},
    )

    page = Linked.get_page(api=tenant)
    second = page.next_page()

    assert second is not None
    assert [item.id for item in second] == [2]
    assert all(item.api is tenant for item in second)
    assert seen == [(tenant, tenant), (tenant, tenant)]


def test_collection_page_hook_must_return_a_page(requests_mock: Any) -> None:
    """A hook that returns something other than a page fails clearly."""

    @json_object
    class Wrong(PagedBase):
        class_url = "/wrong/"

        id: int

        @classmethod
        def collection_page(cls, resp: requests.Response, *, api: API) -> Any:
            return api.json(resp)

    requests_mock.get("https://example.com/v1/wrong/", json=[{"id": 1}])

    with pytest.raises(TypeError, match="must return a Page"):
        Wrong.get_page()


def test_page_built_by_a_hook_holds_objects_but_cannot_navigate(
    requests_mock: Any,
) -> None:
    """A hook may build its own page, which then cannot read its links."""

    @json_object
    class Built(PagedBase):
        class_url = "/built/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: requests.Response, *, api: API
        ) -> Page[Any]:
            return Page(api.json(resp), url=resp.url, next_url="?page=2")

    requests_mock.get("https://example.com/v1/built/", json=[{"id": 1}])

    page = Built.get_page()

    assert [item.id for item in page] == [1]
    with pytest.raises(UsageError, match="cannot read other pages"):
        page.next_page()
