"""A set is refused wherever it would be written as a sequence of any items.

A set has no order. The order in which it iterates strings depends on the
process and on how the set was built, so a sequence written from a set would
differ from one run to the next, and a save could send a change for an equal
set. A set field writes its items sorted, a query sends a set sorted, and a
list or a tuple keeps its order.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://set-plain-sequences.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class Labeled(Base):
    """A model with sequences of plain values, a fixed tuple, and a set."""

    class_url = "/labeled/"
    id: int
    labels: list[str]
    codes: tuple[int, ...]
    queue: deque[str]
    pair: tuple[int, int]
    grid: list[list[str]]
    labelset: set[str]


def loaded(mode: str = "lazy") -> Labeled:
    """Return an object that has read its fields."""
    return Labeled(
        json={
            "id": 1,
            "labels": ["x"],
            "codes": [1],
            "queue": ["x"],
            "pair": [1, 2],
            "grid": [["x"]],
            "labelset": ["x"],
        },
        write_mode=mode,  # type: ignore[arg-type]
    )


def assign(field: str, value: Any, mode: str) -> None:
    """Assign a value to a field of a loaded object."""
    setattr(loaded(mode), field, value)


WRITES: dict[str, Callable[[str, Any], Any]] = {
    "assignment": lambda f, v: assign(f, v, "lazy"),
    "eager assignment": lambda f, v: assign(f, v, "eager"),
    "create": lambda f, v: Labeled.create(**{f: v}),
    "get_or_create lookup": lambda f, v: Labeled.get_or_create(**{f: v}),
    "get_or_create defaults": lambda f, v: Labeled.get_or_create(
        id=1, defaults={f: v}
    ),
}
WORDS = ["labels", "queue"]
SETS: dict[str, Callable[[], Any]] = {
    "set": lambda: {"d", "b", "c", "a"},
    "frozenset": lambda: frozenset("dbca"),
    "a view of a dict's keys": lambda: {"b": 1, "a": 2}.keys(),
}
NUMBERS: dict[str, Callable[[], Any]] = {
    "set": lambda: {4, 2, 3, 1},
    "frozenset": lambda: frozenset({4, 2, 3, 1}),
    "a view of a dict's keys": lambda: {2: 1, 1: 2}.keys(),
}


def refused(write: Callable[[str, Any], Any], field: str, value: Any) -> None:
    """Assert that a write names the field and sends no request."""
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(
            FieldConversionError, match=rf"Labeled\.{field}: .*no order"
        ):
            write(field, value)
        assert mocker.call_count == 0


@pytest.mark.parametrize("make", SETS.values(), ids=SETS)
@pytest.mark.parametrize("field", WORDS)
@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
def test_a_set_for_a_sequence_of_strings_is_refused_before_a_request(
    write: Callable[[str, Any], Any], field: str, make: Callable[[], Any]
) -> None:
    """A list of strings and a deque of them take no set."""
    refused(write, field, make())


@pytest.mark.parametrize("make", NUMBERS.values(), ids=NUMBERS)
@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
def test_a_set_for_a_variable_length_tuple_is_refused_before_a_request(
    write: Callable[[str, Any], Any], make: Callable[[], Any]
) -> None:
    """A tuple of integers of any length takes no set."""
    refused(write, "codes", make())


@pytest.mark.parametrize("make", SETS.values(), ids=SETS)
@pytest.mark.parametrize(
    "write",
    [w for name, w in WRITES.items() if name != "get_or_create lookup"],
    ids=[name for name in WRITES if name != "get_or_create lookup"],
)
def test_a_set_inside_a_nested_sequence_is_refused_before_a_request(
    write: Callable[[str, Any], Any], make: Callable[[], Any]
) -> None:
    """A set as an inner list of a list of lists is refused too."""
    refused(write, "grid", [make()])


@pytest.mark.parametrize("mode", ["lazy", "eager"])
def test_a_set_put_into_a_nested_sequence_in_place_is_refused_by_save(
    mode: str,
) -> None:
    """A set that no assignment gave is refused where it would be sent."""
    obj = loaded(mode)
    obj.grid.append({"b", "a"})  # type: ignore[arg-type]
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(
            FieldConversionError, match=r"Labeled\.grid: .*no order"
        ):
            obj.save()
        assert mocker.call_count == 0


@pytest.mark.parametrize("field", ["labels", "codes", "queue", "grid"])
def test_a_refused_set_leaves_nothing_for_save_to_send(field: str) -> None:
    """The refused assignment changes no field, so a save sends nothing."""
    obj = loaded()
    value: Any = {"b", "a"} if field != "codes" else {2, 1}
    if field == "grid":
        value = [value]
    before = obj.json
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(FieldConversionError):
            setattr(obj, field, value)
        obj.save()
        assert mocker.call_count == 0
    assert obj.json == before


def test_a_set_for_a_fixed_length_tuple_is_refused() -> None:
    """A fixed-length tuple keeps its own message."""
    with pytest.raises(FieldConversionError, match="Fixed-length"):
        loaded().pair = {1, 2}  # type: ignore[assignment]


def test_a_list_and_a_tuple_for_a_sequence_keep_their_order() -> None:
    """Only a set has no order to keep."""
    obj = loaded()
    for value in (["b", "a"], ("b", "a")):
        obj.labels = value  # type: ignore[assignment]
        assert obj.json["labels"] == ["b", "a"]
        obj.queue = value  # type: ignore[assignment]
        assert obj.json["queue"] == ["b", "a"]
    obj.codes = (3, 1, 2)
    assert obj.json["codes"] == [3, 1, 2]
    obj.grid = [["b", "a"], ["c"]]
    assert obj.json["grid"] == [["b", "a"], ["c"]]


def test_a_set_field_writes_its_items_sorted() -> None:
    """A field declared as a set takes a set and sorts it."""
    obj = loaded()
    obj.labelset = {"b", "d", "a", "c"}
    assert obj.json["labelset"] == ["a", "b", "c", "d"]


def test_a_query_sends_a_set_for_a_sequence_field_sorted() -> None:
    """The refusal is for a write. A lookup compares sets by their items."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Labeled.filter(labels={"b", "a", "c"}))
        list(Labeled.filter(labels__in={"b", "a", "c"}))
    queries = [r.url.partition("?")[2] for r in mocker.request_history]
    assert queries == ["labels=a&labels=b&labels=c", "labels__in=a%2Cb%2Cc"]


def test_a_query_of_a_set_in_a_nested_sequence_keeps_its_own_error() -> None:
    """A query is no write, so it refuses the nested collection as before."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match="contains a collection") as info:
            list(Labeled.filter(grid=[{"b", "a"}]))
        assert type(info.value) is ValueError
        assert mocker.call_count == 0


@pytest.mark.parametrize("make", SETS.values(), ids=SETS)
def test_encode_json_refuses_a_set_for_a_sequence_annotation(
    make: Callable[[], Any],
) -> None:
    """The function refuses what an assignment refuses."""
    with pytest.raises(TypeError, match="no order"):
        encode_json(make(), list[str])


def test_encode_json_sorts_a_set_for_a_set_annotation() -> None:
    """A set annotation writes the items sorted."""
    assert encode_json({"d", "b", "a"}, set[str]) == ["a", "b", "d"]
