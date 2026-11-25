"""JSON without a key, or with ``null`` for it, names no object.

The JSON of an object, an embedded related object, an item of a collection of
relations, and an item of a query refuse the same JSON, with a message that
names the fault the same way, and take the same JSON that holds a key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    ResponseShapeError,
    json_object,
)

BASE = "https://json-without-a-key.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class Owner(Base):
    """A related model with a key."""

    class_url = "/owners/"
    id: int


@json_object
class Thing(Base):
    """A model with a relation and a collection of relations."""

    class_url = "/things/"
    id: int
    owner: Owner
    owners: list[Owner]


def _object(case: Any) -> Any:
    return Owner(json=case)


def _embedded(case: Any) -> Any:
    return Thing(json={"id": 1, "owner": case}).owner


def _item(case: Any) -> Any:
    return Thing(json={"id": 1, "owners": [case]}).owners[0]


def _query_item(case: Any) -> Any:
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "owners/", json=[case])
        return next(iter(Owner.filter()))


def _assigned_over(case: Any) -> Any:
    """Assign the owner that embedded JSON without a key already names."""
    thing = Thing(json={"id": 1, "owner": case})
    thing.owner = Owner(id=7)
    return thing


ENTRY_POINTS: dict[str, Callable[[Any], Any]] = {
    "Owner(json=...)": _object,
    "an embedded object": _embedded,
    "an item of a collection": _item,
    "an item of a query": _query_item,
}

WITHOUT_A_KEY: list[tuple[Any, str]] = [
    ({"id": None}, "holds null for its key, so it names no Owner"),
    ({}, "holds no key, so it names no Owner"),
    ({"name": "x"}, "holds no key, so it names no Owner"),
]


@pytest.mark.parametrize(
    ("case", "words"), WITHOUT_A_KEY, ids=["null", "empty", "other"]
)
@pytest.mark.parametrize(
    "read", ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
def test_json_without_a_key_is_refused_with_the_same_words(
    read: Callable[[Any], Any], case: Any, words: str
) -> None:
    """Every entry point says that the JSON holds no key, or ``null``."""
    with pytest.raises((FieldConversionError, ResponseShapeError)) as caught:
        read(case)
    assert words in str(caught.value)


@pytest.mark.parametrize("case", [{"id": 0}, {"id": 7}])
@pytest.mark.parametrize(
    "read", ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
def test_json_with_a_key_is_taken_at_every_entry_point(
    read: Callable[[Any], Any], case: Any
) -> None:
    """A key of zero is a key."""
    assert read(case).pk == case["id"]


@pytest.mark.parametrize("case", [{"id": None}, {}])
def test_an_embedded_object_without_a_key_is_replaced_by_an_assignment(
    case: Any,
) -> None:
    """An object that names no key is not the object that is assigned."""
    thing = _assigned_over(case)
    assert thing.owner.pk == 7
