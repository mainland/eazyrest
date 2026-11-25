"""``get_page(url=...)`` refuses a URL that it cannot parse before it sends.

It raises ``PaginationError``, a ``ValueError``, whose message quotes none of
the URL and which has no ``__cause__`` or ``__context__``. A request method
keeps raising the class that requests raises for the same URL.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import requests_mock
from requests.exceptions import InvalidURL, MissingSchema

from eazyrest import (
    API,
    JSONObject,
    PaginationError,
    follow_next_links,
    json_object,
)

BASE = "https://page-urls.example/v1/"
api = API(BASE)


@json_object
class Item(JSONObject):
    """A model whose pages the tests read."""

    class_url = "/items/"
    id: int


Item.register_api(api)

UNREADABLE = {
    "a port out of range": "https://page-urls.example:99999/v1/items/",
    "no host": "https://",
    "an invalid bracketed host": "http://[bad/",
    "an unclosed IPv6 host": "https://[::1/",
    "a bracket after a backslash": "//\\[",
}


@pytest.mark.parametrize("url", UNREADABLE.values(), ids=UNREADABLE)
def test_a_url_that_cannot_be_parsed_raises_pagination_error(url: str) -> None:
    """Nothing is sent, and the message holds none of the URL."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(
            PaginationError, match="cannot be parsed"
        ) as caught:
            Item.get_page(url=url)
        assert mocker.call_count == 0

    assert isinstance(caught.value, ValueError)
    assert "bad" not in str(caught.value)
    assert "99999" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# The URLs that cannot be resolved against the base URL, which is where an
# entry point first reads one.
BRACKETS = {name: url for name, url in UNREADABLE.items() if "[" in url}


def _get_page(url: str) -> object:
    """Read a page of a query by its URL."""
    return Item.get_page(url=url)


def _get_page_through_a_view(url: str) -> object:
    """Read a page by its URL with the options of a view."""
    return Item.get_page(url=url, api=api._scoped({"timeout": 3}))


def _follow_next_links(url: str) -> object:
    """Read the pages that follow a first page that has this URL."""
    return list(follow_next_links(api, {"results": []}, url=url))


ENTRY_POINTS = {
    "get_page": _get_page,
    "get_page through a view": _get_page_through_a_view,
    "follow_next_links": _follow_next_links,
}


@pytest.mark.parametrize("entry", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
@pytest.mark.parametrize("url", BRACKETS.values(), ids=BRACKETS)
def test_every_entry_point_names_the_same_error_for_a_url_it_cannot_parse(
    entry: Callable[[str], object], url: str
) -> None:
    """The class does not depend on which error requests raises for it."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(
            PaginationError, match="cannot be parsed"
        ) as caught:
            entry(url)
        assert mocker.call_count == 0

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


REQUEST_ERRORS = {
    "an invalid bracketed host": InvalidURL,
    "an unclosed IPv6 host": InvalidURL,
    "a bracket after a backslash": MissingSchema,
}


@pytest.mark.parametrize("name", REQUEST_ERRORS)
def test_a_request_method_keeps_the_class_that_requests_raises(
    name: str,
) -> None:
    """Only the page request of a query changes."""
    with pytest.raises(REQUEST_ERRORS[name]):
        api.get(UNREADABLE[name])
