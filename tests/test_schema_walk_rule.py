"""One walk reads a conversion's schema, for the check and for the hoisting.

A conversion's schema is checked when the conversion is made, and its
definitions move into a model's ``$defs`` when the model's schema is made. Both
read the same parts of the schema as schemas, as JSON data, and as references.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eazyrest import Conversion, JSONObject, json_object
from eazyrest._models import _hoist_nested_defs


def keep(value: Any, ty: Any) -> Any:
    """Decode or encode a value as it is."""
    return value


def conversion(schema: Any) -> Conversion:
    """Return a conversion whose schema is ``schema``."""
    return Conversion(decode=keep, encode=keep, schema=schema)


def function_conversion(schema: Any) -> Conversion:
    """Return a conversion that makes its schema with a function."""
    return Conversion(
        decode=keep,
        encode=keep,
        schema=lambda ty, mode: json.loads(json.dumps(schema)),
    )


class Thing:
    """A type that a conversion reads."""


class Kennel:
    """A second type whose schema has a definition of the same name."""


def model_schema(schema: Any, make: Any = conversion) -> dict[str, Any]:
    """Return the schema of a model with a field of a type of ``schema``."""

    @json_object
    class Holder(JSONObject):
        """A model with a field that has a conversion's schema."""

        conversions = {Thing: make(schema)}  # noqa: RUF012
        id: int
        thing: Thing

    return Holder.json_schema()


DATA = [
    "const",
    "enum",
    "default",
    "examples",
    "dependentRequired",
    "example",
    "x-extension",
    "discriminatorish",
]
SCHEMA_KEYWORDS = [
    "items",
    "additionalProperties",
    "not",
    "contains",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
    "additionalItems",
]
MAP_KEYWORDS = [
    "properties",
    "patternProperties",
    "dependentSchemas",
    "definitions",
    "dependencies",
]
LIST_KEYWORDS = ["anyOf", "allOf", "oneOf", "prefixItems"]
MAKERS = [conversion, function_conversion]
NESTED = {"$defs": {"D": {"type": "null"}}, "$ref": "#/$defs/D"}


def hoisted(schema: Any) -> tuple[Any, dict[str, Any]]:
    """Move the definitions below a schema's root into shared ones."""
    defs: dict[str, Any] = {}
    return _hoist_nested_defs(schema, defs), defs


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("keyword", DATA)
def test_the_value_of_a_data_keyword_is_data(keyword: str, make: Any) -> None:
    """A value that looks like a schema in a data keyword is kept as it is."""
    data = {"member": {"$id": "urn:x", "$defs": {"D": {"type": "null"}}}}
    schema = model_schema({keyword: data}, make)
    assert schema["properties"]["thing"] == {keyword: data}
    assert hoisted({keyword: data}) == ({keyword: data}, {})


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("keyword", SCHEMA_KEYWORDS)
def test_the_value_of_a_schema_keyword_is_a_schema(
    keyword: str, make: Any
) -> None:
    """A schema in a schema keyword is checked and is hoisted."""
    with pytest.raises(TypeError, match=r"\$id"):
        model_schema({keyword: {"$id": "urn:x"}}, make)
    assert hoisted({keyword: NESTED}) == (
        {keyword: {"$ref": "#/$defs/D"}},
        {"D": {"type": "null"}},
    )


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("keyword", MAP_KEYWORDS)
def test_the_members_of_a_map_keyword_are_schemas(
    keyword: str, make: Any
) -> None:
    """A map keyword's members are schemas and its names are not keywords."""
    with pytest.raises(TypeError, match=r"\$id"):
        model_schema({keyword: {"member": {"$id": "urn:x"}}}, make)
    names = {name: {"type": "string"} for name in ("$id", "const", "$ref")}
    assert model_schema({keyword: names}, make)["properties"]["thing"] == {
        keyword: names
    }
    assert hoisted({keyword: {"const": NESTED}}) == (
        {keyword: {"const": {"$ref": "#/$defs/D"}}},
        {"D": {"type": "null"}},
    )


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("keyword", LIST_KEYWORDS)
def test_the_members_of_a_list_keyword_are_schemas(
    keyword: str, make: Any
) -> None:
    """The members of a list of schemas are checked and hoisted."""
    with pytest.raises(TypeError, match=r"\$id"):
        model_schema({keyword: [{"type": "null"}, {"$id": "urn:x"}]}, make)
    assert hoisted({keyword: [NESTED]}) == (
        {keyword: [{"$ref": "#/$defs/D"}]},
        {"D": {"type": "null"}},
    )


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    "schema",
    [
        {
            "properties": {
                "$ref": {"type": "string"},
                "s": {"type": "string"},
            },
            "dependentRequired": {"$ref": ["s"]},
        },
        {
            "properties": {
                "$id": {"type": "string"},
                "r": {"type": "integer"},
            },
            "dependentRequired": {"$id": ["r"]},
        },
        {"type": "object", "example": {"$id": "doc-1", "title": "First"}},
        {"type": "object", "x-note": {"$ref": "#/$defs/Missing"}},
    ],
    ids=["ref", "id", "example", "extension"],
)
def test_a_property_named_like_a_keyword_in_data_is_data(
    schema: Any, make: Any
) -> None:
    """A name or key in a data keyword, or an unlisted one, is no keyword."""
    assert model_schema(schema, make)["properties"]["thing"] == schema


CAT = {"type": "object", "title": "Cat"}


def mapped(target: Any) -> dict[str, Any]:
    """Return a schema whose discriminator maps a tag to ``target``."""
    return {
        "oneOf": [{"$ref": "#/$defs/Cat"}],
        "discriminator": {"propertyName": "kind", "mapping": {"x": target}},
        "$defs": {"Cat": CAT},
    }


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    "target", ["#/$defs/Dog", "#/$defs/Dog/properties/name"]
)
def test_a_mapping_that_names_a_missing_definition_is_refused(
    target: str, make: Any
) -> None:
    """A mapping value into ``$defs`` must name a definition, as ``$ref``."""
    with pytest.raises(TypeError, match="definition that its"):
        model_schema(mapped(target), make)
    with pytest.raises(TypeError, match="definition that its"):
        model_schema({"$ref": target, "$defs": {"Cat": CAT}}, make)


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    "target", ["Cat", "Dog", "#/components/schemas/Cat", "", 5, None]
)
def test_other_forms_of_a_mapping_value_stay_as_they_are(
    target: Any, make: Any
) -> None:
    """A bare name, another pointer, or a non-string is not checked."""
    schema = model_schema(mapped(target), make)
    assert schema["properties"]["thing"]["discriminator"]["mapping"] == {
        "x": target
    }


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("target", ["#/$defs/Cat", "#/$defs/Cat/title"])
def test_a_mapping_that_names_a_definition_is_accepted(
    target: str, make: Any
) -> None:
    """A mapping value that names a definition of the schema is accepted."""
    schema = model_schema(mapped(target), make)
    assert schema["$defs"]["Cat"] == CAT


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
def test_a_discriminator_tag_is_no_keyword(make: Any) -> None:
    """A tag named like a keyword in a mapping is data."""
    schema = mapped("#/$defs/Cat")
    schema["discriminator"]["mapping"] = {"$id": "#/$defs/Cat"}
    assert model_schema(schema, make)["$defs"]["Cat"] == CAT


def test_a_mapping_is_renamed_with_its_definition_on_a_collision() -> None:
    """A mapping value follows a definition that moves under a new name."""

    @json_object
    class Home(JSONObject):
        """A model whose two conversions each define ``Cat``."""

        conversions = {  # noqa: RUF012
            Thing: conversion(mapped("#/$defs/Cat")),
            Kennel: conversion(
                {"$ref": "#/$defs/Cat", "$defs": {"Cat": {"type": "string"}}}
            ),
        }
        id: int
        pet: Thing
        kennel: Kennel

    schema = Home.json_schema()
    names = sorted(schema["$defs"])
    assert names == ["Cat", "Cat_2"]
    first = schema["properties"]["pet"]["discriminator"]["mapping"]["x"]
    other = schema["properties"]["kennel"]["$ref"]
    assert {first, other} == {"#/$defs/Cat", "#/$defs/Cat_2"}
    assert schema["$defs"][first[8:]] == CAT
    assert schema["$defs"][other[8:]] == {"type": "string"}


def test_a_nested_definition_with_a_mapping_is_hoisted() -> None:
    """A definition below the root moves up with its mapping renamed."""
    inner = mapped("#/$defs/Cat")
    schema, defs = hoisted({"properties": {"pet": inner}})
    assert defs == {"Cat": CAT}
    assert schema["properties"]["pet"]["discriminator"]["mapping"] == {
        "x": "#/$defs/Cat"
    }
    assert "$defs" not in schema["properties"]["pet"]


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
def test_a_definition_named_like_a_keyword_is_a_definition(make: Any) -> None:
    """The names in ``$defs`` are no keywords, though a definition may be."""
    definitions = {
        "$id": {"type": "string"},
        "const": {"$ref": "#/$defs/$id"},
    }
    schema = model_schema(
        {"$ref": "#/$defs/const", "$defs": definitions}, make
    )
    assert schema["$defs"] == definitions
