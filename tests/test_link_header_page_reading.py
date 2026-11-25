"""Both readers of a page linked by ``Link`` headers read it the same way."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    ResponseShapeError,
    follow_link_headers,
    link_headers_page,
)

BASE = "https://link-pages.example/v1/"
URL = BASE + "items/"
api = API(BASE)

READERS: dict[str, Callable[[Any], list[Any]]] = {
    "follow_link_headers": lambda resp: list(follow_link_headers(api, resp)),
    "link_headers_page": lambda resp: list(link_headers_page(api, resp)),
}


def _first(body: Any, link: str | None = None) -> Any:
    """Return the response to a request of the first page."""
    headers = {} if link is None else {"Link": link}
    with requests_mock.Mocker() as mocker:
        mocker.get(URL, text=json.dumps(body), headers=headers)
        return api.get(URL)


@pytest.mark.parametrize("read", READERS.values(), ids=READERS)
@pytest.mark.parametrize("body", [{"a": 1}, "text", 3, None])
def test_a_page_that_is_no_list_is_refused_with_one_message(
    read: Callable[[Any], list[Any]], body: Any
) -> None:
    """The error names the type that the page holds."""
    resp = _first(body)
    with pytest.raises(
        ResponseShapeError,
        match=f"Pages linked by Link headers must be JSON lists, got "
        f"{type(body).__name__}",
    ):
        read(resp)


@pytest.mark.parametrize("read", READERS.values(), ids=READERS)
def test_a_page_without_a_next_link_is_the_last(
    read: Callable[[Any], list[Any]],
) -> None:
    """Its items are all that is read."""
    assert read(_first([1, 2])) == [1, 2]


@pytest.mark.parametrize("read", READERS.values(), ids=READERS)
def test_the_next_link_is_read_by_its_relation(
    read: Callable[[Any], list[Any]],
) -> None:
    """A link of another relation is no next page."""
    resp = _first([1], f'<{URL}?page=0>; rel="prev"')
    assert read(resp) == [1]


def test_both_readers_resolve_the_next_link_against_the_page() -> None:
    """The page follows it, and the iterator reads the page that it names."""
    link = '</v1/items/?page=2>; rel="next"'
    second = URL + "?page=2"
    with requests_mock.Mocker() as mocker:
        mocker.get(URL, json=[1], headers={"Link": link})
        mocker.get(second, json=[2])
        assert list(follow_link_headers(api, api.get(URL))) == [1, 2]
        assert link_headers_page(api, api.get(URL)).next_url == second
