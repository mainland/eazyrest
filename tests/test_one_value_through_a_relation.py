"""A lookup of one value through a relation takes no collection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://through.example/")


@json_object
class Author(JSONObject):
    """A related model."""

    class_url = "/authors/"
    id: int
    name: str


@json_object(pk="slug")
class Topic(JSONObject):
    """A related model with a text key."""

    class_url = "/topics/"
    slug: str


@json_object
class Article(JSONObject):
    """A model with a relation and a collection of them."""

    class_url = "/articles/"
    id: int
    author: Author
    topics: list[Topic]


for model in (Author, Topic, Article):
    model.register_api(api)

CALLS: dict[str, Callable[..., Any]] = {
    "filter": lambda **kw: list(Article.filter(**kw)),
    "get": lambda **kw: Article.get(**kw),
    "get_page": lambda **kw: Article.get_page(**kw),
    "get_or_create": lambda **kw: Article.get_or_create(**kw),
}


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize(
    "name",
    [
        "author__id",
        "author__pk",
        "author__id__exact",
        "author__id__gt",
        "author__id__gte",
        "author__id__lt",
        "author__id__lte",
        "author__name__gt",
        "topics__slug",
        "topics__pk",
    ],
)
@pytest.mark.parametrize("value", [[7, 8], (7, 8), {7, 8}])
def test_a_collection_is_refused_naming_the_parameter(
    call: str, name: str, value: Any
) -> None:
    """The error comes before any request."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=f"{name} for Article") as caught:
            CALLS[call](**{name: value})
        assert type(caught.value) is ValueError
        assert mocker.call_count == 0


@pytest.mark.parametrize(
    ("params", "query"),
    [
        ({"author__id": 7}, "author__id=7"),
        ({"author__id__in": [7, 8]}, "author__id__in=7,8"),
        ({"author__name": ["a", "b"]}, "author__name=a&author__name=b"),
        ({"author__id__gt": 7}, "author__id__gt=7"),
    ],
)
def test_a_value_or_a_lookup_of_several_values_is_sent(
    params: dict[str, Any], query: str
) -> None:
    """Only a collection for a lookup of one value is refused."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Article.filter(**params))
    assert unquote(urlsplit(mocker.request_history[0].url).query) == query
