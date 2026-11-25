"""Conversions between Python values and their JSON forms.

A conversion describes one Python type's JSON form: how to read the type
from JSON, how to write it, and its JSON schema. The default table holds
eazyrest's conversions for ``datetime``, ``date``, ``time``, ``timedelta``,
``UUID``, ``Decimal``, and enums. JSON scalars need none.
"""

from __future__ import annotations

import collections
import datetime
import decimal
import enum
import math
import re
import types
import typing
import uuid
import weakref
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    MutableSequence,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Any, Literal, NamedTuple, cast
from urllib.parse import unquote

import isodate

from ._aliases import _expand_aliases
from .dateparse import parse_datetime, parse_duration

_typing_extensions: types.ModuleType | None
try:
    import typing_extensions as _typing_extensions_module
except ImportError:  # pragma: no cover
    _typing_extensions = None
else:
    _typing_extensions = _typing_extensions_module

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
    ``FieldConversionError`` that names the field, with the error as its cause.
    Their messages should not quote the value, which may be a secret, as the
    default conversions' messages do not. Any other exception is a bug in the
    conversion, and propagates unchanged, and so does an error of eazyrest that
    the conversion raises, such as a ``ResponseShapeError`` or a
    ``PaginationError``, though each is a ``ValueError``.
    """

    decode: Callable[[Any, type[Any]], Any]
    """Read a JSON value, other than ``null``, as a value of the declared
    type. ``null`` reads as ``None`` for every type, without a conversion."""

    encode: Callable[[Any, type[Any]], Any]
    """Write a value of the declared type as JSON. It is called only for
    instances of the type that the entry is keyed by, so a plain
    ``datetime`` read from a field declared as a subclass of ``datetime``
    writes back. Any other value, such as a string assigned to a
    ``datetime`` field, is written as it is. The result, like a value written
    as it is, must be JSON, or the write raises ``FieldConversionError``."""

    schema: (
        Mapping[str, Any]
        | Callable[[type[Any], Literal["read", "write"]], Mapping[str, Any]]
    ) = field(default_factory=lambda: MappingProxyType({}))
    """The JSON schema of the JSON form, for ``json_schema()``, or a function
    of the declared type and the mode, ``"read"`` or ``"write"``, that
    returns it. The schema is a mapping, such as the schema of a default
    conversion, which may hold other mappings and tuples, and ``json_schema()``
    copies it into dictionaries and lists. The default, an empty schema, allows
    any value.

    ``json_schema()`` embeds the schema in a model's, and moves its ``$defs``
    into the model's, so its references may name only its own definitions, as
    ``#/$defs/Name`` or a pointer into one. A schema with another reference,
    such as ``#`` for a schema that refers to itself, or one to a definition
    that its ``$defs`` lacks, or with ``$id``, ``$schema``, an anchor, or
    ``$defs`` below its root, or ``$defs`` at its root that is no mapping,
    raises ``TypeError``, when the conversion is made or, for a function, when
    ``json_schema()`` calls it. So does a schema that is no mapping, as
    ``True`` or ``"string"`` are. Put such a schema in ``$defs``
    and refer to it there."""

    def __post_init__(self) -> None:
        """Check a schema given as a mapping.

        Raises:
            TypeError: If the schema is no mapping, or could not be embedded in
                a model's.
        """
        if not callable(self.schema):
            _check_embeddable(_plain_schema(self.schema))


# Keywords whose values are JSON data rather than schemas, and keywords whose
# values map names to schemas.
_SCHEMA_DATA = frozenset({"const", "enum", "default", "examples"})
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs"}
)
# Keywords that a schema embedded in a model's cannot keep, since they name its
# root or its resource.
_UNEMBEDDABLE = frozenset(
    {
        "$id",
        "$schema",
        "$anchor",
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveAnchor",
        "$recursiveRef",
    }
)


def _plain_schema(schema: Any) -> dict[str, Any]:
    """Copy a schema into plain dictionaries and lists.

    A schema may be any mapping, and may hold other mappings, such as the
    read-only mapping of a default conversion's schema, and tuples. The copy
    has dictionaries and lists only, which is the form that every later step
    reads, so that the check, the copy a model's schema embeds, and the
    renaming of definitions all see one form. It shares no container with the
    schema.

    Args:
        schema: A conversion's schema, or what its function returned.

    Returns:
        The copy.

    Raises:
        TypeError: If ``schema`` is no mapping.
    """
    if not isinstance(schema, Mapping):
        raise TypeError(
            "A conversion's schema must be a mapping, such as "
            f'{{"type": "string"}}, not {type(schema).__name__}'
        )
    return cast("dict[str, Any]", _plain(schema))


def _plain(value: Any) -> Any:
    """Copy a part of a schema into plain dictionaries and lists.

    Args:
        value: The part.

    Returns:
        A dictionary for a mapping, a list for a list or a tuple, and any other
        value as it is.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _definition_name(token: str) -> str:
    """Return the name of a definition from its token in a reference.

    Args:
        token: The part of a reference such as ``#/$defs/Cat/properties/name``
            after ``$defs/`` and before the next ``/``, escaped as a JSON
            Pointer in a URI fragment.

    Returns:
        The definition's name, such as ``Cat``.
    """
    return unquote(token).replace("~1", "/").replace("~0", "~")


class _SchemaPart(NamedTuple):
    """A subschema of a schema, or a reference in it, with its place.

    Attributes:
        kind: ``"schema"`` for a subschema, ``"$ref"`` for the value of a
            ``$ref``, and ``"mapping"`` for a value in the ``mapping`` of a
            ``discriminator``.
        holder: The dictionary or list that holds the part.
        key: The part's key or index in ``holder``.
        value: The part, which is ``holder[key]``.
    """

    kind: Literal["schema", "$ref", "mapping"]
    holder: Any
    key: Any
    value: Any


def _schema_parts(schema: Any) -> Iterator[_SchemaPart]:
    """Yield each subschema of a schema and each reference in it.

    This is the one place that decides which values of a schema are schemas,
    which are JSON data, and which are references. The data keywords hold JSON,
    so nothing in them is yielded. The keywords that map names to schemas yield
    the schemas and not the names, so a property named ``"$id"`` is no keyword.
    A ``discriminator`` yields the values of its ``mapping`` as references, and
    nothing else of it.

    The schema must hold dictionaries and lists only, as ``_plain()`` gives. A
    consumer may change a subschema where it is yielded, since the walk reads
    the subschema's members after it yields it. A subschema is yielded before
    the subschemas in it.

    Args:
        schema: A schema.

    Yields:
        Each part, the schema itself first.
    """
    yield from _schema_parts_at([schema], 0)


def _schema_parts_at(holder: Any, key: Any) -> Iterator[_SchemaPart]:
    """Yield the subschemas and references of a part of a schema.

    Args:
        holder: The dictionary or list that holds the part.
        key: The part's key or index in ``holder``.

    Yields:
        Each part, as ``_schema_parts()`` does.
    """
    value = holder[key]
    if isinstance(value, list):
        for index in range(len(value)):
            yield from _schema_parts_at(value, index)
        return
    if not isinstance(value, dict):
        return
    yield _SchemaPart("schema", holder, key, value)
    for name, item in list(value.items()):
        if name in _SCHEMA_DATA:
            continue
        if name == "$ref":
            yield _SchemaPart("$ref", value, name, item)
        elif name == "discriminator" and isinstance(item, dict):
            mapping = item.get("mapping")
            if isinstance(mapping, dict):
                for tag, target in list(mapping.items()):
                    yield _SchemaPart("mapping", mapping, tag, target)
        elif name in _SCHEMA_MAPS:
            if isinstance(item, dict):
                for member in list(item):
                    yield from _schema_parts_at(item, member)
        else:
            yield from _schema_parts_at(value, name)


def _renamed_references(schema: Any, rename: Callable[[Any], Any]) -> Any:
    """Return a copy of a schema with each reference replaced.

    Args:
        schema: A schema.
        rename: Function that takes the value of a ``$ref`` or of a
            ``discriminator``'s ``mapping`` and returns its replacement.

    Returns:
        A copy of ``schema`` in dictionaries and lists, that shares no
        container with it.
    """
    copy = _plain(schema)
    for part in _schema_parts(copy):
        if part.kind != "schema":
            part.holder[part.key] = rename(part.value)
    return copy


def _check_embeddable(schema: Any) -> None:
    """Check that a conversion's schema can be embedded in a model's.

    Args:
        schema: The schema, in dictionaries and lists, as ``_plain()`` gives.

    Raises:
        TypeError: If the schema has ``$id``, ``$schema``, an anchor, ``$defs``
            below its root or at it that is no mapping, or a reference, in
            ``$ref`` or in the ``mapping`` of a ``discriminator``, other than
            to a definition that it has. A value in a ``mapping`` that is no
            reference into ``$defs``, such as a bare schema name, is not
            checked.
    """
    own = schema.get("$defs") if isinstance(schema, Mapping) else None
    if (
        isinstance(schema, Mapping)
        and "$defs" in schema
        and not isinstance(own, Mapping)
    ):
        raise TypeError(
            "A conversion's schema may have $defs only as a mapping of "
            f"definitions, not {type(own).__name__}"
        )
    defs = own if isinstance(own, Mapping) else ()
    for part in _schema_parts(schema):
        if part.kind == "schema":
            for key in part.value:
                if key in _UNEMBEDDABLE:
                    raise TypeError(
                        f"A conversion's schema with {key} cannot be embedded "
                        "in a model's schema"
                    )
                if key == "$defs" and part.value is not schema:
                    raise TypeError(
                        "A conversion's schema may have $defs only at its root"
                    )
            continue
        value = part.value
        if part.kind == "$ref" and not (
            isinstance(value, str) and value.startswith("#/$defs/")
        ):
            raise TypeError(
                f"A conversion's schema may refer only to its own $defs, as "
                f"#/$defs/Name, not to {value!r}"
            )
        if isinstance(value, str) and value.startswith("#/$defs/"):
            token = value[8:].partition("/")[0]
            if _definition_name(token) not in defs:
                raise TypeError(
                    f"A conversion's schema refers to {value!r}, which names "
                    "a definition that its $defs lacks"
                )


_UUID_TEXT = re.compile(
    r"(?:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
    r"|\{[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}"
    r"|urn:uuid:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32})"
)
"""The spellings of a ``UUID``'s text that eazyrest reads, in ASCII hex.

These are the hyphenated, compact, braced, and URN forms. ``uuid.UUID`` reads
more, such as a ``0x`` prefix, a sign, an underscore, hyphens anywhere,
surrounding whitespace, and digits that are not ASCII, so a key or a field of
another spelling is refused rather than read as the UUID that its digits name.
"""


def _decode_uuid(value: Any) -> uuid.UUID:
    """Decode a JSON UUID string.

    Args:
        value: UUID in the hyphenated, compact, braced, or URN form, in ASCII
            hex of either case, such as
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

    if _UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID is not valid")
    return uuid.UUID(value)


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
    if not isinstance(value, str):
        raise TypeError(
            f"Date values must be strings, got {type(value).__name__}"
        )
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
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
        value: Decimal that a parse produced.

    Returns:
        ``value``.

    Raises:
        ValueError: If ``value`` is not finite, as the parse of an exponent
            beyond its limits gives under a context that does not trap
            ``InvalidOperation``, or its exponent is out of range. The message
            does not quote ``value``.
    """
    if not value.is_finite():
        raise ValueError("Decimal is not a finite number")
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
        ValueError: If a string is not a decimal number, a float is not finite,
            or the exponent is beyond 4300 in either direction. The message
            does not quote ``value``.
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
        if not math.isfinite(value):
            # A response cannot hold one, but JSON that a caller passes can.
            raise ValueError("Decimal is not a finite number")
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

    Python's ``time.fromisoformat()`` accepts different forms on different
    versions, so the format is checked here, and the documented forms hold on
    every version.

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
        tzinfo = datetime.UTC
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


def _member_reading(value: Any, member_values: Iterable[Any]) -> Any:
    """Return the form of a JSON value that is compared with member values.

    Args:
        value: JSON value read.
        member_values: The JSON values of the members that ``value`` may name.

    Returns:
        ``value``, or the integer it equals if it is a float with an integral
        value and every member's value is an integer, so that the reading does
        not depend on which combination of a ``Flag`` the process has made
        before.
    """
    if (
        isinstance(value, float)
        and value.is_integer()
        and all(
            isinstance(member_value, int)
            and not isinstance(member_value, bool)
            for member_value in member_values
        )
    ):
        return int(value)
    return value


def _names_member_value(member_value: Any, value: Any) -> bool:
    """Return whether a JSON value is a member's value.

    Args:
        member_value: The JSON value of a member.
        value: JSON value read.

    Returns:
        Whether the two are equal, where a boolean equals no number, although
        Python finds them equal.
    """
    return member_value == value and isinstance(member_value, bool) == (
        isinstance(value, bool)
    )


def _decode_enum(cls: type[enum.Enum], value: Any) -> enum.Enum:
    """Decode a JSON value as a member of an enum.

    Args:
        cls: Enum class to decode to.
        value: Value of a member of ``cls``.

    Returns:
        The member of ``cls`` whose value is ``value``, as
        ``_member_reading()`` and ``_names_member_value()`` read it.

    Raises:
        ValueError: If ``cls`` has no such member. The message does not quote
            ``value``. A JSON boolean is not a member whose value is a
            number, nor a number one whose value is a boolean, although
            Python finds them equal.
    """
    value = _member_reading(value, (member.value for member in cls))
    try:
        member = cls(value)
    except ValueError as err:
        # Python quotes the value that the enum lacks.
        if repr(value) in str(err):
            raise ValueError(f"Value is not a valid {cls.__name__}") from err

        raise
    if member.value == value and not _names_member_value(member.value, value):
        raise ValueError(f"Value is not a valid {cls.__name__}")
    return member


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


def _string_or_number(form: str) -> Callable[[type[Any], str], Any]:
    """Return the schema of a string format that a read also takes as a number.

    A datetime reads a Unix timestamp, and a duration a number of seconds.

    Args:
        form: The string's format, such as ``"date-time"``.

    Returns:
        A function of the declared type and mode that gives the schema.
    """

    def schema(cls: type[Any], mode: str) -> Mapping[str, Any]:
        del cls
        written = {"type": "string", "format": form}
        if mode == "read":
            return {"anyOf": [written, {"type": "number"}]}
        return written

    return schema


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

    A ``Flag`` reads and writes the combinations of its members, as ``3`` for
    two members of values ``1`` and ``2``, so its schema is an integer, not the
    values of the single members that iteration yields.

    Args:
        cls: The declared enum type.
        mode: ``"read"`` or ``"write"``, which an enum's schema does not
            depend on.

    Returns:
        The schema.
    """
    del mode
    if issubclass(cls, enum.Flag):
        return {"type": "integer"}
    return {"enum": [member.value for member in cls]}


def _schema_of_entry(entry: Conversion, ty: Any, mode: str) -> dict[str, Any]:
    """Return the schema that a conversion gives a declared type.

    Args:
        entry: The conversion.
        ty: The type to pass to it.
        mode: ``"read"`` or ``"write"``.

    Returns:
        A copy of the schema in plain dictionaries and lists, which the caller
        may change.

    Raises:
        TypeError: If a function that makes the schema returns no mapping, or a
            schema that could not be embedded in a model's.
    """
    if callable(entry.schema):
        schema = _plain_schema(
            entry.schema(ty, cast(Literal["read", "write"], mode))
        )
        _check_embeddable(schema)
        return schema
    return _plain_schema(entry.schema)


DEFAULT_CONVERSIONS: Mapping[type[Any], Conversion] = MappingProxyType(
    {
        datetime.datetime: Conversion(
            decode=lambda value, _: parse_datetime(value),
            encode=lambda value, _: _encode_datetime(value),
            schema=_string_or_number("date-time"),
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
            schema=_string_or_number("duration"),
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
        collections.deque,
        Iterable,
        Collection,
        Sequence,
        MutableSequence,
        AbstractSet,
        MutableSet,
        Mapping,
        MutableMapping,
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
    if not _is_typed_dict(final):
        # An entry encodes the instances of its key, so isinstance() must
        # work. A TypedDict's entry encodes any mapping instead.
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
        Whether ``ty`` is a class, which a parameterized type such as
        ``list[int]`` is not.
    """
    return isinstance(ty, type)


def _is_typed_dict(ty: Any) -> bool:
    """Return whether a type is a ``TypedDict`` class.

    Args:
        ty: Type to test.

    Returns:
        Whether ``ty`` is a ``TypedDict``, from ``typing`` or
        ``typing_extensions``.
    """
    if _typing_extensions is not None:
        return bool(_typing_extensions.is_typeddict(ty))
    return typing.is_typeddict(ty)  # pragma: no cover


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
    """Return the supertype of a ``NewType``, as it converts.

    Args:
        ty: Declared type.

    Returns:
        The type that ``ty`` was made from, with a named type alias expanded
        and ``Annotated`` removed, so that a ``NewType`` of either converts as
        the type that the alias or the ``Annotated`` holds. ``None`` if ``ty``
        is not a ``NewType``.

    Raises:
        TypeError: If the supertype names a type by a string, or an alias in it
            cannot be expanded.
    """
    supertype = _annotated_supertype(ty)
    # Aliases and Annotated can wrap one another to any depth.
    while typing.get_origin(supertype) is Annotated:
        supertype = _expand_aliases(typing.get_args(supertype)[0])
    return supertype


def _annotated_supertype(ty: Any) -> Any:
    """Return the supertype of a ``NewType``, with its ``Annotated`` kept.

    Args:
        ty: Declared type.

    Returns:
        The type that ``ty`` was made from, with a named type alias expanded.
        ``None`` if ``ty`` is not a ``NewType``.

    Raises:
        TypeError: If the supertype names a type by a string. Python never
            resolves a string in a ``NewType``'s supertype, so it would stay a
            string in the type that eazyrest converts, which cannot be
            converted as the type that the string names. Also if an alias in
            the supertype cannot be expanded.
    """
    if isinstance(ty, type):
        return None
    supertype = getattr(ty, "__supertype__", None)
    if supertype is not None and _names_by_string(supertype):
        raise TypeError(
            f"The NewType {getattr(ty, '__name__', ty)!r} has a string in its "
            f"supertype {supertype!r}. Write the type itself."
        )
    if supertype is None:
        return None
    return _expand_aliases(supertype)


def _names_by_string(ty: Any) -> bool:
    """Return whether a type names a type by a string, at any depth.

    The values of a ``Literal`` and the metadata of an ``Annotated`` are
    strings that name nothing, so they do not count.

    Args:
        ty: Type to examine.

    Returns:
        Whether ``ty``, or a type argument within it, is a string or a
        ``ForwardRef``.
    """
    if isinstance(ty, (str, typing.ForwardRef)):
        return True
    origin = typing.get_origin(ty)
    if origin is Literal:
        return False
    args = typing.get_args(ty)
    if origin is Annotated:
        args = args[:1]
    return any(_names_by_string(arg) for arg in args)


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
