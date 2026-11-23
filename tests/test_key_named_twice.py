"""A query that names the key under two names is refused."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://twice.example/")


@json_object(field_map={"id": "postId"})
class Entry(JSONObject):
    """A model whose key is sent as postId."""

    class_url = "/entries/"
    id: int


Entry.register_api(api)

CALLS: dict[str, Callable[..., Any]] = {
    "filter": lambda **kw: list(Entry.filter(**kw)),
    "get": lambda **kw: Entry.get(**kw),
    "get_page": lambda **kw: Entry.get_page(**kw),
    "get_or_create": lambda **kw: Entry.get_or_create(**kw),
}


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize(
    "params",
    [
        {"pk": 7, "postId": 8},
        {"postId": 8, "pk": 7},
        {"pk__exact": 7, "postId": 8},
        {"pk__gt": 7, "postId__gt": 8},
        {"id__in": [1], "postId__in": [2]},
        {"pk__range": [1, 2], "postId__range": [3, 4]},
        {"pk": 7, "postId__exact": 8},
    ],
)
def test_the_key_and_its_json_name_are_refused_together(
    call: str, params: dict[str, Any]
) -> None:
    """The error is a ValueError that names both parameters."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=r"both (compare|send)") as caught:
            CALLS[call](**params)
        assert type(caught.value) is ValueError
        for name in params:
            assert name in str(caught.value)
        assert mocker.call_count == 0


@pytest.mark.parametrize(
    "params",
    [
        {"pk__gt": 7, "postId__lt": 8},
        {"pk": 7, "postId__icontains": "8"},
    ],
)
def test_other_comparisons_and_names_are_sent(params: dict[str, Any]) -> None:
    """A suffix that compares another thing, or none, is no second key."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Entry.filter(**params))
        assert mocker.call_count == 1
