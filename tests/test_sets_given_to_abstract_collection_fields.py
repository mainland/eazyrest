"""A set is refused by a field declared as any abstract sequence or collection.

``Sequence``, ``MutableSequence``, ``Collection``, and ``Iterable`` read as
sequences, so a set has no order to write to them, though a type checker
accepts a set for a ``Collection`` or an ``Iterable``. A field declared as a
set takes it.
"""

from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Iterable,
    MutableSequence,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://set-abstract-collections.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class Holder(Base):
    """A model with a field of each abstract collection type."""

    class_url = "/holders/"
    id: int
    sequence: Sequence[int]
    mutable: MutableSequence[int]
    collection: Collection[int]
    iterable: Iterable[int]
    abstract_set: AbstractSet[int]
    mutable_set: MutableSet[int]


JSON = {
    "id": 1,
    "sequence": [1],
    "mutable": [1],
    "collection": [1],
    "iterable": [1],
    "abstract_set": [1],
    "mutable_set": [1],
}


def loaded() -> Holder:
    """Return an object that has read its fields."""
    return Holder(json=JSON, write_mode="lazy")


WRITES: dict[str, Callable[[str, Any], Any]] = {
    "assignment": lambda f, v: setattr(loaded(), f, v),
    "constructor": lambda f, v: Holder(json=JSON, write_mode="lazy", **{f: v}),
    "create": lambda f, v: Holder.create(**{f: v}),
}
REFUSING = ["sequence", "mutable", "collection", "iterable"]
SETS: dict[str, Callable[[], Any]] = {
    "set": lambda: {4, 2, 3, 1},
    "frozenset": lambda: frozenset({4, 2, 3, 1}),
    "a view of a dict's keys": lambda: {2: 1, 1: 2}.keys(),
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
@pytest.mark.parametrize("field", REFUSING)
@pytest.mark.parametrize("make", SETS.values(), ids=SETS)
def test_a_set_has_no_order_to_write_to_a_sequence_or_collection(
    write: Callable[[str, Any], Any], field: str, make: Callable[[], Any]
) -> None:
    """The error names the field and says that the set has no order."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "holders/", json={"id": 2})
        with pytest.raises(FieldConversionError, match=rf"Holder\.{field}"):
            write(field, make())
        assert mocker.call_count == 0


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
@pytest.mark.parametrize("field", ["abstract_set", "mutable_set"])
def test_a_set_field_takes_a_set(
    write: Callable[[str, Any], Any], field: str
) -> None:
    """A field declared as a set writes its items sorted."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "holders/", json={"id": 2})
        write(field, {4, 2, 3, 1})


@pytest.mark.parametrize("field", REFUSING)
def test_a_list_is_taken_by_a_sequence_or_collection(field: str) -> None:
    """A list is the order to write, so it is taken."""
    holder = loaded()
    setattr(holder, field, [4, 2, 3, 1])
