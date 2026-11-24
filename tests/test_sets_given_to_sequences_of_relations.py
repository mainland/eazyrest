"""A set is refused where it would be written as a sequence of relations.

A set has no order, and a model object hashes by its class, whose hash is its
address, so a sequence written from a set would differ from one run to the
next, even under a fixed ``PYTHONHASHSEED``. A set field takes a set, a query
sends a set sorted, and a list or a tuple of relations keeps its order.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://set-sequences.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object(pk="slug")
class Tag(Base):
    """A related model with a text key."""

    class_url = "/tags/"
    slug: str


@json_object
class Post(Base):
    """A model with sequences of relations and a set of them."""

    class_url = "/posts/"
    id: int
    tags: list[Tag]
    tagt: tuple[Tag, ...]
    tagq: deque[Tag]
    tagset: set[Tag]
    scores: list[int]


def tags() -> set[Tag]:
    """Return a set of related objects."""
    return {Tag(slug=name) for name in "dbcae"}


def loaded(mode: str = "lazy") -> Post:
    """Return a post that has read its fields."""
    return Post(
        json={
            "id": 1,
            "tags": [],
            "tagt": [],
            "tagq": [],
            "tagset": [],
            "scores": [],
        },
        write_mode=mode,  # type: ignore[arg-type]
    )


def assign(field: str, value: Any, mode: str) -> None:
    """Assign a value to a field of a loaded post."""
    setattr(loaded(mode), field, value)


WRITES: dict[str, Callable[[str, Any], Any]] = {
    "assignment": lambda f, v: assign(f, v, "lazy"),
    "eager assignment": lambda f, v: assign(f, v, "eager"),
    "create": lambda f, v: Post.create(**{f: v}),
    "get_or_create": lambda f, v: Post.get_or_create(id=1, defaults={f: v}),
}
SETS = {
    "set": tags,
    "frozenset": lambda: frozenset(tags()),
    "a view of a dict's keys": lambda: {
        Tag(slug="b"): 1,
        Tag(slug="a"): 2,
    }.keys(),
}


@pytest.mark.parametrize("make", SETS.values(), ids=SETS)
@pytest.mark.parametrize("field", ["tags", "tagt", "tagq"])
@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
def test_a_set_for_a_sequence_of_relations_is_refused_before_a_request(
    write: Callable[[str, Any], Any], field: str, make: Callable[[], Any]
) -> None:
    """The error names the field and says that a set has no order."""
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(
            FieldConversionError, match=rf"Post\.{field}: .*no order"
        ):
            write(field, make())
        assert mocker.call_count == 0


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
def test_a_set_for_a_set_of_relations_is_taken(
    write: Callable[[str, Any], Any],
) -> None:
    """A set field writes its keys, sorted."""
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(
            requests_mock.ANY, requests_mock.ANY, json={"id": 1}
        )
        mocker.get(requests_mock.ANY, json=[])
        write("tagset", tags())
        if mocker.call_count:
            body = mocker.request_history[-1].json()
            assert body["tagset"] == list("abcde")


@pytest.mark.parametrize("field", ["tags", "tagt", "tagq"])
def test_a_list_or_a_tuple_for_a_sequence_of_relations_keeps_its_order(
    field: str,
) -> None:
    """Only a set has no order."""
    post = loaded()
    for value in (
        [Tag(slug="b"), Tag(slug="a")],
        (Tag(slug="b"), Tag(slug="a")),
    ):
        setattr(post, field, value)
        assert post.json[field] == ["b", "a"]


def test_a_query_still_sends_a_set_for_a_sequence_sorted() -> None:
    """The refusal is for a write. A lookup compares sets by their keys."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Post.filter(tags=tags()))
        list(Post.filter(tags__in=tags()))
    queries = [r.url.partition("?")[2] for r in mocker.request_history]
    assert queries == [
        "tags=a&tags=b&tags=c&tags=d&tags=e",
        "tags__in=a%2Cb%2Cc%2Cd%2Ce",
    ]
