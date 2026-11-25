"""A first page given without its URL has no URL that a link can repeat."""

from __future__ import annotations

import pytest
import requests_mock

from eazyrest import API, PaginationError, follow_next_links

BASE = "https://first-page.example/v1/"


def test_a_link_to_the_base_url_is_read_when_the_first_page_has_no_url() -> (
    None
):
    """The base URL is not a page, so the link is no repeat of one."""
    api = API(BASE)
    first = {"results": [{"id": 1}], "next": BASE}
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE, json={"results": [{"id": 2}], "next": None})

        items = list(follow_next_links(api, first))

    assert items == [{"id": 1}, {"id": 2}]


def test_a_link_to_the_first_pages_url_is_a_repeat_when_it_is_given() -> None:
    """With ``url=``, the same link names a page already read."""
    api = API(BASE)
    first = {"results": [{"id": 1}], "next": BASE}
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE, json={"results": [{"id": 2}], "next": None})

        with pytest.raises(PaginationError):
            list(follow_next_links(api, first, url=BASE))
