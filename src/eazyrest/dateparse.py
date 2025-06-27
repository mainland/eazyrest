
import datetime

import isodate
import pytimeparse2

def parse_timedelta(delta: str) -> datetime.timedelta:
    """Parse a duration.

    Args:
        date (str): A date to parse

    Returns:
        datetime.timedelta: Parsed timedelta object
    """
    pytimeparse2.disable_dateutil()

    td = pytimeparse2.parse(delta, as_timedelta=True)
    if td is not None:
        return td

    return isodate.parse_duration(delta)
