"""A link that cannot be requested is refused before it counts as a page."""

from __future__ import annotations

import pytest
import requests_mock

from eazyrest import (
    API,
    JSONObject,
    LimitExceeded,
    PaginationError,
    follow_next_links,
    json_object,
)

OTHER = "https://other.example/items/?page=2"


@json_object
class Item(JSONObject):
    """A model read from pages of a collection."""

    class_url = "/items/"
    id: int


def limited() -> API:
    """Return an API that allows one page."""
    api = API("https://api.example/")
    api.max_pages = 1
    Item.register_api(api)
    return api


def test_follow_next_links_refuses_the_origin_not_the_page_count() -> None:
    """The error is about the origin, whatever the limit on pages is."""
    api = limited()
    payload = {"results": [{"id": 1}], "next": OTHER}
    with requests_mock.Mocker() as mocker:
        with pytest.raises(PaginationError) as caught:
            list(follow_next_links(api, payload, url="/items/"))
        assert mocker.call_count == 0
    assert not isinstance(caught.value, LimitExceeded)


def test_filter_refuses_the_origin_not_the_page_count() -> None:
    """The same holds for the pages that a query reads."""
    limited()
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://api.example/items/",
            json={"results": [{"id": 1}], "next": OTHER},
        )
        with pytest.raises(PaginationError) as caught:
            list(Item.filter())
        assert mocker.call_count == 1
    assert not isinstance(caught.value, LimitExceeded)


def test_a_link_on_the_origin_is_stopped_by_the_page_count() -> None:
    """A page that could be read is still counted against the limit."""
    limited()
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://api.example/items/",
            json={
                "results": [{"id": 1}],
                "next": "https://api.example/items/?page=2",
            },
        )
        with pytest.raises(LimitExceeded, match="max_pages"):
            list(Item.filter())
