"""A write_options() block leaves the object as it found it."""

from __future__ import annotations

import copy
import itertools
import pickle
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object

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


def test_blocks_that_end_in_the_order_they_began_are_refused() -> None:
    """The first block to end raises, and no options stay on the object."""

    def edit(obj: Item, etag: str) -> Any:
        """Hold a block open across a ``yield``."""
        with obj.write_options(headers={"If-Match": etag}):
            yield

    obj = item()
    first = edit(obj, "v1")
    next(first)
    second = edit(obj, "v2")
    next(second)
    with pytest.raises(UsageError, match="block that began later"):
        first.close()
    # The block that is still open keeps its options until it ends.
    assert vars(obj)["_write_options"]["headers"]["If-Match"] == "v2"
    second.close()
    assert "_write_options" not in vars(obj)
    obj.title = "b"
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://options.example/items/1/", json={})
        obj.save()
        assert "If-Match" not in mocker.last_request.headers


def test_blocks_that_end_in_the_reverse_order_are_not_refused() -> None:
    """Nested blocks end in reverse order and restore each level."""
    obj = item()
    with obj.write_options(headers={"X-Outer": "1"}):
        with obj.write_options(headers={"X-Inner": "2"}):
            pass
        assert vars(obj)["_write_options"] == {"headers": {"X-Outer": "1"}}
    assert "_write_options" not in vars(obj)


TITLES = itertools.count()


def edit(obj: Item, headers: dict[str, str]) -> Any:
    """Hold a block of the object open across a ``yield``."""
    with obj.write_options(headers=headers):
        yield


def sent_headers(obj: Item) -> Any:
    """Save a change to the object and return the headers that it sent."""
    obj.title = f"title {next(TITLES)}"
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://options.example/items/1/", json={})
        obj.save()
    return mocker.last_request.headers


def test_an_ended_block_keeps_none_of_its_options_for_the_next_write() -> None:
    """The block that is still open sends only what it and the class set."""
    obj = item()
    first = edit(obj, {"If-Match": "v1"})
    next(first)
    second = edit(obj, {"X-Second": "2"})
    next(second)
    with pytest.raises(UsageError, match="block that began later"):
        first.close()
    headers = sent_headers(obj)
    assert headers["X-Second"] == "2"
    assert "If-Match" not in headers
    second.close()


def test_the_object_takes_the_merge_of_the_blocks_still_open() -> None:
    """Of three nested blocks, the middle one ends first."""
    obj = item()
    blocks = [
        edit(obj, {"X-First": "1", "X-Shared": "first"}),
        edit(obj, {"X-Second": "2"}),
        edit(obj, {"X-Third": "3", "X-Shared": "third"}),
    ]
    for block in blocks:
        next(block)
    with pytest.raises(UsageError, match="block that began later"):
        blocks[1].close()
    headers = sent_headers(obj)
    assert "X-Second" not in headers
    assert (headers["X-First"], headers["X-Third"]) == ("1", "3")
    # The blocks that remain merge in the order that they began.
    assert headers["X-Shared"] == "third"
    with pytest.raises(UsageError, match="block that began later"):
        blocks[0].close()
    headers = sent_headers(obj)
    assert "X-First" not in headers
    assert headers["X-Third"] == "3"
    blocks[2].close()
    assert "_write_options" not in vars(obj)


@pytest.mark.parametrize("make", COPIES.values(), ids=COPIES.keys())
def test_a_copy_made_in_a_block_sends_only_the_options_of_its_own_block(
    make: Callable[[Any], Any],
) -> None:
    """A copy takes none of the original's options, then its own block's."""
    obj = item()
    with obj.write_options(headers=HEADERS):
        duplicate = make(obj)
    with duplicate.write_options(headers={"X-Copy": "1"}):
        headers = sent_headers(duplicate)
    assert headers["X-Copy"] == "1"
    assert "If-Match" not in headers
