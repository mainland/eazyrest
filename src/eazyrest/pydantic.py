"""Pydantic types that convert values as eazyrest's fields do.

Inside a Pydantic model, a ``TypedDict``, or a Pydantic dataclass, Pydantic's
own rules convert ``datetime``, ``date``, ``time``, ``timedelta``, ``UUID``,
and ``Decimal`` values, and they differ from eazyrest's. For example, Pydantic
reads ``"P1Y"`` as 365 days and a large number as milliseconds since the Unix
epoch. Annotate a value with one of these types to convert it as an eazyrest
field of the corresponding type would: by the conversion table of the model
whose field holds it, so that it follows that model's conversions, or by the
default table outside a model.

Importing this module requires Pydantic, which the ``pydantic`` extra installs.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import types
import typing
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Annotated, Any, Literal, TypeVar, Union, cast

try:
    import pydantic  # noqa: F401
except ImportError as err:
    raise ImportError(
        "eazyrest.pydantic needs Pydantic, which the eazyrest[pydantic] extra "
        "installs"
    ) from err
import typing_extensions
from pydantic import (
    BaseModel,
    GetJsonSchemaHandler,
    PlainSerializer,
    PlainValidator,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
)
from pydantic.fields import FieldInfo
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import (
    CoreSchema,
    ErrorDetails,
    PydanticKnownError,
    PydanticSerializationError,
    PydanticUndefined,
    core_schema,
)
from typing_extensions import get_type_hints, is_typeddict

from ._conversion import (
    _current_conversion,
    _final_supertype,
    _schema_of_entry,
    _supertype,
)
from ._models import (
    JSONObject,
    _annotations,
    _is_named_tuple,
    _is_type_alias,
    _resolve_alias_value,
    _seal,
    lenient_issubclass,
)

__all__ = ["UUID", "Date", "Datetime", "Decimal", "Duration", "Time"]


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


_TABLE_SERIALIZERS: set[Callable[[Any], Any]] = set()
"""The serializers of the types in this module, which encode by the table."""


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

    _TABLE_SERIALIZERS.add(serialize)
    # A model's table may write any JSON value, such as a timestamp for a
    # datetime, and each type's JSON schema is given separately.
    return PlainSerializer(serialize, return_type=Any)


class _TableSchema:
    """JSON schema of a type, from the conversion table in use.

    In a model's field, the table is the model's. Elsewhere, it is the
    default one. Pydantic's validation schema is the read form, and its
    serialization schema the written form.
    """

    def __init__(self, ty: type[Any]) -> None:
        """Give the schema of a type.

        Args:
            ty: A type that the default table converts, such as ``datetime``.
        """
        self.ty = ty

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        """Return the schema of the type's JSON form.

        Args:
            core_schema: Pydantic's core schema of the type.
            handler: Pydantic's schema handler, whose mode gives the form.

        Returns:
            The schema of the type's conversion, or, for the written form of
            a value that another serializer writes, that serializer's schema.
        """
        mode = "read" if handler.mode == "validation" else "write"
        serialization = core_schema.get("serialization")
        if (
            mode == "write"
            and serialization is not None
            and serialization.get("function") not in _TABLE_SERIALIZERS
        ):
            return handler(core_schema)
        found = _current_conversion(self.ty)
        return _seal(_schema_of_entry(found.entry, found.declared, mode))


class _WriteSchema(GenerateJsonSchema):
    """Generates the JSON schema of what eazyrest writes.

    eazyrest writes a round trip's serialization, which differs from what
    Pydantic's serialization schema describes in two ways: a
    ``pydantic.Json`` field writes its JSON text, as it reads, and a
    computed field is not written.
    """

    def json_schema(self, schema: core_schema.JsonSchema) -> JsonSchemaValue:
        """Describe a ``pydantic.Json`` field as the JSON text it writes.

        Args:
            schema: Core schema of the field.

        Returns:
            The schema of a string that holds JSON of the field's type.
        """
        content = self.generate_inner(
            schema.get("schema") or core_schema.any_schema()
        )
        return {
            "type": "string",
            "contentMediaType": "application/json",
            "contentSchema": content,
        }

    @staticmethod
    def _name_required_computed_fields(
        computed_fields: list[core_schema.ComputedField],
    ) -> list[tuple[str, bool, core_schema.ComputedField]]:
        """Leave out computed fields, which a round trip does not write.

        Args:
            computed_fields: The computed fields, unused.

        Returns:
            No fields.
        """
        del computed_fields
        return []


def _json_schema(adapter: TypeAdapter[Any], mode: str) -> dict[str, Any]:
    """Return the JSON schema of what a Pydantic field reads or writes.

    Args:
        adapter: ``TypeAdapter`` for the field's type.
        mode: ``"read"`` or ``"write"``.

    Returns:
        Pydantic's validation schema for ``"read"``. For ``"write"``, its
        serialization schema, adjusted to what a round trip writes.
    """
    if mode == "read":
        return adapter.json_schema(mode="validation")
    return adapter.json_schema(
        mode="serialization", schema_generator=_WriteSchema
    )


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


def _is_time(value: Any) -> bool:
    return isinstance(value, datetime.time)


def _is_decimal(value: Any) -> bool:
    return isinstance(value, decimal.Decimal)


Datetime = Annotated[
    datetime.datetime,
    _validator(_is_datetime, datetime.datetime),
    _serializer(_is_datetime, datetime.datetime),
    _TableSchema(datetime.datetime),
]
"""A ``datetime`` read from ISO 8601 text or a Unix timestamp in seconds.

A UTC offset becomes a ``datetime.timezone``. The value is written in ISO 8601.
"""

Date = Annotated[
    datetime.date,
    _validator(_is_date, datetime.date),
    _serializer(_is_date, datetime.date),
    _TableSchema(datetime.date),
]
"""A ``date`` read from and written in ISO 8601.

A ``datetime`` is rejected.
"""

Duration = Annotated[
    datetime.timedelta,
    _validator(_is_timedelta, datetime.timedelta),
    _serializer(_is_timedelta, datetime.timedelta),
    _TableSchema(datetime.timedelta),
]
"""A ``timedelta`` read by ``parse_duration()`` and written in ISO 8601.

Durations with years or months are rejected.
"""

UUID = Annotated[
    uuid.UUID,
    _validator(_is_uuid, uuid.UUID),
    _serializer(_is_uuid, uuid.UUID),
    _TableSchema(uuid.UUID),
]
"""A ``UUID`` read from and written as a string."""

Time = Annotated[
    datetime.time,
    _validator(_is_time, datetime.time),
    _serializer(_is_time, datetime.time),
    _TableSchema(datetime.time),
]
"""A ``time`` read from and written in ISO 8601, such as ``"09:30:00"``.

A UTC offset becomes a ``datetime.timezone``. Pydantic would also read a number
as seconds since midnight.
"""

Decimal = Annotated[
    decimal.Decimal,
    _validator(_is_decimal, decimal.Decimal),
    _serializer(_is_decimal, decimal.Decimal),
    _TableSchema(decimal.Decimal),
]
"""A ``Decimal`` read from a string or a number, and written as a string.

It is written in positional notation with its digits, such as ``"1.10"``, as
Django REST Framework writes a ``DecimalField``. Pydantic would also read
strings with spaces or underscores, and would write ``Decimal("1E+2")`` as
``"1E+2"`` rather than ``"100"``.
"""


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
        # The validator and serializer are plain functions, whose schema says
        # nothing about the values, so the table's conversion gives it.
        _TableSchema(cls),
    ]


_IS_VALUE: dict[Any, Callable[[Any], bool]] = {
    datetime.datetime: _is_datetime,
    datetime.date: _is_date,
    datetime.timedelta: _is_timedelta,
    datetime.time: _is_time,
    uuid.UUID: _is_uuid,
    decimal.Decimal: _is_decimal,
}
"""Whether a value has one of the types that this module replaces."""


def _table_type(base: type[Any], key: Any) -> Any:
    """Return a type whose values convert by a table entry, as a field's do.

    Args:
        base: The type of the values, such as ``datetime.datetime`` or an
            enum.
        key: The type whose conversion in the active table converts them,
            ``base`` or a ``NewType`` of it, which converts as ``base`` if it
            has no entry of its own.

    Returns:
        ``base``, annotated with a validator, a serializer, and a schema that
        use the conversion of ``key``.
    """
    is_value = _IS_VALUE.get(base)
    if is_value is None:

        def is_value(value: Any) -> bool:
            return isinstance(value, base)

    return Annotated[
        base,
        _validator(is_value, key),
        _serializer(is_value, key),
        _TableSchema(key),
    ]


_EAZYREST_TYPES: dict[Any, Any] = {
    datetime.datetime: Datetime,
    datetime.date: Date,
    datetime.timedelta: Duration,
    datetime.time: Time,
    uuid.UUID: UUID,
    decimal.Decimal: Decimal,
}


def _check_undeclared_keys(
    ty: Any, seen: set[Any] | None = None, *, model_extra: str | None = None
) -> None:
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
    value that drops keys deletes them on the server as well. A field that
    Pydantic excludes from writes, and a dataclass's init-only field, drop
    their keys too, but a dataclass's class variables are not fields.

    A Pydantic model follows its own config, and so does a ``TypedDict``
    without a config of its own inside it, which takes the model's, but the
    types of a model's fields are checked. A dataclass without a config of
    its own inside a model takes the model's too, so it is safe only inside a
    model with ``extra="forbid"``. A ``NamedTuple`` declares no keys, and the
    classes inside it take the config that it is inside.

    Args:
        ty: Type to check.
        seen: Types already checked, which a type that refers to itself
            reaches again.
        model_extra: The ``extra`` setting of the Pydantic model that ``ty``
            is inside, through only classes without configs of their own,
            which take it, or ``None`` outside a model.

    Raises:
        TypeError: If a class that the type names would drop keys, by its
            config, an excluded field, or an init-only field, or its
            annotations cannot be resolved.
    """
    seen = set() if seen is None else seen
    try:
        key: Any = (ty, model_extra)
        if key in seen:
            return
    except TypeError:
        # Annotated metadata can make a type unhashable.
        key = (id(ty), model_extra)
        if key in seen:
            return
    seen.add(key)

    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    for nested, nested_extra in _nested_types(ty, origin, args, model_extra):
        _check_undeclared_keys(nested, seen, model_extra=nested_extra)


def _nested_types(
    ty: Any, origin: Any, args: tuple[Any, ...], model_extra: str | None
) -> list[tuple[Any, str | None]]:
    """Return the types that one level of a type names, checking its class.

    Args:
        ty: Type to inspect.
        origin: ``typing.get_origin(ty)``.
        args: ``typing.get_args(ty)``.
        model_extra: The ``extra`` setting of the Pydantic model whose config
            ``ty`` takes, as ``_check_undeclared_keys()`` says, or ``None``.

    Returns:
        The types to check next, such as the value of a named alias or the
        field types of a class, each with the model setting that it takes.

    Raises:
        TypeError: If ``ty`` is a class, or a parameterized one, that would
            drop keys, or whose annotations cannot be resolved.
    """
    if _is_type_alias(ty):
        return [(value, model_extra) for value in _alias_value(ty, (), ())]
    if _is_type_alias(origin):
        values = _alias_value(origin, origin.__type_params__, args)
        return [(value, model_extra) for value in values]
    supertype = _supertype(ty)
    if supertype is not None:
        return [(supertype, model_extra)]
    if isinstance(ty, TypeVar):
        bound = [ty.__bound__] if ty.__bound__ is not None else []
        return [(arg, model_extra) for arg in [*bound, *ty.__constraints__]]
    if origin is Literal:
        return []
    if origin is Annotated:
        return [(args[0], model_extra)]

    cls = ty if origin is None else origin
    if lenient_issubclass(cls, BaseModel):
        # A parameterized generic model is a class of its own, whose fields
        # have its type arguments.
        extra = cls.model_config.get("extra") or "ignore"
        return [(hint, extra) for hint in _model_field_types(cls)]
    if not (
        is_typeddict(cls) or _is_dataclass_class(cls) or _is_named_tuple(cls)
    ):
        return [(arg, model_extra) for arg in args]
    # A class without a config of its own inside a model takes the model's.
    takes = (
        model_extra
        if getattr(cls, "__pydantic_config__", None) is None
        else None
    )
    if _is_named_tuple(cls):
        nested = model_extra
    elif (is_typeddict(cls) and takes is not None) or takes == "forbid":
        nested = takes
    else:
        _check_extra(cls)
        nested = None
    params = getattr(cls, "__parameters__", ())
    substitutions = dict(zip(params, args, strict=False)) if args else {}
    return [
        (_substitute(hint, substitutions), nested)
        for hint in _field_types(cls)
    ]


def _model_field_types(cls: type[BaseModel]) -> list[Any]:
    """Return the types of a Pydantic model's fields.

    Args:
        cls: Pydantic model class.

    Returns:
        The types.

    Raises:
        TypeError: If the model excludes a field from writes, or its
            annotations cannot be resolved, so that they cannot be checked.
    """
    if not getattr(cls, "__pydantic_complete__", True):
        # A model that names a class defined after it holds the name as a
        # ForwardRef, behind which nothing would be checked, until it is
        # rebuilt.
        try:
            complete = cls.model_rebuild()
        except Exception as err:
            raise _unresolved(cls, err) from err
        if complete is False:
            raise _unresolved(cls, NameError("a name is undefined"))
    for name, info in cls.model_fields.items():
        _check_written(cls, name, info)
    return [info.annotation for info in cls.model_fields.values()]


def _check_written(cls: Any, name: str, info: FieldInfo) -> None:
    """Refuse a field that Pydantic reads but may not write.

    Args:
        cls: Class of the field.
        name: Name of the field.
        info: Pydantic's description of the field.

    Raises:
        TypeError: If the field is excluded from writes, always or by a
            condition, since a write would then drop its key.
    """
    if info.exclude or getattr(info, "exclude_if", None) is not None:
        raise TypeError(
            f"{cls.__name__} would drop the key of its field {name}, which "
            "Pydantic excludes from writes. Remove the exclusion, or annotate "
            "the field's JSON form."
        )


def _field_types(cls: Any) -> list[Any]:
    """Return the types of the values that a class reads and writes.

    These are the types of a ``TypedDict``'s keys and of its extra items, and
    of the fields of a ``NamedTuple`` or dataclass, not its class variables.
    A Pydantic dataclass gives the types that Pydantic resolved, which may
    name classes that its annotations cannot. A standard library dataclass's
    fields are resolved one by one, so that a class variable whose
    annotation names a type that exists only for type checkers does not
    matter.

    Args:
        cls: ``TypedDict``, dataclass, or ``NamedTuple`` class.

    Returns:
        The types.

    Raises:
        TypeError: If the class has an init-only field, which it reads but
            does not write, or a field that Pydantic excludes from writes, or
            the annotations of its fields cannot be resolved, so that they
            cannot be checked.
    """
    if _is_dataclass_class(cls):
        for name, field in cls.__dataclass_fields__.items():
            if field._field_type is dataclasses._FIELD_INITVAR:  # type: ignore[attr-defined]
                _refuse_init_only(cls, name)
        pydantic_fields = getattr(cls, "__pydantic_fields__", None)
        if pydantic_fields is not None and getattr(
            cls, "__pydantic_complete__", False
        ):
            for name, info in pydantic_fields.items():
                _check_written(cls, name, info)
            return [info.annotation for info in pydantic_fields.values()]
        field_hints = []
        for field in dataclasses.fields(cls):
            hint = _field_hint(cls, field.name)
            default = (
                PydanticUndefined
                if field.default is dataclasses.MISSING
                else field.default
            )
            info = FieldInfo.from_annotated_attribute(hint, default)
            _check_written(cls, field.name, info)
            field_hints.append(hint)
        return field_hints

    try:
        hints = get_type_hints(cls, include_extras=True)
        extra = _extra_items(cls) if is_typeddict(cls) else None
    except Exception as err:
        raise _unresolved(cls, err) from err
    if is_typeddict(cls):
        return [*hints.values(), *([extra] if extra is not None else [])]
    return [hints[name] for name in cls._fields if name in hints]


def _field_hint(cls: Any, name: str) -> Any:
    """Resolve the annotation of one field of a dataclass.

    Args:
        cls: Dataclass.
        name: Name of one of its fields.

    Returns:
        The field's type, with ``Annotated`` metadata.

    Raises:
        TypeError: If the annotation cannot be resolved.
    """
    owner = next(base for base in cls.__mro__ if name in _annotations(base))
    holder = type(
        owner.__name__,
        (),
        {
            "__module__": owner.__module__,
            "__annotations__": {name: _annotations(owner)[name]},
        },
    )
    # A class defined inside a function is absent from its module's globals.
    # Its namespace resolves its own name, without the defaults of its
    # fields, which could hide the types that their annotations name.
    fields = {field.name for field in dataclasses.fields(cls)}
    localns = {
        key: value for key, value in vars(owner).items() if key not in fields
    }
    localns[owner.__name__] = owner
    try:
        return get_type_hints(holder, localns=localns, include_extras=True)[
            name
        ]
    except Exception as err:
        raise _unresolved(cls, err) from err


def _unresolved(cls: Any, err: Exception) -> TypeError:
    """Return the error for a class whose field types cannot be resolved.

    Args:
        cls: The class.
        err: The error that resolving raised.

    Returns:
        The error to raise.
    """
    return TypeError(
        f"The fields of {cls.__name__} cannot be checked for the keys that it "
        f"would drop, because their annotations cannot be resolved: {err}"
    )


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


def _is_dataclass_class(cls: Any) -> bool:
    """Return whether a type is a dataclass class.

    Args:
        cls: Type to test.

    Returns:
        Whether ``cls`` is a dataclass, of Pydantic or the standard library,
        and a class, not an instance.
    """
    return isinstance(cls, type) and dataclasses.is_dataclass(cls)


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

    ``datetime``, ``date``, ``time``, ``timedelta``, ``UUID``, ``Decimal``, and
    enum types that ``ty`` names, also as type arguments or as the supertype
    of a ``NewType``, are replaced by types that convert as eazyrest's fields
    do. Types inside a class that ``ty`` names are left to that class.

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
            return _table_type(supertype, ty)
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
            a mask, or because it is a mapping with keys that the field's
            type does not write. The message does not quote the value.
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

    read_back = _decode(adapter, encoded)
    # Pydantic writes a TypedDict without the keys that its config forbids
    # or ignores, and a dataclass or model without the fields of a subclass,
    # at any depth, and the dumps below lack them too.
    if _drops(written, read_back):
        raise ValueError(
            "The value has keys or fields that the field does not write, so "
            "writing it would drop them"
        )

    if not _same(adapter, written, read_back):
        raise ValueError("The value does not read back as written")

    return encoded


def _drops(written: Any, read_back: Any) -> bool:
    """Return whether writing a value dropped some of its keys or fields.

    Only the shape is compared: the keys of mappings, the fields of
    dataclasses and models, and the lengths of sequences and sets, at any
    depth. The values themselves are compared by their dumps.

    Args:
        written: The value written.
        read_back: The value that its JSON reads as.

    Returns:
        Whether ``read_back`` lacks a key, field, or item that ``written``
        has.
    """
    if isinstance(written, Mapping):
        if not isinstance(read_back, Mapping) or len(read_back) < len(written):
            return True
        return any(
            _drops(item, read_back[key])
            for key, item in written.items()
            if key in read_back
        )
    if isinstance(written, BaseModel):
        fields = _names(written)
        if not isinstance(read_back, BaseModel) or not fields <= _names(
            read_back
        ):
            return True
        return any(
            _drops(getattr(written, name), getattr(read_back, name))
            for name in fields
        )
    if dataclasses.is_dataclass(written) and not isinstance(written, type):
        fields = {field.name for field in dataclasses.fields(written)}
        if not dataclasses.is_dataclass(read_back) or not fields <= {
            field.name for field in dataclasses.fields(read_back)
        }:
            return True
        return any(
            _drops(getattr(written, name), getattr(read_back, name))
            for name in fields
        )
    if isinstance(written, (list, tuple)):
        if not isinstance(read_back, (list, tuple)) or len(read_back) != len(
            written
        ):
            return True
        return any(
            _drops(item, back)
            for item, back in zip(written, read_back, strict=True)
        )
    if isinstance(written, (set, frozenset)):
        return not isinstance(read_back, (set, frozenset)) or len(
            read_back
        ) != len(written)
    return False


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
        names = _names(written)
        return (
            type(read_back) is type(written)
            and names == _names(read_back)
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
                (name, _key(getattr(value, name))) for name in _names(value)
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


def _names(model: BaseModel) -> set[str]:
    """Return the names of a model's fields and of its undeclared keys.

    Args:
        model: Pydantic model instance.

    Returns:
        The names of its class's fields, and the keys of its
        ``model_extra``, if its config keeps undeclared keys.
    """
    return set(type(model).model_fields) | set(model.model_extra or {})


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
