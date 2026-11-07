"""Tests of tuples, NamedTuples, dataclasses, and unions as field types."""

from __future__ import annotations

import collections
import dataclasses
import datetime
import enum
import uuid
from collections.abc import (
    Collection,
    KeysView,
    MutableMapping,
    MutableSequence,
    MutableSet,
)
from collections.abc import Set as AbstractSet
from typing import Any, Generic, Literal, NamedTuple, NewType, TypeVar

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://structures.example.com/"
ItemT = TypeVar("ItemT")


class Shade(enum.Enum):
    """Enum, which converts, so a union cannot hold it."""

    DARK = "dark"


class Point(NamedTuple):
    """Named tuple, whose JSON form is an array."""

    x: int
    when: datetime.date


@dataclasses.dataclass
class Window:
    """Dataclass, whose JSON form is an object."""

    start: datetime.datetime
    tags: list[str] = dataclasses.field(default_factory=list)
    note: str | None = None


@dataclasses.dataclass
class LongWindow(Window):
    """Subclass of ``Window`` with a field that ``Window`` lacks."""

    end: datetime.datetime | None = None


class PlainWindow(Window):
    """Subclass of ``Window`` that adds no fields."""


@dataclasses.dataclass(frozen=True)
class Pair:
    """Frozen dataclass, which a set can hold."""

    left: int
    right: uuid.UUID


@dataclasses.dataclass
class Box(Generic[ItemT]):
    """Generic dataclass, which eazyrest converts only unparameterized."""

    item: ItemT


class StructureBase(JSONObject):
    """Base model of these tests."""


StructureBase.register_api(API(BASE))


@json_object
class Owner(StructureBase):
    """Related model."""

    class_url = "/owners/"

    id: int


@json_object
class Shape(StructureBase):
    """Model with tuple, NamedTuple, dataclass, and union fields."""

    class_url = "/shapes/"

    id: int
    span: tuple[int, datetime.date]
    one: tuple[uuid.UUID]
    empty: tuple[()]
    # A bare tuple, which the test covers, is an incomplete type to mypy.
    loose: tuple  # type: ignore[type-arg]
    point: Point
    window: Window
    pairs: frozenset[Pair]
    number: int | str
    maybe: int | str | None


DAY = datetime.date(2026, 11, 7)
TOKEN = uuid.UUID("6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40")
START = datetime.datetime(2026, 11, 7, 9, tzinfo=datetime.timezone.utc)

SHAPE_JSON: dict[str, Any] = {
    "id": 1,
    "span": [3, "2026-11-07"],
    "one": [str(TOKEN)],
    "empty": [],
    "loose": [1, "a", [2]],
    "point": [4, "2026-11-07"],
    "window": {"start": "2026-11-07T09:00:00+00:00", "tags": ["a"]},
    "pairs": [{"left": 1, "right": str(TOKEN)}],
    "number": "five",
    "maybe": None,
}


def shape(**changes: Any) -> Shape:
    """Return a shape read from ``SHAPE_JSON``, with some values replaced.

    Args:
        **changes: JSON values to replace.

    Returns:
        The shape.
    """
    return Shape(json={**SHAPE_JSON, **changes})


def test_structures_read_by_position_and_field() -> None:
    """Tuples and NamedTuples read by position, dataclasses by field."""
    item = shape()

    assert item.span == (3, DAY)
    assert item.one == (TOKEN,)
    assert item.empty == ()
    assert item.loose == (1, "a", [2])
    assert item.point == Point(4, DAY)
    assert isinstance(item.point, Point)
    assert item.window == Window(START, ["a"])
    assert item.pairs == frozenset({Pair(1, TOKEN)})
    assert item.number == "five"
    assert item.maybe is None


def test_structures_write_their_json_forms() -> None:
    """Tuples and NamedTuples write arrays, and dataclasses objects."""
    item = shape()

    with requests_mock.Mocker() as mock:
        patch = mock.patch(BASE + "shapes/1/", status_code=204)
        item.span = (5, DAY)
        item.point = Point(6, DAY)
        item.window = Window(START, note="n")
        item.number = 7
        item.save()

    assert patch.last_request.json() == {
        "span": [5, "2026-11-07"],
        "point": [6, "2026-11-07"],
        "window": {
            "start": "2026-11-07T09:00:00+00:00",
            "tags": [],
            "note": "n",
        },
        "number": 7,
    }


def test_changes_to_a_dataclass_in_place_are_saved() -> None:
    """A change made in place to a dataclass value is detected and sent."""
    item = shape()

    with requests_mock.Mocker() as mock:
        patch = mock.patch(BASE + "shapes/1/", status_code=204)
        item.window.tags.append("b")
        item.save()

    assert patch.last_request.json() == {
        "window": {
            "start": "2026-11-07T09:00:00+00:00",
            "tags": ["a", "b"],
            "note": None,
        }
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("span", [3], r"must have 2 items, got 1"),
        ("span", "3", r"must be JSON arrays"),
        ("point", [1, "2026-11-07", 3], r"must have 2 items, got 3"),
        ("span", [3, "x"], r"^Shape\.span: \[1\]: "),
        ("point", [1, "x"], r"^Shape\.point: \['when'\]: "),
        ("window", {"start": "x"}, r"^Shape\.window: \['start'\]: "),
        ("window", {"tags": []}, r"missing 1 required"),
        ("window", {"start": START.isoformat(), "x": 1}, "does not declare"),
        ("window", [1], r"must be JSON objects"),
    ],
)
def test_structures_refuse_values_of_other_shapes(
    field: str, value: Any, message: str
) -> None:
    """A value of the wrong length, shape, or keys raises when read.

    A dataclass cannot keep keys that it does not declare, and writing it
    back would delete them on the server, so it refuses them.

    Args:
        field: Name of the field read.
        value: Its JSON value.
        message: Pattern that the error should match.
    """
    with pytest.raises(FieldConversionError, match=message):
        getattr(shape(**{field: value}), field)


def test_structure_writes_refuse_values_of_other_types() -> None:
    """A value of the wrong length or type raises when assigned."""
    item = shape()

    with pytest.raises(FieldConversionError, match="must have 2 items"):
        item.span = (1,)  # type: ignore[assignment]
    with pytest.raises(FieldConversionError, match="must be Window instances"):
        item.window = {"start": START}  # type: ignore[assignment]

    # A subclass's own fields would be dropped, so it is refused, but one
    # that adds no fields writes as the declared class.
    with pytest.raises(FieldConversionError, match="LongWindow has fields"):
        item.window = LongWindow(START, end=START)
    item.window = PlainWindow(START)
    assert item.json["window"] == {
        "start": START.isoformat(),
        "tags": [],
        "note": None,
    }


def test_union_of_converting_members_is_refused_when_used() -> None:
    """A union passes through only if every member does, even when null."""

    @json_object
    class Mixed(StructureBase):
        class_url = "/mixed/"

        id: int
        when: str | datetime.datetime
        shade: Shade | int
        maybe: str | datetime.datetime | None
        dark: Literal[Shade.DARK] | int
        plain: Literal["dark", 1] | None

    item = Mixed(
        json={
            "id": 1,
            "when": "2026-11-07",
            "shade": "dark",
            "maybe": None,
            "dark": "dark",
            "plain": "dark",
        }
    )

    with pytest.raises(FieldConversionError, match="do not all pass through"):
        _ = item.when
    with pytest.raises(FieldConversionError, match="do not all pass through"):
        _ = item.shade
    # A null value does not hide the refusal, on a read or a write.
    with pytest.raises(FieldConversionError, match="do not all pass through"):
        _ = item.maybe
    with pytest.raises(FieldConversionError, match="do not all pass through"):
        item.maybe = None
    # A Literal with an enum member reads the member, so it converts too.
    with pytest.raises(FieldConversionError, match="do not all pass through"):
        _ = item.dark
    with pytest.raises(FieldConversionError, match="do not all pass through"):
        item.dark = Shade.DARK
    assert item.plain == "dark"


@pytest.mark.parametrize(
    "annotation",
    [
        int | list[int],
        tuple[int, Owner],
        Box[int],
        collections.Counter[str],
        collections.OrderedDict[str, datetime.date],
        KeysView[str],
    ],
)
def test_types_that_cannot_convert_are_refused(annotation: Any) -> None:
    """A union with a structure, a model in a tuple, and a generic dataclass.

    So is any other parameterized type that eazyrest does not convert,
    whose items would otherwise pass through unconverted.

    Args:
        annotation: The field's annotation.
    """
    namespace = {
        "__annotations__": {"id": int, "value": annotation},
        "class_url": "/refused/",
        "__module__": __name__,
    }
    refused: Any = json_object(type("Refused", (StructureBase,), namespace))

    with pytest.raises(FieldConversionError):
        _ = refused(json={"id": 1, "value": [1]}).value


def test_structure_schemas() -> None:
    """Tuples are arrays by position, dataclasses objects, unions anyOf."""
    properties = Shape.json_schema()["properties"]
    date = {"type": "string", "format": "date"}

    assert properties["span"] == {
        "type": "array",
        "prefixItems": [{"type": "integer"}, date],
        "items": False,
        "minItems": 2,
        "maxItems": 2,
    }
    assert properties["point"]["prefixItems"] == [{"type": "integer"}, date]
    assert properties["loose"] == {"type": "array", "items": {}}
    assert properties["window"] == {
        "type": "object",
        "properties": {
            "start": {
                "anyOf": [
                    {"type": "string", "format": "date-time"},
                    {"type": "number"},
                ]
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "additionalProperties": False,
        "required": ["start"],
    }
    assert properties["number"] == {
        "anyOf": [{"type": "integer"}, {"type": "string"}]
    }
    assert properties["maybe"] == {
        "anyOf": [
            {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            {"type": "null"},
        ]
    }


Dates = NewType("Dates", list[datetime.date])


@json_object
class Collections(StructureBase):
    """Model with each kind of collection whose items eazyrest converts."""

    class_url = "/collections/"

    id: int
    mutable: MutableSequence[datetime.date]
    collection: Collection[datetime.date]
    abstract_set: AbstractSet[datetime.date]
    mutable_set: MutableSet[datetime.date]
    queue: collections.deque[datetime.date]
    by_name: MutableMapping[str, datetime.date]
    dates: Dates


SECOND = datetime.date(2026, 1, 2)


def test_abstract_collections_convert_their_items() -> None:
    """Each collection type reads as a concrete one, its items converted.

    A ``NewType`` of a collection converts as the collection.
    """
    item = Collections(
        json={
            "id": 1,
            **{
                key: ["2026-01-02"]
                for key in (
                    "mutable",
                    "collection",
                    "abstract_set",
                    "mutable_set",
                    "queue",
                    "dates",
                )
            },
            "by_name": {"a": "2026-01-02"},
        }
    )

    assert item.mutable == [SECOND]
    assert item.collection == (SECOND,)
    assert item.abstract_set == frozenset({SECOND})
    assert item.mutable_set == {SECOND}
    assert item.queue == collections.deque([SECOND])
    assert item.by_name == {"a": SECOND}
    assert item.dates == [SECOND]

    item.mutable.append(SECOND)
    item.queue = collections.deque([SECOND, SECOND])
    item.by_name["b"] = SECOND
    assert item.json["queue"] == ["2026-01-02", "2026-01-02"]
    with requests_mock.Mocker() as mock:
        mock.patch(f"{BASE}collections/1/", json={"id": 1})
        item.save()
        assert mock.last_request.json() == {
            "mutable": ["2026-01-02", "2026-01-02"],
            "queue": ["2026-01-02", "2026-01-02"],
            "by_name": {"a": "2026-01-02", "b": "2026-01-02"},
        }
    schema = Collections.json_schema()["properties"]
    assert schema["abstract_set"]["uniqueItems"] is True
    assert schema["mutable_set"]["uniqueItems"] is True
    assert "uniqueItems" not in schema["collection"]


@dataclasses.dataclass
class Node:
    """Dataclass that holds itself."""

    value: int
    children: list[Node]


class Link(NamedTuple):
    """``NamedTuple`` that holds itself."""

    value: int
    rest: Link | None


@json_object
class Tree(StructureBase):
    """Model with structures that refer to themselves."""

    class_url = "/trees/"

    id: int
    node: Node
    link: Link


def test_structures_inside_themselves_have_finite_schemas() -> None:
    """A dataclass or ``NamedTuple`` inside itself allows any value there."""
    properties = Tree.json_schema()["properties"]

    assert properties["node"]["properties"]["children"] == {
        "type": "array",
        "items": {"type": "object"},
    }
    assert properties["link"]["prefixItems"][1] == {
        "anyOf": [{"type": "array"}, {"type": "null"}]
    }
    tree = Tree(
        json={"id": 1, "node": {"value": 1, "children": []}, "link": [1, None]}
    )
    assert tree.node == Node(1, [])
    assert tree.link == Link(1, None)


@dataclasses.dataclass(eq=False)
class Counter:
    """Mutable dataclass that hashes by identity, so a set can hold it."""

    n: int


@pytest.mark.parametrize(
    "annotation", [AbstractSet[Counter], frozenset[Counter], set[Counter]]
)
def test_items_of_a_set_changed_in_place_are_saved(annotation: Any) -> None:
    """A change to an item of a set, even a frozenset, is saved.

    Args:
        annotation: Set annotation of the field.
    """
    tally_type: Any = json_object(
        type(
            "Tally",
            (JSONObject,),
            {
                "class_url": "/tallies/",
                "__annotations__": {"id": int, "counters": annotation},
            },
        )
    )
    tally_type.register_api(API(BASE))
    tally = tally_type(json={"id": 1, "counters": [{"n": 1}]})
    next(iter(tally.counters)).n = 2

    with requests_mock.Mocker() as mock:
        patch = mock.patch(f"{BASE}tallies/1/", status_code=204)
        tally.save()

    assert patch.last_request.json() == {"counters": [{"n": 2}]}
    assert tally.json["counters"] == [{"n": 2}]
