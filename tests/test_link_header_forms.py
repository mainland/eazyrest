"""A Link header is read as RFC 8288 section 3 writes it."""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    JSONObject,
    follow_link_headers,
    json_object,
    link_headers_page,
    next_links_page,
)

api = API("https://link.example/")


@json_object
class Paged(JSONObject):
    """A model whose page hook reads the Link header."""

    class_url = "/paged/"
    id: int

    @classmethod
    def collection_page(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Read the page and its Link header."""
        return link_headers_page(api, resp)


@json_object
class Followed(JSONObject):
    """A model whose items hook follows the Link header."""

    class_url = "/followed/"
    id: int

    @classmethod
    def collection_items_from_response(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Follow the Link header from the first page."""
        return follow_link_headers(api, resp)


Paged.register_api(api)
Followed.register_api(api)

BASE = "https://link.example"

NEXT = f"{BASE}/paged/?page=2"

FORMS: dict[str, tuple[str, str]] = {
    "plain": ('<{u}?page=2>; rel="next"', "?page=2"),
    "a_token": ("<{u}?page=2>; rel=next", "?page=2"),
    "a_semicolon_in_the_path": ('<{u};v=1?page=2>; rel="next"', ";v=1?page=2"),
    "a_semicolon_in_the_query": (
        '<{u}?sort=a;b&page=2>; rel="next"',
        "?sort=a;b&page=2",
    ),
    "a_comma_in_the_target": ('<{u},x?page=2>; rel="next"', ",x?page=2"),
    "an_equals_sign_before_rel": (
        '<{u}?page=2>; title="page=2"; rel="next"',
        "?page=2",
    ),
    "a_semicolon_in_a_value_before_rel": (
        '<{u}?page=2>; title="a; b"; rel="next"',
        "?page=2",
    ),
    "a_comma_in_a_value_before_rel": (
        '<{u}?page=2>; title="a, <b>"; rel="next"',
        "?page=2",
    ),
    "a_parameter_without_a_value": (
        '<{u}?page=2>; crossorigin; rel="next"',
        "?page=2",
    ),
    "capital_names": ('<{u}?page=2>; REL="Next"', "?page=2"),
    "several_types": ('<{u}?page=2>; rel="Prev NEXT"', "?page=2"),
    "no_space": ('<{u}?page=2>;rel="next"', "?page=2"),
    "a_second_rel": ('<{u}?page=2>; rel="next"; rel="last"', "?page=2"),
    "after_another_link": (
        '<{u}?page=9>; rel="last", <{u}?page=2>; rel="next"',
        "?page=2",
    ),
}


def run(model: type[Any], collection: str, call: str, form: str) -> list[int]:
    """Read two pages, with the first page's Link header in a given form."""
    url = f"{BASE}/{collection}/"
    header, tail = FORMS[form]
    with requests_mock.Mocker() as mocker:
        # The mocker tries the latest registration first.
        mocker.get(
            url, json=[{"id": 1}], headers={"Link": header.format(u=url)}
        )
        mocker.get(url + tail, json=[{"id": 2}])
        query = model.filter if call == "filter" else model.all
        return [item.id for item in query()]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("call", ["filter", "all"])
@pytest.mark.parametrize(
    ("model", "collection"), [(Paged, "paged"), (Followed, "followed")]
)
def test_the_next_page_is_read_in_every_form(
    model: type[Any], collection: str, call: str, form: str
) -> None:
    """The traversal reaches the second page and reads its item."""
    assert run(model, collection, call, form) == [1, 2]


def test_a_link_without_a_rel_ends_the_traversal() -> None:
    """A header whose links name no next page is the last page."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            f"{BASE}/paged/",
            json=[{"id": 1}],
            headers={"Link": f'<{BASE}/paged/?page=2>; title="next"'},
        )
        assert [item.id for item in Paged.filter()] == [1]


@pytest.mark.parametrize(
    "header",
    [
        '<{u}?page=2>; rel="next", <{u}?page=3>; rel="next"',
        '<{u}?page=2>; rel="next", <{u}?page=3>; rel="Next"',
        '<{u}?page=2>; rel="next", <next>',
    ],
    ids=["same_rel_twice", "same_rel_in_another_case", "target_named_next"],
)
def test_the_first_next_link_in_the_header_is_followed(header: str) -> None:
    """A later link neither replaces the next link nor removes it."""
    url = f"{BASE}/paged/"
    with requests_mock.Mocker() as mocker:
        mocker.get(
            url, json=[{"id": 1}], headers={"Link": header.format(u=url)}
        )
        mocker.get(url + "?page=2", json=[{"id": 2}])
        mocker.get(url + "?page=3", json=[{"id": 3}])
        assert [item.id for item in Paged.filter()] == [1, 2]


@json_object
class Roaming(JSONObject):
    """A model whose page hook lets its pages link to another origin."""

    class_url = "/roaming/"
    id: int

    @classmethod
    def collection_page(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Read the page, which may link to another origin."""
        return next_links_page(api, resp, allow_other_origins=True)


Roaming.register_api(api)


def test_a_page_of_get_page_keeps_the_origins_its_hook_allows() -> None:
    """``next_page()`` follows a link to another origin if the hook allows."""
    elsewhere = "https://elsewhere.example/roaming/?page=2"
    with requests_mock.Mocker() as mocker:
        mocker.get(
            f"{BASE}/roaming/",
            json={"results": [{"id": 1}], "next": elsewhere},
        )
        mocker.get(elsewhere, json={"results": [{"id": 2}], "next": None})
        page = Roaming.get_page()
        following = page.next_page()
    assert following is not None
    assert [item.id for item in following] == [2]
