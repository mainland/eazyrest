"""A conversion's schema is checked and hoisted inside a list or a tuple."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Literal

import pytest

from eazyrest import Conversion, JSONObject, json_object

SEQUENCES: dict[str, Callable[..., Any]] = {"list": list, "tuple": tuple}


def unembeddable(sequence: Callable[..., Any]) -> dict[str, dict[str, Any]]:
    """Return schemas that cannot be embedded, with the sequence kind given."""
    return {
        "a_reference_to_the_root": {
            "anyOf": sequence([{"$ref": "#"}, {"type": "null"}])
        },
        "an_id": {
            "anyOf": sequence(
                [{"$id": "https://x.example/s"}, {"type": "null"}]
            )
        },
        "an_anchor": {"anyOf": sequence([{"$anchor": "a"}, {"type": "null"}])},
        "defs_below_the_root": {
            "anyOf": sequence(
                [{"$defs": {"A": {"type": "null"}}}, {"type": "null"}]
            )
        },
        "a_reference_to_nothing": {
            "anyOf": sequence([{"$ref": "#/$defs/Missing"}, {"type": "null"}])
        },
    }


NAMES = list(unembeddable(list))


class Thing:
    """A value of a conversion."""


def conversion(schema: Any) -> Conversion:
    """Return a conversion of ``Thing`` that has the given schema."""
    return Conversion(
        decode=lambda value, cls: cls(),
        encode=lambda value, cls: None,
        schema=schema,
    )


@pytest.mark.parametrize("kind", SEQUENCES)
@pytest.mark.parametrize("name", NAMES)
def test_a_schema_given_as_a_mapping_is_refused_inside_a_sequence(
    name: str, kind: str
) -> None:
    """The conversion raises ``TypeError`` when it is made."""
    with pytest.raises(TypeError):
        conversion(unembeddable(SEQUENCES[kind])[name])


@pytest.mark.parametrize("kind", SEQUENCES)
@pytest.mark.parametrize("name", NAMES)
def test_a_schema_given_by_a_function_is_refused_inside_a_sequence(
    name: str, kind: str
) -> None:
    """The model's schema raises ``TypeError`` when it calls the function."""
    schema = unembeddable(SEQUENCES[kind])[name]

    @json_object
    class Holder(JSONObject):
        """A model whose conversion gives the schema."""

        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Thing: conversion(lambda ty, mode: schema)
        }
        id: int
        thing: Thing

    with pytest.raises(TypeError):
        Holder.json_schema()


class Other:
    """A value whose conversion defines the same name differently."""


@pytest.mark.parametrize("mode", ["read", "write"])
def test_definitions_inside_a_tuple_are_renamed_with_their_references(
    mode: Literal["read", "write"],
) -> None:
    """A reference in a tuple follows its definition to its new name."""

    @json_object
    class Holder(JSONObject):
        """A model whose conversions define ``Part`` differently."""

        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Thing: conversion(
                {
                    "anyOf": ({"$ref": "#/$defs/Part"}, {"type": "null"}),
                    "$defs": {"Part": {"type": "integer"}},
                }
            ),
            Other: conversion(
                {
                    "anyOf": ({"$ref": "#/$defs/Part"}, {"type": "null"}),
                    "$defs": {"Part": {"type": "string"}},
                }
            ),
        }
        id: int
        thing: Thing
        other: Other

    schema = Holder.json_schema(mode)
    assert schema["$defs"] == {
        "Part": {"type": "integer"},
        "Part_2": {"type": "string"},
    }
    properties = schema["properties"]
    assert properties["thing"]["anyOf"][0] == {"$ref": "#/$defs/Part"}
    assert properties["other"]["anyOf"][0] == {"$ref": "#/$defs/Part_2"}
