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

import dataclasses
import datetime
import enum
import types
import typing
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Annotated, Any, Literal, TypeVar, Union, cast

import typing_extensions
from pydantic import (
    BaseModel,
    PlainSerializer,
    PlainValidator,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
    WithJsonSchema,
)
from pydantic_core import (
    ErrorDetails,
    PydanticKnownError,
    PydanticSerializationError,
)
from typing_extensions import get_type_hints, is_typeddict

from ._conversion import _current_conversion, _final_supertype, _supertype
from .eazyrest import (
    JSONObject,
    _is_named_tuple,
    _is_type_alias,
    _resolve_alias_value,
    lenient_issubclass,
)

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


# The exceptions that the encode functions of table conversions raise during
# one of eazyrest's dumps. A table serializer encodes only values of its type,
# so a TypeError or ValueError is the conversion's refusal of the value, and
# any other exception is a bug in the conversion. Pydantic's union serializer
# catches both and writes the value by another member, or by its inferred
# type, so they are recorded here and raised after the dump.
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
            # A union's serializer tries its members again after a failure,
            # which no conversion should see.
            raise bugs[0]
        found = _current_conversion(ty)
        try:
            return found.entry.encode(value, found.declared)
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


def _is_dataclass_class(cls: Any) -> bool:
    """Return whether a type is a dataclass class.

    Args:
        cls: Type to test.

    Returns:
        Whether ``cls`` is a dataclass, of Pydantic or the standard library,
        and a class, not an instance.
    """
    return isinstance(cls, type) and dataclasses.is_dataclass(cls)


def _check_undeclared_keys(ty: Any, seen: set[Any] | None = None) -> None:
    """Check that every class a type names keeps or rejects undeclared keys.

    Under Pydantic's default config, validating and serializing a ``TypedDict``
    or a dataclass drop the keys it does not declare. Saving such a value
    would then delete those keys on the server. A ``TypedDict`` keeps them
    under ``extra="allow"``, but Pydantic never writes them for a dataclass,
    of Pydantic or the standard library, so only ``extra="forbid"``, which
    rejects them, is safe for it.

    Every ``TypedDict`` and dataclass that the type names is checked, at any
    depth: as a type argument, as the value of a named alias, as the
    supertype of a ``NewType``, as the bound of a type variable, as the type
    of a field of a ``TypedDict``, dataclass, or ``NamedTuple``, or as the
    type of a ``TypedDict``'s extra items, with the type arguments of a
    parameterized generic class in place of its type parameters. A nested
    value that drops keys deletes them on the server as well. A dataclass's
    init-only field drops its key too, but its class variables are not
    fields. A Pydantic model follows its own config, so the types of its
    fields are not checked.

    Args:
        ty: Type to check.
        seen: Types already checked, which a type that refers to itself
            reaches again.

    Raises:
        TypeError: If a class that the type names would drop keys, by its
            config or an init-only field, or its annotations cannot be
            resolved.
    """
    seen = set() if seen is None else seen
    try:
        key: Any = ty
        if key in seen:
            return
    except TypeError:
        # Annotated metadata can make a type unhashable.
        key = id(ty)
        if key in seen:
            return
    seen.add(key)

    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    for nested in _nested_types(ty, origin, args):
        _check_undeclared_keys(nested, seen)


def _nested_types(ty: Any, origin: Any, args: tuple[Any, ...]) -> list[Any]:
    """Return the types that one level of a type names, checking its class.

    Args:
        ty: Type to inspect.
        origin: ``typing.get_origin(ty)``.
        args: ``typing.get_args(ty)``.

    Returns:
        The types to check next, such as the value of a named alias or the
        field types of a class.

    Raises:
        TypeError: If ``ty`` is a ``TypedDict`` or dataclass, or a
            parameterized one, that would drop keys, or whose annotations
            cannot be resolved.
    """
    if _is_type_alias(ty):
        return _alias_value(ty, (), ())
    if _is_type_alias(origin):
        return _alias_value(origin, origin.__type_params__, args)
    supertype = _supertype(ty)
    if supertype is not None:
        return [supertype]
    if isinstance(ty, TypeVar):
        bound = [ty.__bound__] if ty.__bound__ is not None else []
        return [*bound, *ty.__constraints__]
    if origin is Literal:
        return []
    if origin is Annotated:
        return [args[0]]

    cls = ty if origin is None else origin
    if not (
        is_typeddict(cls) or _is_dataclass_class(cls) or _is_named_tuple(cls)
    ):
        return list(args)
    if not _is_named_tuple(cls):
        _check_extra(cls)
    params = getattr(cls, "__parameters__", ())
    substitutions = dict(zip(params, args, strict=False)) if args else {}
    return [_substitute(hint, substitutions) for hint in _field_types(cls)]


def _field_types(cls: Any) -> list[Any]:
    """Return the types of the values that a class reads and writes.

    These are the types of a ``TypedDict``'s keys and of its extra items, and
    of the fields of a ``NamedTuple`` or dataclass, not its class variables.
    A Pydantic dataclass gives the types that Pydantic resolved, which may
    name classes that its annotations cannot.

    Args:
        cls: ``TypedDict``, dataclass, or ``NamedTuple`` class.

    Returns:
        The types.

    Raises:
        TypeError: If the class has an init-only field, which it reads but
            does not write, or its annotations cannot be resolved, so that
            its fields cannot be checked.
    """
    pydantic_fields = getattr(cls, "__pydantic_fields__", None)
    if (
        _is_dataclass_class(cls)
        and pydantic_fields is not None
        and getattr(cls, "__pydantic_complete__", False)
    ):
        for name, info in pydantic_fields.items():
            if info.init_var:
                _refuse_init_only(cls, name)
        return [info.annotation for info in pydantic_fields.values()]

    try:
        hints = get_type_hints(cls, include_extras=True)
        extra = _extra_items(cls) if is_typeddict(cls) else None
    except Exception as err:
        raise TypeError(
            f"The fields of {cls.__name__} cannot be checked for the keys "
            f"that it would drop, because its annotations cannot be resolved: "
            f"{err}"
        ) from err
    if is_typeddict(cls):
        return [*hints.values(), *([extra] if extra is not None else [])]
    if _is_named_tuple(cls):
        return [hints[name] for name in cls._fields if name in hints]
    for name, hint in hints.items():
        if isinstance(hint, dataclasses.InitVar):
            _refuse_init_only(cls, name)
    names = {field.name for field in dataclasses.fields(cls)}
    return [hint for name, hint in hints.items() if name in names]


def _extra_items(cls: Any) -> Any:
    """Return the type of a ``TypedDict``'s extra items.

    Args:
        cls: ``TypedDict`` class.

    Returns:
        The type given by its ``extra_items``, resolved if it is a string, or
        ``None`` if it gives none.

    Raises:
        NameError: If a string type names nothing.
    """
    extra = getattr(cls, "__extra_items__", None)
    if extra is None or any(
        extra is getattr(module, "NoExtraItems", None)
        for module in (typing, typing_extensions)
    ):
        return None
    if isinstance(extra, (str, typing.ForwardRef)):
        # Resolve it as an annotation of the TypedDict's module.
        namespace = {
            "__annotations__": {"extra": extra},
            "__module__": cls.__module__,
        }
        holder = type("_ExtraItems", (), namespace)
        return get_type_hints(holder, include_extras=True)["extra"]
    return extra


def _refuse_init_only(cls: Any, name: str) -> None:
    """Refuse a dataclass with an init-only field.

    Args:
        cls: Dataclass.
        name: Name of its init-only field.

    Raises:
        TypeError: Always, since a write would drop the field's key.
    """
    raise TypeError(
        f"{cls.__name__} would drop the key of its init-only field {name}, "
        "which it reads but does not write. Make it a field."
    )


def _alias_value(
    alias: Any, params: tuple[Any, ...], args: tuple[Any, ...]
) -> list[Any]:
    """Return the value of a named alias, with type arguments in place.

    Args:
        alias: Named type alias.
        params: The alias's type parameters.
        args: Type arguments for ``params``.

    Returns:
        A list of the alias's value, or no types if a string in the value
        names nothing, which Pydantic reports.
    """
    try:
        value = _resolve_alias_value(alias)
    except TypeError:
        return []
    return [_substitute(value, dict(zip(params, args, strict=False)))]


def _substitute(ty: Any, substitutions: Mapping[Any, Any]) -> Any:
    """Return a type with type arguments in place of its type variables.

    Named aliases are left as they are, so that one that refers to itself
    does not expand without end.

    Args:
        ty: Type to change.
        substitutions: Types to put in place of type variables.

    Returns:
        ``ty`` with each type variable in ``substitutions`` replaced, or
        ``ty`` itself if it has none.
    """
    if not substitutions:
        return ty
    if isinstance(ty, TypeVar):
        return substitutions.get(ty, ty)
    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if not args or origin is Literal:
        return ty

    new_args = tuple(_substitute(arg, substitutions) for arg in args)
    if all(new is old for new, old in zip(new_args, args, strict=True)):
        return ty
    try:
        if origin is Annotated:
            return Annotated[new_args]
        if origin is types.UnionType:
            origin = Union
        return origin[new_args]
    except TypeError:
        # A form that cannot be rebuilt, such as a Callable's argument list,
        # names no class that drops keys.
        return ty


def _check_extra(cls: Any) -> None:
    """Check that one class keeps or rejects the keys it does not declare.

    Args:
        cls: ``TypedDict`` or dataclass to check.

    Raises:
        TypeError: If the class's config would drop undeclared keys.
    """
    extra = getattr(cls, "__pydantic_config__", {}).get("extra")
    if _is_dataclass_class(cls):
        if extra != "forbid":
            raise TypeError(
                f"{cls.__name__} would drop the keys it does not declare, "
                "because Pydantic never writes them for a dataclass. Give it "
                'extra="forbid", as with @pydantic.with_config(pydantic.'
                'ConfigDict(extra="forbid")), or use a Pydantic model or a '
                'TypedDict with extra="allow".'
            )
    elif extra not in ("allow", "forbid"):
        raise TypeError(
            f"{cls.__name__} would drop the keys it does not declare. "
            "Decorate it with "
            '@pydantic.with_config(pydantic.ConfigDict(extra="allow")).'
        )


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
        TypeError: If Pydantic cannot convert the type, cannot apply its
            metadata, such as ``strict``, to a type that eazyrest converts,
            or the type names a ``TypedDict`` or dataclass that would drop the
            keys it does not declare.
    """
    _check_undeclared_keys(annotation)
    try:
        return TypeAdapter(_eazyrest_types(annotation))
    except PydanticUserError as err:
        # A PydanticUserError is a TypeError in some Pydantic releases and a
        # RuntimeError in others.
        raise TypeError(str(err)) from err
    except RuntimeError as err:
        # Pydantic raises a plain RuntimeError for a constraint that it cannot
        # apply to a type with a custom validator, as eazyrest's are.
        if not str(err).startswith("Unable to apply constraint"):
            raise

        raise TypeError(
            f"{err}. eazyrest converts datetime, date, time, timedelta, UUID, "
            "Decimal, and enum values itself, so Pydantic metadata that "
            "changes how "
            "Pydantic would read them, such as strict, does not apply"
        ) from err


# Context members of Pydantic's errors that quote the input, by error type. A
# None key applies to every type. The "error" member of most parsing errors
# names what is wrong, as in "input is too short", without quoting the input.
# The messages of custom validators, value_error and assertion_error, are the
# application's own.
_INPUT_CONTEXT: dict[str | None, frozenset[str]] = {
    None: frozenset({"tag", "attribute", "encoding_error"}),
    "uuid_parsing": frozenset({"error"}),
    "get_attribute_error": frozenset({"error"}),
    "iteration_error": frozenset({"error"}),
    "mapping_type": frozenset({"error"}),
}

_REDACTED = "[redacted]"


def _schema_strings(schema: Any) -> set[str]:
    """Return every string in a Pydantic core schema.

    The strings include every field name, alias, and literal value that the
    schema declares. A location part among them names something the
    application declared, so it is not secret.

    Args:
        schema: Core schema, or a part of one.

    Returns:
        The strings that ``schema`` holds, as keys or values, at any depth.
    """
    strings: set[str] = set()
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            strings.add(node)
        elif isinstance(node, Mapping):
            pending.extend(node.keys())
            pending.extend(node.values())
        elif isinstance(node, (list, tuple)):
            pending.extend(node)
    return strings


def _safe_location(
    loc: tuple[int | str, ...], value: Any, declared: set[str]
) -> str:
    """Return an error location without the keys that came from the input.

    A location part that is a key of a mapping in the input, and that the
    schema does not declare, is a key that the input chose, such as a key of a
    ``dict[str, int]``, or an undeclared key that ``extra="forbid"`` rejects.
    It is shown as ``[key]``. Field names, recognizable sequence indices, and
    declared union labels are shown as they are.

    Args:
        loc: Location of the error, as Pydantic reports it.
        value: Input that was validated.
        declared: Strings that the schema declares.

    Returns:
        The location's parts, joined by dots.
    """
    parts = []
    node = value
    for part in loc:
        if isinstance(node, Mapping) and part in node:
            parts.append(str(part) if part in declared else "[key]")
            node = node[part]
            continue
        # A decoder or validator can transform the input before the failure.
        # Its locations then need not describe nodes of the original value.
        parts.append(
            "[key]"
            if (isinstance(part, str) and part not in declared)
            or (isinstance(part, int) and not isinstance(node, (list, tuple)))
            else str(part)
        )
        if isinstance(part, int) and isinstance(node, (list, tuple)):
            node = node[part] if -len(node) <= part < len(node) else None
        elif isinstance(part, str) and not isinstance(node, Mapping):
            node = getattr(node, part, None)
    return ".".join(parts)


def _safe_message(error: ErrorDetails) -> str:
    """Return an error's message without the input that it quotes.

    Args:
        error: Error, as ``ValidationError.errors()`` reports it.

    Returns:
        The message, rebuilt from its type and context with each context
        member that quotes the input replaced, or the message as it is if its
        context quotes nothing.
    """
    ctx = error.get("ctx")
    if not ctx:
        return error["msg"]
    redact = _INPUT_CONTEXT[None] | _INPUT_CONTEXT.get(
        error["type"], frozenset()
    )
    if redact.isdisjoint(ctx):
        return error["msg"]
    safe = {
        name: _REDACTED if name in redact else member
        for name, member in ctx.items()
    }
    try:
        return PydanticKnownError(cast(Any, error["type"]), safe).message()
    except (KeyError, TypeError, ValueError):
        return _REDACTED


def _validation_error(
    err: ValidationError, value: Any, schema: Any
) -> ValueError:
    """Return a ``ValueError`` that describes a validation error.

    Pydantic's own message quotes the input, which may be a secret that the
    server sent or the caller assigned. The returned error's message gives only
    where and why each value is invalid. It omits the input, a mapping key that
    the input chose, and a context value, such as a discriminated union's tag,
    that Pydantic copies from the input. A custom validator's own message is
    kept.

    Args:
        err: Validation error that Pydantic raised.
        value: Input that was validated.
        schema: Core schema of the type that ``value`` was validated as.

    Returns:
        ``ValueError`` whose message lists the location and reason of each of
        ``err``'s errors, without the input.
    """
    declared = _schema_strings(schema)
    reasons = []
    for error in err.errors(include_url=False, include_input=False):
        location = _safe_location(error["loc"], value, declared)
        message = _safe_message(error)
        reasons.append(f"{location}: {message}" if location else message)

    return ValueError("; ".join(reasons))


def _decode(adapter: TypeAdapter[Any], value: Any) -> Any:
    """Return the field value of a JSON value.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        value: JSON value to decode.

    Returns:
        Field value of ``value``.

    Raises:
        ValueError: If the value is not valid for the field. The message does
            not quote the value.
    """
    try:
        return adapter.validate_python(value)
    except ValidationError as err:
        raise _validation_error(err, value, adapter.core_schema) from err


def _encode(adapter: TypeAdapter[Any], value: Any) -> Any:
    """Return the JSON value of a field value.

    A value of the field's type is serialized as it is. Otherwise, it is
    validated as input first, so that a field typed as ``Base64Bytes``, for
    example, writes both ``b"hello"`` and its encoding, ``"aGVsbG8="``, as
    ``"aGVsbG8="``. Validating every value first would fail for such a type,
    which would decode raw bytes as base64. Models are written by their
    fields' aliases, which is how they are read.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        value: Field value to encode.

    Returns:
        JSON value of ``value``.

    Raises:
        ValueError: If the value is not valid input for the field, or the field
            could not read its JSON value back as the value written, for
            example because the value breaks a constraint that only
            validation checks, or because it is a ``SecretStr``, which writes
            a mask. The message does not quote the value.
    """
    # A round trip writes a value that validation reads back, such as the
    # JSON text of a pydantic.Json field rather than the value it holds.
    try:
        # A failure here, of a serializer given a value that is not yet
        # valid, means the value must be validated first. A bug in a
        # conversion propagates.
        encoded = _dump(
            adapter,
            value,
            unvalidated=True,
            mode="json",
            round_trip=True,
            by_alias=True,
            warnings="error",
        )
        written = value
    except PydanticSerializationError:
        written = _decode(adapter, value)
        # Pydantic's warnings quote the value.
        encoded = _dump(
            adapter,
            written,
            mode="json",
            round_trip=True,
            by_alias=True,
            warnings="none",
        )

    if not _same(adapter, written, _decode(adapter, encoded)):
        raise ValueError("The value does not read back as written")

    return encoded


def _same(adapter: TypeAdapter[Any], written: Any, read_back: Any) -> bool:
    """Return whether a value read back as the value written.

    The Python dumps are compared rather than the values, because a model's
    equality also compares its private attributes, which are not written. A
    secret dumps as itself, so a mask that reads back is still caught.
    Pydantic dumps the items of a set into a set, which fails for an item
    that dumps as a dict, such as a frozen dataclass, so then the values are
    compared by their fields, keys, and items instead.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        written: The value written.
        read_back: The value that its JSON reads as.

    Returns:
        Whether ``read_back`` is the same as ``written``.
    """
    try:
        return bool(
            _python_dump(adapter, read_back) == _python_dump(adapter, written)
        )
    except TypeError:
        return _same_values(written, read_back)


def _same_values(written: Any, read_back: Any) -> bool:
    """Return whether two values are the same, field by field.

    Models and dataclasses compare by their type and their written fields,
    mappings by their keys and values, sequences item by item, and sets by
    matching each item to a distinct item of the other. Other values compare
    by equality, which compares a secret's value rather than its mask.

    Args:
        written: The value written.
        read_back: The value that its JSON reads as.

    Returns:
        Whether the values are the same.
    """
    if isinstance(written, BaseModel):
        names = set(type(written).model_fields) | set(
            written.model_extra or {}
        )
        return (
            type(read_back) is type(written)
            and names
            == set(type(read_back).model_fields)
            | set(read_back.model_extra or {})
            and all(
                _same_values(getattr(written, name), getattr(read_back, name))
                for name in names
            )
        )
    if dataclasses.is_dataclass(written) and not isinstance(written, type):
        return type(read_back) is type(written) and all(
            _same_values(
                getattr(written, field.name), getattr(read_back, field.name)
            )
            for field in dataclasses.fields(written)
        )
    if isinstance(written, Mapping):
        return (
            isinstance(read_back, Mapping)
            and written.keys() == read_back.keys()
            and all(
                _same_values(item, read_back[key])
                for key, item in written.items()
            )
        )
    if isinstance(written, (list, tuple)):
        return (
            type(read_back) is type(written)
            and len(read_back) == len(written)
            and all(
                _same_values(item, back)
                for item, back in zip(written, read_back, strict=True)
            )
        )
    if isinstance(written, (set, frozenset)):
        if not isinstance(read_back, (set, frozenset)) or len(
            read_back
        ) != len(written):
            return False
        try:
            return _items(written) == _items(read_back)
        except TypeError:
            # An unhashable value, which a set can hold inside a model,
            # matches pairwise.
            pass
        unmatched = list(read_back)
        for item in written:
            match = next(
                (
                    i
                    for i, back in enumerate(unmatched)
                    if _same_values(item, back)
                ),
                None,
            )
            if match is None:
                return False
            del unmatched[match]
        return True
    return bool(written == read_back)


def _items(values: Any) -> Counter[Any]:
    """Count the items of a collection by their structural keys.

    Args:
        values: Items of a set.

    Returns:
        How many items have each key of ``_key()``.

    Raises:
        TypeError: If an item holds a value that cannot be hashed.
    """
    return Counter(_key(value) for value in values)


def _key(value: Any) -> Any:
    """Return a hashable key of a value that compares as ``_same_values()``.

    Args:
        value: Value to key.

    Returns:
        A key equal to that of another value exactly when ``_same_values()``
        finds the two the same.

    Raises:
        TypeError: If the value holds a value that cannot be hashed.
    """
    if isinstance(value, BaseModel):
        return (
            type(value),
            frozenset(
                (name, _key(getattr(value, name)))
                for name in set(type(value).model_fields)
                | set(value.model_extra or {})
            ),
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (
            type(value),
            tuple(
                _key(getattr(value, field.name))
                for field in dataclasses.fields(value)
            ),
        )
    if isinstance(value, Mapping):
        return (
            Mapping,
            frozenset((key, _key(item)) for key, item in value.items()),
        )
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(_key(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return (set, frozenset(_items(value).items()))
    hash(value)
    return (None, value)


def _python_dump(adapter: TypeAdapter[Any], value: Any) -> Any:
    """Return the Python dump of a field value, for comparing values.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        value: Field value to dump.

    Returns:
        ``value`` dumped in Python mode.
    """
    return _dump(adapter, value, round_trip=True, by_alias=True)


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
    from its ``encode`` is a bug in it. Either propagates unchanged, as it
    does for a field without Pydantic, even when a union's serializer then
    writes the value by another member, except that a refusal of a value
    that is not yet valid fails the dump, so that the value is validated
    first.

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
        TypeError: If a conversion refuses a valid value, with its reason.
        ValueError: If a conversion refuses a valid value, with its reason.
    """
    bugs: list[Exception] = []
    token = _ENCODE_BUGS.set(bugs)
    failure: PydanticSerializationError | None = None
    try:
        result = adapter.dump_python(value, **options)
    except PydanticSerializationError as err:
        failure = err
    finally:
        _ENCODE_BUGS.reset(token)

    # Raised outside the except block, an exception keeps its own cause.
    for bug in bugs:
        if not isinstance(bug, (TypeError, ValueError)):
            raise bug
    if bugs:
        # A conversion refused a value of its type, which a union's
        # serializer may then have written by another member. A value that
        # is not yet valid is validated and dumped again.
        if unvalidated:
            raise PydanticSerializationError(str(bugs[0]))
        raise bugs[0]
    if failure is not None:
        cause = failure.__cause__
        while isinstance(cause, PydanticSerializationError):
            cause = cause.__cause__
        if (
            not unvalidated
            and cause is not None
            and not isinstance(cause, (TypeError, ValueError))
        ):
            raise cause
        raise failure
    return result
