"""Tests for date and duration parsing helpers."""

from __future__ import annotations

import datetime
import sys
from typing import Any, cast

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


def test_parse_datetime_date_only_uses_midnight() -> None:
    """Missing time components should not come from the current time."""
    parsed = parse_datetime(
        "2024-01-02",
        use_dateparser=False,
        tzinfo=datetime.timezone.utc,
    )

    assert parsed == datetime.datetime(
        2024, 1, 2, tzinfo=datetime.timezone.utc
    )


def test_parse_datetime_uses_dateparser_on_request() -> None:
    """``use_dateparser=True`` parses with ``dateparser``."""
    parsed = parse_datetime(
        "April 1, 2024 12:00",
        use_dateparser=True,
        tzinfo=datetime.timezone.utc,
    )

    assert parsed == datetime.datetime(
        2024, 4, 1, 12, tzinfo=datetime.timezone.utc
    )


def test_parse_datetime_names_missing_dateparser_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without dateparser, the error says which extra to install."""
    monkeypatch.setitem(sys.modules, "dateparser", None)

    with pytest.raises(ModuleNotFoundError, match=r"eazyrest\[dateparser\]"):
        parse_datetime("2024-01-02", use_dateparser=True)


def test_parse_datetime_does_not_need_dateparser_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default parser works without the dateparser extra."""
    monkeypatch.setitem(sys.modules, "dateparser", None)

    parsed = parse_datetime("2024-01-02T03:04:05Z")

    assert parsed == datetime.datetime(
        2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc
    )


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
    """Supported duration forms parse to the expected ``timedelta``."""
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


@pytest.mark.parametrize(
    "text", ["bogus", "", "-", "1h30", "1h -30m", "1 hour and 30 minutes"]
)
def test_parse_duration_rejects_unsupported_durations(text: str) -> None:
    """Strings in no supported form raise ``ValueError``."""
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_duration_names_unknown_unit() -> None:
    """An unknown unit is named in the error."""
    with pytest.raises(ValueError, match="unknown unit 'fortnight'"):
        parse_duration("1 fortnight")


@pytest.mark.parametrize("delta", [None, True, ["1h"]])
def test_parse_duration_rejects_other_types(delta: Any) -> None:
    """Only strings and numbers are durations."""
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
    """Years and months have no fixed length, in any format."""
    with pytest.raises(ValueError, match="years or months"):
        parse_duration(text)
