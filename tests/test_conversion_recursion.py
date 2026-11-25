"""Code that a read calls and that recurses without end raises its own error.

A field whose type contains itself reports a value that nests more deeply than
the stack takes as ``FieldConversionError``. A ``RecursionError`` that a
decode, an enum's ``_missing_()``, or a constructor raises is a bug in that
code, not in the value, so it propagates as it is, whatever the field's type.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any, Literal, NamedTuple

import pytest
from typing_extensions import TypedDict

from eazyrest import (
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

_DEPTH = 50


def _descend(depth: int) -> Any:
    """Recurse to the fixed depth, then raise the error of a runaway one."""
    if depth >= _DEPTH:
        raise RecursionError("maximum recursion depth exceeded")
    return _descend(depth + 1)


def runaway(*_: Any) -> Any:
    """Raise the error of a recursion without end, after 50 calls.

    A recursion that really has no end is not safe to run. CPython 3.13.5 does
    not stop at the recursion limit an ordinary call of a function that takes
    ``*args`` or has defaults, such as ``def f(*_): return f()``, and 3.15.1
    does not stop one that unpacks its arguments, such as ``def f(*a): return
    f(*a)``. Each grows until memory gives out and then crashes the
    interpreter.
    """
    return _descend(0)


class Stamp:
    """A class that a conversion decodes by code that recurses."""


@dataclasses.dataclass
class Link:
    """A dataclass that holds itself, with a recursing ``__post_init__()``."""

    label: str = ""
    next: Link | None = None

    def __post_init__(self) -> None:
        """Recurse without end."""
        runaway()


@dataclasses.dataclass
class Flat:
    """A dataclass that holds nothing, with a recursing ``__post_init__()``."""

    label: str = ""

    def __post_init__(self) -> None:
        """Recurse without end."""
        runaway()


@dataclasses.dataclass
class Whole:
    """A self-holding dataclass that a conversion replaces."""

    next: Whole | None = None


class Mood(enum.Enum):
    """An enum whose ``_missing_()`` recurses."""

    CALM = "calm"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        return runaway()


class Chain(TypedDict, total=False):
    """A ``TypedDict`` that holds itself, with a member that has a decode."""

    when: Stamp
    mood: Mood
    pick: Literal[Mood.CALM]
    held: Whole
    next: Chain | None


class Flatter(TypedDict, total=False):
    """A ``TypedDict`` that holds nothing, with a member that has a decode."""

    when: Stamp


class Cell(NamedTuple):
    """A ``NamedTuple`` that holds itself, with a member that has a decode."""

    when: Stamp
    next: Cell | None = None


class Pair(NamedTuple):
    """A ``NamedTuple`` that holds itself, with a recursing constructor."""

    label: str
    next: Pair | None = None


def _recursing_new(cls: type[Pair], *args: Any) -> Pair:
    runaway()
    return tuple.__new__(cls, args)


Pair.__new__ = _recursing_new  # type: ignore[method-assign,assignment]


@json_object
class Doc(JSONObject):
    """A model with a field of each type, and a recursing decode of two."""

    conversions = {  # noqa: RUF012
        Stamp: Conversion(decode=runaway, encode=lambda value, cls: "x"),
        Whole: Conversion(decode=runaway, encode=runaway),
    }
    class_url = "/docs/"
    id: int
    whole: Whole
    chain: Chain
    flatter: Flatter
    cell: Cell
    pair: Pair
    link: Link
    flat: Flat
    mood: Mood
    pick: Literal[Mood.CALM]


ROWS = {
    "decode of a self-holding type": ("whole", {}),
    "decode in a member of a TypedDict that holds itself": (
        "chain",
        {"when": "x"},
    ),
    "decode in a TypedDict that holds nothing": ("flatter", {"when": "x"}),
    "decode in a member of a NamedTuple that holds itself": (
        "cell",
        ["x", None],
    ),
    "enum decode in a TypedDict that holds itself": (
        "chain",
        {"mood": "other"},
    ),
    "Literal decode in a TypedDict that holds itself": (
        "chain",
        {"pick": "other"},
    ),
    "decode of a converted class in a TypedDict that holds itself": (
        "chain",
        {"held": {}},
    ),
    "constructor of a NamedTuple that holds itself": ("pair", ["a", None]),
    "post-init of a dataclass that holds itself": ("link", {"label": "a"}),
    "post-init of a dataclass that holds nothing": ("flat", {"label": "a"}),
    "enum decode of a field": ("mood", "other"),
    "Literal decode of a field": ("pick", "other"),
}


@pytest.mark.parametrize(("field", "raw"), ROWS.values(), ids=ROWS)
def test_a_recursion_in_code_that_a_read_calls_is_raised_as_it_is(
    field: str, raw: Any
) -> None:
    """The error is the code's own, whatever the field's type."""
    obj = Doc(json={"id": 1, field: raw})
    with pytest.raises(RecursionError) as caught:
        getattr(obj, field)
    assert not isinstance(caught.value, FieldConversionError)


def test_a_recursion_in_an_encode_is_raised_as_it_is() -> None:
    """An assignment reports the encode's own error, not a value too deep."""
    obj = Doc(json={"id": 1})
    with pytest.raises(RecursionError) as caught:
        obj.whole = Whole()
    assert not isinstance(caught.value, FieldConversionError)
