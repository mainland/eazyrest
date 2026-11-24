"""A query parameter's name is resolved once, and a creation reads names once.

A query resolves each name to a field, the kind of key that it starts with, and
a suffix. Whether a parameter takes one value, several, or the two of a range
follows from that result, and the name's text is read only for a name of no
field. A creation sets a field for a field's Python name, and the key for
``pk`` and the key's JSON and write keys. Any other name is an extra.
"""

from __future__ import annotations

import contextlib
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

URL = "https://names.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(URL))


@json_object(
    field_map={
        "title": "t__in",
        "stamp": "d__range",
        "deep": "a__b",
        "tags": "Tags",
    },
    write_map={"title": "t__range"},
)
class Keyed(Base):
    """Fields whose keys end in a lookup, or hold ``__``."""

    class_url = "/keyed/"
    id: int
    title: str
    stamp: str
    deep: str
    tags: list[str]
    opt__in: str
    opt__range: str


@json_object(field_map={"id": "doc__id"})
class DK(Base):
    """A key whose JSON name holds ``__``."""

    class_url = "/dk/"
    id: int


@json_object
class User(Base):
    """A model that a relation names."""

    class_url = "/users/"
    id: int


@json_object(
    field_map={"id": "postId", "owner": "ownerId"},
    write_map={"id": "postId_w"},
)
class Posted(Base):
    """A key whose JSON key, write key, and field name all differ."""

    class_url = "/posted/"
    id: int
    owner: User


class Seen:
    """The requests of a call."""

    def __init__(self, mocker: Any) -> None:
        """Hold the mocker that records them."""
        self.mocker = mocker

    @property
    def requests(self) -> list[Any]:
        """Return the requests sent so far."""
        return list(self.mocker.request_history)

    def queries(self) -> list[str]:
        """Return the decoded query of each GET sent so far."""
        return [
            unquote(urlsplit(request.url).query)
            for request in self.requests
            if request.method == "GET"
        ]


def echoed(request: Any) -> dict[str, Any]:
    """Return the members of a JSON body, or none for a form."""
    try:
        body = request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@pytest.fixture
def seen() -> Iterator[Seen]:
    """Serve an empty collection, and echo a created object with a key."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(
            requests_mock.ANY,
            json=lambda request, context: {"id": 9, **echoed(request)},
        )
        mocker.patch(requests_mock.ANY, json={})
        yield Seen(mocker)


def lookup(call: Callable[[], Any]) -> None:
    """Run a lookup, ignoring that it found nothing."""
    with contextlib.suppress(DoesNotExist):
        call()


def as_results(call: Callable[[], Any]) -> Callable[[], Any]:
    """Return a call that reads the results of a query to the end."""
    return lambda: list(call())


# The entry points that look objects up, each given the parameters by keyword
# and by ``params=``.
LOOKUPS: dict[str, Callable[[dict[str, Any]], Callable[[], Any]]] = {
    "filter": lambda kw: as_results(lambda: Keyed.filter(**kw)),
    "filter params": lambda kw: as_results(lambda: Keyed.filter(params=kw)),
    "get": lambda kw: lambda: lookup(lambda: Keyed.get(**kw)),
    "get params": lambda kw: lambda: lookup(lambda: Keyed.get(params=kw)),
    "get_page": lambda kw: lambda: Keyed.get_page(**kw),
    "get_page params": lambda kw: lambda: Keyed.get_page(params=kw),
    "get_or_create": lambda kw: lambda: Keyed.get_or_create(**kw),
    "get_or_create params": lambda kw: lambda: Keyed.get_or_create(params=kw),
}

# Each parameter, the query that every lookup sends for it, or the text that
# the refusal holds. The kinds of name are a field's Python name, ``pk``, a
# JSON key and a write key that end in a lookup, a key that holds ``__``, a
# Python name that ends in a lookup, and a name of no field. The suffixes are
# none, a lookup of one value, ``in``, ``range``, and one that compares no
# value.
QUERIES: list[tuple[str, Any, str | None, str | None]] = [
    ("title", "a", "t__in=a", None),
    ("title__in", ["a", "b"], "t__in__in=a,b", None),
    ("t__in", "a", "t__in=a", None),
    ("t__in", ["a"], None, "compares one value"),
    ("t__in__exact", "a", "t__in__exact=a", None),
    ("t__in__exact", ["a"], None, "compares one value"),
    ("t__in__in", ["a", "b"], "t__in__in=a,b", None),
    ("t__in__in", "a", None, "takes several values"),
    ("t__range", "a", "t__range=a", None),
    ("t__range", ["a", "b"], None, "compares one value"),
    ("t__range__range", ["a", "b"], "t__range__range=a,b", None),
    ("t__range__range", ["a"], None, "holds 1 items"),
    ("t__range__range", "a", None, "takes several values"),
    ("d__range", "a", "d__range=a", None),
    ("d__range", ["a", "b"], None, "compares one value"),
    ("d__range__range", ["a", "b"], "d__range__range=a,b", None),
    ("stamp__range", ["a", "b"], "d__range__range=a,b", None),
    ("stamp__range", ["a"], None, "holds 1 items"),
    ("opt__in", "a", "opt__in=a", None),
    ("opt__in", ["a"], None, "compares one value"),
    ("opt__in__in", ["a", "b"], "opt__in__in=a,b", None),
    ("opt__range", "a", "opt__range=a", None),
    ("opt__range__range", ["a", "b"], "opt__range__range=a,b", None),
    ("opt__range__range", ["a"], None, "holds 1 items"),
    ("pk", 3, "pk=3", None),
    ("pk__in", [3, 4], "pk__in=3,4", None),
    ("id__range", [1, 2], "id__range=1,2", None),
    ("id__range", [1], None, "holds 1 items"),
    ("a__b", "x", "a__b=x", None),
    ("a__b", ["x"], None, "compares one value"),
    ("a__b__in", ["x", "y"], "a__b__in=x,y", None),
    ("Tags", ["x", "y"], "Tags=x&Tags=y", None),
    ("tags__in", ["x", "y"], "Tags__in=x,y", None),
    ("x", "a", "x=a", None),
    ("x__y", "a", "x__y=a", None),
    ("x__in", ["a", "b"], "x__in=a,b", None),
    ("x__in", "a", None, "takes several values"),
    ("x__range", ["a"], None, "holds 1 items"),
    ("x__icontains", "a", "x__icontains=a", None),
    ("title__iexact", "a", "t__in__iexact=a", None),
]


# The JSON keys and write keys that hold ``__``, which a lookup of
# ``get_or_create()`` would read as a field and a suffix.
HOLDING_KEYS = {"t__in", "t__range", "d__range", "a__b"}


@pytest.mark.parametrize("entry", LOOKUPS)
@pytest.mark.parametrize(
    ("name", "value", "query", "refusal"),
    QUERIES,
    ids=[f"{name}={value!r}" for name, value, *_ in QUERIES],
)
def test_a_parameter_is_sent_or_refused_as_its_resolution_says(
    seen: Seen,
    entry: str,
    name: str,
    value: Any,
    query: str | None,
    refusal: str | None,
) -> None:
    """Every entry point sends the same query, or refuses before a request."""
    call = LOOKUPS[entry]({name: value})
    if entry.startswith("get_or_create") and name in HOLDING_KEYS:
        # A creation could not send the key that the lookup reads as a field
        # and a suffix.
        with pytest.raises(ValueError, match="holds '__'"):
            call()
        assert seen.requests == []
        return
    if refusal is not None:
        with pytest.raises(ValueError, match=refusal):
            call()
        assert seen.requests == []
        return
    call()
    assert seen.queries()[0] == query


# The names that are no field's Python name and that the creation reads as the
# key, and those that it sends as extras.
KEY_NAMES = ["pk", "id"]
EXTRA_NAMES = ["_pk", "t__range", "d__range", "a__b", "Tags", "x"]


CREATIONS: dict[str, Callable[[dict[str, Any]], Callable[[], Any]]] = {
    "create": lambda kw: lambda: Keyed.create(**kw),
    "create fields": lambda kw: lambda: Keyed.create(fields=kw),
    "get_or_create defaults": lambda kw: (
        lambda: Keyed.get_or_create(defaults=kw)
    ),
}


@pytest.mark.parametrize("entry", CREATIONS)
@pytest.mark.parametrize("name", KEY_NAMES)
def test_the_key_is_set_by_pk_and_by_its_name(
    seen: Seen, entry: str, name: str
) -> None:
    """``pk`` and the key's field name both send the key."""
    CREATIONS[entry]({name: 7})()
    assert seen.requests[-1].json() == {"id": 7}


@pytest.mark.parametrize("entry", CREATIONS)
@pytest.mark.parametrize("name", EXTRA_NAMES)
def test_any_other_name_is_sent_as_an_extra(
    seen: Seen, entry: str, name: str
) -> None:
    """A name that is no field's Python name and sets no key is not a field."""
    CREATIONS[entry]({name: 7})()
    assert seen.requests[-1].json()[name] == 7


@pytest.mark.parametrize("name", ["pk", "id", "postId", "postId_w"])
@pytest.mark.parametrize(
    "call",
    [
        lambda kw: Posted.create(**kw),
        lambda kw: Posted.create(fields=kw),
        lambda kw: Posted.get_or_create(**kw),
        lambda kw: Posted.get_or_create(defaults=kw),
    ],
    ids=["create", "create fields", "get_or_create", "get_or_create defaults"],
)
def test_the_key_is_set_by_each_of_its_names(
    seen: Seen, call: Callable[[dict[str, Any]], Any], name: str
) -> None:
    """The key's field name, JSON key, write key, and ``pk`` all name it."""
    call({name: 7})
    assert seen.requests[-1].json() == {"postId_w": 7}


@pytest.mark.parametrize("name", ["pk", "id", "postId", "postId_w"])
@pytest.mark.parametrize(
    "call",
    [
        lambda kw: Posted.create(**kw),
        lambda kw: Posted.get_or_create(**kw),
        lambda kw: Posted.get_or_create(defaults=kw),
    ],
    ids=["create", "get_or_create", "get_or_create defaults"],
)
def test_a_key_of_another_type_is_refused_under_each_of_its_names(
    seen: Seen, call: Callable[[dict[str, Any]], Any], name: str
) -> None:
    """The key is built as every key is, whatever name it was given by."""
    with pytest.raises(FieldConversionError, match=r"Posted\.id"):
        call({name: "7"})
    assert seen.requests == []


# A refused collection advises the lookup of several values, which replaces the
# suffix that compares one value and follows a name that has none.
ADVICE = [
    ("t__in", "t__in__in"),
    ("t__in__exact", "t__in__in"),
    ("t__range__gt", "t__range__in"),
    ("a__b__lt", "a__b__in"),
    ("opt__in", "opt__in__in"),
    ("opt__in__exact", "opt__in__in"),
    ("stamp", "stamp__in"),
    ("ownerId", "ownerId__in"),
    ("ownerId__id", "ownerId__id__in"),
    ("ownerId__id__gt", "ownerId__id__in"),
    ("ownerId__pk__lte", "ownerId__pk__in"),
]


@pytest.mark.parametrize(("name", "advice"), ADVICE)
@pytest.mark.parametrize("entry", ["filter", "get", "get_page"])
def test_a_refused_collection_advises_the_lookup_of_several_values(
    seen: Seen, entry: str, name: str, advice: str
) -> None:
    """The advised name is built from the name's resolution."""
    model = Posted if name.startswith("owner") else Keyed
    call = getattr(model, entry)
    with pytest.raises(ValueError, match=rf"such as {advice}$"):
        call(**{name: [3, 4]})
    assert seen.requests == []


@pytest.mark.parametrize("entry", CREATIONS)
def test_the_private_alias_of_the_key_is_no_field(
    seen: Seen, entry: str
) -> None:
    """``_pk`` is sent as it is, so a string is no refused key."""
    CREATIONS[entry]({"_pk": "x"})()
    assert seen.requests[-1].json() == {"_pk": "x"}


def test_the_private_alias_of_the_key_in_a_lookup_and_a_creation(
    seen: Seen,
) -> None:
    """Both the lookup and the creation send ``_pk`` as given."""
    Keyed.get_or_create(_pk=7, id=8)
    assert seen.queries() == ["_pk=7&id=8"]
    assert seen.requests[-1].json() == {"_pk": 7, "id": 8}


def test_the_private_alias_of_the_key_is_no_upload_name(seen: Seen) -> None:
    """An upload named ``_pk`` is sent under that name, not as the key."""
    seen.mocker.post(requests_mock.ANY, json={"id": 1})
    seen.mocker.put(requests_mock.ANY, json={"id": 1})
    seen.mocker.patch(requests_mock.ANY, json={"id": 1})
    Keyed.create(id=1, files={"_pk": b"x"})
    assert b'name="_pk"' in seen.requests[-1].body
    obj = Keyed(json={"id": 1, "title": "a"})
    obj.save(files={"_pk": b"x"})
    assert b'name="_pk"' in seen.requests[-1].body


@pytest.mark.parametrize("name", ["_pk", "pk", "id"])
def test_a_creation_with_files_sends_each_name_by_the_same_rule(
    seen: Seen, name: str
) -> None:
    """The form field of an extra or of the key is named as in a body."""
    seen.mocker.post(requests_mock.ANY, json={"id": 7})
    Keyed.create(fields={name: 7}, files={"f": b"x"})
    sent = "_pk" if name == "_pk" else "id"
    assert f'name="{sent}"'.encode() in seen.requests[-1].body


@pytest.mark.parametrize("name", ["_pk", "pk", "id"])
@pytest.mark.parametrize("entry", ["filter", "get_page"])
def test_a_field_prefetched_is_named_by_its_python_name(
    seen: Seen, entry: str, name: str
) -> None:
    """``_pk`` and the key's other names are no related field."""
    call = getattr(Keyed, entry)
    with pytest.raises(ValueError, match="not a "):
        call(prefetch=[name])
    assert seen.requests == []


def single_pass() -> Iterator[str]:
    """Return a generator of two names."""
    return (item for item in ("a", "b"))


@pytest.mark.parametrize("name", ["t__in", "t__range", "Tags", "_pk", "x"])
@pytest.mark.parametrize(
    "entry",
    [
        "create",
        "get_or_create kwargs",
        "get_or_create params",
        "get_or_create defaults",
    ],
)
def test_a_generator_under_an_extra_is_refused_before_any_request(
    seen: Seen, entry: str, name: str
) -> None:
    """The creation cannot send a generator, whichever way it is given."""
    kw: dict[str, Any] = {"id": 1, name: single_pass()}
    calls: dict[str, Callable[[], Any]] = {
        "create": lambda: Keyed.create(**kw),
        "get_or_create kwargs": lambda: Keyed.get_or_create(**kw),
        "get_or_create params": lambda: Keyed.get_or_create(
            params={"id": 1, name: single_pass()}
        ),
        "get_or_create defaults": lambda: Keyed.get_or_create(
            id=1, defaults={name: single_pass()}
        ),
    }
    with pytest.raises((TypeError, ValueError)):
        calls[entry]()
    assert seen.requests == []


@pytest.mark.parametrize("entry", ["kwargs", "params", "defaults"])
def test_a_generator_under_a_field_name_is_read_once(
    seen: Seen, entry: str
) -> None:
    """The field's conversion reads it, so the lookup and the body match."""
    if entry == "defaults":
        Keyed.get_or_create(id=1, defaults={"tags": single_pass()})
    elif entry == "params":
        Keyed.get_or_create(params={"id": 1, "tags": single_pass()})
    else:
        Keyed.get_or_create(id=1, tags=single_pass())
    assert seen.requests[-1].json()["Tags"] == ["a", "b"]


def test_a_generator_under_a_name_that_only_the_lookup_reads_is_read_once(
    seen: Seen,
) -> None:
    """A lookup of several values is not sent by the creation."""
    Keyed.get_or_create(id=1, tags__in=single_pass())
    assert seen.queries() == ["id=1&Tags__in=a,b"]


def test_a_generator_under_a_json_key_in_a_defaults_lookup_name(
    seen: Seen,
) -> None:
    """A name with a suffix is no field of the creation.

    A generator under one in ``defaults=`` is refused.
    """
    with pytest.raises(TypeError, match="generator"):
        Keyed.get_or_create(id=1, defaults={"tags__in": single_pass()})
    assert seen.requests == []


@pytest.mark.parametrize(
    ("model", "name", "advice"),
    [
        (Keyed, "a__b", "Look deep up as deep"),
        (DK, "doc__id", "Look the key up as id or pk"),
    ],
)
def test_the_refusal_of_a_json_key_with_underscores_says_to_look_it_up(
    seen: Seen, model: type[JSONObject], name: str, advice: str
) -> None:
    """The advice reads as a sentence."""
    kw: dict[str, Any] = {name: 1}
    with pytest.raises(ValueError, match=advice):
        model.get_or_create(**kw)
    assert seen.requests == []
