"""``get_or_create()`` refuses a generator extra as ``create()`` does.

A single-pass iterable under the name of a field is read once for both the
lookup and the creation. Under a name that is no field it cannot be a JSON
body, so the creation refuses it before any request.
"""

from __future__ import annotations

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://generator-extras.example/"
api = API(BASE)


@json_object
class Item(JSONObject):
    """A model with a field that holds a list."""

    class_url = "/items/"
    id: int
    tags: list[int]


Item.register_api(api)


def numbers() -> object:
    """Return a generator of two numbers."""
    return (i for i in range(2))


@pytest.mark.parametrize(
    "call",
    [
        lambda: Item.create(id=7, x=numbers()),
        lambda: Item.get_or_create(id=7, x=numbers()),
        lambda: Item.get_or_create(id=7, params={"x": numbers()}),
        lambda: Item.get_or_create(id=7, defaults={"x": numbers()}),
        lambda: Item.get_or_create(id=7, defaults={"x__y": numbers()}),
    ],
    ids=["create", "kwargs", "params", "defaults", "defaults with __"],
)
def test_a_generator_under_a_name_that_is_no_field_is_refused(
    call: object,
) -> None:
    """No request is sent."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(TypeError, match="generator"):
            call()  # type: ignore[operator]
        assert mocker.call_count == 0


def test_a_generator_under_the_name_of_a_field_is_read_once() -> None:
    """The lookup and the creation both get its items."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "items/", json=[])
        mocker.post(BASE + "items/", json={"id": 7, "tags": [0, 1]})
        item, created = Item.get_or_create(id=7, tags=numbers())
    assert created
    assert item.tags == [0, 1]
    assert mocker.request_history[1].json() == {"id": 7, "tags": [0, 1]}


def test_a_generator_under_a_lookup_only_name_is_read_once() -> None:
    """A lookup such as ``tags__in`` is not sent, so it is read as before."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "items/", json=[{"id": 7, "tags": [0]}])
        _, created = Item.get_or_create(id=7, tags__in=numbers())
    assert not created
    assert mocker.last_request.url.endswith("?id=7&tags__in=0%2C1")
