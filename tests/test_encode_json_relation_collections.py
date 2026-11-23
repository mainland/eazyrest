"""``encode_json()`` refuses the collections of relations that a field does."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any, NewType

import pytest

from eazyrest import JSONObject, encode_json, json_object


@json_object(pk="slug")
class Tag(JSONObject):
    """A related model keyed by text."""

    class_url = "/tags/"
    slug: str


A, B = Tag(slug="a"), Tag(slug="b")
Tags = NewType("Tags", Iterable[Tag])  # type: ignore[misc]

REFUSED: dict[str, tuple[Any, Any]] = {
    "collection": (Collection[Tag], [A, B]),
    "iterable": (Iterable[Tag], [A, B]),
    "optional_collection": (Collection[Tag] | None, [A]),
    "newtype_of_an_iterable": (Tags, [A]),
    "optional_items": (list[Tag | None], [A, B]),
    "optional_items_of_a_set": (set[Tag | None], {A}),
    "list_of_lists": (list[list[Tag]], [[A], [B]]),
    "list_of_sets": (list[set[Tag]], [{A, B}]),
}


def declares(annotation: Any) -> None:
    """Declare a model with a field of the annotation."""
    model: Any = type(
        "Model",
        (JSONObject,),
        {
            "__annotations__": {"id": int, "field": annotation},
            "class_url": "/models/",
        },
    )
    json_object(model)


@pytest.mark.parametrize("name", REFUSED)
def test_a_field_refuses_what_encode_json_refuses(name: str) -> None:
    """The refusal is the same for the field and for the function."""
    annotation, value = REFUSED[name]
    with pytest.raises(TypeError) as field_error:
        declares(annotation)
    with pytest.raises(TypeError) as encode_error:
        encode_json(value, annotation)
    reason = str(field_error.value).partition(" is ")[2]
    assert str(encode_error.value).partition(" is ")[2] == reason


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (list[Tag], [A, B], ["a", "b"]),
        (set[Tag], {B, A}, ["a", "b"]),
        (list[Tag] | None, None, None),
    ],
)
def test_the_collections_that_a_field_takes_are_encoded(
    annotation: Any, value: Any, expected: Any
) -> None:
    """A sequence and a set of relations encode as lists of keys."""
    assert encode_json(value, annotation) == expected
