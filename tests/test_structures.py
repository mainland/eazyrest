"""Tests of tuples, NamedTuples, dataclasses, and unions as field types."""

from __future__ import annotations

import collections
import dataclasses
import datetime
import enum
import sys
import typing
import uuid
from collections.abc import (
    Collection,
    KeysView,
    MutableMapping,
    MutableSequence,
    MutableSet,
)
from collections.abc import Set as AbstractSet
from typing import (
    Any,
    Generic,
    Literal,
    NamedTuple,
    NewType,
    TypedDict,
    TypeVar,
)

import pytest
import requests_mock

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)
from eazyrest._models import (
    DataclassType,
    FixedTupleType,
    NamedTupleType,
    analyze_type,
)

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
START = datetime.datetime(2026, 11, 7, 9, tzinfo=datetime.UTC)

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


@pytest.mark.parametrize(
    "annotation",
    [
        str | datetime.datetime,
        Shade | int,
        str | datetime.datetime | None,
        # A Literal with an enum member reads the member, so it converts too.
        Literal[Shade.DARK] | int,
        list[str | datetime.datetime],
        dict[str, str | datetime.datetime],
        tuple[int, str | datetime.datetime],
    ],
    ids=repr,
)
def test_union_of_converting_members_is_refused_when_decorated(
    annotation: Any,
) -> None:
    """A union passes through only if every member does, even when null.

    The class is refused when it is decorated, and the message names the field,
    also for a union inside an optional, a collection, a mapping, or a
    fixed-length tuple.

    Args:
        annotation: The field's annotation.
    """
    namespace = {
        "__annotations__": {"id": int, "value": annotation},
        "class_url": "/mixed/",
        "__module__": __name__,
    }

    with pytest.raises(TypeError, match=r"^Mixed\.value: ") as refused:
        json_object(type("Mixed", (StructureBase,), namespace))

    assert not isinstance(refused.value, FieldConversionError)
    assert "do not all pass through" in str(refused.value)


@dataclasses.dataclass
class _Scaled:
    """Dataclass with an init-only field, with a default."""

    x: int
    scale: dataclasses.InitVar[int] = 1


@dataclasses.dataclass
class _Required(_Scaled):
    """Dataclass that inherits an init-only field."""

    y: int = 0


@pytest.mark.parametrize("cls", [_Scaled, _Required])
def test_dataclass_with_an_init_only_field_is_refused_when_decorated(
    cls: type[Any],
) -> None:
    """A dataclass's init-only field is not kept, so the field is refused.

    The message names the init-only field rather than keys that the dataclass
    does not declare, and a field typed by a class that inherits one is refused
    too.

    Args:
        cls: Dataclass with an init-only field.
    """
    namespace = {
        "__annotations__": {"id": int, "value": cls},
        "class_url": "/scaled/",
        "__module__": __name__,
    }

    with pytest.raises(TypeError, match=r"^Holder\.value: ") as refused:
        json_object(type("Holder", (StructureBase,), namespace))

    assert not isinstance(refused.value, FieldConversionError)
    assert "init-only field scale" in str(refused.value)


@pytest.mark.parametrize(
    ("alias", "origin"),
    [
        (typing.List, list),  # noqa: UP006
        (typing.Dict, dict),  # noqa: UP006
        (typing.Tuple, tuple),  # noqa: UP006
        (typing.Set, set),  # noqa: UP006
        (typing.Type, type),  # noqa: UP006
        (typing.Hashable, collections.abc.Hashable),
        (typing.Sequence, collections.abc.Sequence),
    ],
    ids=repr,
)
def test_a_bare_typing_alias_converts_as_its_origin(
    alias: Any, origin: type[Any]
) -> None:
    """``typing.List`` and the other bare aliases read as ``list`` and so on.

    They are not parameterized, so refusing them as a parameterized type, or as
    a mapping with keys that are not strings, names a defect that is not there.

    Args:
        alias: Bare alias from ``typing``.
        origin: The class that it stands for.
    """
    assert analyze_type(alias) == analyze_type(origin)


def test_a_bare_typing_alias_is_a_field_type() -> None:
    """A field typed by ``typing.Dict`` reads its JSON object as it is."""
    namespace = {
        "__annotations__": {"id": int, "value": typing.Dict},  # noqa: UP006
        "class_url": "/bare/",
        "__module__": __name__,
    }
    model: Any = json_object(type("Bare", (StructureBase,), namespace))

    assert model(json={"id": 1, "value": {"a": 1}}).value == {"a": 1}


def test_an_empty_tuple_type_is_still_a_fixed_tuple() -> None:
    """``tuple[()]`` has no arguments but is not a bare alias."""
    assert analyze_type(tuple[()]) == FixedTupleType(())


_Code = NewType("_Code", str)
"""String type that a subclass's conversions may convert."""

_Address = NewType("_Address", str)
"""Another string type, which a subclass converts, off the union."""

_CODE_CONVERSION = Conversion(
    decode=lambda value, _: _Code(value), encode=lambda value, _: str(value)
)


def test_inherited_union_is_refused_when_a_subclass_converts_it() -> None:
    """A subclass's conversions can make an inherited union one that converts.

    The base passes the ``NewType`` ``_Code`` through, and the subclass
    converts it, so the subclass is refused when it is decorated, as it would
    be if it declared the field itself.
    """

    @json_object
    class Base(StructureBase):
        class_url = "/bases/"

        id: int
        value: int | _Code

    namespace = {
        "conversions": {_Code: _CODE_CONVERSION},
        "__module__": __name__,
    }
    with pytest.raises(TypeError, match=r"^Sub\.value: ") as refused:
        json_object(type("Sub", (Base,), namespace))

    assert not isinstance(refused.value, FieldConversionError)
    assert "do not all pass through" in str(refused.value)


def test_inherited_union_is_kept_when_a_subclass_converts_other_types() -> (
    None
):
    """A subclass that converts other types keeps an inherited union."""

    @json_object
    class Base(StructureBase):
        class_url = "/bases/"

        id: int
        value: int | _Code

    namespace = {
        "conversions": {_Address: _CODE_CONVERSION},
        "__module__": __name__,
    }
    sub: Any = json_object(type("Sub", (Base,), namespace))

    assert sub(json={"id": 1, "value": "a"}).value == "a"


_WhenName = NewType(  # type: ignore[valid-newtype]
    "_WhenName", str | datetime.datetime
)


def test_union_inside_a_new_type_is_refused_when_decorated() -> None:
    """A ``NewType`` of a union with a converting member is refused."""
    with pytest.raises(TypeError, match=r"^Named\.value: ") as refused:

        @json_object
        class Named(StructureBase):
            class_url = "/named/"

            id: int
            value: _WhenName

    assert not isinstance(refused.value, FieldConversionError)
    assert "do not all pass through" in str(refused.value)


_WHEN = datetime.datetime(2026, 11, 7, 1, 2, 3, tzinfo=datetime.UTC)
_WHEN_JSON = "2026-11-07T01:02:03+00:00"
_OPTIONAL = typing.Optional[datetime.datetime]  # noqa: UP045
_UNION = typing.Union[None, datetime.datetime]  # noqa: UP007


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (_OPTIONAL, _WHEN_JSON, _WHEN),
        (datetime.datetime | None, _WHEN_JSON, _WHEN),
        (_UNION, _WHEN_JSON, _WHEN),
        (
            list[datetime.datetime] | None,
            [_WHEN_JSON],
            [_WHEN],
        ),
    ],
    ids=["Optional", "pipe", "Union", "list"],
)
def test_an_optional_type_is_not_a_refused_union(
    annotation: Any, value: Any, expected: Any
) -> None:
    """``T | None`` in any spelling is an optional, which converts ``T``.

    Args:
        annotation: The field's annotation.
        value: JSON value that the field reads.
        expected: The converted value.
    """
    namespace = {
        "__annotations__": {"id": int, "value": annotation},
        "class_url": "/optionals/",
        "__module__": __name__,
    }
    model: Any = json_object(type("Optional_", (StructureBase,), namespace))

    assert model(json={"id": 1, "value": value}).value == expected
    assert model(json={"id": 1, "value": None}).value is None


def test_union_of_members_that_pass_through_is_decorated() -> None:
    """A union whose members all pass through unchanged converts."""

    @json_object
    class Plain(StructureBase):
        class_url = "/plain/"

        id: int
        either: int | str
        literal: Literal["dark", 1] | None

    item = Plain(json={"id": 1, "either": "a", "literal": "dark"})

    assert item.either == "a"
    assert item.literal == "dark"


class _MixedMembers(TypedDict):
    """A ``TypedDict`` whose members are unions that convert."""

    when: str | datetime.datetime
    maybe: str | datetime.datetime | None


def test_union_in_a_typed_dict_is_refused_when_used() -> None:
    """A structure's members are checked when the field is first used."""

    @json_object
    class Holder(StructureBase):
        class_url = "/holders/"

        id: int
        record: _MixedMembers

    item = Holder(
        json={"id": 1, "record": {"when": "2026-11-07", "maybe": None}}
    )

    with pytest.raises(FieldConversionError, match="do not all pass through"):
        _ = item.record
    with pytest.raises(FieldConversionError, match="do not all pass through"):
        item.record = {"when": "2026-11-07", "maybe": None}


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

    The class is refused when it is decorated, and the message names the field.

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

    with pytest.raises(TypeError) as refused:
        json_object(type("Refused", (StructureBase,), namespace))

    assert not isinstance(refused.value, FieldConversionError)
    assert str(refused.value).startswith("Refused.value: ")


def test_a_field_that_cannot_convert_is_refused_naming_the_field() -> None:
    """A ``Counter[str]`` field raises ``TypeError`` when it is decorated.

    The message names the model and the field, and the reason is the cause.
    """
    with pytest.raises(TypeError, match=r"^Tally\.counts: ") as refused:

        @json_object
        class Tally(StructureBase):
            class_url = "/tallies/"

            id: int
            counts: collections.Counter[str]

    assert not isinstance(refused.value, FieldConversionError)
    assert isinstance(refused.value.__cause__, TypeError)


def test_a_field_naming_a_class_defined_later_is_decorated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that is not defined yet does not refuse the class.

    The field raises the undefined name when it is first used, and converts
    once the class exists.

    Args:
        monkeypatch: Defines the class as a global, after decoration.
    """

    @json_object
    class Entry(StructureBase):
        class_url = "/entries/"

        id: int
        owner: LaterOwner

    entry = Entry(json={"id": 1, "owner": 2})
    with pytest.raises(NameError, match="LaterOwner"):
        _ = entry.owner

    LaterOwner = NewType("LaterOwner", int)

    monkeypatch.setitem(globals(), "LaterOwner", LaterOwner)

    assert entry.owner == 2


class _Counted(TypedDict):
    """A ``TypedDict`` whose member eazyrest does not convert."""

    counts: collections.Counter[str]


def test_a_typed_dict_member_that_cannot_convert_is_refused_when_used() -> (
    None
):
    """A structure's members are analyzed when the field is first used.

    So the class is decorated, and the field raises ``FieldConversionError``.
    """

    @json_object
    class Census(StructureBase):
        class_url = "/censuses/"

        id: int
        counted: _Counted

    census = Census(json={"id": 1, "counted": {"counts": {"a": 1}}})

    with pytest.raises(FieldConversionError, match=r"^Census\.counted: "):
        _ = census.counted


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="The type statement needs Python 3.12"
)
def test_a_type_alias_naming_a_class_defined_later_is_decorated() -> None:
    """A type alias whose value is not evaluable yet does not refuse the class.

    The field raises the undefined name when it is first used, and converts
    once the class exists.
    """
    namespace: dict[str, Any] = {}
    exec("type Pending = list[LaterItem]", namespace)  # noqa: S102

    pending: Any = json_object(
        type(
            "Pending",
            (StructureBase,),
            {
                "__annotations__": {"id": int, "items": namespace["Pending"]},
                "class_url": "/pending/",
            },
        )
    )
    obj = pending(json={"id": 1, "items": [1, 2]})
    with pytest.raises(NameError, match="LaterItem"):
        _ = obj.items

    namespace["LaterItem"] = int

    assert obj.items == [1, 2]


@pytest.mark.parametrize(
    "annotation",
    [
        typing.LiteralString,
        typing.Never,
        typing.NoReturn,
        typing.Self,
        typing.Final,
        typing.Literal,
        typing.TypeVar("T"),
        typing.ParamSpec("P"),
        len,
        datetime,
        3,
    ],
    ids=repr,
)
def test_an_annotation_that_is_not_a_type_is_refused_when_decorated(
    annotation: Any,
) -> None:
    """A special form, a type variable, or a value as a field type raises.

    Its JSON would otherwise pass through as it is. The message names the
    field.

    Args:
        annotation: The field's annotation.
    """
    namespace = {
        "__annotations__": {"id": int, "value": annotation},
        "class_url": "/refused/",
        "__module__": __name__,
    }

    with pytest.raises(TypeError, match=r"^Refused\.value: .*not a type"):
        json_object(type("Refused", (StructureBase,), namespace))


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


class NamesNothingTuple(NamedTuple):
    """Named tuple whose annotation names nothing."""

    value: Nowhere  # type: ignore[name-defined]  # noqa: F821


@dataclasses.dataclass
class NamesNothingDataclass:
    """Dataclass whose annotation names nothing."""

    value: Nowhere  # type: ignore[name-defined]  # noqa: F821


@pytest.mark.parametrize(
    ("ty", "name"),
    [
        (NamesNothingTuple, "NamesNothingTuple"),
        (NamesNothingDataclass, "NamesNothingDataclass"),
    ],
)
def test_a_structure_annotation_naming_nothing_reports_the_annotations(
    ty: Any, name: str
) -> None:
    """The error is a ``TypeError`` that names the class and the name.

    Args:
        ty: Class whose annotation names nothing.
        name: Its name.
    """
    analyzed = analyze_type(ty)
    assert isinstance(analyzed, NamedTupleType | DataclassType)

    with pytest.raises(
        TypeError,
        match=(
            rf"^The annotations of {name} cannot be resolved: "
            r"name 'Nowhere' is not defined$"
        ),
    ):
        _ = analyzed.fields
