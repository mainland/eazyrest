"""Date and duration parsing helpers used by eazyrest."""

import datetime
import re

import dateparser
import dateutil.parser
import isodate
import pytimeparse2
import tzlocal

# The format of str(datetime.timedelta) and Django's duration_string(), for
# example "1 day, 2:03:04", "-1 day, 23:00:00", or "1 02:03:04.000005".
_STANDARD_DURATION_RE = re.compile(
    r"(?:(?P<days>-?\d+) (?:days?, )?)?"
    r"(?P<hours>\d+):(?P<minutes>\d\d):(?P<seconds>\d\d)"
    r"(?:\.(?P<microseconds>\d{1,6}))?"
)


def parse_datetime(
    date: str,
    use_dateparser: bool = True,
    tzinfo: datetime.tzinfo | None = None,
) -> datetime.datetime:
    """Parse a date/time string.

    Args:
        date: Date/time string to parse.
        use_dateparser: If ``True``, parse with ``dateparser``; otherwise use
            ``dateutil.parser.parse``.
        tzinfo: Time zone to apply when the parsed datetime is naive. If
            omitted, the local time zone is used.

    Returns:
        Parsed timezone-aware ``datetime``.

    Raises:
        dateutil.parser.ParserError: If parsing fails.
    """
    if use_dateparser:
        dt = dateparser.parse(date)
        if dt is None:
            raise dateutil.parser.ParserError(f"Could not parse '{date}'")
    else:
        dt = dateutil.parser.parse(date, default=datetime.datetime.now())

    if dt.tzinfo is None:
        if tzinfo is None:
            tzinfo = tzlocal.get_localzone()

        dt = dt.replace(tzinfo=tzinfo)

    return dt


def parse_duration(delta: str | float) -> datetime.timedelta:
    """Parse a duration.

    The following formats are tried in order:

    - The format of ``str(datetime.timedelta)`` and Django's
      ``duration_string()``, such as ``"1 day, 2:03:04"`` or ``"1 02:03:04"``.
    - Durations understood by ``pytimeparse2``, such as ``"1h 30m"``,
      ``"1:30"``, or a number of seconds.
    - ISO 8601 durations, such as ``"P1DT2H3M4S"`` or ``"-PT1H"``.

    Args:
        delta: Duration to parse.

    Returns:
        Parsed ``timedelta``.

    Raises:
        ValueError: If ``delta`` is not in a supported format, or if it is an
            ISO 8601 duration with years or months, which have no fixed
            length.
    """
    if isinstance(delta, str):
        match = _STANDARD_DURATION_RE.fullmatch(delta)
        if match is not None:
            # Only the days carry a sign, so "-1 day, 23:00:00" is -1 hour.
            return datetime.timedelta(
                days=int(match["days"] or 0),
                hours=int(match["hours"]),
                minutes=int(match["minutes"]),
                seconds=int(match["seconds"]),
                microseconds=int((match["microseconds"] or "").ljust(6, "0")),
            )

    # Without as_timedelta, pytimeparse2 returns seconds rather than a
    # dateutil relativedelta.
    seconds = pytimeparse2.parse(delta)
    if seconds is not None:
        return datetime.timedelta(seconds=seconds)

    duration = isodate.parse_duration(str(delta))
    if isinstance(duration, datetime.timedelta):
        return duration

    raise ValueError(
        f"Duration {delta!r} has years or months, which have no fixed length"
    )
