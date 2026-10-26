"""Regression tests for fixed defects."""

from __future__ import annotations

import datetime

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object


class MockedBase(JSONObject):
    """Models served by the mocked API."""


@pytest.mark.parametrize(
    "value", ["20260401", "2026-W14-3", "2026-04-01T00:00:00"]
)
def test_date_strings_use_the_same_format_on_every_python(value: str) -> None:
    """Basic and week dates cannot depend on the interpreter's parser."""

    @json_object
    class Dated(MockedBase):
        id: int
        date: datetime.date

    dated = Dated(json={"id": 1, "date": value})

    with pytest.raises(FieldConversionError):
        _ = dated.date
