"""Tests for date and duration parsing helpers."""

from __future__ import annotations

import datetime
import re
from typing import Any, cast

import pytest

from eazyrest import parse_datetime, parse_duration

UTC = datetime.timezone.utc


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "2024-01-02T03:04:05Z",
            datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        ),
        (
            "2024-01-02T03:04:05+02:00",
            datetime.datetime(
                2024,
                1,
                2,
                3,
                4,
                5,
                tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
            ),
        ),
        # A string without an offset parses to a naive datetime.
        ("2024-01-02 03:04", datetime.datetime(2024, 1, 2, 3, 4)),  # noqa: DTZ001
        ("2024-01-02", datetime.datetime(2024, 1, 2)),  # noqa: DTZ001
        (1704164645, datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
        (
            1704164645.5,
            datetime.datetime(2024, 1, 2, 3, 4, 5, 500000, tzinfo=UTC),
        ),
    ],
)
def test_parse_datetime_reads_iso_8601_and_timestamps(
    value: Any, expected: datetime.datetime
) -> None:
    """ISO 8601 strings and timestamps parse as datetime fields read them.

    Args:
        value: ISO 8601 string, or Unix timestamp, to parse.
        expected: Datetime that ``value`` should parse to. The parsed
            ``tzinfo`` must also have the same type as its ``tzinfo``.
    """
    parsed = parse_datetime(value)

    assert parsed == expected
    assert type(parsed.tzinfo) is type(expected.tzinfo)


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        ("April 1, 2024", ValueError, "not in ISO 8601 format"),
        ("tomorrow", ValueError, "not in ISO 8601 format"),
        (1e20, ValueError, "out of range"),
        (True, TypeError, "got bool"),
        (None, TypeError, "got NoneType"),
    ],
)
def test_parse_datetime_rejects_other_values(
    value: Any, error: type[Exception], message: str
) -> None:
    """Natural language, huge timestamps, and other types are rejected.

    Args:
        value: Value that is not a datetime that eazyrest reads.
        error: Exception type that parsing ``value`` should raise.
        message: Pattern that the error message should match.
    """
    with pytest.raises(error, match=message):
        parse_datetime(value)


def test_parse_duration_returns_timedelta() -> None:
    """Duration parsing should always produce ``datetime.timedelta``."""
    parsed = parse_duration("1h 30m")

    assert parsed == datetime.timedelta(hours=1, minutes=30)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "1 02:03:04",
            datetime.timedelta(days=1, hours=2, minutes=3, seconds=4),
        ),
        ("-1 23:00:00", datetime.timedelta(hours=-1)),
        (
            "02:03:04.000005",
            datetime.timedelta(hours=2, minutes=3, seconds=4, microseconds=5),
        ),
        ("-1 day, 23:00:00", datetime.timedelta(hours=-1)),
        ("3 days, 0:00:00", datetime.timedelta(days=3)),
        (
            "P1DT2H3M4S",
            datetime.timedelta(days=1, hours=2, minutes=3, seconds=4),
        ),
        ("-PT1H", datetime.timedelta(hours=-1)),
        ("1:30", datetime.timedelta(minutes=1, seconds=30)),
        ("1:30:00", datetime.timedelta(hours=1, minutes=30)),
        ("90:00", datetime.timedelta(minutes=90)),
        ("P2W", datetime.timedelta(days=14)),
        ("1h 30m", datetime.timedelta(hours=1, minutes=30)),
        ("1h30m0s", datetime.timedelta(hours=1, minutes=30)),
        ("1H30M", datetime.timedelta(hours=1, minutes=30)),
        ("1.5 hours", datetime.timedelta(hours=1, minutes=30)),
        ("1 day, 2 hours", datetime.timedelta(days=1, hours=2)),
        ("2 weeks", datetime.timedelta(days=14)),
        ("10m", datetime.timedelta(minutes=10)),
        ("500ms", datetime.timedelta(milliseconds=500)),
        ("10us", datetime.timedelta(microseconds=10)),
        ("3\u00b5s", datetime.timedelta(microseconds=3)),
        ("3\u03bcs", datetime.timedelta(microseconds=3)),
        ("-1h30m", datetime.timedelta(hours=-1, minutes=-30)),
        ("- 1 hour", datetime.timedelta(hours=-1)),
        (" 1h ", datetime.timedelta(hours=1)),
        ("90", datetime.timedelta(seconds=90)),
        ("-1.5", datetime.timedelta(seconds=-1.5)),
        (90, datetime.timedelta(seconds=90)),
        (1.5, datetime.timedelta(seconds=1.5)),
    ],
)
def test_parse_duration_formats(
    text: str | float, expected: datetime.timedelta
) -> None:
    """Supported duration forms parse to the expected ``timedelta``.

    Args:
        text: Duration string, or number of seconds, to parse.
        expected: Duration that ``text`` should parse to.
    """
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "delta",
    [
        datetime.timedelta(0),
        datetime.timedelta(hours=-1),
        datetime.timedelta(days=-3, microseconds=7),
        datetime.timedelta(seconds=1, microseconds=500),
        datetime.timedelta(days=400, hours=5),
    ],
)
def test_parse_duration_inverts_str(delta: datetime.timedelta) -> None:
    """``str(timedelta)`` output parses back to the same duration.

    Args:
        delta: Duration whose ``str()`` output is parsed.
    """
    assert parse_duration(str(delta)) == delta


@pytest.mark.parametrize(
    "text",
    [
        "bogus",
        "",
        "-",
        "1h30",
        "1h -30m",
        "1 hour and 30 minutes",
        "1:60",
        "1:60:00",
    ],
)
def test_parse_duration_rejects_unsupported_durations(text: str) -> None:
    """Strings in no supported form raise ``ValueError``.

    Args:
        text: String that is not a supported duration.
    """
    with pytest.raises(ValueError, match=re.escape(repr(text))):
        parse_duration(text)


@pytest.mark.parametrize(
    "delta",
    [
        "99999999999999999999",
        1e20,
        float("inf"),
        "1000000000 days, 0:00:00",
        "999999999999 weeks",
        "P999999999999D",
    ],
)
def test_parse_duration_rejects_out_of_range_durations(
    delta: str | float,
) -> None:
    """A duration that ``timedelta`` cannot represent is a ``ValueError``.

    Args:
        delta: Duration string, or number of seconds, too large for
            ``timedelta``.
    """
    with pytest.raises(ValueError, match="out of range"):
        parse_duration(delta)


def test_parse_duration_names_unknown_unit() -> None:
    """An unknown unit is named in the error."""
    with pytest.raises(ValueError, match="unknown unit 'fortnight'"):
        parse_duration("1 fortnight")


@pytest.mark.parametrize("delta", [None, True, ["1h"]])
def test_parse_duration_rejects_other_types(delta: Any) -> None:
    """Only strings and numbers are durations.

    Args:
        delta: Value that is neither a string nor a number.
    """
    with pytest.raises(TypeError):
        parse_duration(cast(Any, delta))


@pytest.mark.parametrize(
    "text",
    [
        "P1M",
        "P1Y",
        "P1Y2M3D",
        "1 month",
        "1mo",
        "2 years",
        "1.5y",
        "-1 month",
        "1 day 1 year",
    ],
)
def test_parse_duration_rejects_calendar_units(text: str) -> None:
    """Years and months have no fixed length, in any format.

    Args:
        text: Duration that has years or months.
    """
    with pytest.raises(ValueError, match="years or months"):
        parse_duration(text)
