"""The refusal of an item of a set names no position, and a list's does."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://set-items.example/")


@json_object
class Tag(JSONObject):
    """A related model."""

    class_url = "/tags/"
    id: int


@json_object
class Other(JSONObject):
    """A model that is not the related one."""

    class_url = "/others/"
    id: int


@json_object
class Post(JSONObject):
    """A model with a set and a list of relations."""

    class_url = "/posts/"
    id: int
    tagset: set[Tag]
    tagfs: frozenset[Tag]
    taglist: list[Tag]


Tag.register_api(api)
Post.register_api(api)

POSITION = re.compile(r"\[\d+\]")


def post() -> Post:
    """Return a loaded post."""
    return Post(json={"id": 1, "tagset": [], "tagfs": [], "taglist": []})


def bad_items() -> dict[str, Any]:
    """Return an item of a wrong kind, by what is wrong with it."""
    return {"none": None, "another_model": Other(id=1)}


@pytest.mark.parametrize("field", ["tagset", "tagfs"])
@pytest.mark.parametrize("bad", ["none", "another_model"])
def test_an_item_of_a_set_is_refused_without_a_position(
    field: str, bad: str
) -> None:
    """A set has no order, so the message cannot say where the item is."""
    obj = post()
    items = {Tag(id=1), Tag(id=2), Tag(id=3)}
    value: Any = {*items, bad_items()[bad]}
    if field == "tagfs":
        value = frozenset(value)
    with pytest.raises((TypeError, ValueError)) as caught:
        setattr(obj, field, value)
    assert POSITION.search(str(caught.value)) is None
    assert field in str(caught.value) or isinstance(
        caught.value, FieldConversionError
    )
    assert obj._pending_updates == {}


@pytest.mark.parametrize("bad", ["none", "another_model"])
def test_an_item_of_a_list_is_refused_with_its_position(bad: str) -> None:
    """A sequence keeps the position of the item that is refused."""
    obj = post()
    with pytest.raises((TypeError, ValueError), match=r"\[1\]"):
        obj.taglist = [Tag(id=1), bad_items()[bad], Tag(id=3)]


def test_a_bare_key_added_to_a_set_names_no_position() -> None:
    """A change in place that adds a key to a set is refused the same way."""
    obj = post()
    obj.tagset.add(7)  # type: ignore[arg-type]
    call: Callable[[], Any] = obj.save
    with pytest.raises(ValueError, match="bare key") as caught:
        call()
    assert POSITION.search(str(caught.value)) is None
