"""A ``collection_page()`` that returns no ``Page`` raises one error."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, Page, json_object, link_headers_page

api = API("https://page-result.example/")
RESULT: list[Any] = [None]


@json_object
class Hooked(JSONObject):
    """A model whose page hook gives what ``RESULT`` holds."""

    class_url = "/hooked/"
    id: int

    @classmethod
    def collection_page(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Return the configured result."""
        return RESULT[0]


Hooked.register_api(api)

RESULTS: dict[str, Any] = {
    "a_list": [{"id": 1}],
    "a_dict": {"id": 1},
    "none": None,
}

QUERIES: dict[str, Callable[[], Any]] = {
    "filter": lambda: list(Hooked.filter()),
    "all": lambda: list(Hooked.all()),
    "get": lambda: Hooked.get(),
    "get_page": lambda: Hooked.get_page(),
    "filter_with_a_parameter": lambda: list(Hooked.filter(id=1)),
}


@pytest.mark.parametrize("result", RESULTS.values(), ids=RESULTS.keys())
@pytest.mark.parametrize("query", QUERIES.values(), ids=QUERIES.keys())
def test_a_result_that_is_no_page_raises_type_error_naming_the_hook(
    query: Callable[[], Any], result: Any
) -> None:
    """The error names the hook and what it must return, not a private name."""
    RESULT[0] = result
    try:
        with requests_mock.Mocker() as mocker:
            mocker.get("https://page-result.example/hooked/", json=[])
            with pytest.raises(TypeError) as caught:
                query()
    finally:
        RESULT[0] = None
    message = str(caught.value)
    assert "Hooked.collection_page() must return a Page" in message
    assert "_allow_other_origins" not in message


def test_a_page_that_names_no_next_page_is_read_as_before() -> None:
    """A page of the hook's own making still gives its items."""
    RESULT[0] = Page([{"id": 1}], url="https://page-result.example/hooked/")
    try:
        with requests_mock.Mocker() as mocker:
            mocker.get("https://page-result.example/hooked/", json=[])
            assert [item.id for item in Hooked.filter()] == [1]
    finally:
        RESULT[0] = None


@json_object
class Linked(JSONObject):
    """A model whose hook reads the first page and gives none for the next."""

    class_url = "/linked/"
    id: int

    @classmethod
    def collection_page(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Read the first page, and give a list for any other."""
        if "page=2" in resp.url:
            return [{"id": 2}]
        return link_headers_page(api, resp)


Linked.register_api(api)
LINKED = "https://page-result.example/linked/"


@pytest.mark.parametrize("query", ["filter", "all"])
def test_a_later_page_that_is_no_page_raises_type_error(query: str) -> None:
    """The check applies to each page that the traversal reads."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            LINKED,
            json=[{"id": 1}],
            headers={"Link": f'<{LINKED}?page=2>; rel="next"'},
        )
        mocker.get(f"{LINKED}?page=2", json=[])
        with pytest.raises(
            TypeError, match=r"Linked\.collection_page\(\) must return a Page"
        ):
            list(getattr(Linked, query)())
