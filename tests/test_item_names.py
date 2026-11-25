"""An item whose names are no strings is refused where it is read.

A hook that returns items, a page, and a collection hook all give JSON objects
that the library builds objects from. One rule says that the names of such an
object are strings, checked before an object is built, so that no later write
sends a request and then fails on the same item.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, Page, ResponseShapeError, json_object

BASE = "https://item-names.example/"
api = API(BASE)

ITEMS: list[Any] = []
"""The items that the hooks of the models below return."""


class _Base(JSONObject):
    """A model whose three item hooks give the chosen items."""


@json_object
class ByItems(_Base):
    """A model whose collection hook gives the chosen items."""

    class_url = "/by-items/"
    id: int
    name: str

    @classmethod
    def collection_items(  # type: ignore[no-untyped-def]
        cls, payload, *, api, **kwargs
    ):
        """Return the chosen items."""
        return list(ITEMS)


@json_object
class ByResponse(_Base):
    """A model whose response hook gives the chosen items."""

    class_url = "/by-response/"
    id: int
    name: str

    @classmethod
    def collection_items_from_response(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Return the chosen items."""
        return list(ITEMS)


@json_object
class Plain(_Base):
    """A model that reads its items from the response."""

    class_url = "/plain/"
    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give no object for any key."""
        return {}


@json_object
class ByPage(_Base):
    """A model whose page hook gives a page of the chosen items."""

    class_url = "/by-page/"
    id: int
    name: str

    @classmethod
    def collection_page(  # type: ignore[no-untyped-def]
        cls, resp, *, api, **kwargs
    ):
        """Return a page of the chosen items."""
        return Page(list(ITEMS), url=resp.url)


@json_object
class Owned(_Base):
    """A model with a relation, which a prefetch reads ahead."""

    class_url = "/owned/"
    id: int
    owner: Plain

    @classmethod
    def collection_items(  # type: ignore[no-untyped-def]
        cls, payload, *, api, **kwargs
    ):
        """Return the chosen items."""
        return list(ITEMS)


for model in (ByItems, ByResponse, ByPage, Owned, Plain):
    model.register_api(api)

BAD_ITEMS: dict[str, dict[Any, Any]] = {
    "an integer name": {"id": 1, "name": "a", 5: "five"},
    "a None name": {"id": 1, "name": "a", None: "none"},
}

ENTRY_POINTS: dict[str, Callable[[], Any]] = {
    "collection_items, filter": lambda: list(ByItems.filter()),
    "collection_items, all": lambda: list(ByItems.all()),
    "collection_items, get": lambda: ByItems.get(id=1),
    "collection_items_from_response, filter": lambda: list(
        ByResponse.filter()
    ),
    "collection_items, prefetch": lambda: list(
        Owned.filter(prefetch=["owner"])
    ),
    "collection_page, filter": lambda: list(ByPage.filter()),
    "collection_page, page": lambda: ByPage.get_page(url=BASE + "by-page/"),
}


@pytest.fixture(autouse=True)
def served() -> Any:
    """Serve every resource, and reset the chosen items afterwards."""
    with requests_mock.Mocker() as mocker:
        for path in ("by-items", "by-response", "by-page", "owned"):
            mocker.get(BASE + path + "/", json=[{"id": 1, "name": "a"}])
            mocker.patch(BASE + path + "/1/", json={})
        yield mocker
    ITEMS.clear()


@pytest.mark.parametrize("bad", sorted(BAD_ITEMS))
@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_an_item_with_a_name_that_is_no_string_is_refused_where_it_is_read(
    entry: str, bad: str, served: requests_mock.Mocker
) -> None:
    """No object is built, and no request but the reads is sent."""
    ITEMS[:] = [BAD_ITEMS[bad]]
    with pytest.raises(
        ResponseShapeError, match=r"Item \[0\] .* names are all strings"
    ) as caught:
        ENTRY_POINTS[entry]()
    assert "five" not in str(caught.value)
    assert [call.method for call in served.request_history] == ["GET"]


def test_an_item_after_a_good_one_is_refused_at_its_own_position() -> None:
    """The message names the position of the item that is refused."""
    ITEMS[:] = [{"id": 1, "name": "a"}, BAD_ITEMS["an integer name"]]
    results = ByItems.filter()
    assert next(results).name == "a"
    with pytest.raises(ResponseShapeError, match=r"Item \[1\]"):
        next(results)


def test_a_response_item_with_string_names_is_taken() -> None:
    """The check refuses only a name that is no string."""
    ITEMS[:] = [{"id": 1, "name": "a", "extra": 2}]
    assert [obj.name for obj in ByItems.filter()] == ["a"]
