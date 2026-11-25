"""A model object in the JSON that an object is given is no JSON value.

A model object cannot be copied, but that is not why ``json=`` and
``update_from_json()`` refuse one inside a mapping. The message says that the
object is no JSON value and tells what to give instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, JSONObject, json_object

api = API("https://model-in-json.example/")


@json_object
class Owner(JSONObject):
    """A model whose object is given where JSON belongs."""

    class_url = "/owners/"
    id: int


@json_object
class Thing(JSONObject):
    """A model that is given JSON."""

    class_url = "/things/"
    id: int
    name: str
    tags: list[str]


Owner.register_api(api)
Thing.register_api(api)

BUILDS: dict[str, Callable[[Any], Any]] = {
    "json list": lambda obj: Thing(json={"id": 2, "tags": [obj]}),
    "json nested": lambda obj: Thing(json={"id": 2, "tags": [{"k": obj}]}),
    "json value": lambda obj: Thing(json={"id": 2, "name": obj}),
    "json tuple": lambda obj: Thing(json={"id": 2, "tags": (obj,)}),
    "json nested tuple": lambda obj: Thing(
        json={"id": 2, "tags": [{"k": (1, (obj,))}]}
    ),
    "json set": lambda obj: Thing(json={"id": 2, "tags": {obj}}),
    "json frozenset": lambda obj: Thing(
        json={"id": 2, "tags": frozenset([obj])}
    ),
    "json dict in tuple": lambda obj: Thing(
        json={"id": 2, "tags": ({"k": [obj]},)}
    ),
    "update_from_json tuple": lambda obj: Thing(
        json={"id": 2, "name": "a"}
    ).update_from_json({"tags": (obj,)}),
    "update_from_json list": lambda obj: Thing(
        json={"id": 2, "name": "a"}
    ).update_from_json({"tags": [obj]}),
    "update_from_json value": lambda obj: Thing(
        json={"id": 2, "name": "a"}
    ).update_from_json({"name": obj}),
}


@pytest.mark.parametrize("build", BUILDS.values(), ids=BUILDS.keys())
def test_a_model_object_in_json_is_no_json_value(
    build: Callable[[Any], Any],
) -> None:
    """The error says that the object is no JSON value, not that no copy."""
    with pytest.raises(TypeError, match="Owner object is no JSON value") as e:
        build(Owner(pk=3))
    assert "copied" not in str(e.value)
