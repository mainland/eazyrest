"""``get_page(url=...)`` refuses a URL that it cannot parse before it sends.

It raises ``PaginationError``, a ``ValueError``, whose message quotes none of
the URL and which has no ``__cause__`` or ``__context__``. A request method
keeps raising the class that requests raises for the same URL.
"""

from __future__ import annotations

import pytest
import requests_mock
from requests.exceptions import InvalidURL

from eazyrest import API, JSONObject, PaginationError, json_object

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


BRACKETS = {name: url for name, url in UNREADABLE.items() if "[" in url}


@pytest.mark.parametrize("url", BRACKETS.values(), ids=BRACKETS)
def test_a_request_method_keeps_the_class_that_requests_raises(
    url: str,
) -> None:
    """Only the page request of a query changes."""
    with pytest.raises(InvalidURL):
        api.get(url)
