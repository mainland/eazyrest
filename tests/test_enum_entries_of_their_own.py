"""An entry of its own for enums decides what a member writes.

eazyrest checks the values of an enum, and of a ``Literal`` of its members,
while its own conversion of enums converts them: a member's value must be a
JSON scalar and no ``None``. An entry that a model's ``conversions`` keys by
``enum.Enum`` replaces that conversion, and its ``encode`` decides what a
member writes, so decoration neither calls it for every member nor refuses what
it writes.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

import pytest

from eazyrest import (
    DEFAULT_CONVERSIONS,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

calls: list[str] = []
"""The members that an entry's ``encode`` was asked about."""


class Color(enum.Enum):
    """An enum whose values are strings."""

    RED = "red"
    BLUE = "blue"


class Maybe(enum.Enum):
    """An enum with a member whose value is ``None``."""

    NO = None
    YES = "yes"


class Level(enum.Enum):
    """An enum whose values are tuples."""

    LOW = (1, "low")


def decode(value: Any, cls: Any) -> Any:
    """Read an enum by member name."""
    return cls[value["name"]] if isinstance(value, dict) else cls[value]


def as_object(member: Any, cls: Any) -> Any:
    """Write a member as an object, which is no JSON scalar."""
    calls.append(member.name)
    return {"name": member.name}


def by_name(member: Any, cls: Any) -> Any:
    """Write a member by name, whatever its value."""
    calls.append(member.name)
    return member.name


def retired(member: Any, cls: Any) -> Any:
    """Refuse one member, as a conversion reports a value it cannot write."""
    calls.append(member.name)
    if member is Color.BLUE:
        raise ValueError("BLUE is retired")
    return member.value


def declare(annotation: Any, entry: Conversion, key: Any = enum.Enum) -> Any:
    """Decorate a model whose table has an entry for enums."""
    base: Any = type("Base", (JSONObject,), {"conversions": {key: entry}})
    holder: Any = type(
        "Holder",
        (base,),
        {
            "__annotations__": {"id": int, "value": annotation},
            "class_url": "/holders/",
        },
    )
    return json_object(holder)


ENTRIES = {
    "an object": Conversion(decode=decode, encode=as_object),
    "a name": Conversion(decode=decode, encode=by_name),
    "a refusal": Conversion(decode=decode, encode=retired),
}
ANNOTATIONS: dict[str, tuple[Any, type[enum.Enum]]] = {
    "Color": (Color, Color),
    "Maybe": (Maybe, Maybe),
    "Level": (Level, Level),
    "a Literal of a member": (Literal[Color.RED], Color),
    "an optional enum": (Color | None, Color),
    "a list of an enum": (list[Color], Color),
}


@pytest.mark.parametrize("keyed_by_enum", [True, False], ids=["Enum", "own"])
@pytest.mark.parametrize(
    ("annotation", "owner"), ANNOTATIONS.values(), ids=ANNOTATIONS
)
@pytest.mark.parametrize("entry", ENTRIES.values(), ids=ENTRIES)
def test_an_entry_of_its_own_is_not_asked_about_every_member(
    entry: Conversion,
    annotation: Any,
    owner: type[enum.Enum],
    keyed_by_enum: bool,
) -> None:
    """The class decorates, and no member is encoded to check it."""
    calls.clear()
    declare(annotation, entry, enum.Enum if keyed_by_enum else owner)
    assert calls == []


def test_an_entry_that_writes_objects_reads_and_writes_them() -> None:
    """The entry converts, whatever the enum's own values are."""
    model = declare(Color, ENTRIES["an object"])
    assert model(json={"id": 1, "value": {"name": "RED"}}).value is Color.RED
    obj = model(json={"id": 1, "value": {"name": "RED"}})
    obj.value = Color.BLUE
    assert obj.json["value"] == {"name": "BLUE"}


def test_an_entry_that_refuses_a_member_refuses_it_where_it_is_written() -> (
    None
):
    """The refusal is the conversion's own, for the member it refuses."""
    model = declare(Color, ENTRIES["a refusal"])
    obj = model(json={"id": 1, "value": "RED"})
    obj.value = Color.RED
    with pytest.raises(FieldConversionError, match="BLUE is retired"):
        obj.value = Color.BLUE


def test_an_enum_that_the_entry_is_not_for_is_still_checked() -> None:
    """An entry keyed by one enum does not replace the check of another."""
    entry = Conversion(decode=decode, encode=by_name)
    with pytest.raises(TypeError, match="not a JSON scalar"):
        declare(Level, entry, key=Color)
    with pytest.raises(TypeError, match="has the value None"):
        declare(Maybe, entry, key=Color)


def test_the_default_entry_given_again_is_checked() -> None:
    """Eazyrest's own conversion, given again, is no entry of its own."""
    entry = DEFAULT_CONVERSIONS[enum.Enum]
    with pytest.raises(TypeError, match="has the value None"):
        declare(Maybe, entry)
    with pytest.raises(TypeError, match="not a JSON scalar"):
        declare(Level, entry)
    declare(Color, entry)


def test_a_literal_of_members_is_checked_as_the_enum_is() -> None:
    """A ``Literal`` of an enum's members follows the entry for the enum."""
    declare(Literal[Color.RED], ENTRIES["an object"])
    with pytest.raises(TypeError, match="not a JSON scalar"):
        declare(Literal[Level.LOW], DEFAULT_CONVERSIONS[enum.Enum])
