"""The read schema requires a model's key, and the write schema nothing."""

from __future__ import annotations

import uuid
from typing import Literal

import pytest

from eazyrest import JSONObject, json_object


@json_object
class Plain(JSONObject):
    """An integer key under the name id."""

    class_url = "/plain/"
    id: int
    name: str


@json_object(pk="slug")
class Slugged(JSONObject):
    """A string key under another name."""

    class_url = "/slugged/"
    slug: str


@json_object
class Document(JSONObject):
    """A UUID key."""

    class_url = "/documents/"
    id: uuid.UUID


@json_object(field_map={"id": "postId"}, write_map={"id": "post_id"})
class Post(JSONObject):
    """An integer key whose JSON and write names differ from its field."""

    class_url = "/posts/"
    id: int
    title: str


MODELS = {
    "plain": (Plain, ["id"]),
    "slug": (Slugged, ["slug"]),
    "uuid": (Document, ["id"]),
    "renamed": (Post, ["postId"]),
}


@pytest.mark.parametrize(("model", "required"), MODELS.values(), ids=MODELS)
def test_the_read_schema_requires_the_keys_json_name(
    model: type[JSONObject], required: list[str]
) -> None:
    """Every object that is read holds its key."""
    schema = model.json_schema("read")
    assert schema["required"] == required
    assert required[0] in schema["properties"]
    assert model.json_schema() == schema


@pytest.mark.parametrize(("model", "required"), MODELS.values(), ids=MODELS)
def test_the_write_schema_requires_nothing(
    model: type[JSONObject], required: list[str]
) -> None:
    """A creation sends no key that the server assigns."""
    assert "required" not in model.json_schema("write")


def test_the_schemas_of_a_model_that_is_not_keyed_require_nothing() -> None:
    """A model without a key field has no key to require."""

    @json_object
    class Entry(JSONObject):
        name: str
        kind: Literal["a", "b"]

    for mode in ("read", "write"):
        assert "required" not in Entry.json_schema(mode)
