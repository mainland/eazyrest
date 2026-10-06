"""Conversions between Python values and their JSON forms.

A conversion describes one Python type's JSON form: how to read the type
from JSON, and how to write it. The default table holds eazyrest's
conversions for ``datetime``, ``timedelta``, and enums. JSON scalars need
none.
"""

from __future__ import annotations

import datetime
import enum
import typing
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NamedTuple

import isodate

from .dateparse import parse_datetime, parse_duration


@dataclass(frozen=True, kw_only=True)
class Conversion:
    """How to read and write one Python type's JSON form.

    Each function receives the declared type as well as the value, so that
    one conversion serves a family of types, such as every enum. The fields
    are keyword arguments, so that later versions can add fields.
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


DEFAULT_CONVERSIONS: Mapping[type[Any], Conversion] = MappingProxyType(
    {
        datetime.datetime: Conversion(
            decode=lambda value, _: parse_datetime(value),
            encode=lambda value, _: value.isoformat(),
        ),
        datetime.timedelta: Conversion(
            decode=lambda value, _: parse_duration(value),
            encode=lambda value, _: isodate.duration_isoformat(value),
        ),
        enum.Enum: Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, _: value.value,
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
