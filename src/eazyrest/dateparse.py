
import datetime
from typing import Optional

import dateparser
import dateutil.parser
import isodate
import pytimeparse2
import tzlocal

def parse_datetime(date: str, use_dateparser: bool=True, tzinfo: Optional[datetime.tzinfo]=None) -> datetime.datetime:
    """Parse a date/time string.

    Args:
        date (str): A date to parse.
        dt = dateparser.parse(date)
        use_dateparser (bool, optional): If True, use the  module. Defaults to True.
        tzinfo (Optional[datetime.tzinfo], optional): Time zone to use for unaware datetime objects. Defaults to None.

    Returns:
        Optional[datetime.datetime]: Parsed datetime or None if parse failed.
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
        date (str): A duration to parse

    Returns:
        datetime.timedelta: Parsed duration.
    """
    pytimeparse2.disable_dateutil()

    timedelta = pytimeparse2.parse(delta, as_timedelta=True)
    if timedelta is not None:
        assert(isinstance(timedelta, datetime.timedelta))
        return timedelta

    return isodate.parse_duration(delta)
