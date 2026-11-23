"""A model object is one operand of any parameter, never its members."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://operands.example/")


@json_object(pk="slug")
class Label(JSONObject):
    """A model with a text key."""

    class_url = "/labels/"
    slug: str
    title: str


@json_object
class Crowd(JSONObject):
    """A model that iterates as members that are not its key."""

    class_url = "/crowds/"
    id: int

    def __iter__(self) -> Iterator[int]:
        """Return members that are not the key."""
        return iter([1, 2, 3])


Label.register_api(api)
Crowd.register_api(api)

CALLS: dict[str, Callable[..., Any]] = {
    "filter": lambda model, **kw: list(model.filter(**kw)),
    "get": lambda model, **kw: model.get(**kw),
    "get_page": lambda model, **kw: model.get_page(**kw),
    "get_or_create": lambda model, **kw: model.get_or_create(**kw),
}


def sent(mocker: Any) -> list[str]:
    """Return the decoded query of each request."""
    return [unquote(urlsplit(r.url).query) for r in mocker.request_history]


@pytest.mark.parametrize("call", ["filter", "get", "get_page"])
@pytest.mark.parametrize("name", ["slug", "pk", "slug__exact", "pk__gt"])
def test_an_iterable_model_for_a_text_key_is_sent_as_its_key(
    call: str, name: str
) -> None:
    """The key of the model, not what it iterates as, is the operand."""

    class Chorus(Label):
        def __iter__(self) -> Iterator[str]:
            return iter(["x", "y"])

    chorus = json_object(Chorus)(slug="a")
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[{"slug": "a", "title": "t"}])
        CALLS[call](Label, **{name: chorus})
    assert sent(mocker) == [f"{name}=a"]


@pytest.mark.parametrize("call", ["filter", "get", "get_page"])
def test_an_iterable_model_for_its_own_key_is_sent_as_its_key(
    call: str,
) -> None:
    """A model that iterates is the key of the lookup on its own key."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[{"id": 5}])
        CALLS[call](Crowd, id=Crowd(id=5))
    assert sent(mocker) == ["id=5"]


def test_an_iterable_model_for_a_key_is_refused_before_a_creation() -> None:
    """get_or_create() cannot post the model, so it raises before a request."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(FieldConversionError, match=r"Crowd\.id"):
            Crowd.get_or_create(id=Crowd(id=5))
        assert mocker.call_count == 0


@pytest.mark.parametrize("call", list(CALLS))
@pytest.mark.parametrize(
    ("model", "name", "value"),
    [
        (Crowd, "id__in", Crowd(id=5)),
        (Crowd, "pk__in", Crowd(id=5)),
        (Crowd, "id__range", Crowd(id=5)),
        (Label, "title__in", Crowd(id=5)),
        (Label, "title__range", Crowd(id=5)),
    ],
)
def test_a_model_is_refused_by_a_lookup_of_several_values(
    call: str, model: type[JSONObject], name: str, value: Any
) -> None:
    """The model is one value, so the lookup refuses it in every method."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=name):
            CALLS[call](model, **{name: value})
        assert mocker.call_count == 0
