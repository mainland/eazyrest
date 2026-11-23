"""What a field and a schema give back is the caller's to change."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from eazyrest import Conversion, JSONObject, json_object


class Thing:
    """A class of the program whose conversion has a schema."""


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"part": {"type": "integer"}},
    "required": ["part"],
    "examples": [{"part": 1}],
}


@json_object
class Holder(JSONObject):
    """A model with a Literal field and a field of a converted class."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Thing: Conversion(
            decode=lambda value, cls: cls(),
            encode=lambda value, cls: {"part": 1},
            schema=SCHEMA,
        )
    }

    class_url = "/holders/"
    id: int
    kind: Literal["a", "b"]
    thing: Thing


def test_a_value_outside_a_literal_is_a_copy_of_the_json() -> None:
    """A field reads what the server sent, which a Literal does not check."""
    holder = Holder(json={"id": 1, "kind": ["x"], "thing": {"part": 1}})
    value: Any = holder.kind
    value.append("y")
    assert holder.json["kind"] == ["x"]


def test_a_schema_is_a_copy_of_the_conversions_own() -> None:
    """Changing a schema that ``json_schema()`` gave changes no other."""
    first = Holder.json_schema()
    first["properties"]["thing"]["properties"]["part"]["type"] = "string"
    first["properties"]["thing"]["required"].append("other")
    first["properties"]["thing"]["examples"].append({"part": 2})
    second = Holder.json_schema()
    assert second["properties"]["thing"] == SCHEMA
    assert SCHEMA["properties"]["part"] == {"type": "integer"}
    assert SCHEMA["required"] == ["part"]
    assert SCHEMA["examples"] == [{"part": 1}]
