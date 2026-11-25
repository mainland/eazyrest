"""A ``Literal`` of flag members does not make the flag keep a member.

A ``Flag`` with ``boundary=KEEP``, and any ``IntFlag``, keeps a pseudo-member
in the enum for every distinct integer it is looked up by. A field declared as
the flag does that for each value a response sends, as Python's enum does. A
``Literal`` of the flag's members matches only their own values, and reads any
other integer as it is, so a response cannot grow the enum.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

import pytest

from eazyrest import (
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

MANY = 200_000


class IPerm(enum.IntFlag):
    """An ``IntFlag``, which keeps what no member names."""

    R = 1
    W = 2


class KPerm(enum.Flag, boundary=enum.KEEP):
    """A ``Flag`` that keeps what no member names."""

    R = 1
    W = 2


class CPerm(enum.Flag, boundary=enum.CONFORM):
    """A ``Flag`` that drops the bits that no member names."""

    R = 1
    W = 2


class SPerm(enum.Flag, boundary=enum.STRICT):
    """A ``Flag`` that refuses what no member names."""

    R = 1
    W = 2


class MPerm(enum.Flag, boundary=enum.KEEP):
    """A flag with a ``_missing_()`` of its own."""

    R = 1

    @classmethod
    def _missing_(cls, value: object) -> Any:
        """Read 99 as ``R``."""
        return cls.R if value == 99 else None


class NonePerm(enum.Flag, boundary=enum.KEEP):
    """A flag whose ``_missing_()`` is a ``staticmethod`` that maps nothing."""

    R = 1
    W = 2

    @staticmethod
    def _missing_(value: object) -> Any:
        """Map nothing."""
        return None


class MapPerm(enum.Flag, boundary=enum.KEEP):
    """A flag whose ``_missing_()`` is a ``staticmethod`` that maps 5."""

    R = 1
    W = 2

    @staticmethod
    def _missing_(value: object) -> Any:
        """Read 5 as ``R``."""
        return MapPerm.R if value == 5 else None


@json_object
class Doc(JSONObject):
    """A model with a ``Literal`` of each flag's members, and the flags."""

    class_url = "/docs/"
    id: int
    ipicks: list[Literal[IPerm.R, IPerm.W]]
    kpicks: list[Literal[KPerm.R, KPerm.W]]
    cpick: Literal[CPerm.R]
    spick: Literal[SPerm.R]
    mpick: Literal[MPerm.R]
    nonepick: Literal[NonePerm.R, NonePerm.W]
    mappick: Literal[MapPerm.R, MapPerm.W]
    ifield: list[IPerm]
    kfield: list[KPerm]
    cfield: list[CPerm]
    sfield: list[SPerm]


@json_object
class Mapped(JSONObject):
    """A model whose conversion of ``IPerm`` reads every integer as ``R``."""

    conversions = {  # noqa: RUF012
        IPerm: Conversion(
            decode=lambda value, cls: IPerm.R,
            encode=lambda value, cls: int(value.value),
        )
    }
    class_url = "/mapped/"
    id: int
    pick: Literal[IPerm.R, IPerm.W]


def _read(field: str, raw: Any) -> Any:
    return getattr(Doc(json={"id": 1, field: raw}), field)


@pytest.mark.parametrize(
    ("field", "flag"), [("ipicks", IPerm), ("kpicks", KPerm)]
)
def test_a_literal_of_flag_members_keeps_no_member_for_what_a_server_sends(
    field: str, flag: Any
) -> None:
    """The enum's table of members is the same after a read."""
    before = dict(flag._value2member_map_)
    raw = list(range(100, 100 + MANY))
    read = _read(field, raw)
    assert read == raw
    assert flag._value2member_map_ == before


@pytest.mark.parametrize(
    ("field", "flag"), [("ipicks", IPerm), ("kpicks", KPerm)]
)
def test_a_literal_of_flag_members_reads_members_and_other_integers(
    field: str, flag: Any
) -> None:
    """A member's own value is the member, and any other is as it is."""
    read = _read(field, [1, 2, 3, 0, -2, -1, 4, 1.0])
    assert read[:2] == [flag.R, flag.W]
    assert read[0] is flag.R
    assert read[1] is flag.W
    assert read[2:7] == [3, 0, -2, -1, 4]
    assert [type(item) for item in read[2:7]] == [int] * 5
    assert read[7] is flag.R


def test_a_literal_of_a_conforming_flag_reads_what_the_flag_conforms_to() -> (
    None
):
    """``CONFORM`` drops the bit that no member has, so 5 is ``R``."""
    assert _read("cpick", 5) is CPerm.R
    assert _read("cpick", 2) == 2


def test_a_literal_of_a_strict_flag_reads_other_integers_as_they_are() -> None:
    """``STRICT`` refuses 5, which a ``Literal`` does not check."""
    assert _read("spick", 5) == 5
    assert _read("spick", 1) is SPerm.R


def test_a_literal_of_a_flag_with_its_own_missing_still_maps() -> None:
    """Only ``Flag``'s own ``_missing_()`` is skipped."""
    assert _read("mpick", 99) is MPerm.R
    assert _read("mpick", 5) == 5


def test_a_literal_of_flag_members_asks_a_conversion_of_the_models_own() -> (
    None
):
    """Only eazyrest's own conversion of enums is skipped."""
    assert Mapped(json={"id": 1, "pick": 5}).pick is IPerm.R
    assert Mapped(json={"id": 1, "pick": 2}).pick is IPerm.W


def test_a_literal_of_a_flag_with_a_static_missing_asks_it() -> None:
    """A ``staticmethod`` is not ``Flag``'s own ``_missing_()``."""
    assert _read("nonepick", 5) == 5
    assert _read("mappick", 5) is MapPerm.R


@pytest.mark.parametrize(
    ("field", "flag", "kept"),
    [("ifield", IPerm, 1000), ("kfield", KPerm, 1000)],
)
def test_a_field_of_a_flag_that_keeps_members_keeps_one_per_value(
    field: str, flag: Any, kept: int
) -> None:
    """The field reads each integer as a combination, as Python's enum does."""
    before = len(flag._value2member_map_)
    read = _read(field, list(range(1000, 1000 + kept)))
    assert all(isinstance(item, flag) for item in read)
    assert len(flag._value2member_map_) - before == kept


def test_a_field_of_a_conforming_or_strict_flag_keeps_only_combinations() -> (
    None
):
    """``CONFORM`` maps to a combination and ``STRICT`` refuses."""
    read = _read("cfield", list(range(1000, 2000)))
    assert all(isinstance(item, CPerm) for item in read)
    after_first = len(CPerm._value2member_map_)
    _read("cfield", list(range(2000, 3000)))
    assert len(CPerm._value2member_map_) == after_first <= 4
    with pytest.raises(FieldConversionError):
        _read("sfield", list(range(1000, 2000)))
    assert len(SPerm._value2member_map_) == 2
