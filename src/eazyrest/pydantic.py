"""Pydantic types that convert values as eazyrest's fields do.

Inside a Pydantic model, a ``TypedDict``, or a Pydantic dataclass,
Pydantic's own rules convert ``datetime``, ``date``, ``timedelta``, and
``UUID`` values, and they differ from eazyrest's. For example, Pydantic
reads ``"P1Y"`` as 365 days and a large number as milliseconds since the
Unix epoch. Annotate a value with one of these types to convert it as an
eazyrest field of the corresponding type would.

Importing this module requires Pydantic, which the ``pydantic`` extra
installs.
"""

from __future__ import annotations

import datetime
import enum
import types
import typing
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, Union

import isodate
from pydantic import (
    PlainSerializer,
    PlainValidator,
    PydanticUserError,
    TypeAdapter,
    WithJsonSchema,
)
from pydantic_core import PydanticSerializationError

from .dateparse import parse_duration
from .eazyrest import (
    JSONObject,
    _decode_datetime,
    _decode_uuid,
    lenient_issubclass,
)

__all__ = ["UUID", "Date", "Datetime", "Duration"]


def _validator(
    is_value: Callable[[Any], bool], decode: Callable[[Any], Any]
) -> PlainValidator:
    """Return a validator that keeps values and decodes JSON values.

    Pydantic reports a ``ValueError`` from a validator as a validation
    error but lets a ``TypeError`` escape, so a ``TypeError`` from
    ``decode`` is raised as a ``ValueError``.
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
    is_value: Callable[[Any], bool],
    encode: Callable[[Any], Any],
    return_type: Any = str,
) -> PlainSerializer:
    """Return a serializer that encodes only values of the type.

    Serializing another value fails, so that eazyrest validates the
    value before it writes it.
    """

    def serialize(value: Any) -> Any:
        if not is_value(value):
            raise TypeError(f"Unexpected value of type {type(value).__name__}")

        return encode(value)

    return PlainSerializer(serialize, return_type=return_type)


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
    _validator(_is_datetime, _decode_datetime),
    _serializer(_is_datetime, datetime.datetime.isoformat),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""A ``datetime`` read from ISO 8601 text or a Unix timestamp in seconds.

A UTC offset becomes a ``datetime.timezone``. The value is written in
ISO 8601.
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


def _enum_type(cls: type[enum.Enum]) -> Any:
    """Return an enum type that converts values as eazyrest does.

    Unlike Pydantic, eazyrest does not convert ``"1"`` to a member whose
    value is ``1``.
    """

    def is_member(value: Any) -> bool:
        return isinstance(value, cls)

    return Annotated[
        cls,
        _validator(is_member, cls),
        _serializer(is_member, lambda member: member.value, Any),
    ]


_EAZYREST_TYPES: dict[Any, Any] = {
    datetime.datetime: Datetime,
    datetime.date: Date,
    datetime.timedelta: Duration,
    uuid.UUID: UUID,
}


def _eazyrest_types(ty: Any) -> Any:
    """Return a type with eazyrest's conversions for the types it names.

    ``datetime``, ``date``, ``timedelta``, ``UUID``, and enum types that
    ``ty`` names, also as type arguments, are replaced by types that
    convert as eazyrest's fields do. Types inside a class that ``ty``
    names are left to that class.

    Raises:
        TypeError: If ``ty`` names an eazyrest model, which Pydantic cannot
            convert.
    """
    if lenient_issubclass(ty, JSONObject):
        raise TypeError(
            f"Pydantic cannot convert {ty.__name__}, an eazyrest model"
        )
    if isinstance(ty, type) and ty in _EAZYREST_TYPES:
        return _EAZYREST_TYPES[ty]
    if lenient_issubclass(ty, enum.Enum):
        return _enum_type(ty)

    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if not args or origin is Literal:
        return ty
    if origin is Annotated:
        return Annotated[(_eazyrest_types(args[0]),) + args[1:]]

    new_args = tuple(_eazyrest_types(arg) for arg in args)
    if all(new is old for new, old in zip(new_args, args, strict=True)):
        return ty
    if origin is types.UnionType:
        origin = Union

    return origin[new_args]


def _type_adapter(annotation: Any) -> TypeAdapter[Any]:
    """Return a ``TypeAdapter`` for a field type that Pydantic converts.

    Raises:
        TypeError: If Pydantic cannot convert the type.
    """
    try:
        return TypeAdapter(_eazyrest_types(annotation))
    except PydanticUserError as err:
        # A PydanticUserError is a TypeError in some Pydantic releases and a
        # RuntimeError in others.
        raise TypeError(str(err)) from err


def _encode(adapter: TypeAdapter[Any], value: Any) -> Any:
    """Return the JSON value of a field value.

    A value of the field's type is serialized as it is. Otherwise, it is
    validated as input first, so that a field typed as ``Base64Bytes``,
    for example, writes both ``b"hello"`` and its encoding,
    ``"aGVsbG8="``, as ``"aGVsbG8="``. Validating every value first would
    fail for such a type, which would decode raw bytes as base64.

    Raises:
        ValueError: If the value is not valid input for the field, or the
            field could not read its JSON value back, for example because
            the value breaks a constraint that only validation checks.
    """
    try:
        encoded = adapter.dump_python(value, mode="json", warnings="error")
    except PydanticSerializationError:
        encoded = adapter.dump_python(
            adapter.validate_python(value), mode="json"
        )

    adapter.validate_python(encoded)
    return encoded
