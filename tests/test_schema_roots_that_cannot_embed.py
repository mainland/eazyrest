"""A conversion's schema is refused if it could not be embedded in a model's.

``json_schema()`` embeds the schema of a conversion in the model's, moving its
``$defs`` into the model's. A ``$defs`` that is no mapping could not be moved,
and a ``$schema`` below the root of a model's schema is forbidden by JSON
Schema 2020-12, as ``$id`` is. The conversion raises ``TypeError`` when it is
made, or when ``json_schema()`` calls a function that returns the schema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import Conversion, FieldConversionError, JSONObject, json_object


class Money:
    """A class of the program that a conversion reads."""


def entry(schema: Any) -> Conversion:
    """Return a conversion with a schema."""
    return Conversion(
        decode=lambda value, cls: Money(),
        encode=lambda value, cls: "1",
        schema=schema,
    )


def declare(conversion: Conversion) -> Any:
    """Decorate a model whose field is converted by ``conversion``."""
    base: Any = type(
        "Base", (JSONObject,), {"conversions": {Money: conversion}}
    )
    model: Any = type(
        "Bill",
        (base,),
        {
            "__annotations__": {"id": int, "total": Money},
            "class_url": "/bills/",
        },
    )
    return json_object(model)


BAD_DEFS = {
    "a list": [{"type": "string"}],
    "a string": "text",
    "None": None,
    "true": True,
    "a number": 3,
}


@pytest.mark.parametrize("defs", BAD_DEFS.values(), ids=BAD_DEFS)
def test_a_defs_that_is_no_mapping_is_refused_when_the_conversion_is_made(
    defs: Any,
) -> None:
    """The error names ``$defs`` and the type that was found."""
    with pytest.raises(TypeError, match=r"\$defs only as a mapping"):
        entry({"type": "string", "$defs": defs})


@pytest.mark.parametrize("defs", BAD_DEFS.values(), ids=BAD_DEFS)
def test_a_function_that_returns_such_a_defs_is_refused_by_json_schema(
    defs: Any,
) -> None:
    """The refusal comes when ``json_schema()`` calls the function."""
    model = declare(entry(lambda ty, mode: {"type": "string", "$defs": defs}))
    with pytest.raises(FieldConversionError, match=r"Bill\.total"):
        model.json_schema()


SCHEMAS: dict[str, Callable[[], Any]] = {
    "at the root": lambda: {
        "$schema": "https://json-schema.org/draft/2020-12/schema"
    },
    "of another dialect": lambda: {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "string",
    },
    "in a property": lambda: {
        "type": "object",
        "properties": {"a": {"$schema": "https://example.test/schema"}},
    },
    "in a definition": lambda: {
        "$defs": {"Part": {"$schema": "https://example.test/schema"}},
        "$ref": "#/$defs/Part",
    },
}


@pytest.mark.parametrize("make", SCHEMAS.values(), ids=SCHEMAS)
def test_a_schema_that_has_a_schema_keyword_is_refused(
    make: Callable[[], Any],
) -> None:
    """``$schema`` is refused as ``$id`` is."""
    with pytest.raises(TypeError, match=r"\$schema cannot be embedded"):
        entry(make())


def test_a_function_that_returns_a_schema_keyword_is_refused() -> None:
    """The refusal comes when ``json_schema()`` calls the function."""
    model = declare(
        entry(lambda ty, mode: {"$schema": "https://example.test/s"})
    )
    with pytest.raises(FieldConversionError, match=r"\$schema"):
        model.json_schema()


def test_a_defs_that_is_a_mapping_is_embedded() -> None:
    """A mapping of definitions is taken and moved into the model's."""
    schema = {"$defs": {"Part": {"type": "string"}}, "$ref": "#/$defs/Part"}
    model = declare(entry(schema))
    assert "Part" in model.json_schema()["$defs"]
