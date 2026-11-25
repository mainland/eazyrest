"""One rule gives the order and distinctness of a set's items.

A set's items are written sorted by their JSON, each JSON value once. A write,
the comparison of a write with the stored items, and a query on a set field
agree on which items that is.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://set-items.example/"
api = API(BASE)


@json_object
class Bag(JSONObject):
    """A model with a set of any JSON values."""

    class_url = "/bags/"
    id: int
    items: set[Any]


Bag.register_api(api)

# Items given, and the JSON that a set field writes for them.
WRITTEN: list[tuple[list[Any], list[Any]]] = [
    ([], []),
    ([3, 1, 2, 1], [1, 2, 3]),
    (["b", "a", "b", "A"], ["A", "a", "b"]),
    ([True, 1, 1.0, "1", None, False, 0], [None, False, True, 0, 1, "1"]),
    ([[2], [1], [1]], [[1], [2]]),
    ([{"b": 1}, {"a": 2}, {"a": 2}], [{"a": 2}, {"b": 1}]),
    ([2.5, 2, "2", [2], {"k": 2}, None], [None, 2, 2.5, "2", [2], {"k": 2}]),
    ([1.0, 1], [1.0]),
]

# Items given, and the text that a query sends for each.
QUERIED: list[tuple[list[Any], list[str]]] = [
    ([3, 1, 2, 1], ["1", "2", "3"]),
    (["b", "a", "b", "A"], ["A", "a", "b"]),
    ([2, 1.5, 1, 1], ["1", "1.5", "2"]),
]


@pytest.mark.parametrize(("items", "expected"), WRITTEN, ids=repr)
def test_a_write_sends_the_items_sorted_and_distinct(
    items: list[Any], expected: list[Any]
) -> None:
    """A set field writes its items sorted by JSON, each JSON value once."""
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "bags/1/", json={"id": 1, "items": expected})
        obj = Bag(json={"id": 1, "items": [0.5]}, write_mode="lazy")
        obj.items = items  # type: ignore[assignment]
        obj.save()
        assert mocker.request_history[-1].json() == {"items": expected}


@pytest.mark.parametrize(("items", "expected"), WRITTEN, ids=repr)
def test_a_write_is_no_change_if_the_stored_items_are_the_same_set(
    items: list[Any], expected: list[Any]
) -> None:
    """Stored items in another order, or repeated, are the same items."""
    with requests_mock.Mocker() as mocker:
        obj = Bag(
            json={"id": 1, "items": [*reversed(expected), *expected]},
            write_mode="lazy",
        )
        obj.items = items  # type: ignore[assignment]
        obj.save()
        assert mocker.request_history == []


@pytest.mark.parametrize(("items", "expected"), WRITTEN, ids=repr)
def test_a_write_is_a_change_if_an_item_differs(
    items: list[Any], expected: list[Any]
) -> None:
    """Items that differ from the stored ones in one value are a change."""
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "bags/1/", json={"id": 1, "items": expected})
        obj = Bag(
            json={"id": 1, "items": [*expected, "other"]}, write_mode="lazy"
        )
        obj.items = items  # type: ignore[assignment]
        obj.save()
        assert mocker.request_history[-1].json() == {"items": expected}


@pytest.mark.parametrize(("items", "texts"), QUERIED, ids=repr)
def test_a_query_sends_the_items_sorted_and_distinct(
    items: list[Any], texts: list[str]
) -> None:
    """A query on a set field repeats the parameter in written order."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "bags/", json=[])
        list(Bag.filter(items=items))
        query = parse_qsl(urlsplit(mocker.request_history[-1].url).query)
        assert query == [("items", text) for text in texts]
