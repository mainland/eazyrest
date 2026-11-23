"""An empty fixed tuple has a valid JSON Schema."""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest
from jsonschema import Draft202012Validator

from eazyrest import JSONObject, json_object


class Nothing(NamedTuple):
    """A tuple class without fields."""


@json_object
class Holder(JSONObject):
    """A model with fixed tuples of no items and of two."""

    class_url = "/holders/"
    id: int
    empty: tuple[()]
    named: Nothing
    pair: tuple[int, str]


@pytest.mark.parametrize("mode", ["read", "write"])
def test_the_schema_of_a_model_with_an_empty_tuple_is_valid(mode: Any) -> None:
    """The schema passes the dialect's own check."""
    schema = Holder.json_schema(mode)
    Draft202012Validator.check_schema(schema)
    properties = schema["properties"]
    for name in ("empty", "named"):
        assert "prefixItems" not in properties[name]
    assert properties["pair"]["prefixItems"] == [
        {"type": "integer"},
        {"type": "string"},
    ]


@pytest.mark.parametrize("mode", ["read", "write"])
def test_an_empty_tuple_accepts_only_an_empty_array(mode: Any) -> None:
    """The array is empty, and an array with an item is refused."""
    validator = Draft202012Validator(Holder.json_schema(mode))
    good = {"id": 1, "empty": [], "named": [], "pair": [1, "a"]}
    assert validator.is_valid(good)
    assert not validator.is_valid({**good, "empty": [1]})
    assert not validator.is_valid({**good, "named": [1]})
