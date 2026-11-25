"""A ``Literal`` of enum members reads a value as an enum field reads it.

An enum field decodes a value by the enum's whole rule, ``_missing_()``
included. A ``Literal`` of the enum's members matches the same values and
returns the member, so the two fields give one object for one JSON value.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

import pytest

from eazyrest import Conversion, JSONObject, json_object


class Status(str, enum.Enum):  # noqa: UP042
    """An enum that maps other spellings of its values to members."""

    OPEN = "open"
    CLOSED = "closed"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        """Match a value in any letter case."""
        if isinstance(value, str):
            for member in cls:
                if member.value == value.lower():
                    return member
        return None


class Level(enum.Enum):
    """An enum of numbers."""

    LOW = 1
    HIGH = 2


@json_object
class Doc(JSONObject):
    """A model that declares each enum as a field and as a ``Literal``."""

    class_url = "/docs/"
    id: int
    status: Status
    status_literal: Literal[Status.OPEN, Status.CLOSED]
    some: Literal[Status.OPEN]
    level: Level
    level_literal: Literal[Level.LOW, Level.HIGH]


def _read(field: str, raw: Any) -> Any:
    return getattr(Doc(json={"id": 1, field: raw}), field)


@pytest.mark.parametrize("raw", ["open", "OPEN", "Closed", "cLoSeD"])
def test_a_literal_reads_a_value_that_the_enum_maps_to_a_member(
    raw: str,
) -> None:
    """The Literal returns the member that the enum field returns."""
    member = _read("status", raw)
    assert isinstance(member, Status)
    assert _read("status_literal", raw) is member


@pytest.mark.parametrize("raw", [1, 2, 1.0, 2.0])
def test_a_literal_reads_the_number_of_a_member_as_the_member(
    raw: Any,
) -> None:
    """A whole float names the member, as for the field."""
    assert _read("level_literal", raw) is _read("level", raw)


@pytest.mark.parametrize("raw", ["nope", 3, True, None, ["open"]])
def test_a_literal_keeps_a_value_that_no_member_names(raw: Any) -> None:
    """A ``Literal`` does not check, so the value is read as it is."""
    assert _read("status_literal", raw) == raw
    assert _read("level_literal", raw) == raw


def test_a_literal_keeps_a_value_that_names_a_member_it_does_not_hold() -> (
    None
):
    """The member that the enum finds must be one of the Literal's."""
    assert _read("status", "CLOSED") is Status.CLOSED
    assert _read("some", "CLOSED") == "CLOSED"
    assert _read("some", "OPEN") is Status.OPEN


class Tone(enum.Enum):
    """An enum whose conversion writes names and cannot read them."""

    LOW = 1
    HIGH = 2


def _unreadable(value: Any, cls: type[Any]) -> Any:
    """Refuse every value."""
    raise ValueError("unreadable")


@json_object
class Toned(JSONObject):
    """A model whose enum converts by name in one direction."""

    conversions = {  # noqa: RUF012
        Tone: Conversion(
            decode=_unreadable, encode=lambda value, cls: value.name.lower()
        )
    }
    class_url = "/toned/"
    id: int
    tone: Literal[Tone.LOW, Tone.HIGH]


def test_a_literal_matches_the_json_a_conversion_writes_for_a_member() -> None:
    """A member is read from the value that its conversion encodes."""
    assert Toned(json={"id": 1, "tone": "low"}).tone is Tone.LOW
    assert Toned(json={"id": 1, "tone": "high"}).tone is Tone.HIGH
    other: Any = Toned(json={"id": 1, "tone": "other"}).tone
    assert other == "other"
