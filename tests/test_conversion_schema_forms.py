"""A conversion's schema is copied into plain dictionaries and lists once.

Every later step reads that one form: the check that a schema can be embedded,
the copy that a model's schema holds, and the renaming of definitions. A schema
that is no mapping is refused when the conversion is made.
"""

from __future__ import annotations

import datetime
import json
from collections import UserDict
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

import pytest

from eazyrest import (
    DEFAULT_CONVERSIONS,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)


class Span:
    """A value whose conversion reuses a default's schema."""


class Other:
    """A value whose conversion defines the same name differently."""


def conversion(schema: Any) -> Conversion:
    """Return a conversion of a value that has the given schema."""
    return Conversion(
        decode=lambda value, cls: cls(),
        encode=lambda value, cls: [],
        schema=schema,
    )


DATE = DEFAULT_CONVERSIONS[datetime.date].schema


@pytest.mark.parametrize("mode", ["read", "write"])
def test_a_default_schema_reused_inside_another_is_written(
    mode: Literal["read", "write"],
) -> None:
    """The read-only mapping of a default becomes a plain dictionary."""

    @json_object
    class Booking(JSONObject):
        """A model whose conversion holds the default date schema."""

        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Span: conversion(
                {"type": "array", "items": DATE, "anyOf": (DATE,)}
            )
        }
        class_url = "/bookings/"
        id: int
        stay: Span

    stay = Booking.json_schema(mode)["properties"]["stay"]
    assert stay["items"] == {"type": "string", "format": "date"}
    assert stay["anyOf"] == [{"type": "string", "format": "date"}]
    assert json.loads(json.dumps(stay)) == stay


@pytest.mark.parametrize("mode", ["read", "write"])
def test_a_mapping_that_is_no_dict_follows_its_definition_when_renamed(
    mode: Literal["read", "write"],
) -> None:
    """A reference held by another mapping type is renamed with the rest."""

    @json_object
    class Holder(JSONObject):
        """A model whose conversions define ``Part`` differently."""

        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Span: conversion(
                {
                    "anyOf": [UserDict({"$ref": "#/$defs/Part"})],
                    "$defs": {"Part": {"type": "integer"}},
                }
            ),
            Other: conversion(
                {
                    "anyOf": [UserDict({"$ref": "#/$defs/Part"})],
                    "$defs": {"Part": {"type": "string"}},
                }
            ),
        }
        class_url = "/holders/"
        id: int
        span: Span
        other: Other

    schema = Holder.json_schema(mode)
    assert schema["properties"]["span"]["anyOf"] == [{"$ref": "#/$defs/Part"}]
    assert schema["properties"]["other"]["anyOf"] == [
        {"$ref": "#/$defs/Part_2"}
    ]
    assert json.loads(json.dumps(schema)) == schema


@pytest.mark.parametrize(
    "schema", [True, False, "string", 7, None, [{"type": "string"}]]
)
def test_a_schema_that_is_no_mapping_is_refused_when_it_is_made(
    schema: Any,
) -> None:
    """The conversion raises ``TypeError`` that says what is wrong."""
    with pytest.raises(TypeError, match="schema must be a mapping"):
        conversion(schema)


@pytest.mark.parametrize("schema", [True, "string", [{"type": "string"}]])
def test_a_function_that_returns_no_mapping_is_refused(schema: Any) -> None:
    """The model's schema raises when it calls the function."""

    @json_object
    class Holder(JSONObject):
        """A model whose conversion's function gives the schema."""

        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Span: conversion(lambda ty, mode: schema)
        }
        class_url = "/holders/"
        id: int
        span: Span

    with pytest.raises(FieldConversionError) as caught:
        Holder.json_schema()
    assert isinstance(caught.value.__cause__, TypeError)


class Bit:
    """A value whose definition of ``Flag`` is the numbers 0 and 1."""


class Truth:
    """A value whose definition of ``Flag`` is the booleans."""


@pytest.mark.parametrize("mode", ["read", "write"])
def test_definitions_that_differ_as_true_and_one_are_kept_apart(
    mode: Literal["read", "write"],
) -> None:
    """Python's ``True == 1`` does not make two definitions one."""

    @json_object
    class Switch(JSONObject):
        """A model whose conversions define ``Flag`` differently."""

        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Bit: conversion(
                {
                    "$ref": "#/$defs/Flag",
                    "$defs": {"Flag": {"enum": [0, 1]}},
                }
            ),
            Truth: conversion(
                {
                    "$ref": "#/$defs/Flag",
                    "$defs": {"Flag": {"enum": [False, True]}},
                }
            ),
        }
        class_url = "/switches/"
        id: int
        bit: Bit
        truth: Truth

    schema = Switch.json_schema(mode)
    assert schema["$defs"]["Flag"]["enum"] == [0, 1]
    assert schema["$defs"]["Flag"]["enum"][0] is not False
    assert schema["$defs"]["Flag_2"]["enum"] == [False, True]
    assert schema["properties"]["bit"] == {"$ref": "#/$defs/Flag"}
    assert schema["properties"]["truth"] == {"$ref": "#/$defs/Flag_2"}
