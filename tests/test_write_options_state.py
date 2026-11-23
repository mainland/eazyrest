"""A write_options() block leaves the object as it found it."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://options.example/")
HEADERS = {"If-Match": "etag"}


@json_object
class Person(JSONObject):
    """A related model."""

    class_url = "/people/"
    id: int
    name: str


@json_object
class Item(JSONObject):
    """A model that relates to a person."""

    class_url = "/items/"
    id: int
    title: str
    owner: Person


Person.register_api(api)
Item.register_api(api)

COPIES: dict[str, Callable[[Any], Any]] = {
    "copy": copy.copy,
    "deepcopy": copy.deepcopy,
    "pickle": lambda obj: pickle.loads(pickle.dumps(obj)),
    "pickle0": lambda obj: pickle.loads(pickle.dumps(obj, protocol=0)),
}


def item() -> Item:
    """Return an item with its owner embedded."""
    return Item(json={"id": 1, "title": "t", "owner": {"id": 7, "name": "a"}})


def test_a_block_leaves_no_write_options_on_the_object() -> None:
    """An object that had none has none again."""
    obj = item()
    with obj.write_options(headers=HEADERS):
        with obj.write_options(params={"x": "1"}):
            assert vars(obj)["_write_options"]
        assert vars(obj)["_write_options"] == {"headers": HEADERS}
    assert "_write_options" not in vars(obj)


def test_a_block_that_raises_leaves_no_write_options_on_the_object() -> None:
    """The options go also when the block fails."""
    obj = item()
    with pytest.raises(RuntimeError), obj.write_options(headers=HEADERS):
        raise RuntimeError
    assert "_write_options" not in vars(obj)


@pytest.mark.parametrize("make", COPIES.values(), ids=COPIES.keys())
def test_an_object_that_used_a_block_can_be_copied_and_pickled(
    make: Callable[[Any], Any],
) -> None:
    """The object and an owner that caches it copy as one that never did."""
    obj = item()
    with obj.write_options(headers=HEADERS):
        pass
    with obj.owner.write_options(headers=HEADERS):
        pass
    duplicate = make(obj)
    assert duplicate == obj
    assert duplicate.owner == obj.owner
    assert duplicate.owner is not obj.owner
    assert "_write_options" not in vars(duplicate)
    assert "_write_options" not in vars(duplicate.owner)
    assert make(obj.owner) == obj.owner


@pytest.mark.parametrize("make", COPIES.values(), ids=COPIES.keys())
def test_a_copy_made_in_a_block_sends_none_of_its_options(
    make: Callable[[Any], Any],
) -> None:
    """The options belong to the block, not to a copy of the object."""
    obj = item()
    with obj.write_options(headers=HEADERS):
        duplicate = make(obj)
    duplicate.title = "new"
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://options.example/items/1/", json={})
        duplicate.save()
    assert "If-Match" not in mocker.request_history[0].headers
