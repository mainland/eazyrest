"""Stateful property tests of an object whose fields Pydantic converts.

The state machine of ``test_object_state`` runs again on a subclass of its
model with three fields that Pydantic converts: bytes written as base64, a
Pydantic model that can be changed in place, and a dict sent as JSON text.
The model of the fake server holds each field's JSON value, as Pydantic
writes it, and compares reads after Pydantic reads that value back.
"""

from __future__ import annotations

import copy
import functools
from typing import Any, ClassVar

import pytest

pytest.importorskip("pydantic")

import hypothesis.strategies as st
from hypothesis import HealthCheck, settings
from hypothesis.stateful import rule
from pydantic import Base64Bytes, BaseModel, ConfigDict, Json, TypeAdapter
from test_object_state import (
    DECLARED,
    FIELDS,
    VALUES,
    WORDS,
    Doc,
    ObjectStateMachine,
)

from eazyrest import json_object


class Point(BaseModel):
    """Pydantic model that a field holds and a caller changes in place."""

    model_config = ConfigDict(extra="forbid")

    x: int
    y: int


@json_object
class PydanticDoc(Doc):
    """The state machine's model, with fields that Pydantic converts."""

    blob: Base64Bytes
    point: Point
    counts: Json[dict[str, int]]


ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "blob": TypeAdapter(Base64Bytes),
    "point": TypeAdapter(Point),
    "counts": TypeAdapter(Json[dict[str, int]]),
}
"""The Pydantic adapter of each converted field."""

CONVERTED = tuple(ADAPTERS)

SMALL = st.integers(min_value=-3, max_value=3)
PYTHON_VALUES: dict[str, st.SearchStrategy[Any]] = {
    "blob": st.binary(max_size=4),
    "point": st.builds(Point, x=SMALL, y=SMALL),
    "counts": st.dictionaries(WORDS, SMALL, max_size=2),
}
"""Python values of each converted field."""


def encode(field: str, value: Any) -> Any:
    """Return a converted field's JSON value, as Pydantic writes it.

    Args:
        field: Name of the converted field.
        value: Python value of the field.

    Returns:
        The JSON value that the field writes.
    """
    return ADAPTERS[field].dump_python(value, mode="json", round_trip=True)


class PydanticStateMachine(ObjectStateMachine):
    """Run the object state machine on a model with converted fields."""

    MODEL: ClassVar[type[Doc]] = PydanticDoc
    FIELDS: ClassVar[tuple[str, ...]] = (*FIELDS, *CONVERTED)
    DECLARED: ClassVar[tuple[str, ...]] = (*DECLARED, *CONVERTED)
    VALUES: ClassVar[dict[str, st.SearchStrategy[Any]]] = {
        **VALUES,
        **{
            field: PYTHON_VALUES[field].map(functools.partial(encode, field))
            for field in CONVERTED
        },
    }

    def python_value(self, field: str, value: Any) -> Any:
        """Return the Python value whose JSON is a field's JSON value.

        Args:
            field: Name of the field.
            value: JSON value of the field.

        Returns:
            The value, as reading the field returns it.
        """
        if field in ADAPTERS:
            return ADAPTERS[field].validate_python(copy.deepcopy(value))
        return super().python_value(field, value)

    @rule(field=st.sampled_from(CONVERTED))
    def read_converted(self, field: str) -> None:
        """A read of a converted field returns the expected value.

        Args:
            field: Name of the converted field to read.
        """
        self.read(field)

    @rule(data=st.data(), field=st.sampled_from(CONVERTED))
    def assign_converted(self, data: st.DataObject, field: str) -> None:
        """Assign a converted field, which writes Pydantic's JSON value.

        Args:
            data: Hypothesis data object that draws the new Python value.
            field: Name of the converted field to assign.
        """
        value = data.draw(PYTHON_VALUES[field])
        encoded = encode(field, value)
        unchanged = encoded == self.stored[field]
        if self.poisoned and self.sends(unchanged):
            self.refused(lambda: setattr(self.obj, field, value))
            return

        setattr(self.obj, field, value)
        self.assign_value(field, encoded, unchanged)

    @rule(data=st.data(), field=st.sampled_from(CONVERTED))
    def push_converted(self, data: st.DataObject, field: str) -> None:
        """A pushed server update of a converted field overrides it.

        Args:
            data: Hypothesis data object that draws the server's new value.
            field: Name of the converted field that the server updates.
        """
        self.push(data, field)

    @rule(x=SMALL)
    def move_point(self, x: int) -> None:
        """Read the Pydantic model and change it in place.

        Args:
            x: New value of the point's ``x``.
        """
        self.read("point")
        assert isinstance(self.obj, PydanticDoc)
        self.obj.point.x = x
        self.expected["point"]["x"] = x
        self.in_place.add("point")

    @rule(key=WORDS, value=SMALL)
    def set_count(self, key: str, value: int) -> None:
        """Read the dict that a field holds as JSON text, and change it.

        Args:
            key: Key to set in the object's ``counts`` dict.
            value: Value to store under ``key``.
        """
        self.read("counts")
        assert isinstance(self.obj, PydanticDoc)
        self.obj.counts[key] = value
        counts = self.python_value("counts", self.expected["counts"])
        counts[key] = value
        self.expected["counts"] = encode("counts", counts)
        self.in_place.add("counts")


PydanticStateMachine.TestCase.settings = settings(
    max_examples=100,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_pydantic_object_state = PydanticStateMachine.TestCase
