"""Conversions between Python values and their JSON forms.

A conversion describes one Python type's JSON form: how to read the type
from JSON, how to write it, and its JSON schema. The default table holds
eazyrest's conversions for ``datetime``, ``date``, ``time``, ``timedelta``,
``UUID``, ``Decimal``, and enums. JSON scalars need none.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import datetime
import decimal
import enum
import re
import typing
import uuid
import weakref
from collections.abc import (
    Callable,
    Generator,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, NamedTuple, cast

import isodate

from .dateparse import parse_datetime, parse_duration

_ONE_MINUTE = datetime.timedelta(minutes=1)
"""The unit of an ISO 8601 UTC offset."""


@dataclass(frozen=True, kw_only=True)
class Conversion:
    """How to read and write one Python type's JSON form.

    Each function receives the declared type as well as the value, so that
    one conversion serves a family of types, such as every enum. The fields
    are keyword arguments, so that later versions can add fields.

    ``decode`` and ``encode`` report a value that they cannot convert by
    raising ``TypeError`` or ``ValueError``, which reaches the caller as a
    ``FieldConversionError`` that names the field, with the error as its
    cause. Their messages should not quote the value, which may be a secret,
    as the default conversions' messages do not. Any other exception is a
    bug in the conversion, and propagates unchanged.
    """

    decode: Callable[[Any, type[Any]], Any]
    """Read a JSON value, other than ``null``, as a value of the declared
    type. ``null`` reads as ``None`` for every type, without a conversion."""

    encode: Callable[[Any, type[Any]], Any]
    """Write a value of the declared type as JSON. It is called only for
    instances of the type that the entry is keyed by, so a plain
    ``datetime`` read from a field declared as a subclass of ``datetime``
    writes back. Any other value, such as a string assigned to a
    ``datetime`` field, is written as it is."""

    schema: (
        Mapping[str, Any]
        | Callable[[type[Any], Literal["read", "write"]], Mapping[str, Any]]
    ) = field(default_factory=lambda: MappingProxyType({}))
    """The JSON schema of the JSON form, for ``json_schema()``, or a function
    of the declared type and the mode, ``"read"`` or ``"write"``, that
    returns it. The default, an empty schema, allows any value.

    ``json_schema()`` embeds the schema in a model's, and moves its
    ``$defs`` into the model's, so its references may name only its own
    definitions, as ``#/$defs/Name`` or a pointer into one. A schema with
    another reference, such as ``#`` for a schema that refers to itself, or
    with ``$id``, an anchor, or ``$defs`` below its root, raises
    ``TypeError``, when the conversion is made or, for a function, when
    ``json_schema()`` calls it. Put such a schema in ``$defs`` and refer to
    it there."""

    def __post_init__(self) -> None:
        """Check a schema given as a mapping.

        Raises:
            TypeError: If the schema could not be embedded in a model's.
        """
        if not callable(self.schema):
            _check_embeddable(self.schema)


# Keywords whose values are JSON data rather than schemas, and keywords that
# a schema embedded in a model's cannot keep, since they name its root or
# its resource.
_SCHEMA_DATA = frozenset({"const", "enum", "default", "examples"})
_UNEMBEDDABLE = frozenset(
    {
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveAnchor",
        "$recursiveRef",
    }
)


def _check_embeddable(schema: Any, *, root: bool = True) -> None:
    """Check that a conversion's schema can be embedded in a model's.

    Args:
        schema: The schema, or part of it.
        root: Whether ``schema`` is the whole schema.

    Raises:
        TypeError: If the schema has ``$id``, an anchor, ``$defs`` below its
            root, or a reference other than to one of its own definitions.
    """
    if isinstance(schema, list):
        for item in schema:
            _check_embeddable(item, root=False)
        return
    if not isinstance(schema, Mapping):
        return
    for key, value in schema.items():
        if key in _SCHEMA_DATA:
            continue
        if key in _UNEMBEDDABLE:
            raise TypeError(
                f"A conversion's schema with {key} cannot be embedded in a "
                "model's schema"
            )
        if key == "$defs" and not root:
            raise TypeError(
                "A conversion's schema may have $defs only at its root"
            )
        if key == "$ref" and not (
            isinstance(value, str) and value.startswith("#/$defs/")
        ):
            raise TypeError(
                f"A conversion's schema may refer only to its own $defs, as "
                f"#/$defs/Name, not to {value!r}"
            )
        if key in (
            "properties",
            "patternProperties",
            "$defs",
            "dependentSchemas",
        ):
            if isinstance(value, Mapping):
                for item in value.values():
                    _check_embeddable(item, root=False)
            continue
        _check_embeddable(value, root=False)


def _decode_uuid(value: Any) -> uuid.UUID:
    """Decode a JSON UUID string.

    Args:
        value: UUID in any form that ``uuid.UUID`` accepts, such as
            ``"6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40"``.

    Returns:
        Decoded ``UUID``.

    Raises:
        TypeError: If ``value`` is not a string.
        ValueError: If ``value`` is not a UUID. The message does not quote
            ``value``, as Python's may.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"UUID values must be strings, got {type(value).__name__}"
        )

    try:
        return uuid.UUID(value)
    except ValueError as err:
        raise ValueError("UUID is not valid") from err


def _decode_date(value: Any) -> datetime.date:
    """Decode a JSON ISO 8601 date string.

    Args:
        value: ISO 8601 date, such as ``"2024-04-01"``.

    Returns:
        Decoded ``date``.

    Raises:
        TypeError: If ``value`` is not a string.
        ValueError: If ``value`` is not an ISO 8601 date. The message does not
            quote ``value``.
    """
    if (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None
    ):
        raise ValueError("Date is not in ISO 8601 format (YYYY-MM-DD)")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as err:
        # Python's message names a field out of range, and from Python 3.14
        # its value.
        raise ValueError(
            "Date has a year, month, or day out of range"
        ) from err


_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
"""A decimal number as JSON and Django REST Framework write it."""


_MAX_DECIMAL_EXPONENT = 4300
"""The largest exponent, in either direction, of a decimal that converts.

A decimal is written in positional notation, so ``"1E+20000000"``, 11
bytes, would write 20 MB. The bound is the number of digits that Python
converts between ``int`` and ``str`` by default."""


def _check_decimal_exponent(value: decimal.Decimal) -> decimal.Decimal:
    """Check that a decimal's positional form has a bounded length.

    Args:
        value: Finite decimal.

    Returns:
        ``value``.

    Raises:
        ValueError: If ``value``'s exponent is out of range. The message
            does not quote ``value``.
    """
    # A zero with a positive exponent writes one digit, but a zero with a
    # negative one writes each zero after the point, as "0e-5" writes
    # "0.00000".
    adjusted = value.adjusted()
    if adjusted < -_MAX_DECIMAL_EXPONENT or (
        value and adjusted > _MAX_DECIMAL_EXPONENT
    ):
        raise ValueError(
            "Decimal has an exponent beyond "
            f"+/-{_MAX_DECIMAL_EXPONENT}, whose positional form is too long"
        )
    return value


def _decode_decimal(value: Any) -> decimal.Decimal:
    """Decode a JSON decimal, written as a string or a number.

    Django REST Framework writes a ``DecimalField`` as a string, such as
    ``"1.10"``, which keeps its digits, or as a number when its
    ``COERCE_DECIMAL_TO_STRING`` setting is false.

    Args:
        value: Decimal number as a string, or a JSON number.

    Returns:
        Decoded ``Decimal``. A float converts from its shortest decimal form,
        so ``0.1`` reads as ``Decimal("0.1")``.

    Raises:
        TypeError: If ``value`` is neither a string nor a number. A boolean is
            not a number.
        ValueError: If a string is not a decimal number, or its exponent is
            beyond 4300 in either direction. The message does not quote
            ``value``.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(
            "Decimal values must be strings or numbers, got "
            f"{type(value).__name__}"
        )
    if isinstance(value, str):
        # Decimal() also reads spaces, underscores, "NaN", and "Infinity".
        if _DECIMAL.fullmatch(value) is None:
            raise ValueError("Decimal is not a decimal number")
        try:
            parsed = decimal.Decimal(value)
        except decimal.InvalidOperation:
            # Decimal() refuses an exponent beyond its own limits.
            raise ValueError(
                "Decimal has an exponent beyond "
                f"+/-{_MAX_DECIMAL_EXPONENT}, whose positional form is too "
                "long"
            ) from None
        return _check_decimal_exponent(parsed)
    if isinstance(value, float):
        return _check_decimal_exponent(decimal.Decimal(repr(value)))
    return _check_decimal_exponent(decimal.Decimal(value))


def _encode_decimal(value: decimal.Decimal) -> str:
    """Encode a decimal as a JSON string, as Django REST Framework does.

    Args:
        value: Decimal to encode.

    Returns:
        ``value`` in positional notation, with its digits, such as ``"1.10"``
        or ``"100"`` for ``Decimal("1E+2")``.

    Raises:
        ValueError: If ``value`` is not finite, or its exponent is beyond 4300
            in either direction.
    """
    if not value.is_finite():
        raise ValueError("Decimal field values must be finite")
    return format(_check_decimal_exponent(value), "f")


_TIME = re.compile(
    r"([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,6}))?)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?"
)
"""An ISO 8601 time of day, with optional seconds, fraction, and offset."""


def _decode_time(value: Any) -> datetime.time:
    """Decode a JSON ISO 8601 time string.

    Python's ``time.fromisoformat()`` reads more forms on Python 3.11 than on
    3.10, so the format is checked here, the same on every version.

    Args:
        value: ISO 8601 time, such as ``"09:30"``, ``"09:30:00.5"``, or
            ``"09:30:00+02:00"``.

    Returns:
        Decoded ``time``. A UTC offset becomes a ``datetime.timezone``.

    Raises:
        TypeError: If ``value`` is not a string.
        ValueError: If ``value`` is not an ISO 8601 time. The message does not
            quote ``value``.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"Time values must be strings, got {type(value).__name__}"
        )
    match = _TIME.fullmatch(value)
    if match is None:
        raise ValueError(
            "Time is not in ISO 8601 format (HH:MM[:SS[.ffffff]][+HH:MM])"
        )

    hour, minute, second, fraction, offset = match.groups()
    tzinfo: datetime.tzinfo | None = None
    if offset == "Z":
        tzinfo = datetime.timezone.utc
    elif offset is not None:
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError("Time has a UTC offset out of range")
        sign = -1 if offset[0] == "-" else 1
        tzinfo = datetime.timezone(
            sign * datetime.timedelta(hours=hours, minutes=minutes)
        )
    try:
        return datetime.time(
            int(hour),
            int(minute),
            int(second or 0),
            int((fraction or "0").ljust(6, "0")),
            tzinfo=tzinfo,
        )
    except ValueError as err:
        # Python's message names the field, and from Python 3.14 its value.
        raise ValueError(
            "Time has an hour, minute, or second out of range"
        ) from err


def _encode_time(value: datetime.time) -> str:
    """Encode a time as an ISO 8601 string.

    Args:
        value: Time to encode.

    Returns:
        ``value`` in ISO 8601, such as ``"09:30:00"``.

    Raises:
        ValueError: If ``value`` has a UTC offset that is not a whole number of
            minutes, which ISO 8601 cannot write.
    """
    offset = value.utcoffset()
    if offset is not None and offset % _ONE_MINUTE:
        raise ValueError(
            "Time field values must have a UTC offset of whole minutes"
        )
    return value.isoformat()


def _decode_enum(cls: type[enum.Enum], value: Any) -> enum.Enum:
    """Decode a JSON value as a member of an enum.

    Args:
        cls: Enum class to decode to.
        value: Value of a member of ``cls``.

    Returns:
        The member of ``cls`` whose value is ``value``.

    Raises:
        ValueError: If ``cls`` has no such member. The message does not quote
            ``value``.
    """
    try:
        return cls(value)
    except ValueError as err:
        # Python quotes the value that the enum lacks.
        if repr(value) in str(err):
            raise ValueError(f"Value is not a valid {cls.__name__}") from err

        raise


def _decode_enum_value(value: Any, cls: type[Any]) -> enum.Enum:
    """Decode a JSON value as a member of the declared enum.

    Args:
        value: Value of a member of ``cls``.
        cls: The field's enum type.

    Returns:
        The member of ``cls`` whose value is ``value``.
    """
    return _decode_enum(cls, value)


def _encode_datetime(value: Any) -> Any:
    """Encode a ``datetime`` as ISO 8601.

    Args:
        value: Value of a ``datetime`` field.

    Returns:
        The ISO 8601 text.

    Raises:
        ValueError: If the UTC offset is not a whole number of minutes.
    """
    # isoformat() writes an offset such as +00:19:32, which a zone gives some
    # old dates, but ISO 8601 offsets are whole minutes, so neither the field
    # nor a server reads it.
    offset = value.utcoffset()
    if offset is not None and offset % _ONE_MINUTE:
        raise ValueError(
            "Datetime field values must have a UTC offset of whole minutes"
        )
    return value.isoformat()


def _encode_date(value: Any) -> Any:
    """Encode a ``date`` as an ISO 8601 date.

    Args:
        value: Value of a ``date`` field.

    Returns:
        The ISO 8601 text.

    Raises:
        TypeError: If ``value`` is a ``datetime``, whose ISO 8601 form has a
            time, which a date field cannot read back.
    """
    if isinstance(value, datetime.datetime):
        raise TypeError("Date field values must be dates, got datetime")
    return value.isoformat()


def _encode_enum(value: Any, cls: type[Any]) -> Any:
    """Encode an enum member as its value.

    Args:
        value: Member of ``cls``.
        cls: The field's enum type.

    Returns:
        The member's value.
    """
    del cls
    return value.value


def _string(form: str) -> Mapping[str, Any]:
    """Return the schema of a string in a JSON schema format.

    Args:
        form: The format, such as ``"date-time"``.

    Returns:
        The schema.
    """
    return MappingProxyType({"type": "string", "format": form})


def _decimal_schema(cls: type[Any], mode: str) -> Mapping[str, Any]:
    """Return a ``Decimal``'s schema, which a read and a write differ in.

    Args:
        cls: The declared type.
        mode: ``"read"`` or ``"write"``.

    Returns:
        A decimal string, or a JSON number too in a read.
    """
    del cls
    written = {"type": "string", "format": "decimal"}
    if mode == "read":
        return {"anyOf": [written, {"type": "number"}]}
    return written


def _enum_schema(cls: type[Any], mode: str) -> Mapping[str, Any]:
    """Return the schema of an enum: the values of its members.

    Args:
        cls: The declared enum type.
        mode: ``"read"`` or ``"write"``, which an enum's schema does not
            depend on.

    Returns:
        The schema.
    """
    del mode
    return {"enum": [member.value for member in cls]}


def _schema_of_entry(entry: Conversion, ty: Any, mode: str) -> dict[str, Any]:
    """Return the schema that a conversion gives a declared type.

    Args:
        entry: The conversion.
        ty: The type to pass to it.
        mode: ``"read"`` or ``"write"``.

    Returns:
        A copy of the schema, which the caller may change.
    """
    if callable(entry.schema):
        schema = entry.schema(ty, cast(Literal["read", "write"], mode))
        _check_embeddable(schema)
    else:
        schema = entry.schema
    return copy.deepcopy(dict(schema))


DEFAULT_CONVERSIONS: Mapping[type[Any], Conversion] = MappingProxyType(
    {
        datetime.datetime: Conversion(
            decode=lambda value, _: parse_datetime(value),
            encode=lambda value, _: _encode_datetime(value),
            schema=_string("date-time"),
        ),
        datetime.date: Conversion(
            decode=lambda value, _: _decode_date(value),
            encode=lambda value, _: _encode_date(value),
            schema=_string("date"),
        ),
        uuid.UUID: Conversion(
            decode=lambda value, _: _decode_uuid(value),
            encode=lambda value, _: str(value),
            schema=_string("uuid"),
        ),
        datetime.timedelta: Conversion(
            decode=lambda value, _: parse_duration(value),
            encode=lambda value, _: isodate.duration_isoformat(value),
            schema=_string("duration"),
        ),
        datetime.time: Conversion(
            decode=lambda value, _: _decode_time(value),
            encode=lambda value, _: _encode_time(value),
            schema=_string("time"),
        ),
        decimal.Decimal: Conversion(
            decode=lambda value, _: _decode_decimal(value),
            encode=lambda value, _: _encode_decimal(value),
            schema=_decimal_schema,
        ),
        enum.Enum: Conversion(
            decode=_decode_enum_value,
            encode=_encode_enum,
            schema=_enum_schema,
        ),
    }
)
"""eazyrest's conversions, keyed by the type they convert."""


_OWN_TYPES: frozenset[type[Any]] = frozenset(
    {
        object,
        type(None),
        bool,
        int,
        float,
        str,
        list,
        tuple,
        dict,
        set,
        frozenset,
        Iterable,
        Sequence,
        Mapping,
    }
)
"""Types that eazyrest converts itself, which cannot key a conversion.

An entry for one would also convert its subclasses, as one for ``int``
would convert ``bool`` and ``IntEnum`` fields, or would compete with the
conversion of a collection's items."""

_TABLES: weakref.WeakKeyDictionary[type[Any], Mapping[Any, Conversion]] = (
    weakref.WeakKeyDictionary()
)
"""Each class's conversion table, merged through its MRO."""


def _table_of(cls: type[Any]) -> Mapping[Any, Conversion]:
    """Return a class's conversion table.

    The table is the default one, updated by the ``conversions`` mapping of
    each class in ``cls``'s MRO, from its last base to ``cls`` itself, so a
    subclass's entries replace its bases'. A model computes its table when
    it is defined, so that a bad entry fails then, and the table does not
    change afterward.

    Args:
        cls: Model class.

    Returns:
        The merged table.

    Raises:
        TypeError: If a ``conversions`` attribute is not a mapping, or has a
            key that is not a type or a ``NewType``, a key that eazyrest
            converts itself, such as ``int`` or ``list``, or a value that is
            not a ``Conversion``.
    """
    table = _TABLES.get(cls)
    if table is not None:
        return table
    merged: dict[Any, Conversion] = dict(DEFAULT_CONVERSIONS)
    for base in reversed(cls.__mro__):
        own = vars(base).get("conversions")
        if own is None:
            continue
        if not isinstance(own, Mapping):
            raise TypeError(
                f"{base.__name__}.conversions must be a mapping, got "
                f"{type(own).__name__}"
            )
        for key, entry in own.items():
            _check_entry(base, key, entry)
        merged.update(own)
    table = MappingProxyType(merged)
    _TABLES[cls] = table
    return table


def _check_entry(owner: type[Any], key: Any, entry: Any) -> None:
    """Check one entry of a class's ``conversions``.

    Args:
        owner: The class that declares the entry, for messages.
        key: The entry's key.
        entry: The entry's value.

    Raises:
        TypeError: If ``key`` is not a type or a ``NewType``, is a type
            whose instances ``isinstance()`` cannot recognize, such as
            ``Any``, or is a type that eazyrest converts itself, or ``entry``
            is not a ``Conversion``.
    """
    if not (_is_class(key) or _supertype(key) is not None):
        raise TypeError(
            f"{owner.__name__}.conversions has the key {key!r}, which is "
            "neither a type nor a NewType"
        )
    final = _final_supertype(key)
    if not _is_class(final):
        raise TypeError(
            f"{owner.__name__}.conversions has the key {key.__name__}, a "
            f"NewType of {final!r}, which is not a class"
        )
    # An entry encodes the instances of its key, so isinstance() must work.
    try:
        isinstance(None, final)
    except TypeError:
        raise TypeError(
            f"{owner.__name__}.conversions has the key {key!r}, whose "
            "instances cannot be recognized with isinstance()"
        ) from None
    try:
        own_type = key in _OWN_TYPES
    except TypeError:
        own_type = False
    if own_type:
        raise TypeError(
            f"{owner.__name__}.conversions has the key {key.__name__}, a type "
            "that eazyrest converts itself. An entry for it would also "
            "convert its subclasses, as one for int would convert bool. Key "
            "the entry by a subclass or a NewType instead."
        )
    if not isinstance(entry, Conversion):
        raise TypeError(
            f"{owner.__name__}.conversions[{key!r}] is not a Conversion"
        )


def _is_class(ty: Any) -> bool:
    """Return whether an annotation is a class, not a parameterized one.

    Args:
        ty: Annotation.

    Returns:
        Whether ``ty`` is a class. Python 3.10 counts ``list[int]`` as a
        type, so a parameterized type is excluded explicitly.
    """
    return isinstance(ty, type) and typing.get_origin(ty) is None


def _final_supertype(ty: Any) -> Any:
    """Return the type that a ``NewType``, or a chain of them, was made from.

    Args:
        ty: Declared type.

    Returns:
        The first supertype that is not a ``NewType``, or ``ty`` itself.
    """
    while (supertype := _supertype(ty)) is not None:
        ty = supertype
    return ty


def _supertype(ty: Any) -> Any:
    """Return the supertype of a ``NewType``.

    Args:
        ty: Declared type.

    Returns:
        The type that ``ty`` was made from, or ``None`` if ``ty`` is not a
        ``NewType``.
    """
    if isinstance(ty, type):
        return None
    return getattr(ty, "__supertype__", None)


class _Found(NamedTuple):
    """A declared type's conversion, as ``_conversion_of()`` finds it."""

    entry: Conversion
    """The conversion."""

    declared: Any
    """The type to pass to the conversion's functions: the declared type, or
    the supertype of a ``NewType`` that converts as its supertype."""

    key: Any
    """The table key that the conversion was found under, whose instances
    the conversion encodes."""


def _conversion_of(
    ty: Any, table: Mapping[Any, Conversion] = DEFAULT_CONVERSIONS
) -> _Found | None:
    """Return the conversion for a declared type, if it has one.

    An entry for exactly ``ty`` comes first. A class then converts by the
    entry of the nearest class in its MRO, so an enum converts by the
    ``Enum`` entry unless it has one of its own, and a ``NewType`` without
    an entry converts as its supertype.

    Args:
        ty: Declared type of a field or item.
        table: Conversion table of the model class.

    Returns:
        The conversion, the type to pass to it, and the key it was found
        under. ``None`` for a type whose JSON value is read and written as it
        is.
    """
    try:
        if ty in table:
            return _Found(table[ty], ty, ty)
    except TypeError:
        # An unhashable annotation has no entry.
        return None
    supertype = _supertype(ty)
    if supertype is not None:
        return _conversion_of(supertype, table)
    if not isinstance(ty, type):
        return None
    for base in ty.__mro__[1:]:
        if base in table:
            return _Found(table[base], ty, base)
    return None


def _is_instance(value: Any, ty: Any) -> bool:
    """Return whether a value is an instance of a declared type.

    Args:
        value: Value to write.
        ty: Declared type, which may be a ``NewType``.

    Returns:
        Whether ``value`` is an instance of ``ty``, or of a ``NewType``'s
        supertype.
    """
    supertype = _supertype(ty)
    if supertype is not None:
        return _is_instance(value, supertype)
    return isinstance(ty, type) and isinstance(value, ty)


_ACTIVE_TABLE: contextvars.ContextVar[Mapping[Any, Conversion] | None] = (
    contextvars.ContextVar("eazyrest_active_table", default=None)
)
"""The table of the model whose field Pydantic is converting, if any."""


@contextlib.contextmanager
def _active_table(
    table: Mapping[Any, Conversion],
) -> Generator[None, None, None]:
    """Make a model's table the one that ``eazyrest.pydantic`` types use.

    Args:
        table: Conversion table of the model whose field Pydantic converts.

    Yields:
        Nothing. The previous table is restored afterward.
    """
    token = _ACTIVE_TABLE.set(table)
    try:
        yield
    finally:
        _ACTIVE_TABLE.reset(token)


def _current_conversion(ty: Any) -> _Found:
    """Return the conversion of a type in the active table, or the default.

    Args:
        ty: A type with a default conversion, such as ``datetime.datetime``
            or an enum.

    Returns:
        The conversion, the type to pass to it, and its key.
    """
    table = _ACTIVE_TABLE.get()
    found = _conversion_of(ty, DEFAULT_CONVERSIONS if table is None else table)
    assert found is not None, ty
    return found
