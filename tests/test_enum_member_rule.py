"""A JSON value names an enum member by one rule at every entry point.

The rule holds for a field of the enum and for a field of a ``Literal`` of its
members. A JSON value names the member whose value it equals. A float with an
integral value names the member whose value is that integer if every member's
value is an integer, and a boolean equals no number.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

import pytest

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://enum-member-rule.example/")


class Color(enum.Enum):
    """An enum of integers."""

    RED = 1
    GREEN = 2


class Switch(enum.Enum):
    """An enum of booleans."""

    ON = True
    OFF = False


class Level(enum.IntEnum):
    """An enum that mixes in ``int``."""

    LOW = 1
    HIGH = 2


class Ratio(enum.Enum):
    """An enum of floats."""

    HALF = 0.5
    WHOLE = 1.0


class Mixed(enum.Enum):
    """An enum of an integer and a string."""

    ONE = 1
    NAME = "name"


class Perm(enum.Flag):
    """A flag, whose integral float reads before its combinations exist."""

    READ = 1
    WRITE = 2


@json_object
class Paint(JSONObject):
    """A model with each enum as a field and as a ``Literal`` of members."""

    class_url = "/paints/"
    id: int
    color: Color
    color_literal: Literal[Color.RED, Color.GREEN]
    switch: Switch
    switch_literal: Literal[Switch.ON, Switch.OFF]
    level: Level
    level_literal: Literal[Level.LOW, Level.HIGH]
    ratio: Ratio
    ratio_literal: Literal[Ratio.HALF, Ratio.WHOLE]
    perm: Perm
    perm_literal: Literal[Perm.READ, Perm.WRITE]
    mixed: Mixed
    mixed_literal: Literal[Mixed.ONE, Mixed.NAME]


Paint.register_api(api)

START = {
    "id": 1,
    "color": 1,
    "color_literal": 1,
    "switch": True,
    "switch_literal": True,
    "level": 1,
    "level_literal": 1,
    "ratio": 0.5,
    "ratio_literal": 0.5,
    "perm": 1,
    "perm_literal": 1,
    "mixed": 1,
    "mixed_literal": 1,
}

CASES: list[tuple[str, Any, Any]] = [
    # (field, JSON value, member it names, or None for no member)
    ("color", 1, Color.RED),
    ("color", 2, Color.GREEN),
    ("color", 1.0, Color.RED),
    ("color", 2.0, Color.GREEN),
    ("color", 1.5, None),
    ("color", 3, None),
    ("color", True, None),
    ("color", "1", None),
    ("switch", True, Switch.ON),
    ("switch", False, Switch.OFF),
    ("switch", 1, None),
    ("switch", 0, None),
    ("switch", 1.0, None),
    ("level", 1, Level.LOW),
    ("level", 2.0, Level.HIGH),
    ("level", True, None),
    ("level", 3.0, None),
    ("ratio", 0.5, Ratio.HALF),
    ("ratio", 1.0, Ratio.WHOLE),
    ("ratio", 1, Ratio.WHOLE),
    ("ratio", True, None),
    ("perm", 1, Perm.READ),
    ("perm", 2.0, Perm.WRITE),
    ("mixed", 1, Mixed.ONE),
    ("mixed", "name", Mixed.NAME),
    ("mixed", True, None),
    ("mixed", 1.0, Mixed.ONE),
]


def _id(case: tuple[str, Any, Any]) -> str:
    return f"{case[0]}-{case[1]!r}"


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_a_field_of_the_enum_and_one_of_its_members_read_one_value_alike(
    case: tuple[str, Any, Any],
) -> None:
    """The enum's field and the ``Literal``'s field name the same member."""
    name, value, expected = case
    literal_name = f"{name}_literal"
    obj = Paint(json={**START, name: value, literal_name: value})
    if expected is None:
        with pytest.raises(FieldConversionError):
            getattr(obj, name)
        # A Literal does not check, so it keeps the JSON as it is.
        read = getattr(obj, literal_name)
        assert type(read) is type(value)
        assert read == value
    else:
        assert getattr(obj, name) is expected
        assert getattr(obj, literal_name) is expected
