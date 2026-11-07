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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .dateparse import parse_duration


@dataclass(frozen=True, kw_only=True)
class Conversion:
    """How to read and write one Python type's JSON form.

    Each function receives the declared type as well as the value, so that
    one conversion serves a family of types, such as every enum.
    """

    decode: Callable[[Any, type[Any]], Any]
    """Read a JSON value, other than ``null``, as a value of the declared
    type. ``null`` reads as ``None`` for every type, without a conversion."""

    encode: Callable[[Any, type[Any]], Any]
    """Write a value of the declared type as JSON. It is called only for
    instances of the declared type, so any other value, such as a string
    assigned to a ``datetime`` field, is written as it is."""


DEFAULT_CONVERSIONS: Mapping[type[Any], Conversion] = MappingProxyType(
    {
        datetime.datetime: Conversion(
            decode=lambda value, _: datetime.datetime.fromtimestamp(
                value, datetime.timezone.utc
            ),
            encode=lambda value, _: value.timestamp(),
        ),
        datetime.timedelta: Conversion(
            decode=lambda value, _: parse_duration(value),
            encode=lambda value, _: str(value),
        ),
        enum.Enum: Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, _: value.value,
        ),
    }
)
"""eazyrest's conversions, keyed by the type they convert."""


def _is_class(ty: Any) -> bool:
    """Return whether an annotation is a class, not a parameterized one.

    Args:
        ty: Annotation.

    Returns:
        Whether ``ty`` is a class. Python 3.10 counts ``list[int]`` as a
        type, so a parameterized type is excluded explicitly.
    """
    return isinstance(ty, type) and typing.get_origin(ty) is None


def _conversion_of(ty: Any) -> Conversion | None:
    """Return the conversion for a declared type, if it has one.

    An enum converts by the ``Enum`` entry. Any other type converts only by
    an entry for exactly that type.

    Args:
        ty: Declared type of a field or item.

    Returns:
        The conversion, or ``None`` for a type whose JSON value is read and
        written as it is.
    """
    if not _is_class(ty):
        return None
    if ty in DEFAULT_CONVERSIONS:
        return DEFAULT_CONVERSIONS[ty]
    if issubclass(ty, enum.Enum):
        return DEFAULT_CONVERSIONS[enum.Enum]
    return None
