"""Tests for date and duration parsing helpers."""

from __future__ import annotations

import datetime
from typing import Any, cast

import pytest

from eazyrest import parse_datetime, parse_duration

UTC = datetime.UTC


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
        (
            "02:03:04.5",
            datetime.timedelta(
                hours=2, minutes=3, seconds=4, milliseconds=500
            ),
        ),
        (
            "02:03:04.25",
            datetime.timedelta(
                hours=2, minutes=3, seconds=4, milliseconds=250
            ),
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
        "\u0661h",
        "\u0661\u0662",
        "\u0661:\u0663\u0660",
        "\u0661\u0662 days, 1:00:00",
        "1:00:00.\u0661",
    ],
)
def test_parse_duration_rejects_unsupported_durations(text: str) -> None:
    """Strings in no supported form raise ``ValueError`` without quoting them.

    Args:
        text: String that is not a supported duration.
    """
    with pytest.raises(ValueError, match="not in a supported format") as info:
        parse_duration(text)

    assert repr(text) not in str(info.value)


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
    with pytest.raises(ValueError, match="unknown unit"):
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


def test_a_datetime_out_of_range_raises_value_error() -> None:
    """24:00 on the last day that ``datetime`` represents is out of range."""
    with pytest.raises(ValueError, match="out of range"):
        parse_datetime("9999-12-31T24:00:00")


@pytest.mark.parametrize(
    "text",
    [
        " 1h",
        "1h ",
        " 1h ",
        "\t1h",
        "1h\n",
        "\x1f1h",
        "\u00a01h",
        "1h\u3000",
        " 90",
        "90 ",
        " 1:30",
        "1:30 ",
        " P1D",
        "P1D ",
        " -1h",
        "1h, ",
    ],
)
def test_parse_duration_refuses_surrounding_whitespace(text: str) -> None:
    """A duration with whitespace around it is refused, in every form.

    Args:
        text: Duration written with whitespace before or after it.
    """
    with pytest.raises(ValueError, match="surrounding whitespace"):
        parse_duration(text)


@pytest.mark.parametrize(
    "text", ["1h 30m", "- 1 hour", "1h, 30m", "1 day, 2:03"]
)
def test_parse_duration_keeps_whitespace_inside_a_duration(text: str) -> None:
    """Whitespace between the parts of a duration is still allowed.

    Args:
        text: Duration written with whitespace between its parts.
    """
    assert isinstance(parse_duration(text), datetime.timedelta)


MICRO = chr(0x00B5)
GREEK_MU = chr(0x03BC)
KELVIN = chr(0x212A)
LONG_S = chr(0x017F)
CAPITAL_MU = chr(0x039C)
FULLWIDTH_H = chr(0xFF48)
DOTLESS_I = chr(0x0131)
DOTTED_CAPITAL_I = chr(0x0130)


@pytest.mark.parametrize(
    "text",
    [
        f"1w{KELVIN}",
        "1 " + "weeks".replace("k", KELVIN),
        f"1{KELVIN}",
        f"1m{LONG_S}",
        f"1{LONG_S}",
        f"1{CAPITAL_MU}s",
        f"1{FULLWIDTH_H}",
        f"1{DOTLESS_I}",
        f"1{DOTTED_CAPITAL_I}",
    ],
    ids=[
        "kelvin sign for k in wk",
        "kelvin sign in weeks",
        "kelvin sign alone",
        "long s after m",
        "long s alone",
        "capital Greek mu",
        "fullwidth h",
        "dotless i",
        "dotted capital i",
    ],
)
def test_parse_duration_reads_no_non_ascii_letter_as_a_unit_letter(
    text: str,
) -> None:
    """A letter that case folding turns into an ASCII letter is no unit.

    Args:
        text: Duration whose unit holds such a letter.
    """
    with pytest.raises(ValueError, match="Duration"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1wk", datetime.timedelta(weeks=1)),
        ("1WK", datetime.timedelta(weeks=1)),
        ("1Wk", datetime.timedelta(weeks=1)),
        ("1 Hours", datetime.timedelta(hours=1)),
        ("2 SECONDS", datetime.timedelta(seconds=2)),
        (f"1{MICRO}s", datetime.timedelta(microseconds=1)),
        (f"1{GREEK_MU}s", datetime.timedelta(microseconds=1)),
        (f"1{MICRO}S", datetime.timedelta(microseconds=1)),
        ("1 mS", datetime.timedelta(milliseconds=1)),
    ],
)
def test_parse_duration_reads_both_cases_of_the_ascii_unit_letters(
    text: str, expected: datetime.timedelta
) -> None:
    """Units are spelled in either case, and the micro signs stay.

    Args:
        text: Duration with a unit.
        expected: The duration it names.
    """
    assert parse_duration(text) == expected
