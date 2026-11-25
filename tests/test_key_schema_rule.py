"""One rule gives the JSON schema of a key's type.

The schema of an ``int``, a ``str``, or a ``UUID`` is that of its JSON form,
whether the type is a model's key, the key that a relation names, or any other
field.
"""

import uuid
from typing import Any

import pytest

from eazyrest import JSONObject, json_object

SCHEMAS: list[tuple[type, dict[str, Any]]] = [
    (int, {"type": "integer"}),
    (str, {"type": "string"}),
    (uuid.UUID, {"type": "string", "format": "uuid"}),
]


def _id(case: tuple[type, Any]) -> str:
    return case[0].__name__


def model_of(key_type: type, **attributes: Any) -> type[JSONObject]:
    """Return a model that has a key and a field of ``key_type``."""

    @json_object
    class Thing(JSONObject):
        """A model with a key and another field of one type."""

        class_url = "/things/"
        id: key_type  # type: ignore[valid-type]
        other: key_type  # type: ignore[valid-type]

    return Thing


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("case", SCHEMAS, ids=_id)
def test_a_key_and_a_field_of_one_type_have_one_schema(
    case: tuple[type, dict[str, Any]], mode: str
) -> None:
    """A model's key has the schema that a field of its type has."""
    key_type, expected = case
    properties = model_of(key_type).json_schema(mode)["properties"]  # type: ignore[arg-type]
    assert properties["id"] == expected
    assert properties["other"] == expected


@pytest.mark.parametrize("case", SCHEMAS, ids=_id)
def test_a_relation_names_the_schema_of_the_key_of_its_model(
    case: tuple[type, dict[str, Any]],
) -> None:
    """A related object is its key, and a read also takes it embedded."""
    key_type, expected = case
    related = model_of(key_type)

    @json_object
    class Holder(JSONObject):
        """A model with a relation."""

        class_url = "/holders/"
        id: int
        thing: related  # type: ignore[valid-type]

    read = Holder.json_schema("read")["properties"]["thing"]
    write = Holder.json_schema("write")["properties"]["thing"]
    assert write == expected
    assert read["anyOf"][0] == expected
