"""A field's JSON key in a query compares as the field's Python name does."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://names.example/")


@json_object
class Owner(JSONObject):
    """Related model."""

    class_url = "/owners/"
    id: int


@json_object(
    field_map={"title": "Title", "tags": "Tags", "point": "Pt"},
    write_map={"owner": "owner_id", "title": "Title_w"},
)
class Item(JSONObject):
    """A model whose fields have JSON keys and write keys of their own."""

    class_url = "/items/"
    id: int
    title: str
    tags: list[str]
    owner: Owner
    point: tuple[int, int]


Owner.register_api(api)
Item.register_api(api)


def sent(**query: Any) -> str:
    """Return the decoded query string that a filter sends."""
    with requests_mock.Mocker() as mocker:
        lookup = mocker.get(requests_mock.ANY, json=[])
        list(Item.filter(**query))
    return unquote(urlsplit(lookup.last_request.url).query)


@pytest.mark.parametrize(
    "name",
    [
        "Title",
        "Title__exact",
        "Title__lt",
        "Title__lte",
        "Title__gt",
        "Title__gte",
        "Title_w",
        "Title_w__gte",
        "owner_id",
        "owner_id__exact",
        "Pt",
        "Pt__gt",
        "Tags__exact",
        "Tags__lt",
    ],
)
def test_a_collection_for_a_json_key_of_one_value_is_refused(
    name: str,
) -> None:
    """The refusal names the parameter and a lookup of several values."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        query: dict[str, Any] = {name: ["a", "b"]}
        with pytest.raises(ValueError, match=name) as caught:
            list(Item.filter(**query))
        assert mocker.call_count == 0
    assert "__in" in str(caught.value)


def test_a_collection_for_the_json_key_of_a_collection_is_sent() -> None:
    """A field of items takes a collection by its JSON key, as by its name."""
    assert sent(Tags=["a", "b"]) == "Tags=a&Tags=b"
    assert sent(tags=["a", "b"]) == "Tags=a&Tags=b"


def test_one_value_and_several_values_by_a_json_key_are_sent() -> None:
    """Only a collection for one value is refused."""
    assert sent(Title="a") == "Title=a"
    assert sent(Title__in=["a", "b"]) == "Title__in=a,b"
    assert sent(owner_id__in=[1, 2]) == "owner_id__in=1,2"
