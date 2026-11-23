"""Any set in a query is sent sorted, whatever class it is."""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://set-like.example/")


@json_object
class Tag(JSONObject):
    """A related model keyed by text."""

    class_url = "/tags/"
    id: str


@json_object
class Post(JSONObject):
    """A model with a sequence of relations."""

    class_url = "/posts/"
    id: int
    tags: list[Tag]
    title: str


Tag.register_api(api)
Post.register_api(api)


class Backwards(AbstractSet[str]):
    """A set whose iteration order is the reverse of the sorted order."""

    def __init__(self, *items: str) -> None:
        """Hold the items."""
        self._items = frozenset(items)

    def __contains__(self, item: object) -> bool:
        """Return whether the item is held."""
        return item in self._items

    def __iter__(self) -> Iterator[str]:
        """Iterate in descending order."""
        return iter(sorted(self._items, reverse=True))

    def __len__(self) -> int:
        """Return the number of items."""
        return len(self._items)


SETS: dict[str, Any] = {
    "a_set_class_of_the_program": Backwards("a", "b", "c"),
    "keys_of_a_dict": {"c": 1, "a": 2, "b": 3}.keys(),
}


def sent(**params: Any) -> str:
    """Return the query that filter() sends for the parameters."""
    with requests_mock.Mocker() as mocker:
        mocker.get("https://set-like.example/posts/", json=[])
        list(Post.filter(**params))
        return str(mocker.last_request.query)


@pytest.mark.parametrize("value", SETS.values(), ids=SETS.keys())
def test_a_set_for_a_sequence_relation_is_sent_sorted(value: Any) -> None:
    """The relation's own parameter repeats its items in order."""
    assert sent(tags=value) == "tags=a&tags=b&tags=c"


@pytest.mark.parametrize("value", SETS.values(), ids=SETS.keys())
def test_a_set_for_a_lookup_of_several_values_is_sent_sorted(
    value: Any,
) -> None:
    """A lookup that takes several values sends them in order."""
    assert sent(tags__in=value) == "tags__in=a%2cb%2cc"


@pytest.mark.parametrize("value", SETS.values(), ids=SETS.keys())
def test_a_set_for_a_name_that_is_no_field_is_sent_sorted(value: Any) -> None:
    """A name that no field has repeats its items in order."""
    assert sent(extra=value) == "extra=a&extra=b&extra=c"
