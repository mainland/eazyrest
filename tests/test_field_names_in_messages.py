"""An error names a field as ``Model.field: detail``, once.

A conversion error, an item of a collection inside one, and a refusal of a
declaration all name the field the same way, and a position goes after the name
and never repeats it.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object
from eazyrest._models import _at_position


@json_object
class Thing(JSONObject):
    """A model with collections of dates."""

    id: int
    day: datetime.date
    days: list[datetime.date]
    grid: list[list[datetime.date]]


def _read(field: str, value: Any) -> str:
    thing = Thing(json={"id": 1, field: value})
    with pytest.raises(FieldConversionError) as caught:
        getattr(thing, field)
    return str(caught.value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("day", 5, "Thing.day: Date values must be strings, got int"),
        (
            "days",
            ["2024-04-01", 5],
            "Thing.days: [1]: Date values must be strings, got int",
        ),
        (
            "grid",
            [["2024-04-01"], ["2024-04-02", 5]],
            "Thing.grid: [1]: [1]: Date values must be strings, got int",
        ),
    ],
)
def test_a_conversion_error_names_its_field_once(
    field: str, value: Any, message: str
) -> None:
    """The position of an item follows the field's name."""
    assert _read(field, value) == message


def test_a_detail_that_begins_with_the_field_name_is_kept() -> None:
    """A detail that starts as the field's name does is kept whole."""
    error = FieldConversionError(Thing, "day", ValueError("Thing.day: x"))
    error.__cause__ = ValueError("Thing.day: x")

    assert str(_at_position(error, 3)) == "Thing.day: [3]: Thing.day: x"


def _field() -> Any:
    pydantic = pytest.importorskip("pydantic")
    return Annotated[int, pydantic.Field(gt=0)]


def _dataclass_member() -> Any:
    pydantic = pytest.importorskip("pydantic")
    return dataclasses.make_dataclass(
        "Point", [("x", Annotated[int, pydantic.Field(gt=0)])]
    )


def _typed_dict_key() -> Any:
    pydantic = pytest.importorskip("pydantic")
    return TypedDict(  # type: ignore[operator]
        "Keyed", {"name": Annotated[str, pydantic.Field(alias="label")]}
    )


@pytest.mark.parametrize(
    ("annotation", "prefix"),
    [
        (_field, "Holder.value: "),
        (_dataclass_member, "Holder.value: Point.x: "),
        (_typed_dict_key, "Holder.value: Keyed.name: "),
        (lambda: int | datetime.date, "Holder.value: "),
    ],
    ids=["field", "dataclass member", "typed dict key", "union"],
)
def test_a_refusal_of_a_declaration_names_its_field_once(
    annotation: Callable[[], Any], prefix: str
) -> None:
    """Each refusal begins with the field or member that it names."""
    body = {
        "__module__": __name__,
        "__annotations__": {"id": int, "value": annotation()},
    }
    with pytest.raises(TypeError) as caught:
        json_object(type("Holder", (JSONObject,), body))
    message = str(caught.value)
    assert message.startswith(prefix)
    assert message.count("Holder.value: ") == 1
