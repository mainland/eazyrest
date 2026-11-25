"""An enum whose lookup returns no member refuses the value.

An ``IntFlag`` with ``boundary=EJECT`` returns a plain ``int`` for a value that
names no combination of its members. A field of the flag refuses that value as
it refuses any other that the enum lacks, and a ``Literal`` of its members
reads the value as it is.
"""

from __future__ import annotations

import enum
from typing import Literal

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object


class Perm(enum.IntFlag, boundary=enum.EJECT):
    """A flag that gives an ``int`` for a value that is no combination."""

    R = 1
    W = 2


@json_object
class Doc(JSONObject):
    """A model with the flag as a field and in a ``Literal``."""

    class_url = "/docs/"
    id: int
    perm: Perm
    pick: Literal[Perm.R, Perm.W]


def test_a_field_refuses_a_value_that_the_enum_ejects() -> None:
    """The refusal is the field's, not an ``AttributeError``."""
    with pytest.raises(FieldConversionError, match=r"Doc\.perm"):
        _ = Doc(json={"id": 1, "perm": 5}).perm


@pytest.mark.parametrize(
    ("raw", "member"), [(1, Perm.R), (3, Perm.R | Perm.W)]
)
def test_a_field_reads_a_combination_of_members(
    raw: int, member: Perm
) -> None:
    """A value that the flag does not eject is read."""
    assert Doc(json={"id": 1, "perm": raw}).perm is member


@pytest.mark.parametrize("raw", [1, 2, 5, -1])
def test_a_literal_reads_a_value_that_the_enum_ejects_as_it_is(
    raw: int,
) -> None:
    """A member is read as the member, and any other value as itself."""
    read = Doc(json={"id": 1, "pick": raw}).pick
    expected = {1: Perm.R, 2: Perm.W}.get(raw, raw)
    assert read == expected
    assert type(read) is type(expected)
