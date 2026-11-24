"""A field's JSON key or write key sends an operand only as a plain value.

The key sends no conversion, so an operand whose type is not exactly ``str``,
``int``, ``float``, or ``None`` would go out as its ``str()``, which is not
what the field's Python name sends. A model is the exception when it is of the
relation's own class, which both names send as its key.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import (
    API,
    DoesNotExist,
    FieldConversionError,
    JSONObject,
    json_object,
)
from eazyrest.pagination import follow_next_links

URL = "https://operands.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(URL))


class Hue(str, enum.Enum):  # noqa: UP042
    """An enum that mixes in ``str``."""

    RED = "red"


class Level(int, enum.Enum):
    """An enum that mixes in ``int``."""

    HIGH = 3


class Weight(float, enum.Enum):
    """An enum that mixes in ``float``."""

    HEAVY = 2.5


class Label(str):
    """A subclass of ``str`` that is no enum."""


class Plain(enum.Enum):
    """An enum that mixes in nothing."""

    RED = "red"


@json_object(
    field_map={
        "hue": "Hue",
        "level": "Level",
        "weight": "Weight",
        "label": "Label",
        "text": "Text",
    },
    write_map={"hue": "Hue_w", "level": "Level_w"},
)
class Painted(Base):
    """Fields of an enum type, and of plain types."""

    class_url = "/painted/"
    id: int
    hue: Hue
    level: Level
    weight: float
    label: str
    text: str


@json_object
class User(Base):
    """A model that relations name."""

    class_url = "/users/"
    id: int


@json_object
class Other(Base):
    """Another model keyed by a number."""

    class_url = "/others/"
    id: int


@json_object(pk="code")
class Chosen(User):
    """A subclass of ``User`` that chose another key field."""

    class_url = "/chosen/"
    code: str


@json_object(pk="slug")
class Tag(Base):
    """A model keyed by a string."""

    class_url = "/tags/"
    slug: str


@json_object(
    field_map={"user": "userId", "tags": "tagIds", "later": "Later"},
    write_map={"user": "userId_w", "tags": "tagIds_w"},
)
class Task(Base):
    """Relations whose JSON keys and write keys differ from their names."""

    class_url = "/tasks/"
    id: int
    user: User
    tags: list[Tag]
    later: NotDefinedYet  # type: ignore[name-defined]  # noqa: F821


@pytest.fixture
def requests_seen() -> Iterator[requests_mock.Mocker]:
    """Serve an empty collection."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"id": 9})
        yield mocker


def looked_up(call: Callable[[], Any]) -> None:
    """Run a lookup, ignoring that ``get()`` found nothing."""
    with contextlib.suppress(DoesNotExist):
        call()


def query_calls(
    model: Any, kw: dict[str, Any]
) -> dict[str, Callable[[], Any]]:
    """Return the calls that look up with a query, by entry point."""
    return {
        "filter": lambda: list(model.filter(**kw)),
        "filter params": lambda: list(model.filter(params=kw)),
        "get": lambda: looked_up(lambda: model.get(**kw)),
        "get params": lambda: looked_up(lambda: model.get(params=kw)),
        "get_page": lambda: model.get_page(**kw),
        "get_page params": lambda: model.get_page(params=kw),
        "get_or_create": lambda: model.get_or_create(**kw),
        "get_or_create params": lambda: model.get_or_create(params=kw),
    }


ENTRIES = list(query_calls(Painted, {}))

# The entry points that only look up. A creation cannot send a model under a
# name that is no field's Python name, so it refuses one.
LOOKUPS = [entry for entry in ENTRIES if not entry.startswith("get_or_create")]

# The operands that no JSON key or write key sends. Each is a pair of the key
# that it is given to and the Python name that the refusal advises.
REFUSED = [
    pytest.param("Hue", "hue", Hue.RED, id="str enum"),
    pytest.param("Hue_w", "hue", Hue.RED, id="str enum by write key"),
    pytest.param("Level", "level", Level.HIGH, id="int enum"),
    pytest.param("Level_w", "level", Level.HIGH, id="int enum by write key"),
    pytest.param("Weight", "weight", Weight.HEAVY, id="float enum"),
    pytest.param("Label", "label", Label("a"), id="str subclass"),
    pytest.param("Text", "text", Hue.RED, id="enum for a plain field"),
    pytest.param("Text", "text", Level.HIGH, id="int enum for a plain field"),
    pytest.param("Text", "text", Plain.RED, id="plain enum"),
    pytest.param("Text", "text", True, id="boolean"),
]


@pytest.mark.parametrize("suffix", ["", "__exact", "__gt"])
@pytest.mark.parametrize(("key", "advice", "value"), REFUSED)
@pytest.mark.parametrize("entry", ENTRIES)
def test_an_operand_that_is_no_plain_value_is_refused_by_a_key(
    requests_seen: requests_mock.Mocker,
    entry: str,
    key: str,
    advice: str,
    value: Any,
    suffix: str,
) -> None:
    """The refusal names the parameter, and advises the Python name."""
    name = key + suffix
    with pytest.raises(ValueError, match=rf"\b{name}\b.*Pass it as {advice}"):
        query_calls(Painted, {name: value})[entry]()
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("suffix", ["__in", "__range"])
@pytest.mark.parametrize(("key", "advice", "value"), REFUSED)
@pytest.mark.parametrize("entry", ENTRIES)
def test_an_item_that_is_no_plain_value_is_refused_by_a_key_with_position(
    requests_seen: requests_mock.Mocker,
    entry: str,
    key: str,
    advice: str,
    value: Any,
    suffix: str,
) -> None:
    """The refusal names the parameter first, and then the item's position."""
    name = key + suffix
    with pytest.raises(
        ValueError, match=rf"^Query parameter {name}\[1\] for Painted"
    ) as caught:
        query_calls(Painted, {name: ["a" if key != "Level" else 1, value]})[
            entry
        ]()
    assert f"Pass it as {advice}{suffix}" in str(caught.value)
    assert requests_seen.call_count == 0


def test_an_item_of_a_set_has_no_position(
    requests_seen: requests_mock.Mocker,
) -> None:
    """A set's order varies, so the refusal names none."""
    with pytest.raises(ValueError, match=r"^Query parameter Hue__in for "):
        list(Painted.filter(Hue__in={Hue.RED}))
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    ("name", "value", "query"),
    [
        ("Hue", "red", "Hue=red"),
        ("Hue_w", "red", "Hue_w=red"),
        ("Level", 3, "Level=3"),
        ("Weight", 2.5, "Weight=2.5"),
        ("Level__in", [1, 3], "Level__in=1,3"),
    ],
)
def test_a_plain_operand_is_sent_by_a_key_as_given(
    requests_seen: requests_mock.Mocker,
    entry: str,
    name: str,
    value: Any,
    query: str,
) -> None:
    """A field declared as an enum takes the plain value of the member."""
    query_calls(Painted, {name: value})[entry]()
    first = requests_seen.request_history[0]
    assert unquote(urlsplit(first.url).query).startswith(query)


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("name", ["userId", "userId__exact", "userId_w"])
@pytest.mark.parametrize(
    "other",
    [Other(id=3), Tag(slug="a")],
    ids=["a model with another key type", "a model keyed by a string"],
)
def test_a_model_of_another_class_is_refused_by_the_key_of_a_relation(
    requests_seen: requests_mock.Mocker,
    entry: str,
    name: str,
    other: JSONObject,
) -> None:
    """The refusal is the Python name's, and names both classes."""
    with pytest.raises(FieldConversionError) as caught:
        query_calls(Task, {name: other})[entry]()
    message = str(caught.value)
    assert "Task.user" in message
    assert "User" in message
    assert type(other).__name__ in message
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("entry", LOOKUPS)
@pytest.mark.parametrize("name", ["userId", "userId_w", "userId__in"])
def test_an_object_keyed_by_another_field_is_refused_by_the_key_of_a_relation(
    requests_seen: requests_mock.Mocker, entry: str, name: str
) -> None:
    """Its key is no key of the related model, as for the field's name."""
    chosen = Chosen(json={"id": 1, "code": "a"})
    value = [chosen] if name.endswith("__in") else chosen
    with pytest.raises(FieldConversionError, match="another primary key"):
        query_calls(Task, {name: value})[entry]()
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        ("Level", None, "is None"),
        ("Level_w", None, "is None"),
        ("Level__in", [1, None], "contains None"),
        ("Level__range", [None, 3], "contains None"),
    ],
)
def test_none_is_refused_by_a_key_as_by_the_fields_name(
    requests_seen: requests_mock.Mocker,
    entry: str,
    name: str,
    value: Any,
    problem: str,
) -> None:
    """``None`` is a plain value of the key, and requests would omit it."""
    with pytest.raises(ValueError, match=rf"because its value {problem}\b"):
        query_calls(Painted, {name: value})[entry]()
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_model_of_another_class_in_a_list_names_its_position(
    requests_seen: requests_mock.Mocker, entry: str
) -> None:
    """A collection of operands is checked item by item."""
    with pytest.raises(FieldConversionError, match=r"\[1\]"):
        query_calls(Task, {"userId__in": [User(id=1), Other(id=3)]})[entry]()
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("entry", LOOKUPS)
def test_a_model_of_the_relations_own_class_is_sent_as_its_key(
    requests_seen: requests_mock.Mocker, entry: str
) -> None:
    """Both names of the relation send the key."""
    query_calls(Task, {"userId": User(id=3)})[entry]()
    first = requests_seen.request_history[0]
    assert unquote(urlsplit(first.url).query).startswith("userId=3")


@pytest.mark.parametrize("entry", LOOKUPS)
@pytest.mark.parametrize("name", ["tagIds", "tagIds_w"])
def test_a_model_under_the_key_of_a_collection_of_relations_is_its_key(
    requests_seen: requests_mock.Mocker, entry: str, name: str
) -> None:
    """A model, alone or in a list, is sent as its key."""
    query_calls(Task, {name: Tag(slug="a")})[entry]()
    assert (
        unquote(urlsplit(requests_seen.request_history[0].url).query)[
            : len(name) + 2
        ]
        == f"{name}=a"
    )


@pytest.mark.parametrize("entry", LOOKUPS)
def test_a_list_of_models_under_the_key_of_a_collection_of_relations(
    requests_seen: requests_mock.Mocker, entry: str
) -> None:
    """Each model is sent as its key, and a model of another class refused."""
    query_calls(Task, {"tagIds": [Tag(slug="a"), Tag(slug="b")]})[entry]()
    query = unquote(urlsplit(requests_seen.request_history[0].url).query)
    assert query.startswith("tagIds=a&tagIds=b")
    with pytest.raises(FieldConversionError, match=r"Task\.tags"):
        query_calls(Task, {"tagIds": [Tag(slug="a"), User(id=1)]})[entry]()


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_collection_under_the_key_of_a_field_not_defined_yet_is_one_value(
    requests_seen: requests_mock.Mocker, entry: str
) -> None:
    """The type is not needed to send a name as given.

    A collection is refused as a lookup of one value.
    """
    with pytest.raises(ValueError, match="compares one value"):
        query_calls(Task, {"Later": ["a", "b"]})[entry]()
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_model_under_the_key_of_a_field_not_defined_yet_is_refused(
    requests_seen: requests_mock.Mocker, entry: str
) -> None:
    """Whether the field is a relation is not known, so a model is refused."""
    with pytest.raises(ValueError, match="Pass it as later"):
        query_calls(Task, {"Later": User(id=1)})[entry]()
    assert requests_seen.call_count == 0


@pytest.mark.parametrize("count", [True, False, "3", 3.0, None])
def test_a_count_that_is_no_integer_is_none(count: Any) -> None:
    """A boolean is an ``int`` but no collection size."""
    api = API(URL)
    assert (
        follow_next_links(api, {"count": count, "results": []}).count is None
    )


@pytest.mark.parametrize("entry", ["get_or_create", "get_or_create params"])
def test_a_creation_cannot_send_a_model_under_a_key(
    requests_seen: requests_mock.Mocker, entry: str
) -> None:
    """The model is an extra of the creation, which a body cannot hold."""
    with pytest.raises(TypeError, match="User"):
        query_calls(Task, {"userId": User(id=3)})[entry]()
    assert requests_seen.call_count == 0
