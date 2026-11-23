"""A query reads each parameter's name as the field that it names, once.

A name starts with a key of a field, which is its Python name, its JSON key, or
its write key. The longest key that is the whole name or is followed by ``__``
wins, so a key that holds ``__`` is found whole.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock as requests_mock_module

from eazyrest import API, JSONObject, json_object

URL = "https://example.com/v1/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(URL))


@json_object
class User(Base):
    """A model with a key."""

    class_url = "/users/"
    id: int


@json_object
class Crew(User):
    """A user that iterates as members that are not its key."""

    def __iter__(self) -> Iterator[str]:
        """Return members that are not the user's key."""
        return iter(["x", "y"])


@json_object(
    field_map={"owner": "owner__ref", "title": "title__r"},
    write_map={"title": "title__w", "label": "label__w"},
)
class Doc(Base):
    """Fields whose JSON keys or write keys hold ``__``."""

    class_url = "/docs/"
    id: int
    owner: User
    title: str
    label: str
    tags: list[str]


@json_object(field_map={"later": "Later"})
class Late(Base):
    """A field whose type is never defined."""

    class_url = "/late/"
    id: int
    later: NotDefined  # type: ignore[name-defined]  # noqa: F821


@json_object(field_map={"user": "userId"})
class Task(Base):
    """A relation whose JSON key differs from its name."""

    class_url = "/tasks/"
    id: int
    user: User


@pytest.fixture
def server() -> Iterator[Any]:
    """Serve one object for every request."""
    with requests_mock_module.Mocker() as mocker:
        mocker.get(requests_mock_module.ANY, json=[{"id": 1}])
        yield mocker


def query_of(mocker: Any) -> str:
    """Return the decoded query string of the last request."""
    return unquote(urlsplit(mocker.last_request.url).query)


@pytest.mark.parametrize(
    "name",
    [
        "owner__ref",
        "owner__ref__exact",
        "owner__ref__gt",
        "title__r",
        "title__r__exact",
        "title__w",
        "title__w__gte",
        "label__w",
    ],
)
def test_a_key_that_holds_two_underscores_refuses_a_collection(
    name: str, server: Any
) -> None:
    """A JSON key or write key is found whole, whatever it holds."""
    with pytest.raises(ValueError, match=name):
        list(Doc.filter(params={name: ["a", "b"]}))

    assert server.call_count == 0


@pytest.mark.parametrize("name", ["owner__ref", "title__r", "title__w"])
def test_a_key_that_holds_two_underscores_is_sent_as_given(
    name: str, server: Any
) -> None:
    """A JSON key or write key is an ordinary name, sent as it is."""
    list(Doc.filter(params={name: "a"}))
    assert query_of(server) == f"{name}=a"

    list(Doc.filter(params={f"{name}__in": ["a", "b"]}))
    assert query_of(server) == f"{name}__in=a,b"


def test_a_suffix_that_compares_no_value_may_take_a_collection(
    server: Any,
) -> None:
    """A lookup that compares no one value takes what it is given."""
    list(Doc.filter(title__w__contains=["a", "b"]))

    assert query_of(server) == "title__w__contains=a&title__w__contains=b"


@pytest.mark.parametrize("name", ["owner__ref", "title__r", "title__w"])
def test_get_or_create_refuses_a_key_that_holds_two_underscores(
    name: str, server: Any
) -> None:
    """The creation would not send the field, so the call is refused."""
    with pytest.raises(ValueError, match=name):
        Doc.get_or_create(params={name: "a"})

    assert server.call_count == 0


def test_get_or_create_takes_a_python_name_that_holds_two_underscores(
    server: Any,
) -> None:
    """A lookup of a field by its own name creates the field."""
    server.get(URL + "docs/", json=[])
    server.post(URL + "docs/", json={"id": 2})

    Doc.get_or_create(title="a")

    assert server.last_request.json() == {"title__w": "a"}


def test_a_name_with_no_field_is_sent_as_given(server: Any) -> None:
    """A name that starts with no key of a field is not read as one."""
    list(Doc.filter(titles=["a", "b"], title_x__gt=["a", "b"]))

    assert query_of(server) == "titles=a&titles=b&title_x__gt=a&title_x__gt=b"


def test_a_field_not_defined_yet_is_sent_as_given_by_its_json_key(
    server: Any,
) -> None:
    """A JSON key needs no type, so an undefined one does not stop it."""
    list(Late.filter(Later=3))
    assert query_of(server) == "Later=3"

    list(Late.filter(Later__in=[3, 4]))
    assert query_of(server) == "Later__in=3,4"

    with pytest.raises(NameError):
        list(Late.filter(later=3))


def test_an_iterable_model_given_by_a_json_key_is_one_value(
    server: Any,
) -> None:
    """A model is sent as its key, whatever it iterates as."""
    crew = Crew(json={"id": 5})

    list(Task.filter(userId=crew))
    assert query_of(server) == "userId=5"

    list(Task.filter(userId__in=[crew]))
    assert query_of(server) == "userId__in=5"
