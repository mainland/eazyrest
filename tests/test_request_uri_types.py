"""A request URI is a ``str`` or ``bytes``, and ``bytes`` are decoded.

Anything else raises ``TypeError`` that names the argument, before a request is
sent. A relative ``bytes`` URI is decoded as UTF-8 and resolved against the
base URL, as an absolute one is decoded and sent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from requests.exceptions import InvalidURL
from requests_mock import ANY

from eazyrest import (
    API,
    JSONObject,
    PaginationError,
    follow_next_links,
    json_object,
)

BASE = "https://uri-types.example/v1/"
api = API(BASE)
view = api._scoped({"timeout": 3})


@json_object
class Item(JSONObject):
    """A model whose pages the tests read."""

    class_url = "/items/"
    id: int


Item.register_api(api)

Entry = Callable[[Any], Any]

REQUEST_METHODS = ["get", "post", "put", "patch", "delete"]

REQUESTS: dict[str, Entry] = {
    **{f"API.{name}": getattr(api, name) for name in REQUEST_METHODS},
    **{f"view.{name}": getattr(view, name) for name in REQUEST_METHODS},
}
RESOLVERS: dict[str, Entry] = {
    "API.resolve_url": api.resolve_url,
    "view.resolve_url": view.resolve_url,
}
ENTRIES = {**REQUESTS, **RESOLVERS}

NOT_URIS = {"an int": 5, "None": None, "a float": 1.5, "a list": ["a"]}


@pytest.mark.parametrize("entry", ENTRIES.values(), ids=ENTRIES)
@pytest.mark.parametrize("value", NOT_URIS.values(), ids=NOT_URIS)
def test_a_uri_that_is_neither_str_nor_bytes_raises_a_type_error(
    requests_mock: Any, entry: Entry, value: object
) -> None:
    """The message names the argument and quotes no value."""
    with pytest.raises(TypeError, match=r"^uri must be a str or bytes, got "):
        entry(value)
    assert not requests_mock.called


RELATIVE_AND_ABSOLUTE = {
    "a relative str": ("items/", BASE + "items/"),
    "a relative bytes": (b"items/", BASE + "items/"),
    "a relative bytes with a leading slash": (b"/items/", BASE + "items/"),
    "a relative bytes that is not ASCII": (
        "stra\u00dfe/".encode(),
        BASE + "stra%C3%9Fe/",
    ),
    "an absolute str": ("https://other.example/x", "https://other.example/x"),
    "an absolute bytes": (
        b"https://other.example/x",
        "https://other.example/x",
    ),
}


@pytest.mark.parametrize("entry", REQUESTS.values(), ids=REQUESTS)
@pytest.mark.parametrize(
    ("uri", "sent"), RELATIVE_AND_ABSOLUTE.values(), ids=RELATIVE_AND_ABSOLUTE
)
def test_a_bytes_uri_is_decoded_whether_it_is_relative_or_absolute(
    requests_mock: Any, entry: Entry, uri: str | bytes, sent: str
) -> None:
    """A relative ``bytes`` URI no longer raises ``TypeError``."""
    requests_mock.register_uri(ANY, ANY, json={})

    entry(uri)

    assert requests_mock.last_request.url == sent


@pytest.mark.parametrize("entry", RESOLVERS.values(), ids=RESOLVERS)
@pytest.mark.parametrize(
    ("uri", "resolved"),
    [
        (b"items/", BASE + "items/"),
        (b"https://other.example/x", "https://other.example/x"),
    ],
    ids=["relative", "absolute"],
)
def test_resolve_url_returns_text_for_bytes(
    entry: Entry, uri: bytes, resolved: str
) -> None:
    """The result is a ``str`` whichever type the URI had."""
    assert entry(uri) == resolved


@pytest.mark.parametrize("entry", ENTRIES.values(), ids=ENTRIES)
def test_a_bytes_uri_that_is_not_utf8_raises_invalid_url(
    requests_mock: Any, entry: Entry
) -> None:
    """The error quotes none of the URI and has no cause."""
    with pytest.raises(InvalidURL) as caught:
        entry(b"items/\xff")
    assert "xff" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not requests_mock.called


def _get_page(url: object) -> object:
    """Read a page by its URL."""
    return Item.get_page(url=url)  # type: ignore[arg-type]


def _get_page_through_a_view(url: object) -> object:
    """Read a page by its URL with the options of a view."""
    return Item.get_page(url=url, api=view)  # type: ignore[arg-type]


def _follow_next_links(url: object) -> object:
    """Follow the links of a first page that has this URL."""
    return list(
        follow_next_links(api, {"results": []}, url=url)  # type: ignore[arg-type]
    )


PAGE_ENTRIES: dict[str, Entry] = {
    "get_page": _get_page,
    "get_page through a view": _get_page_through_a_view,
    "follow_next_links": _follow_next_links,
}


@pytest.mark.parametrize("entry", PAGE_ENTRIES.values(), ids=PAGE_ENTRIES)
@pytest.mark.parametrize(
    "value", [5, 1.5, ["a"], object()], ids=["int", "float", "list", "object"]
)
def test_a_page_url_that_is_neither_str_nor_bytes_raises_a_type_error(
    requests_mock: Any, entry: Entry, value: object
) -> None:
    """The message names ``url``, as the argument is called."""
    with pytest.raises(TypeError, match=r"^url must be a str or bytes, got "):
        entry(value)
    assert not requests_mock.called


@pytest.mark.parametrize(
    ("url", "sent"),
    [
        (b"?page=2", BASE + "?page=2"),
        (b"https://uri-types.example/v1/items/?page=2", None),
    ],
    ids=["relative", "absolute"],
)
def test_get_page_decodes_a_bytes_url(
    requests_mock: Any, url: bytes, sent: str | None
) -> None:
    """A page is read at a ``bytes`` URL, relative or absolute."""
    requests_mock.register_uri(ANY, ANY, json=[])

    Item.get_page(url=url)  # type: ignore[arg-type]

    assert requests_mock.last_request.url == (
        sent or url.decode()  # absolute: sent as it stands
    )


def test_get_page_without_a_url_reads_the_collection(
    requests_mock: Any,
) -> None:
    """``None`` is no URL, so the collection's own URL is read."""
    requests_mock.get(BASE + "items/", json=[])

    Item.get_page(url=None)

    assert requests_mock.last_request.url == BASE + "items/"


def test_a_bytes_url_that_is_not_utf8_raises_pagination_error() -> None:
    """The URL cannot be parsed, as any other that is not."""
    with pytest.raises(PaginationError, match="cannot be parsed"):
        Item.get_page(url=b"?page=\xff")  # type: ignore[arg-type]
