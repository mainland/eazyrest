"""Date and duration parsing helpers used by eazyrest."""

import datetime
import re

import dateutil.parser
import isodate
import tzlocal

# A number of seconds, such as "90", "-90", or "1.5".
_SECONDS_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")

# The format of str(datetime.timedelta) and Django's duration_string(), for
# example "1 day, 2:03:04", "-1 day, 23:00:00", or "1 02:03:04.000005". Hours
# are optional, so "1:30" is one minute and thirty seconds, as in Django.
_CLOCK_RE = re.compile(
    r"(?:(?P<days>-?\d+) (?:days?, )?)?"
    r"(?:(?P<hours>\d+):(?=\d\d:))?(?P<minutes>\d+):(?P<seconds>\d\d)"
    r"(?:\.(?P<microseconds>\d{1,6}))?"
)

# An optional sign followed by terms such as "1h", "30 m", or "1.5 hours,".
_SIGN_RE = re.compile(r"(?P<sign>[+-]?)\s*")
_TERM_RE = re.compile(
    r"(?P<number>\d+(?:\.\d*)?|\.\d+)\s*(?P<unit>[a-zµμ]+)\s*,?\s*",
    re.IGNORECASE,
)

# Units of duration terms, keyed by lowercase name, mapped to the timedelta
# arguments that they set. Go writes microseconds with U+00B5 MICRO SIGN and
# also accepts U+03BC GREEK SMALL LETTER MU.
_UNITS = {
    name: unit
    for unit, names in [
        ("weeks", "w wk wks week weeks"),
        ("days", "d day days"),
        ("hours", "h hr hrs hour hours"),
        ("minutes", "m min mins minute minutes"),
        ("seconds", "s sec secs second seconds"),
        ("milliseconds", "ms msec msecs millisecond milliseconds"),
        (
            "microseconds",
            "us \u00b5s \u03bcs usec usecs microsecond microseconds",
        ),
    ]
    for name in names.split()
}

# Units recognized only to reject them, because they have no fixed length.
_CALENDAR_UNITS = frozenset(
    {"y", "yr", "yrs", "year", "years"}
    | {"mo", "mos", "mth", "mths", "month", "months"}
)


def parse_datetime(
    date: str,
    use_dateparser: bool = False,
    tzinfo: datetime.tzinfo | None = None,
) -> datetime.datetime:
    """Parse a date/time string.

    Args:
        date: Date/time string to parse.
        use_dateparser: If ``True``, parse with ``dateparser``, which also
            accepts natural language such as ``"2 days ago"`` and requires
            the ``eazyrest[dateparser]`` extra. Otherwise use
            ``dateutil.parser.parse``.
        tzinfo: Time zone to apply when the parsed datetime is naive. If
            omitted, the local time zone is used.

    Returns:
        Parsed timezone-aware ``datetime``.

    Raises:
        dateutil.parser.ParserError: If parsing fails.
        ModuleNotFoundError: If ``use_dateparser`` is ``True`` and
            ``dateparser`` is not installed.
    """
    if use_dateparser:
        try:
            # dateparser is an optional dependency.
            import dateparser
        except ModuleNotFoundError as err:
            raise ModuleNotFoundError(
                "parse_datetime(use_dateparser=True) requires dateparser. "
                'Install it with: pip install "eazyrest[dateparser]"',
                name="dateparser",
            ) from err

        dt = dateparser.parse(date)
        if dt is None:
            raise dateutil.parser.ParserError(f"Could not parse '{date}'")
    else:
        dt = dateutil.parser.parse(date)

    if dt.tzinfo is None:
        if tzinfo is None:
            tzinfo = tzlocal.get_localzone()

        dt = dt.replace(tzinfo=tzinfo)

    return dt


def _calendar_units_error(delta: str) -> ValueError:
    """Return the error for a duration with years or months."""
    return ValueError(
        f"Duration {delta!r} has years or months, which have no fixed length"
    )


def _parse_terms(text: str) -> datetime.timedelta | None:
    """Parse a duration written as terms with units, such as ``"1h 30m"``.

    Args:
        text: Duration without surrounding whitespace.

    Returns:
        Parsed ``timedelta``, or ``None`` if ``text`` is not a sequence of
        terms with units.

    Raises:
        ValueError: If a term has an unknown unit, years, or months.
    """
    sign_match = _SIGN_RE.match(text)
    assert sign_match is not None
    pos = sign_match.end()

    total = datetime.timedelta()
    while pos < len(text):
        match = _TERM_RE.match(text, pos)
        if match is None:
            return None

        unit = match["unit"].lower()
        if unit in _CALENDAR_UNITS:
            raise _calendar_units_error(text)
        if unit not in _UNITS:
            raise ValueError(
                f"Duration {text!r} has an unknown unit {match['unit']!r}"
            )

        total += datetime.timedelta(**{_UNITS[unit]: float(match["number"])})
        pos = match.end()

    if pos == sign_match.end():
        return None

    # The sign applies to the whole duration, so "-1h30m" is -90 minutes.
    return -total if sign_match["sign"] == "-" else total


def parse_duration(delta: str | float) -> datetime.timedelta:
    """Parse a duration.

    A number, or a string that holds only a number, is a number of seconds.
    Other strings may have one of these forms:

    - The format of ``str(datetime.timedelta)`` and Django's
      ``duration_string()``, such as ``"1 day, 2:03:04"`` or
      ``"1 02:03:04"``, or minutes and seconds, such as ``"1:30"``.
    - Terms with units, such as ``"1h 30m"``, ``"1.5 hours"``,
      ``"2 weeks"``, or Go's ``"1h30m0s"``. The units are weeks (``w``),
      days (``d``), hours (``h``), minutes (``m``), seconds (``s``),
      milliseconds (``ms``), and microseconds (``us``), also written as
      words such as ``hours``. A leading sign applies to the whole
      duration.
    - ISO 8601 durations, such as ``"P1DT2H3M4S"`` or ``"-PT1H"``.

    Durations with years or months are rejected in every form, because
    those units have no fixed length. A day is 24 hours, as in
    ``datetime.timedelta``.

    Args:
        delta: Duration to parse.

    Returns:
        Parsed ``timedelta``.

    Raises:
        TypeError: If ``delta`` is not a string or a number.
        ValueError: If ``delta`` is not in a supported form, has an unknown
            unit, or has years or months.
    """
    if isinstance(delta, bool) or not isinstance(delta, (str, int, float)):
        raise TypeError(
            "Duration must be a string or a number, "
            f"not {type(delta).__name__}"
        )

    if not isinstance(delta, str):
        return datetime.timedelta(seconds=delta)

    text = delta.strip()
    if _SECONDS_RE.fullmatch(text):
        return datetime.timedelta(seconds=float(text))

    match = _CLOCK_RE.fullmatch(text)
    if match is not None:
        # Only the days carry a sign, so "-1 day, 23:00:00" is -1 hour.
        return datetime.timedelta(
            days=int(match["days"] or 0),
            hours=int(match["hours"] or 0),
            minutes=int(match["minutes"]),
            seconds=int(match["seconds"]),
            microseconds=int((match["microseconds"] or "").ljust(6, "0")),
        )

    duration = _parse_terms(text)
    if duration is not None:
        return duration

    if re.fullmatch(r"[+-]?P.*", text):
        iso_duration = isodate.parse_duration(text)
        if not isinstance(iso_duration, datetime.timedelta):
            raise _calendar_units_error(text)

        return iso_duration

    raise ValueError(f"Unsupported duration format: {delta!r}")
