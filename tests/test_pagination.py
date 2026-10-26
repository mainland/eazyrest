"""Tests for collection pagination helpers."""

from __future__ import annotations

import re
from typing import Any

import pytest
import requests

from eazyrest import API, JSONObject, follow_next_links, json_object


class PagedBase(JSONObject):
    """Base object for models with paginated collection responses."""

    @classmethod
    def collection_items(cls, payload: Any) -> Any:
        """Read every page of a collection response."""
        return follow_next_links(cls.api, payload)


PagedBase.register_api(API("https://example.com/v1/"))


@json_object
class Item(PagedBase):
    """Model whose collection endpoint is paginated."""

    class_url = "/items/"

    id: int


@json_object
class Owner(PagedBase):
    """Related model whose bulk lookup sends no request."""

    class_url = "/owners/"

    id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: API | None = None
    ) -> dict[Any, Owner]:
        """Return an object for each primary key."""
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
        """Record the responses seen by the existing session hook."""
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
    """A null or empty next link marks the last page."""
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
    """A next-page URL on another origin is rejected before any request."""
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
    """Check the destination Requests uses before sending credentials."""
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
    """The opt-in permits both links and redirects to another server."""
    api = API("https://example.com/v1/")
    next_url = "https://cdn.example.net/items/?page=2"
    if redirect:
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
    """Every redirect hop must pass the origin check before it is sent."""
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
    """``collection_items()`` sees the query's API as ``cls.api``."""
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
