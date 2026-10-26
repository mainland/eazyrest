"""Tests for date and duration parsing helpers."""

from __future__ import annotations

import datetime

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
