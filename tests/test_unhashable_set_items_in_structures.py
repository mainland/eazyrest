"""A set of items that have no hash is refused, however the item is spelled.

A ``NewType`` stands for the class that it was made from. A ``NamedTuple``, a
tuple, a frozenset, and a frozen dataclass hash their members, so one with a
member that has no hash has none either. A set that decoration admits still
meets an item that has none at a read, as ``set[Any]`` does, and raises
eazyrest's own message, which does not differ between Python versions.
"""

from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple, NewType, Optional

import pytest
from typing_extensions import TypedDict

from eazyrest import Conversion, FieldConversionError, JSONObject, json_object


class Cells(NamedTuple):
    """A tuple class with a member that has no hash."""

    name: str
    cells: list[int]


class Plain(NamedTuple):
    """A tuple class whose members have a hash."""

    name: str
    total: int


@dataclasses.dataclass(frozen=True)
class FrozenWithList:
    """A frozen dataclass with a member that has no hash."""

    name: str
    cells: list[int]


@dataclasses.dataclass(frozen=True)
class FrozenWithDict:
    """A frozen dataclass with a member that has no hash."""

    extra: dict[str, int]


@dataclasses.dataclass(frozen=True)
class FrozenPlain:
    """A frozen dataclass whose members have a hash."""

    name: str


@dataclasses.dataclass(frozen=True)
class Chain:
    """A frozen dataclass that holds itself, whose members have a hash."""

    name: str
    next: Chain | None = None


class Later(NamedTuple):
    """A tuple class whose member is a name that is defined later."""

    value: Missing  # type: ignore[name-defined]  # noqa: F821


class Row(TypedDict):
    """A ``TypedDict``, which has no hash."""

    x: int


class Mutable:
    """A class of the program that defines equality, so it has no hash."""

    def __eq__(self, other: object) -> bool:
        """Equal to everything."""
        return True


ListCells = NewType("ListCells", list)  # type: ignore[type-arg]
DictObj = NewType("DictObj", dict)  # type: ignore[type-arg]
Rows = NewType("Rows", tuple[list[int], ...])
Inner = NewType("Inner", Cells)
Outer = NewType("Outer", Inner)


def declare(annotation: Any, base: type = JSONObject, **namespace: Any) -> Any:
    """Decorate a model with one field of the given annotation."""
    model: Any = type(
        "Shape",
        (base,),
        {
            "__annotations__": {"id": int, "value": annotation},
            "class_url": "/shapes/",
            **namespace,
        },
    )
    return json_object(model)


REFUSED: dict[str, tuple[Any, str]] = {
    "a NewType of list": (ListCells, "list"),
    "a NewType of dict": (DictObj, "dict"),
    "a NewType of a tuple of lists": (Rows, "list"),
    "a NewType of a NewType of a NamedTuple": (Outer, "list"),
    "a NamedTuple with a list": (Cells, "list"),
    "an optional NamedTuple with a list": (
        Optional[Cells],  # noqa: UP045
        "list",
    ),
    "a tuple of NamedTuples with a list": (tuple[Cells, ...], "list"),
    "a frozen dataclass with a list": (FrozenWithList, "list"),
    "a frozen dataclass with a dict": (FrozenWithDict, "dict"),
    "a frozenset of lists": (frozenset[list[int]], "list"),
    "a tuple of frozensets of lists": (
        tuple[frozenset[list[int]], ...],
        "list",
    ),
    "a TypedDict": (Row, "Row"),
    "a NewType of a TypedDict": (NewType("Typed", Row), "Row"),
}


@pytest.mark.parametrize("kind", [set, frozenset])
@pytest.mark.parametrize(("item", "named"), REFUSED.values(), ids=REFUSED)
def test_a_set_of_such_items_is_refused_naming_the_item(
    kind: Any, item: Any, named: str
) -> None:
    """The error is raised at decoration and names the field and the type."""
    with pytest.raises(TypeError, match=rf"^Shape\.value: .*{named} has none"):
        declare(kind[item])


ADMITTED: dict[str, Any] = {
    "a NamedTuple of hashable members": Plain,
    "a frozen dataclass of hashable members": FrozenPlain,
    "a frozen dataclass that holds itself": Chain,
    "a NamedTuple with a member not defined yet": Later,
    "a NewType of int": NewType("Number", int),
    "a tuple of NamedTuples": tuple[Plain, ...],
}


@pytest.mark.parametrize("kind", [set, frozenset])
@pytest.mark.parametrize("item", ADMITTED.values(), ids=ADMITTED)
def test_a_set_of_items_that_have_a_hash_is_admitted(
    kind: Any, item: Any
) -> None:
    """Only items that cannot be hashed are refused."""
    declare(kind[item])


def test_a_class_that_a_conversion_reads_is_admitted() -> None:
    """The conversion decides what the item reads as, so the class passes."""
    entry = Conversion(
        decode=lambda value, _: tuple(value),
        encode=lambda value, _: list(value),
        schema={"type": "array"},
    )
    declare(set[Mutable], conversions={Mutable: entry})


@pytest.mark.parametrize("kind", [set, frozenset])
def test_an_item_that_has_no_hash_at_a_read_has_eazyrests_message(
    kind: Any,
) -> None:
    """``Any`` admits the set, and a list in it is named, not Python's text."""
    model = declare(kind[Any])
    with pytest.raises(FieldConversionError) as caught:
        model(json={"id": 1, "value": [[1]]}).value  # noqa: B018
    assert (
        str(caught.value)
        == "Shape.value: Set items must have a hash, got list"
    )
    assert "unhashable" not in str(caught.value)


def test_an_item_that_has_a_hash_reads_as_a_set() -> None:
    """The check refuses nothing that a set can hold."""
    model = declare(set[Any])
    assert model(json={"id": 1, "value": [1, "a", 1]}).value == {1, "a"}


class Money:
    """A class that a conversion refuses with an error of its own."""


def refusing(value: Any, cls: Any) -> Any:
    """Raise a ``FieldConversionError`` that has no cause."""
    raise FieldConversionError(JSONObject, "elsewhere", ValueError("own text"))


def test_a_field_conversion_error_without_a_cause_is_raised_as_it_is() -> None:
    """An item's position goes into an error that has a cause, no other."""
    base: Any = type(
        "Base",
        (JSONObject,),
        {
            "conversions": {
                Money: Conversion(decode=refusing, encode=lambda v, c: v)
            }
        },
    )
    model = declare(list[Money], base=base)
    with pytest.raises(FieldConversionError) as caught:
        model(json={"id": 1, "value": ["a", "b"]}).value  # noqa: B018
    assert str(caught.value) == "JSONObject.elsewhere: own text"
    assert caught.value.__cause__ is None
