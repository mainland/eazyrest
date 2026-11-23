"""A float with an integral value reads as the same integer for any enum.

Python finds a combination of flags by its hash only once it has made it, so
reading ``3.0`` raised or gave the combination by whether the process had read
``3`` before. An enum whose values are integers reads an integral float as its
integer, as an ``IntEnum`` always did.
"""

from __future__ import annotations

import enum
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object


def model(annotation: Any) -> Any:
    """Decorate a model with one field of the given annotation."""
    return json_object(
        type(
            "Holder",
            (JSONObject,),
            {
                "__annotations__": {"id": int, "value": annotation},
                "class_url": "/holders/",
            },
        )
    )


def perm() -> Any:
    """Return a ``Flag`` class that no process has read a value of."""
    return enum.Flag("Perm", {"R": 1, "W": 2})


def test_a_flag_reads_an_integral_float_before_its_combination_exists() -> (
    None
):
    """``3.0`` is ``R | W``, whether or not ``3`` was read first."""
    flag = perm()
    holder = model(flag)
    assert holder(json={"id": 1, "value": 3.0}).value == flag.R | flag.W
    assert holder(json={"id": 1, "value": 3}).value == flag.R | flag.W
    assert holder(json={"id": 1, "value": 3.0}).value == flag.R | flag.W


def test_a_fresh_process_reads_the_float_as_the_integer_does() -> None:
    """The first read of the process gives the combination."""
    script = textwrap.dedent(
        """
        import enum
        from eazyrest import JSONObject, json_object

        class Perm(enum.Flag):
            R = 1
            W = 2

        @json_object
        class Holder(JSONObject):
            class_url = "/holders/"
            id: int
            value: Perm

        print(Holder(json={"id": 1, "value": 3.0}).value)
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "Perm.R|W"


@pytest.mark.parametrize("value", [3.5, 8.0, float("inf")])
def test_a_float_that_is_no_member_is_still_refused(value: float) -> None:
    """Only an integral float that names a combination is read."""
    holder = model(perm())
    with pytest.raises(FieldConversionError):
        _ = holder(json={"id": 1, "value": value}).value


def test_an_enum_of_integers_reads_an_integral_float() -> None:
    """The reading of an ``IntEnum`` and of an ``Enum`` agree."""

    class Level(enum.IntEnum):
        LOW = 1

    class Plain(enum.Enum):
        LOW = 1

    assert model(Level)(json={"id": 1, "value": 1.0}).value is Level.LOW
    assert model(Plain)(json={"id": 1, "value": 1.0}).value is Plain.LOW


def test_a_float_is_not_read_as_an_integer_for_other_enums() -> None:
    """An enum with a value that is no integer keeps Python's reading."""

    class Mixed(enum.Enum):
        ONE = 1
        HALF = 0.5

    holder = model(Mixed)
    assert holder(json={"id": 1, "value": 0.5}).value is Mixed.HALF
    assert holder(json={"id": 1, "value": 1.0}).value is Mixed.ONE
