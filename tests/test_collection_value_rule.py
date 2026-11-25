"""One rule says which value is a collection of items, at every entry point.

A collection of items is iterable and is not a string, bytes, or a mapping of
any kind. A read, an assignment, a query, and a prefetch agree on which value
that is.
"""

from __future__ import annotations

from collections import UserDict
from collections.abc import Callable, Iterable
from types import MappingProxyType
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

FETCHED: list[Any] = []
BASE = "https://collection-rule.example/"
api = API(BASE)


@json_object
class Owner(JSONObject):
    """A related model."""

    class_url = "/owners/"
    id: int

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Record the keys that were asked for, and give each an object."""
        FETCHED.extend(pks)
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


@json_object
class Thing(JSONObject):
    """A model with a collection of values and one of related objects."""

    class_url = "/things/"
    id: int
    tags: list[str]
    flags: set[str]
    owners: list[Owner]


Owner.register_api(api)
Thing.register_api(api)

START = {"id": 1, "tags": [], "flags": [], "owners": []}


def read(name: str) -> Callable[[Any], Any]:
    """Return a function that reads a value for a field from JSON."""

    def run(value: Any) -> Any:
        return getattr(Thing(json={**START, name: value}), name)

    run.__name__ = f"read_{name}"
    return run


def assign(name: str) -> Callable[[Any], Any]:
    """Return a function that assigns a value to a field."""

    def run(value: Any) -> Any:
        obj = Thing(json=dict(START), write_mode="lazy")
        setattr(obj, name, value)
        return getattr(obj, name)

    run.__name__ = f"assign_{name}"
    return run


def query(name: str) -> Callable[[Any], Any]:
    """Return a function that encodes a value as a query operand."""

    def run(value: Any) -> Any:
        return Thing._encode_query({name: value}, api)

    run.__name__ = f"query_{name}"
    return run


ORDERED: list[Any] = [["a", "b"], ("a", "b")]
UNORDERED: list[Any] = [["a", "b"], ("a", "b"), {"a", "b"}, frozenset("a")]
NOT_COLLECTIONS: list[Any] = ["ab", b"ab", {"a": 1}, UserDict(a=1), 5]
MAPPINGS: list[Any] = [UserDict(a=1), MappingProxyType({"a": 1})]

ACCEPTING = [
    (entry, value)
    for entry, values in [
        (read("tags"), ORDERED),
        (assign("tags"), ORDERED),
        (query("tags"), ORDERED),
        (read("flags"), UNORDERED),
        (query("flags"), UNORDERED),
        (read("owners"), [[], ()]),
        (assign("owners"), [[], ()]),
    ]
    for value in values
]
REFUSING = [
    (entry, value)
    for entry in [
        read("tags"),
        assign("tags"),
        read("flags"),
        read("owners"),
        assign("owners"),
    ]
    for value in NOT_COLLECTIONS
] + [(query(name), value) for name in ("tags", "flags") for value in MAPPINGS]


def _id(item: Any) -> str:
    return getattr(item, "__name__", type(item).__name__)


@pytest.mark.parametrize(("entry", "value"), ACCEPTING, ids=_id)
def test_a_collection_of_items_is_taken_at_every_entry_point(
    entry: Callable[[Any], Any], value: Iterable[Any]
) -> None:
    """Every entry point takes a collection of items."""
    entry(value)


@pytest.mark.parametrize(("entry", "value"), REFUSING, ids=_id)
def test_a_value_that_is_no_collection_is_refused_at_every_entry_point(
    entry: Callable[[Any], Any], value: Any
) -> None:
    """A string, bytes, any mapping, and a number are no collection."""
    with pytest.raises(FieldConversionError):
        entry(value)


@pytest.mark.parametrize("value", ["ab", b"ab".decode(), {"7": "x"}], ids=_id)
def test_a_prefetch_reads_no_keys_from_a_value_that_is_no_collection(
    value: Any,
) -> None:
    """A string or object where a collection belongs fetches no keys."""
    FETCHED.clear()
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "things/", json=[{**START, "owners": value}])
        list(Thing.filter(prefetch=["owners"]))
        assert FETCHED == []


def test_a_collection_keyed_by_related_objects_is_no_related_collection() -> (
    None
):
    """A mapping whose keys are related objects is not a collection of them."""
    obj = Thing(json=dict(START), write_mode="lazy")
    with pytest.raises(FieldConversionError):
        obj.owners = {Owner(json={"id": 1}): 1}  # type: ignore[assignment]
