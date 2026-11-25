"""The search for a type that contains itself.

A read that runs out of stack reports a value too deep only for a field whose
type contains itself, found through every kind of container and through
``NewType``, and not through a class that the model's table converts, whose
members no read visits.
"""

from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple, NewType

import pytest
from typing_extensions import TypedDict

from eazyrest import (
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)
from eazyrest._conversion import _table_of
from eazyrest._models import _holds_itself, _reading, analyze_type


class ByMap(TypedDict, total=False):
    """Holds itself through a mapping."""

    kids: dict[str, ByMap]


class ByTuple(TypedDict, total=False):
    """Holds itself through a fixed tuple."""

    pair: tuple[ByTuple, int]


class ByNew(TypedDict, total=False):
    """Holds itself through a ``NewType`` of a list."""

    kids: Kids


Kids = NewType("Kids", list[ByNew])


class Ping(TypedDict, total=False):
    """Holds itself through a dataclass that holds it."""

    pong: Pong


@dataclasses.dataclass
class Pong:
    """The dataclass of ``Ping``."""

    ping: Ping | None = None


class Row(NamedTuple):
    """A ``NamedTuple`` that holds itself through a tuple."""

    cells: tuple[Row, ...] = ()


@dataclasses.dataclass
class Money:
    """A dataclass whose annotations cannot be resolved."""

    currency: NotDefinedAnywhere  # type: ignore[name-defined]  # noqa: F821
    amount: int = 0


@dataclasses.dataclass
class Link:
    """A dataclass that holds itself."""

    next: Link | None = None


@json_object
class Holder(JSONObject):
    """A model with a field of each type that holds itself."""

    class_url = "/holders/"
    id: int
    by_map: ByMap
    by_tuple: ByTuple
    by_new: ByNew
    mutual: Ping
    row: Row
    listed: list[dict[str, ByMap]]


@json_object
class Converted(JSONObject):
    """A model that converts the classes whose members are not read."""

    conversions = {  # noqa: RUF012
        Money: Conversion(
            decode=lambda v, c: Money("EUR", v), encode=lambda v, c: {}
        ),
        Link: Conversion(decode=lambda v, c: Link(), encode=lambda v, c: {}),
    }
    class_url = "/converted/"
    id: int
    price: Money
    link: Link
    links: list[Link]


def nested(field: str, depth: int) -> Any:
    """Return a value of ``field`` that nests ``depth`` levels."""
    value: Any
    match field:
        case "by_map":
            value = {}
            for _ in range(depth):
                value = {"kids": {"a": value}}
        case "by_tuple":
            value = {}
            for _ in range(depth):
                value = {"pair": [value, 1]}
        case "by_new":
            value = {}
            for _ in range(depth):
                value = {"kids": [value]}
        case "mutual":
            value = {}
            for _ in range(depth):
                value = {"pong": {"ping": value}}
        case "row":
            value = [[]]
            for _ in range(depth):
                value = [[value]]
        case _:
            value = {}
            for _ in range(depth):
                value = {"kids": {"a": value}}
            value = [{"k": value}]
    return value


@pytest.mark.parametrize(
    "field", ["by_map", "by_tuple", "by_new", "mutual", "row", "listed"]
)
def test_a_value_too_deep_for_the_stack_is_refused_through_any_container(
    field: str,
) -> None:
    """The type holds itself through a mapping, tuple, ``NewType``, or both."""
    getattr(Holder(json={"id": 1, field: nested(field, 3)}), field)
    deep = Holder(json={"id": 1, field: nested(field, 3000)})
    with pytest.raises(FieldConversionError, match=f"Holder.{field}"):
        getattr(deep, field)


@pytest.mark.parametrize(
    ("ty", "expected"),
    [
        (ByMap, True),
        (Row, True),
        (Link, True),
        (Ping, True),
        (list[Link], True),
        (int, False),
    ],
)
def test_a_type_that_the_table_does_not_convert_is_searched(
    ty: Any, expected: bool
) -> None:
    """Without a conversion the search enters the class's members."""
    assert _holds_itself(analyze_type(ty), _table_of(Holder)) is expected


@pytest.mark.parametrize("ty", [Money, Link, list[Link]])
def test_a_type_that_the_table_converts_is_not_entered(ty: Any) -> None:
    """The class is replaced, so its members are skipped."""
    assert _holds_itself(analyze_type(ty), _table_of(Converted)) is False


@pytest.mark.parametrize("ty", [Money, Link])
def test_a_read_that_runs_out_of_stack_in_a_converted_class_is_not_refused(
    ty: Any,
) -> None:
    """The stack's own error is raised, with no search of the class."""
    with (
        pytest.raises(RecursionError) as caught,
        _reading(Converted, "price", analyze_type(ty)),
    ):
        raise RecursionError("maximum recursion depth exceeded")
    assert not isinstance(caught.value, FieldConversionError)
