"""An enum or a ``Literal`` with a float that is not finite is refused."""

from __future__ import annotations

import enum
import math
from typing import Any, Literal

import pytest

from eazyrest import JSONObject, json_object


class Level(enum.Enum):
    """An enum whose members are floats."""

    LOW = 1.5
    HIGH = 2.5


class Unbounded(enum.Enum):
    """An enum with a member that is infinite."""

    LOW = 1.5
    TOP = math.inf


class Unknown(enum.Enum):
    """An enum with a member that is not a number."""

    NOTHING = math.nan


def declare(annotation: Any) -> Any:
    """Decorate a model with one field of the given annotation."""
    model: Any = type(
        "Gauge",
        (JSONObject,),
        {
            "__annotations__": {"id": int, "reading": annotation},
            "class_url": "/gauges/",
        },
    )
    return json_object(model)


@pytest.mark.parametrize(
    "annotation",
    [
        Unbounded,
        Unknown,
        Unbounded | None,
        list[Unbounded],
        Literal[1.5, math.inf],
        Literal[-math.inf],
        Literal[math.nan],
        list[Literal[math.inf]],  # type: ignore[valid-type]
    ],
    ids=[
        "inf_enum",
        "nan_enum",
        "optional_enum",
        "list_of_enums",
        "literal_with_inf",
        "literal_of_minus_inf",
        "literal_of_nan",
        "list_of_literals",
    ],
)
def test_a_value_that_is_not_finite_is_refused_at_decoration(
    annotation: Any,
) -> None:
    """The error says that the float is not finite."""
    with pytest.raises(TypeError, match="not finite"):
        declare(annotation)


@pytest.mark.parametrize("annotation", [Level, Literal[1.5, 2.5]])
def test_finite_floats_are_admitted(annotation: Any) -> None:
    """A finite float is a JSON number."""
    model = declare(annotation)
    assert model(json={"id": 1, "reading": 1.5}).reading in (
        1.5,
        Level.LOW,
    )
