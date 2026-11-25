"""One rule says which UTC offsets a time and a datetime may write.

ISO 8601 writes an offset in whole minutes, so a ``time`` and a ``datetime``
with another offset are refused, whatever writes them.
"""

import datetime
from typing import Any

import pytest

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

api = API("https://utc-offsets.example/")


@json_object
class Event(JSONObject):
    """A model with a time and a datetime field."""

    class_url = "/events/"
    id: int
    at_time: datetime.time
    at_moment: datetime.datetime


Event.register_api(api)

# Offsets in seconds, and whether ISO 8601 can write them.
OFFSETS = [
    (0, True),
    (60, True),
    (3600, True),
    (-3600, True),
    (-19800, True),
    (86340, True),
    (-86340, True),
    (1, False),
    (30, False),
    (61, False),
    (-90, False),
    (19 * 60 + 32, False),
    (86399, False),
]


def moment(seconds: int) -> datetime.datetime:
    """Return a datetime that has an offset of ``seconds``."""
    zone = datetime.timezone(datetime.timedelta(seconds=seconds))
    return datetime.datetime(2024, 4, 1, 9, 30, tzinfo=zone)


def time_of_day(seconds: int) -> datetime.time:
    """Return a time that has an offset of ``seconds``."""
    return moment(seconds).timetz()


def assign_time(seconds: int) -> None:
    """Assign a time to a field."""
    event = Event(
        json={
            "id": 1,
            "at_time": "09:30:00",
            "at_moment": "2024-04-01T09:30:00Z",
        },
        write_mode="lazy",
    )
    event.at_time = time_of_day(seconds)


def assign_moment(seconds: int) -> None:
    """Assign a datetime to a field."""
    event = Event(
        json={
            "id": 1,
            "at_time": "09:30:00",
            "at_moment": "2024-04-01T09:30:00Z",
        },
        write_mode="lazy",
    )
    event.at_moment = moment(seconds)


def encode_time(seconds: int) -> Any:
    """Encode a time as a field writes it."""
    return encode_json(time_of_day(seconds), datetime.time)


def encode_moment(seconds: int) -> Any:
    """Encode a datetime as a field writes it."""
    return encode_json(moment(seconds), datetime.datetime)


def query_time(seconds: int) -> Any:
    """Encode a time as a query on a field sends it."""
    return Event._encode_query({"at_time": time_of_day(seconds)}, api)


def query_moment(seconds: int) -> Any:
    """Encode a datetime as a query on a field sends it."""
    return Event._encode_query({"at_moment": moment(seconds)}, api)


ENTRY_POINTS = [
    assign_time,
    assign_moment,
    encode_time,
    encode_moment,
    query_time,
    query_moment,
]


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(("seconds", "writable"), OFFSETS)
def test_an_offset_is_written_only_in_whole_minutes(
    entry: Any, seconds: int, writable: bool
) -> None:
    """Every entry point refuses an offset that is not whole minutes."""
    if writable:
        entry(seconds)
    else:
        with pytest.raises(
            (ValueError, FieldConversionError), match="whole minutes"
        ):
            entry(seconds)


@pytest.mark.parametrize(
    ("entry", "kind"),
    [
        (assign_time, "Time"),
        (encode_time, "Time"),
        (assign_moment, "Datetime"),
        (encode_moment, "Datetime"),
    ],
    ids=lambda x: x.__name__ if callable(x) else x,
)
def test_the_message_names_the_type(entry: Any, kind: str) -> None:
    """The message names the type whose offset is refused."""
    with pytest.raises(
        (ValueError, FieldConversionError),
        match=f"{kind} field values must have a UTC offset of whole minutes",
    ):
        entry(30)
