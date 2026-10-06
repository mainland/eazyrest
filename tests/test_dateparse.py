"""Tests for date and duration parsing helpers."""

from __future__ import annotations

import datetime

import pytest

from eazyrest import parse_datetime, parse_duration


def test_parse_datetime_respects_explicit_tzinfo() -> None:
    """Naive datetimes should use the caller-provided timezone."""
    tzinfo = datetime.timezone.utc

    parsed = parse_datetime(
        "2024-01-02 03:04:05",
        use_dateparser=False,
        tzinfo=tzinfo,
    )

    assert parsed.tzinfo is tzinfo


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
        (90, datetime.timedelta(seconds=90)),
    ],
)
def test_parse_duration_formats(
    text: str | int, expected: datetime.timedelta
) -> None:
    """Django, ``str(timedelta)``, ISO 8601, and numeric forms parse."""
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
    """``str(timedelta)`` output parses back to the same duration."""
    assert parse_duration(str(delta)) == delta


@pytest.mark.parametrize("text", ["P1M", "bogus"])
def test_parse_duration_rejects_unsupported_durations(text: str) -> None:
    """Unparseable durations and calendar months raise ``ValueError``."""
    with pytest.raises(ValueError):
        parse_duration(text)
