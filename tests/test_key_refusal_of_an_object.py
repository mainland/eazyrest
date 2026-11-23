"""An object refused for a model's own key is refused as a key."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://keyrefusal.example/")


@json_object
class User(JSONObject):
    """A base model."""

    class_url = "/users/"
    id: int


@json_object
class Admin(User):
    """A subclass that keeps the key."""


@json_object(pk="slug")
class Group(JSONObject):
    """A model with another key."""

    class_url = "/groups/"
    slug: str


@json_object
class Member(JSONObject):
    """A model that relates to a user."""

    class_url = "/members/"
    id: int
    user: User


for model in (User, Admin, Group, Member):
    model.register_api(api)

CALLS: dict[str, Callable[..., Any]] = {
    "filter": lambda model, **kw: list(model.filter(**kw)),
    "get": lambda model, **kw: model.get(**kw),
    "get_page": lambda model, **kw: model.get_page(**kw),
    "get_or_create": lambda model, **kw: model.get_or_create(**kw),
}


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("name", ["id", "pk", "id__gt"])
def test_a_base_class_object_for_a_subclass_key_names_the_key(
    call: str, name: str
) -> None:
    """The message calls the key a key, and names both classes."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(FieldConversionError) as caught:
            CALLS[call](Admin, **{name: User(id=7)})
        assert mocker.call_count == 0
    text = str(caught.value)
    assert text.startswith("Admin.id: ")
    assert "key of Admin" in text
    assert "User" in text
    assert "relation" not in text


def test_an_object_that_chose_another_key_is_refused_as_a_key() -> None:
    """A subclass keyed by another field is another model's key."""

    @json_object(pk="slug")
    class Slugged(User):
        slug: str

    with pytest.raises(FieldConversionError, match="another primary key"):
        list(User.filter(id=Slugged(json={"id": 1, "slug": "a"})))


def test_a_relation_is_still_called_a_relation() -> None:
    """The refusal of a relation's value is not changed."""
    with pytest.raises(FieldConversionError, match="A relation to User"):
        list(Member.filter(user=Group(slug="a")))
