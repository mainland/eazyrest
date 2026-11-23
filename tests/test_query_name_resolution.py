"""A query reads each parameter's name as the field that it names, once.

A name starts with a key of a field, which is its Python name, its JSON key, or
its write key. The longest key that is the whole name or is followed by ``__``
wins, so a key that holds ``__`` is found whole.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit
from uuid import UUID

import pytest
import requests_mock as requests_mock_module

from eazyrest import API, Conversion, JSONObject, json_object

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


class Span:
    """An iterable class that a conversion writes as one text."""

    def __init__(self, lo: int, hi: int) -> None:
        """Hold the two ends."""
        self.lo, self.hi = lo, hi

    def __iter__(self) -> Iterator[int]:
        """Return the two ends."""
        return iter((self.lo, self.hi))


@json_object(field_map={"span": "Span"}, write_map={"span": "Span_w"})
class Spanned(Base):
    """A field whose conversion takes an iterable value whole."""

    class_url = "/spanned/"
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Span: Conversion(
            decode=lambda text, declared: Span(*map(int, text.split("-"))),
            encode=lambda span, declared: f"{span.lo}-{span.hi}",
            schema={"type": "string"},
        )
    }
    id: int
    span: Span


class Color(Enum):
    """An enum that a field writes as its value."""

    RED = "red"


@json_object(
    field_map={
        "color": "Color",
        "when": "When",
        "length": "Length",
        "price": "Price",
        "day": "Day",
        "ref": "Ref",
        "data": "Data",
        "owner": "ownerId",
    },
    write_map={"when": "When_w", "owner": "owner_w"},
)
class Colored(Base):
    """Fields whose conversion writes other text than ``str()``."""

    class_url = "/colored/"
    id: int
    color: Color
    when: datetime
    length: timedelta
    price: Decimal
    day: date
    ref: UUID
    data: Any
    owner: User


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


@pytest.mark.parametrize("name", ["Span", "Span__exact", "Span__in", "Span_w"])
def test_a_json_key_refuses_a_value_that_the_python_name_converts(
    name: str, server: Any
) -> None:
    """A JSON key or write key sends no conversion, so it refuses it."""
    with pytest.raises(ValueError, match=name):
        list(Spanned.filter(params={name: Span(1, 2)}))

    assert server.call_count == 0


def test_the_python_name_converts_a_value_that_a_conversion_takes_whole(
    server: Any,
) -> None:
    """The refusal is of the JSON key, since the Python name sends the text."""
    list(Spanned.filter(span=Span(1, 2)))

    assert query_of(server) == "Span=1-2"


def test_a_json_key_sends_a_value_that_no_conversion_takes_whole(
    server: Any,
) -> None:
    """A list of numbers is no value of the field's conversion."""
    list(Spanned.filter(params={"Span__in": [3, 4]}))

    assert query_of(server) == "Span__in=3,4"


MOMENT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "value", "advice"),
    [
        ("Color", Color.RED, "color"),
        ("Color__exact", Color.RED, "color__exact"),
        ("Color__in", [Color.RED], "color__in"),
        ("When", MOMENT, "when"),
        ("When_w", MOMENT, "when"),
        ("When__gt", MOMENT, "when__gt"),
        ("When__range", [MOMENT, MOMENT], "when__range"),
        ("Length", timedelta(hours=1), "length"),
        ("Price", Decimal("1.5"), "price"),
        ("Day", date(2024, 1, 2), "day"),
        ("Ref", UUID(int=7), "ref"),
        ("Data", b"abc", "data"),
        ("Color", User(id=3), "color"),
        ("Color__in", [User(id=3)], "color__in"),
        ("ownerId", Color.RED, "owner"),
        ("owner_w__in", [Color.RED], "owner__in"),
    ],
)
def test_a_json_key_refuses_a_value_that_is_no_json_scalar(
    name: str, value: Any, advice: str, server: Any
) -> None:
    """The key sends no conversion, so only a JSON scalar is sent by it."""
    with pytest.raises(ValueError, match=name) as raised:
        list(Colored.filter(params={name: value}))

    assert f"Pass it as {advice}" in str(raised.value)
    assert server.call_count == 0


def test_a_json_key_refuses_each_operand_that_is_no_json_scalar(
    server: Any,
) -> None:
    """A lookup of several values checks every item, a generator's too."""
    with pytest.raises(ValueError, match="Color__in"):
        list(Colored.filter(Color__in=iter(["red", Color.RED])))

    assert server.call_count == 0


@pytest.mark.parametrize("name", ["Color", "Color__in"])
def test_get_or_create_refuses_a_json_key_given_no_json_scalar(
    name: str, server: Any
) -> None:
    """The lookup is refused before any request, as the creation would be."""
    value: Any = Color.RED if name == "Color" else [Color.RED]
    with pytest.raises(ValueError, match=name):
        Colored.get_or_create(params={name: value})

    assert server.call_count == 0


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Color", "JSON key"),
        ("When_w", "write key"),
        ("When_w__gt", "write key"),
    ],
)
def test_the_refusal_says_whether_the_name_is_a_json_key_or_a_write_key(
    name: str, kind: str
) -> None:
    """A write key is not called a JSON key."""
    with pytest.raises(ValueError, match=name) as raised:
        list(Colored.filter(params={name: Color.RED}))

    message = str(raised.value)
    assert f"the {kind} of" in message
    assert ("JSON name" in message) is False


def test_the_refusal_of_a_value_taken_whole_names_the_kind_of_key() -> None:
    """A JSON key and a write key are named as each, for a value of a Span."""
    with pytest.raises(ValueError, match="the JSON key of span"):
        list(Spanned.filter(Span=Span(1, 2)))
    with pytest.raises(ValueError, match="the write key of span"):
        list(Spanned.filter(Span_w=Span(1, 2)))


def test_a_json_key_sends_a_json_scalar_as_given(server: Any) -> None:
    """A string, number, or boolean is sent, the field's type aside."""
    list(Colored.filter(Color="red", Length=3, Price=1.5))
    assert query_of(server) == "Color=red&Length=3&Price=1.5"

    list(
        Colored.filter(Color__in=["red", "blue"], When="2024-01-02T03:04:05Z")
    )
    assert query_of(server) == "Color__in=red,blue&When=2024-01-02T03:04:05Z"

    list(Colored.filter(Color=True))
    assert query_of(server) == "Color=True"


def test_a_json_key_sends_a_model_given_for_a_relation(server: Any) -> None:
    """A relation takes a model by either key, as its name does."""
    list(Colored.filter(ownerId=User(id=3), owner_w__in=[User(id=4)]))

    assert query_of(server) == "ownerId=3&owner_w__in=4"


def test_a_json_key_refuses_none_as_before() -> None:
    """None is no operand that requests would send."""
    with pytest.raises(ValueError, match="None"):
        list(Colored.filter(Color=None))


def test_a_field_not_defined_yet_sends_a_model_by_its_json_key(
    server: Any,
) -> None:
    """The type is unknown, so a model may be a relation's and is its key."""
    list(Late.filter(Later=User(id=3)))

    assert query_of(server) == "Later=3"


def test_a_field_not_defined_yet_refuses_a_value_that_is_no_scalar() -> None:
    """The refusal of a value needs no type."""
    with pytest.raises(ValueError, match="Later"):
        list(Late.filter(Later=MOMENT))
