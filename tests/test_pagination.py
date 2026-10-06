"""Tests for collection pagination helpers."""

from __future__ import annotations

from typing import Any

import pytest

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


def test_relative_next_link_resolves_against_base_url(
    requests_mock: Any,
) -> None:
    """A relative next-page URL keeps the API's base path."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "/items/?page=2", "results": [{"id": 1}]}
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
            {"data": [{"id": 1}], "links": "/items/?cursor=b"},
            results_key="data",
            next_key="links",
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


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
