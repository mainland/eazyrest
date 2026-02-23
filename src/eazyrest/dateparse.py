"""Date and duration parsing helpers used by eazyrest."""


import datetime
from typing import Optional

import dateparser
import dateutil.parser
import isodate
import pytimeparse2
import tzlocal

def parse_datetime(date: str, use_dateparser: bool = True, tzinfo: Optional[datetime.tzinfo] = None) -> datetime.datetime:
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

        dt = dt.replace(tzinfo=tzlocal.get_localzone())

    return dt

def parse_duration(delta: str) -> datetime.timedelta:
    """Parse a duration string.

    Args:
        delta: Duration string to parse.

    Returns:
        Parsed ``timedelta``.
    """
    pytimeparse2.disable_dateutil()

    timedelta = pytimeparse2.parse(delta, as_timedelta=True)
    if timedelta is not None:
        assert(isinstance(timedelta, datetime.timedelta))
        return timedelta

    return isodate.parse_duration(delta)
