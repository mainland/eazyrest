"""A bulk hook's related objects are adopted wherever they are cached.

An object that a hook builds may hold related objects in a list, tuple, set,
frozenset, or deque. Each of them leaves the hook's client, whose request
options the query's objects do not keep, for the query's own API.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

import pytest

from eazyrest import API, JSONObject, json_object
from eazyrest._models import _adopt_hook_results, _Query

BASE = "https://hook-collections.example/"
api = API(BASE)
OPTIONS = {"headers": {"X-Hook": "1"}}


@json_object
class Tag(JSONObject):
    """A related model that a hook builds."""

    class_url = "/tags/"
    id: int


@json_object
class Box(JSONObject):
    """A model whose hook returns objects that hold tags."""

    class_url = "/boxes/"
    id: int
    tags: list[Tag]


Tag.register_api(api)
Box.register_api(api)

COLLECTIONS: dict[str, Callable[[Iterable[Tag]], Any]] = {
    "list": list,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "deque": deque,
}


@pytest.mark.parametrize("make", COLLECTIONS.values(), ids=COLLECTIONS)
@pytest.mark.parametrize("given", [False, True])
def test_a_related_object_in_a_collection_leaves_the_hooks_client(
    make: Callable[[Iterable[Tag]], Any], given: bool
) -> None:
    """The tag keeps the query's ``api=``, and none of the hook's options."""
    own = api if given else None
    query = _Query.choose(Box, own, OPTIONS)
    hook_client = query.client
    tag = Tag(json={"id": 1}, api=hook_client)
    box = Box(json={"id": 2, "tags": [1]}, api=hook_client)
    box._field_cache["tags"] = make([tag])

    _adopt_hook_results(query, [box])

    assert box._api_override is own
    assert tag._api_override is own
