"""``prefetch=`` is a field name or a sequence of field names, and no more.

A mapping would prefetch its keys, bytes would read as integers, and an integer
would raise the bare error of ``tuple()``. Each query refuses them with an
error that names the argument, before any request.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://prefetch-argument.example/"
api = API(BASE)


@json_object
class Member(JSONObject):
    """A related model with a bulk lookup."""

    class_url = "/members/"
    id: int

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give each key an object."""
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


@json_object
class Team(JSONObject):
    """A model whose lead is prefetched."""

    class_url = "/teams/"
    id: int
    lead: Member


Member.register_api(api)
Team.register_api(api)

QUERIES: dict[str, Callable[[Any], object]] = {
    "filter": lambda value: list(Team.filter(prefetch=value)),
    "all": lambda value: list(Team.all(prefetch=value)),
    "get": lambda value: Team.get(prefetch=value),
    "get_page": lambda value: Team.get_page(prefetch=value),
}

REFUSED: dict[str, Any] = {
    "a mapping": {"lead": True},
    "bytes": b"lead",
    "empty bytes": b"",
    "a bytearray": bytearray(b"lead"),
    "an empty bytearray": bytearray(),
    "a memoryview": memoryview(b"lead"),
    "an empty memoryview": memoryview(b""),
    "an int": 3,
    "a set": {"lead"},
    "a generator": (name for name in ["lead"]),
    "a list of ints": [1],
    "a list with a bytes name": [b"lead"],
    "a list with None": [None],
    "a bool": True,
}

ACCEPTED: dict[str, Any] = {
    "a name": "lead",
    "a list": ["lead"],
    "a tuple": ("lead",),
    "a deque": deque(["lead"]),
    "none": None,
    "an empty list": [],
    "an empty tuple": (),
    "an empty range": range(0),
}


@pytest.mark.parametrize("value", REFUSED.values(), ids=REFUSED)
@pytest.mark.parametrize("query", QUERIES.values(), ids=QUERIES)
def test_a_prefetch_that_names_no_field_or_sequence_is_refused(
    query: Callable[[Any], object], value: Any
) -> None:
    """The error names the argument, and nothing is requested."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "teams/", json=[{"id": 1, "lead": 7}])
        with pytest.raises(TypeError, match=r"prefetch="):
            query(value)
        assert mocker.call_count == 0


@pytest.mark.parametrize("value", ACCEPTED.values(), ids=ACCEPTED)
@pytest.mark.parametrize("query", QUERIES.values(), ids=QUERIES)
def test_a_name_or_a_sequence_of_names_is_read(
    query: Callable[[Any], object], value: Any
) -> None:
    """The query sends its request and prefetches the named field."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "teams/", json=[{"id": 1, "lead": 7}])
        query(value)
        assert mocker.call_count == 1
