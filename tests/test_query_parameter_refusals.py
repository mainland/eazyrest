"""A refusal of a query parameter names the parameter and the model alike.

Each reason that a query parameter is refused gives a message that begins with
the parameter's name, as the caller wrote it, and the model. One parameter is
refused for every reason by its Python name and by its JSON key, and a request
that is refused sends nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://query-parameter-refusals.example/"

ONE_VALUE = (
    "because it compares one value and its value is a collection, which "
    "requests would send as the parameter repeated, and a server applies only "
    "one of those. Pass one value, or use a lookup that takes several, such as"
)
NO_CONVERSION = (
    "which sends no conversion, so a value other than a string, number, or "
    "None would go out other than as the field's name sends it. Pass it as"
)


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object(field_map={"name": "title"}, write_map={"name": "put_name"})
class Thing(Base):
    """A model whose field has a JSON key and a write key."""

    class_url = "/things/"
    id: int
    name: str


@json_object(field_map={"code": "a__b"})
class Odd(Base):
    """A model whose field has a JSON key that holds ``__``."""

    class_url = "/odds/"
    id: int
    code: str


@json_object(field_map={"id": "a__b"})
class OddKey(Base):
    """A model whose key has a JSON key that holds ``__``."""

    class_url = "/odd-keys/"
    id: int
    code: str


PREFIX = "Query parameter {} for Thing cannot be sent, "
ANY = object()

CASES: list[tuple[dict[str, Any], str]] = [
    (
        {"name": None},
        PREFIX.format("name") + "because its value is None, and requests "
        "omits it",
    ),
    (
        {"name": {"a": 1}},
        PREFIX.format("name") + "because its value is a mapping, and "
        "requests sends only its keys",
    ),
    ({"name": ["a", "b"]}, PREFIX.format("name") + f"{ONE_VALUE} name__in"),
    (
        {"name__gt": ["a", "b"]},
        PREFIX.format("name__gt") + f"{ONE_VALUE} name__in",
    ),
    ({"title": ["a", "b"]}, PREFIX.format("title") + f"{ONE_VALUE} title__in"),
    (
        {"name__in": []},
        PREFIX.format("name__in") + "because its value is an empty "
        "collection, and requests omits it",
    ),
    (
        {"name__in": "a"},
        PREFIX.format("name__in") + "because it takes several values and its "
        "value is not a collection of them. Pass a list",
    ),
    (
        {"name__in": ["a,b"]},
        PREFIX.format("name__in") + "because an item's text holds a comma, "
        "which the server would read as two items. Set the API's "
        'list_lookup_style to "repeat" if it reads repeated parameters',
    ),
    (
        {"name__in": [""]},
        PREFIX.format("name__in") + "because an item's text is empty, which "
        "django-filter would read as no filter",
    ),
    (
        {"name__range": ["a"]},
        PREFIX.format("name__range") + "because its value holds 1 items, and "
        "a range takes two, its start and its end",
    ),
    (
        {"name__in": [["a"]]},
        PREFIX.format("name__in") + "because its value contains a "
        "collection, and requests flattens it",
    ),
    (
        {"name__in": [None]},
        PREFIX.format("name__in") + "because its value contains None, and "
        "requests omits that item",
    ),
    (
        {"title": ANY},
        PREFIX.format("title") + "because it is the JSON key of name, "
        f"{NO_CONVERSION} name",
    ),
    (
        {"title__in": ["a", ANY]},
        PREFIX.format("title__in[1]") + "because it is the JSON key of name, "
        f"{NO_CONVERSION} name__in",
    ),
    (
        {"put_name": ANY},
        PREFIX.format("put_name") + "because it is the write key of name, "
        f"{NO_CONVERSION} name",
    ),
    (
        {"name": "a", "title": "b"},
        "Query parameters name and title for Thing both send title",
    ),
    (
        {"id": 1, "pk": 1},
        (
            "Query parameters id and pk for Thing both compare its key. Name "
            "the key once"
        ),
    ),
]


@pytest.mark.parametrize(
    ("params", "message"),
    CASES,
    ids=[", ".join(params) + f" #{n}" for n, (params, _) in enumerate(CASES)],
)
def test_filter_refuses_a_parameter_with_the_same_words(
    params: dict[str, Any], message: str
) -> None:
    """``filter()`` refuses each case before it sends anything."""
    with requests_mock.Mocker() as mock:
        with pytest.raises(ValueError, match=r"^Query parameters? ") as caught:
            list(Thing.filter(**params))
        assert mock.call_count == 0
    assert str(caught.value) == message


ODD = (
    "Query parameter a__b for {model} is {subject} JSON name, which holds "
    "'__', so a lookup would read it as a field and a suffix. Look {advice}"
)


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (
            Odd,
            ODD.format(
                model="Odd", subject="code's", advice="code up as code"
            ),
        ),
        (
            OddKey,
            ODD.format(
                model="OddKey",
                subject="the key's",
                advice="the key up as id or pk",
            ),
        ),
    ],
)
def test_get_or_create_refuses_a_name_that_holds_two_underscores(
    model: type[Any], message: str
) -> None:
    """``get_or_create()`` begins its refusal as a query does."""
    with requests_mock.Mocker() as mock:
        with pytest.raises(ValueError, match=r"^Query parameters? ") as caught:
            model.get_or_create(a__b="x")
        assert mock.call_count == 0
    assert str(caught.value) == message
