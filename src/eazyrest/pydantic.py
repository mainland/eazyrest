"""Pydantic types that convert values as eazyrest's fields do.

Inside a Pydantic model, a ``TypedDict``, or a Pydantic dataclass, Pydantic's
own rules convert ``datetime``, ``date``, ``timedelta``, and ``UUID`` values,
and they differ from eazyrest's. For example, Pydantic reads ``"P1Y"`` as 365
days and a large number as milliseconds since the Unix epoch. Annotate a value
with one of these types to convert it as an eazyrest field of the corresponding
type would: by the conversion table of the model whose field holds it, so that
it follows that model's conversions, or by the default table outside a model.

Importing this module requires Pydantic, which the ``pydantic`` extra installs.
"""

from __future__ import annotations

import datetime
import enum
import types
import typing
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Any, Literal, Union

from pydantic import (
    PlainSerializer,
    PlainValidator,
    PydanticUserError,
    TypeAdapter,
    WithJsonSchema,
)
from pydantic_core import PydanticSerializationError

from ._conversion import _current_conversion, _final_supertype
from .eazyrest import JSONObject, lenient_issubclass

__all__ = ["UUID", "Date", "Datetime", "Duration"]


def _validator(is_value: Callable[[Any], bool], ty: Any) -> PlainValidator:
    """Return a validator that keeps values and decodes JSON values.

    A JSON value decodes by the conversion of ``ty`` in the table of the
    model whose field Pydantic converts, or by the default table outside a
    model, so these types follow a model's own conversions.

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
            if (
                isinstance(value, datetime.datetime)
                and _final_supertype(ty) is datetime.date
            ):
                # A datetime is a date, but its ISO 8601 form has a time,
                # which a date cannot read back.
                raise TypeError("Date values must be dates, got datetime")
            found = _current_conversion(ty)
            return found.entry.decode(value, found.declared)
        except TypeError as err:
            raise ValueError(str(err)) from err

    return PlainValidator(validate)


# The exceptions other than TypeError and ValueError that the encode
# functions of table conversions raise during one of eazyrest's dumps. A
# table serializer encodes only values of its type, so such an exception is a
# bug in the conversion, whatever value is being written, and propagates
# unchanged.
_ENCODE_BUGS: ContextVar[list[Exception] | None] = ContextVar(
    "_ENCODE_BUGS", default=None
)


def _serializer(is_value: Callable[[Any], bool], ty: Any) -> PlainSerializer:
    """Return a serializer that encodes only values of the type.

    A value encodes by the conversion of ``ty`` in the active table, as the
    validator decodes. Serializing another value fails, so that eazyrest
    validates the value before it writes it.

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
        bugs = _ENCODE_BUGS.get()
        if bugs:
            # A union's serializer tries its members again after a bug,
            # which no conversion should see.
            raise bugs[0]
        found = _current_conversion(ty)
        try:
            return found.entry.encode(value, found.declared)
        except (TypeError, ValueError):
            raise
        except Exception as err:
            if bugs is not None:
                bugs.append(err)
            raise

    # A model's table may write any JSON value, such as a timestamp for a
    # datetime.
    return PlainSerializer(serialize, return_type=Any)


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


def _enum_type(cls: type[enum.Enum]) -> Any:
    """Return an enum type that converts values as eazyrest does.

    Unlike Pydantic, eazyrest does not convert ``"1"`` to a member whose value
    is ``1``.

    Args:
        cls: Enum class to convert.

    Returns:
        ``cls``, annotated with a validator and a serializer that convert its
        members as eazyrest does.
    """

    def is_member(value: Any) -> bool:
        return isinstance(value, cls)

    return Annotated[
        cls,
        _validator(is_member, cls),
        _serializer(is_member, cls),
    ]


_IS_VALUE: dict[Any, Callable[[Any], bool]] = {
    datetime.datetime: _is_datetime,
    datetime.date: _is_date,
    datetime.timedelta: _is_timedelta,
    uuid.UUID: _is_uuid,
}
"""Whether a value has one of the types that this module replaces."""


def _table_type(base: type[Any], key: Any, replaced: Any) -> Any:
    """Return a type whose values convert by a table entry, as a field's do.

    Args:
        base: The type of the values, such as ``datetime.datetime`` or an
            enum.
        key: The type whose conversion in the active table converts them,
            ``base`` or a ``NewType`` of it, which converts as ``base`` if it
            has no entry of its own.
        replaced: The type that replaces ``base``, whose JSON schema the new
            type keeps.

    Returns:
        ``base``, annotated with a validator and a serializer that use the
        conversion of ``key``.
    """
    is_value = _IS_VALUE.get(base)
    if is_value is None:

        def is_value(value: Any) -> bool:
            return isinstance(value, base)

    schemas = tuple(
        metadata
        for metadata in typing.get_args(replaced)[1:]
        if not isinstance(metadata, (PlainValidator, PlainSerializer))
    )
    # Mypy targeting Python 3.10 rejects a starred tuple inside a subscript,
    # even in parentheses, so build the tuple first.
    params = (
        base,
        _validator(is_value, key),
        _serializer(is_value, key),
        *schemas,
    )
    return Annotated[params]


_EAZYREST_TYPES: dict[Any, Any] = {
    datetime.datetime: Datetime,
    datetime.date: Date,
    datetime.timedelta: Duration,
    uuid.UUID: UUID,
}


def _eazyrest_types(ty: Any) -> Any:
    """Return a type with eazyrest's conversions for the types it names.

    ``datetime``, ``date``, ``timedelta``, ``UUID``, and enum types that ``ty``
    names, also as type arguments or as the supertype of a ``NewType``, are
    replaced by types that convert as eazyrest's fields do. Types inside a
    class that ``ty`` names are left to that class.

    Args:
        ty: Field type to convert.

    Returns:
        ``ty`` with those types replaced, or ``ty`` itself if it names none of
        them.

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
    supertype = _final_supertype(ty)
    if supertype is not ty:
        # A NewType's values are its supertype's. It converts by its own
        # entry, or else as the supertype does, as a plain field of the
        # NewType would.
        replaced = _eazyrest_types(supertype)
        if replaced is supertype:
            return ty
        if isinstance(supertype, type) and (
            supertype in _IS_VALUE or issubclass(supertype, enum.Enum)
        ):
            return _table_type(supertype, ty, replaced)
        return replaced

    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if not args or origin is Literal:
        return ty
    if origin is Annotated:
        # Mypy targeting Python 3.10 rejects a starred tuple inside a
        # subscript, even in parentheses, so build the tuple first.
        params = (_eazyrest_types(args[0]), *args[1:])
        return Annotated[params]

    new_args = tuple(_eazyrest_types(arg) for arg in args)
    if all(new is old for new, old in zip(new_args, args, strict=True)):
        return ty
    if origin is types.UnionType:
        origin = Union

    return origin[new_args]


def _type_adapter(annotation: Any) -> TypeAdapter[Any]:
    """Return a ``TypeAdapter`` for a field type that Pydantic converts.

    Args:
        annotation: Field type to convert.

    Returns:
        ``TypeAdapter`` for ``annotation``, with eazyrest's conversions for the
        types it names.

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
    validated as input first, so that a field typed as ``Base64Bytes``, for
    example, writes both ``b"hello"`` and its encoding, ``"aGVsbG8="``, as
    ``"aGVsbG8="``. Validating every value first would fail for such a type,
    which would decode raw bytes as base64.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        value: Field value to encode.

    Returns:
        JSON value of ``value``.

    Raises:
        ValueError: If the value is not valid input for the field, or the field
            could not read its JSON value back, for example because the value
            breaks a constraint that only validation checks.
    """
    try:
        # A failure here, of a serializer given a value that is not yet
        # valid, means the value must be validated first. A bug in a
        # conversion propagates.
        encoded = _dump(
            adapter, value, unvalidated=True, mode="json", warnings="error"
        )
    except PydanticSerializationError:
        encoded = _dump(adapter, adapter.validate_python(value), mode="json")

    adapter.validate_python(encoded)
    return encoded


def _dump(
    adapter: TypeAdapter[Any],
    value: Any,
    *,
    unvalidated: bool = False,
    **options: Any,
) -> Any:
    """Dump a value, letting a conversion's unexpected exception propagate.

    Pydantic reports an exception from a serializer as a
    ``PydanticSerializationError``, a ``ValueError``, usually with the
    exception as its cause. A conversion reports a value that it cannot
    convert with a ``TypeError`` or ``ValueError``, and any other exception
    from its ``encode`` is a bug in it, which propagates unchanged, as it does
    for a field without Pydantic, even when a union's serializer then writes
    the value by another member.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        value: Value to dump.
        unvalidated: Whether ``value`` may not be valid yet, so that an
            exception from a serializer other than a table conversion's
            reports the value rather than a bug.
        **options: Keyword arguments for ``TypeAdapter.dump_python()``.

    Returns:
        The dump.

    Raises:
        PydanticSerializationError: If serializing ``value`` fails because
            of the value.
    """
    bugs: list[Exception] = []
    token = _ENCODE_BUGS.set(bugs)
    try:
        result = adapter.dump_python(value, **options)
    except PydanticSerializationError as err:
        # A union's serializer may try another member after a conversion's
        # bug, and report its own failure without the bug as the cause.
        if bugs:
            raise bugs[0] from None
        cause = err.__cause__
        while isinstance(cause, PydanticSerializationError):
            cause = cause.__cause__
        if (
            not unvalidated
            and cause is not None
            and not isinstance(cause, (TypeError, ValueError))
        ):
            raise cause from None
        raise
    finally:
        _ENCODE_BUGS.reset(token)
    if bugs:
        # Another member of a union wrote the value after the bug.
        raise bugs[0]
    return result
