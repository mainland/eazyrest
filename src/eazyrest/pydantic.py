"""Pydantic types that convert values as eazyrest's fields do.

Inside a Pydantic model, a ``TypedDict``, or a Pydantic dataclass, Pydantic's
own rules convert ``datetime``, ``date``, ``timedelta``, and ``UUID`` values,
and they differ from eazyrest's. For example, Pydantic reads ``"P1Y"`` as 365
days and a large number as milliseconds since the Unix epoch. Annotate a value
with one of these types to convert it as an eazyrest field of the corresponding
type would.

Importing this module requires Pydantic, which the ``pydantic`` extra installs.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from typing import Annotated, Any

import isodate
from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from .dateparse import parse_datetime, parse_duration
from .eazyrest import _decode_uuid

__all__ = ["UUID", "Date", "Datetime", "Duration"]


def _validator(
    is_value: Callable[[Any], bool], decode: Callable[[Any], Any]
) -> PlainValidator:
    """Return a validator that keeps values and decodes JSON values.

    Pydantic reports a ``ValueError`` from a validator as a validation error
    but lets a ``TypeError`` escape, so a ``TypeError`` from ``decode`` is
    raised as a ``ValueError``.

    Args:
        is_value: Whether a value already has the type, so that the validator
            keeps it as it is.
        decode: Function that converts a JSON value to a value of the type.

    Returns:
        Pydantic validator that keeps values for which ``is_value`` is true and
        decodes others with ``decode``.
    """

    def validate(value: Any) -> Any:
        if is_value(value):
            return value

        try:
            return decode(value)
        except TypeError as err:
            raise ValueError(str(err)) from err

    return PlainValidator(validate)


def _serializer(
    is_value: Callable[[Any], bool], encode: Callable[[Any], str]
) -> PlainSerializer:
    """Return a serializer that encodes only values of the type.

    Serializing another value fails, so that eazyrest validates the value
    before it writes it.

    Args:
        is_value: Whether a value has the type, so that the serializer encodes
            it.
        encode: Function that converts a value of the type to its JSON string.

    Returns:
        Pydantic serializer that encodes values with ``encode``, and raises
        ``TypeError`` for values for which ``is_value`` is false.
    """

    def serialize(value: Any) -> str:
        if not is_value(value):
            raise TypeError(f"Unexpected value of type {type(value).__name__}")

        return encode(value)

    return PlainSerializer(serialize, return_type=str)


def _is_datetime(value: Any) -> bool:
    return isinstance(value, datetime.datetime)


def _is_date(value: Any) -> bool:
    # A datetime is a date, but its ISO 8601 form has a time, which a date
    # cannot read back.
    return isinstance(value, datetime.date) and not _is_datetime(value)


def _decode_date(value: Any) -> datetime.date:
    if _is_datetime(value):
        raise TypeError("Date values must be dates, got datetime")

    return datetime.date.fromisoformat(value)


def _is_timedelta(value: Any) -> bool:
    return isinstance(value, datetime.timedelta)


def _is_uuid(value: Any) -> bool:
    return isinstance(value, uuid.UUID)


Datetime = Annotated[
    datetime.datetime,
    _validator(_is_datetime, parse_datetime),
    _serializer(_is_datetime, datetime.datetime.isoformat),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""A ``datetime`` read from ISO 8601 text or a Unix timestamp in seconds.

A UTC offset becomes a ``datetime.timezone``. The value is written in ISO 8601.
"""

Date = Annotated[
    datetime.date,
    _validator(_is_date, _decode_date),
    _serializer(_is_date, datetime.date.isoformat),
    WithJsonSchema({"type": "string", "format": "date"}),
]
"""A ``date`` read from and written in ISO 8601.

A ``datetime`` is rejected.
"""

Duration = Annotated[
    datetime.timedelta,
    _validator(_is_timedelta, parse_duration),
    _serializer(_is_timedelta, isodate.duration_isoformat),
    WithJsonSchema({"type": "string", "format": "duration"}),
]
"""A ``timedelta`` read by ``parse_duration()`` and written in ISO 8601.

Durations with years or months are rejected.
"""

UUID = Annotated[
    uuid.UUID,
    _validator(_is_uuid, _decode_uuid),
    _serializer(_is_uuid, str),
    WithJsonSchema({"type": "string", "format": "uuid"}),
]
"""A ``UUID`` read from and written as a string."""
