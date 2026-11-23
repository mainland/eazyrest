"""A set whose items have no hash is refused when the class is decorated."""

from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple

import pytest

from eazyrest import Conversion, JSONObject, json_object


@dataclasses.dataclass
class Point:
    """A dataclass that is not frozen, so it has no hash."""

    x: int


@dataclasses.dataclass(frozen=True)
class Fixed:
    """A frozen dataclass, which has a hash."""

    x: int


class Pair(NamedTuple):
    """A tuple class, which has a hash."""

    a: int
    b: int


def declare(annotation: Any, **namespace: Any) -> Any:
    """Decorate a model with one field of the given annotation."""
    model: Any = type(
        "Shape",
        (JSONObject,),
        {
            "__annotations__": {"id": int, "value": annotation},
            "class_url": "/shapes/",
            **namespace,
        },
    )
    return json_object(model)


@pytest.mark.parametrize(
    ("annotation", "item"),
    [
        (set[Point], "Point"),
        (frozenset[Point], "Point"),
        (set[dict[str, int]], "dict"),
        (frozenset[list[int]], "list"),
        (set[set[int]], "set"),
        (set[Point | None], "Point"),
        (set[tuple[int, list[int]]], "list"),
        (set[Point] | None, "Point"),
    ],
    ids=[
        "dataclass",
        "frozenset_of_dataclass",
        "dict",
        "list",
        "set",
        "optional_item",
        "tuple_holding_a_list",
        "optional_set",
    ],
)
def test_the_field_is_refused_naming_the_model_the_field_and_the_item(
    annotation: Any, item: str
) -> None:
    """The error is a ``TypeError`` raised at decoration."""
    with pytest.raises(TypeError) as caught:
        declare(annotation)
    message = str(caught.value)
    assert message.startswith("Shape.value: ")
    assert f"{item} has none" in message


@pytest.mark.parametrize(
    "annotation",
    [
        set[int],
        set[str],
        frozenset[Fixed],
        set[Pair],
        set[tuple[int, str]],
        set[int | None],
        set[Any],
        list[Point],
    ],
)
def test_a_set_of_items_that_have_a_hash_is_admitted(annotation: Any) -> None:
    """Only a set whose items cannot be hashed is refused."""
    declare(annotation)


def test_an_item_that_a_conversion_reads_is_admitted() -> None:
    """The conversion decides what the item reads as, so the class passes."""
    entry = Conversion(
        decode=lambda value, _: tuple(value),
        encode=lambda value, _: list(value),
        schema={"type": "array"},
    )
    declare(set[Point], conversions={Point: entry})
