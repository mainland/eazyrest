"""A set field's keys are distinct, as stored and as queried."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://sets.example/")


@json_object(pk="slug")
class Tag(JSONObject):
    """A model with a text key."""

    class_url = "/tags/"
    slug: str


@json_object
class Item(JSONObject):
    """A model with sets of related objects and of plain values."""

    class_url = "/items/"
    id: int
    tags: set[Tag]
    frozen: frozenset[Tag]
    numbers: set[int]


Tag.register_api(api)
Item.register_api(api)


def mocked_query(**params: Any) -> str:
    """Run a filter and return the decoded query that it sent."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Item.filter(**params))
    return unquote(urlsplit(mocker.request_history[0].url).query)


@pytest.mark.parametrize("field", ["tags", "frozen", "numbers"])
def test_a_repeated_stored_key_is_not_a_change(field: str) -> None:
    """Assigning a set field its own value sends nothing."""
    stored = [1, 1] if field == "numbers" else ["a", "a"]
    item = Item(json={"id": 1, field: stored})
    setattr(item, field, getattr(item, field))
    with requests_mock.Mocker() as mocker:
        item.save()
    assert mocker.call_count == 0


def test_another_set_is_still_a_change() -> None:
    """A set that differs from the distinct stored keys is sent."""
    item = Item(json={"id": 1, "tags": ["a", "a"]})
    item.tags = {Tag(slug="b")}
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://sets.example/items/1/", json={})
        item.save()
    assert mocker.request_history[0].json() == {"tags": ["b"]}


def test_a_set_field_is_queried_by_its_distinct_keys_sorted() -> None:
    """A repeated key, or two objects of one key, is sent once."""
    assert (
        mocked_query(tags=[Tag(slug="b"), Tag(slug="a"), Tag(slug="b")])
        == "tags=a&tags=b"
    )
    assert mocked_query(numbers=[3, 1, 3, 2]) == (
        "numbers=1&numbers=2&numbers=3"
    )
