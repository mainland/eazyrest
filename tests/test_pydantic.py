"""Tests for Pydantic conversion of field values."""

from __future__ import annotations

import datetime
import subprocess
import sys
import uuid
from typing import Annotated, Any

import pydantic
import pytest
from pydantic import Field, TypeAdapter
from pydantic_core import PydanticSerializationError
from typing_extensions import TypedDict

from eazyrest.pydantic import UUID, Date, Datetime, Duration

TOKEN = uuid.UUID("6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40")
UTC = datetime.timezone.utc


class Schedule(TypedDict):
    """Mapping whose values use eazyrest's conversions."""

    starts: Datetime
    day: Date
    length: Duration
    token: UUID


SCHEDULE = TypeAdapter(Schedule)


def test_importing_eazyrest_does_not_import_pydantic() -> None:
    """Only ``eazyrest.pydantic`` needs Pydantic installed."""
    code = "import sys, eazyrest; assert 'pydantic' not in sys.modules"

    subprocess.run([sys.executable, "-c", code], check=True)


def test_types_read_values_as_eazyrest_fields_do() -> None:
    """The types read the JSON forms that eazyrest's fields read."""
    schedule = SCHEDULE.validate_python(
        {
            "starts": "2024-04-01T14:00:00+02:00",
            "day": "2024-04-01",
            "length": "1 02:03:04",
            "token": str(TOKEN),
        }
    )

    assert schedule == {
        "starts": datetime.datetime(
            2024, 4, 1, 12, tzinfo=datetime.timezone.utc
        ),
        "day": datetime.date(2024, 4, 1),
        "length": datetime.timedelta(days=1, hours=2, minutes=3, seconds=4),
        "token": TOKEN,
    }
    # Pydantic's own datetime would use its TzInfo class.
    assert type(schedule["starts"].tzinfo) is datetime.timezone
    assert TypeAdapter(Datetime).validate_python(1711972800) == (
        datetime.datetime(2024, 4, 1, 12, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        # Pydantic's own types accept each of these values.
        (Datetime, 1.7e12),
        (Datetime, True),
        (Date, 0),
        (Date, datetime.datetime(2024, 4, 1)),  # noqa: DTZ001
        (Duration, "P1Y"),
        (Duration, "P1M"),
        (UUID, TOKEN.int),
    ],
)
def test_types_reject_values_that_eazyrest_fields_reject(
    annotation: Any, value: Any
) -> None:
    """A value that an eazyrest field rejects fails validation."""
    with pytest.raises(pydantic.ValidationError):
        TypeAdapter(annotation).validate_python(value)


def test_types_write_values_as_eazyrest_fields_do() -> None:
    """The types write values in the forms that eazyrest's fields write."""
    schedule: Schedule = {
        "starts": datetime.datetime(2024, 4, 1, 12, tzinfo=UTC),
        "day": datetime.date(2024, 4, 1),
        "length": datetime.timedelta(hours=1, minutes=30),
        "token": TOKEN,
    }

    assert SCHEDULE.dump_python(schedule, mode="json") == {
        "starts": "2024-04-01T12:00:00+00:00",
        "day": "2024-04-01",
        "length": "PT1H30M",
        "token": str(TOKEN),
    }


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (Datetime, "2024-04-01T12:00:00Z"),
        (Date, datetime.datetime(2024, 4, 1, tzinfo=UTC)),
        (Duration, 60),
        (UUID, str(TOKEN)),
    ],
)
def test_types_refuse_to_write_other_values(
    annotation: Any, value: Any
) -> None:
    """Writing a value of another type fails rather than guessing."""
    with pytest.raises(PydanticSerializationError):
        TypeAdapter(annotation).dump_python(
            value, mode="json", warnings="error"
        )


def test_constraints_apply_after_eazyrest_conversion() -> None:
    """Pydantic constraints check the value that eazyrest's codec reads."""
    adapter = TypeAdapter(Annotated[Duration, Field(ge=datetime.timedelta(0))])

    assert adapter.validate_python("1:30") == datetime.timedelta(seconds=90)
    with pytest.raises(pydantic.ValidationError):
        adapter.validate_python("-PT1M")


def test_types_have_json_schemas() -> None:
    """A model that uses the types still has a JSON schema."""
    properties = SCHEDULE.json_schema()["properties"]

    assert {
        name: (schema["type"], schema["format"])
        for name, schema in properties.items()
    } == {
        "starts": ("string", "date-time"),
        "day": ("string", "date"),
        "length": ("string", "duration"),
        "token": ("string", "uuid"),
    }
