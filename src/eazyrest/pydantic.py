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

from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from ._conversion import _conversion_of

__all__ = ["UUID", "Date", "Datetime", "Duration"]


def _validator(is_value: Callable[[Any], bool], ty: Any) -> PlainValidator:
    """Return a validator that keeps values and decodes JSON values.

    A JSON value decodes by the default conversion of ``ty``, as an eazyrest
    field of that type reads it.

    Pydantic reports a ``ValueError`` from a validator as a validation error
    but lets a ``TypeError`` escape, so a ``TypeError`` from the conversion is
    raised as a ``ValueError``.

    Args:
        is_value: Whether a value already has the type, so that the validator
            keeps it as it is.
        ty: The type, which has a default conversion.

    Returns:
        Pydantic validator that keeps values for which ``is_value`` is true and
        decodes others.
    """

    def validate(value: Any) -> Any:
        if is_value(value):
            return value
        try:
            if isinstance(value, datetime.datetime) and ty is datetime.date:
                # A datetime is a date, but its ISO 8601 form has a time,
                # which a date cannot read back.
                raise TypeError("Date values must be dates, got datetime")
            found = _conversion_of(ty)
            assert found is not None, ty
            return found.entry.decode(value, found.declared)
        except TypeError as err:
            raise ValueError(str(err)) from err

    return PlainValidator(validate)


def _serializer(is_value: Callable[[Any], bool], ty: Any) -> PlainSerializer:
    """Return a serializer that encodes only values of the type.

    A value encodes by the default conversion of ``ty``, as the validator
    decodes. Serializing another value fails, so that eazyrest validates the
    value before it writes it.

    Args:
        is_value: Whether a value has the type, so that the serializer encodes
            it.
        ty: The type, which has a default conversion.

    Returns:
        Pydantic serializer that encodes values for which ``is_value`` is
        true, and raises ``TypeError`` for others.
    """

    def serialize(value: Any) -> Any:
        if not is_value(value):
            raise TypeError(f"Unexpected value of type {type(value).__name__}")
        found = _conversion_of(ty)
        assert found is not None, ty
        return found.entry.encode(value, found.declared)

    return PlainSerializer(serialize, return_type=str)


def _is_datetime(value: Any) -> bool:
    return isinstance(value, datetime.datetime)


def _is_date(value: Any) -> bool:
    # A datetime is a date, but its ISO 8601 form has a time, which a date
    # cannot read back.
    return isinstance(value, datetime.date) and not _is_datetime(value)


def _is_timedelta(value: Any) -> bool:
    return isinstance(value, datetime.timedelta)


def _is_uuid(value: Any) -> bool:
    return isinstance(value, uuid.UUID)


Datetime = Annotated[
    datetime.datetime,
    _validator(_is_datetime, datetime.datetime),
    _serializer(_is_datetime, datetime.datetime),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""A ``datetime`` read from ISO 8601 text or a Unix timestamp in seconds.

A UTC offset becomes a ``datetime.timezone``. The value is written in ISO 8601.
"""

Date = Annotated[
    datetime.date,
    _validator(_is_date, datetime.date),
    _serializer(_is_date, datetime.date),
    WithJsonSchema({"type": "string", "format": "date"}),
]
"""A ``date`` read from and written in ISO 8601.

A ``datetime`` is rejected.
"""

Duration = Annotated[
    datetime.timedelta,
    _validator(_is_timedelta, datetime.timedelta),
    _serializer(_is_timedelta, datetime.timedelta),
    WithJsonSchema({"type": "string", "format": "duration"}),
]
"""A ``timedelta`` read by ``parse_duration()`` and written in ISO 8601.

Durations with years or months are rejected.
"""

UUID = Annotated[
    uuid.UUID,
    _validator(_is_uuid, uuid.UUID),
    _serializer(_is_uuid, uuid.UUID),
    WithJsonSchema({"type": "string", "format": "uuid"}),
]
"""A ``UUID`` read from and written as a string."""
