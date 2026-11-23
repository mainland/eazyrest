"""A date field says what it got when the JSON is no string."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object


@json_object
class Event(JSONObject):
    """A model with a date and a time."""

    class_url = "/events/"
    id: int
    day: datetime.date
    at: datetime.time


@pytest.mark.parametrize(
    ("value", "name"),
    [(5, "int"), (1.5, "float"), (True, "bool"), ([1], "list"), ({}, "dict")],
)
def test_a_date_that_is_no_string_names_its_type(
    value: Any, name: str
) -> None:
    """The message is eazyrest's, the same on every Python version."""
    event = Event(json={"id": 1, "day": value, "at": "09:30"})
    with pytest.raises(FieldConversionError) as caught:
        _ = event.day
    assert str(caught.value).endswith(
        f"Date values must be strings, got {name}"
    )
    assert isinstance(caught.value.__cause__, TypeError)


def test_a_time_that_is_no_string_names_its_type() -> None:
    """A time says the same of its own."""
    event = Event(json={"id": 1, "day": "2024-04-01", "at": 5})
    with pytest.raises(FieldConversionError) as caught:
        _ = event.at
    assert str(caught.value).endswith("Time values must be strings, got int")
