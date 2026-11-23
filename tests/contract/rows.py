"""The 1.0 contract for keys, queries, and relations, one row per cell.

Each row is one input at one entry point and the outcome that it must have: the
exact query string or body sent, the value read, or the exception raised, by
class and by the field or parameter that it names. ``test_rows.py`` runs every
row as one assertion.

A row that raises means that it raises before any request, with nothing changed
on the object. A row that names an exception class asserts exactly that class,
so a ``FieldConversionError`` does not meet a row that asks for a
``ValueError``. A name that a row asks the message to hold is matched as a
whole name, so ``id`` is not found in ``Post.id`` or ``postId``.
"""

from __future__ import annotations

import collections
import copy
import datetime
import enum
import json
import pickle
import re
import uuid
from collections import deque
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    MutableSequence,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from decimal import Decimal
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    NewType,
    Optional,
    Union,
)
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

import eazyrest
from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    ResponseShapeError,
    UsageError,
    json_object,
)

U = uuid.UUID("abcdef12-1234-5678-1234-567812345abc")
UP = str(U).upper()
UC = U.hex
LOWER = str(U)


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API("https://rows.example/"))


@json_object
class User(Base):
    class_url = "/users/"
    id: int
    name: str


@json_object
class Admin(User):
    """A subclass that keeps User's key."""


@json_object
class Other(Base):
    class_url = "/others/"
    id: int


@json_object(pk="slug")
class Tag(Base):
    class_url = "/tags/"
    slug: str


@json_object
class Crew(Tag):
    """A tag that can be iterated, as its own members and not its key."""

    def __iter__(self) -> Iterator[str]:
        """Return members that are not the tag's key."""
        return iter(["x", "y"])


@json_object
class Doc(Base):
    class_url = "/docs/"
    id: uuid.UUID


@json_object(field_map={"id": "docId"})
class Note(Base):
    """A model whose key is a UUID under another JSON name."""

    class_url = "/notes/"
    id: uuid.UUID


@json_object(field_map={"id": "postId"})
class Post(Base):
    class_url = "/posts/"
    id: int
    title: str
    owner: Optional[User]
    tags: list[Tag]
    tagt: tuple[Tag, ...]
    readers: list[User]
    tagq: deque[Tag]
    tagset: set[Tag]
    tagfs: frozenset[Tag]
    watchers: set[User]
    point: tuple[int, int]


@json_object
class Member(Base):
    """A set relation to its own model."""

    class_url = "/members/"
    id: int
    friends: set[Member]


@json_object(field_map={"id": "doc__id"})
class DK(Base):
    """A key whose JSON name holds two underscores."""

    class_url = "/dk/"
    id: int


@json_object(pk="doc__id")
class DU(Base):
    """A key whose field name holds two underscores."""

    class_url = "/du/"
    doc__id: int


@json_object
class Bare(Base):
    """A keyed model that declares no ``class_url``."""

    id: int
    name: str


@json_object(field_map={"user": "userId"})
class Task(Base):
    """A relation whose JSON name differs from its field's name."""

    class_url = "/tasks/"
    id: int
    user: User


@json_object(field_map={"title": "Title"})
class Titled(Base):
    """A field that is not the key, with a JSON name of its own."""

    class_url = "/titled/"
    id: int
    title: str


@json_object(write_map={"id": "ident"})
class WK(Base):
    """A key with a write key of its own."""

    class_url = "/wk/"
    id: int


@json_object(field_map={"title": "the__title"})
class DT(Base):
    """A field that is not the key, whose JSON name holds two underscores."""

    class_url = "/dt/"
    id: int
    title: str


class Span:
    """An iterable class that a conversion writes as one text."""

    def __init__(self, lo: int, hi: int) -> None:
        self.lo, self.hi = lo, hi

    def __iter__(self) -> Iterator[int]:
        """Return the two ends."""
        return iter((self.lo, self.hi))


@json_object(field_map={"span": "Span", "spans": "Spans"})
class Spanned(Base):
    """Fields whose conversion takes an iterable value whole."""

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
    spans: list[Span]


class Color(enum.Enum):
    RED = "red"


W = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)


@json_object(
    field_map={"color": "Color", "when": "When", "flag": "Flag"},
    write_map={"when": "When_w"},
)
class Colored(Base):
    """Fields whose conversion writes other text than ``str()``."""

    class_url = "/colored/"
    id: int
    color: Color
    when: datetime.datetime
    flag: bool


@json_object
class Entry(Base):
    """A model with no key."""

    name: str


@json_object
class Shelf(Base):
    """Relations to a model with a UUID key."""

    class_url = "/shelves/"
    id: int
    cover: Optional[Doc]
    papers: list[Doc]
    docset: set[Doc]


BODY: dict[str, Any] = {
    "postId": 1,
    "title": "t",
    "owner": 7,
    "tags": ["a"],
    "tagt": ["a"],
    "readers": [7],
    "tagq": ["a"],
    "tagset": ["b", "a"],
    "tagfs": ["b", "a"],
    "watchers": [7],
    "point": [1, 2],
}


def post(**over: Any) -> Post:
    """Return a post read from ``BODY`` with the given keys replaced."""
    return Post(json={**BODY, **over})


class World:
    """The mocked server and the requests that it saw."""

    def __init__(self, mocker: Any) -> None:
        self.mocker = mocker
        mocker.get(requests_mock.ANY, json=[{"postId": 1, "id": 1}])
        mocker.post(requests_mock.ANY, json={"postId": 9, "id": 9})
        # A collection or a created object names a key of its model's type. A
        # row that registers its own reply replaces these.
        for path, item in (("docs", {"id": LOWER}), ("tags", {"slug": "a"})):
            url = re.compile(rf"https://rows\.example/{path}/")
            mocker.get(url, json=[item])
            mocker.post(url, json=item)
        mocker.patch(requests_mock.ANY, json={})
        mocker.put(requests_mock.ANY, json={})

    @property
    def requests(self) -> list[Any]:
        return list(self.mocker.request_history)

    @property
    def count(self) -> int:
        return int(self.mocker.call_count)

    def query(self, call: Callable[[], Any]) -> str:
        """Run a call that sends one GET and return its decoded query."""
        before = self.count
        call()
        assert self.count == before + 1, "the call sent no single request"
        request = self.requests[-1]
        assert request.method == "GET"
        return unquote(urlsplit(request.url).query)

    def body(self, call: Callable[[], Any], method: str) -> Any:
        """Run a call that sends one write and return its JSON body."""
        before = self.count
        call()
        sent = self.requests[before:]
        assert [r.method for r in sent] == [method], [r.method for r in sent]
        return json.loads(sent[0].text) if sent[0].text else None

    def bodies(self, call: Callable[[], Any]) -> list[tuple[str, Any]]:
        """Run a call and return every write that it sent."""
        before = self.count
        call()
        return [
            (r.method, json.loads(r.text) if r.text else None)
            for r in self.requests[before:]
        ]


def names(message: str, *named: str) -> None:
    """Assert that a message holds each text as a whole name.

    A text is whole if no letter, digit, underscore, or dot comes before or
    after it, so ``id`` is not found in ``Post.id`` and ``Post`` is not found
    in ``Posts``.
    """
    for text in named:
        found = re.search(rf"(?<![\w.]){re.escape(text)}(?!\w)", message)
        assert found, (text, message)


def raises(
    world: World,
    exc: type[Exception],
    call: Callable[[], Any],
    *named: str,
    requests: int = 0,
) -> Exception:
    """Assert that a call raises exactly ``exc``, naming each given text.

    The error is raised before any request, unless ``requests`` says how many
    have been sent by then.
    """
    before = world.count
    caught: Exception | None = None
    try:
        call()
    except Exception as error:
        if type(error) is not exc:
            raise AssertionError(
                f"{type(error).__name__} was raised, not {exc.__name__}: "
                f"{error}"
            ) from error
        caught = error
    if caught is None:
        raise AssertionError(f"{exc.__name__} was not raised")
    names(str(caught), *named)
    assert world.count - before == requests
    return caught


def refuses_assignment(
    world: World,
    obj: JSONObject,
    name: str,
    value: Any,
    *named: str,
    exc: type[Exception] = FieldConversionError,
) -> Exception:
    """Assert that an assignment is refused and changes nothing.

    The field keeps its value, the object's ``json`` is as it was, and a
    following ``save()`` sends nothing. No request is sent by the assignment.
    """
    json_before = copy.deepcopy(obj.json)
    value_before = getattr(obj, name)
    snapshot = copy.deepcopy(value_before)
    error = raises(world, exc, lambda: setattr(obj, name, value), *named)
    assert getattr(obj, name) is value_before
    assert value_before == snapshot
    assert obj.json == json_before
    sent = world.count
    obj.save()
    assert world.count == sent, "a refused assignment left a pending write"
    return error


def queries(model: type[JSONObject], **kw: Any) -> list[Callable[[], Any]]:
    """Return the calls that only look an object up: filter and get."""
    return [
        lambda: list(model.filter(**kw)),
        lambda: model.get(**kw),
        lambda: list(model.filter(params=kw)),
        lambda: model.get(params=kw),
    ]


def creations(model: type[JSONObject], **kw: Any) -> list[Callable[[], Any]]:
    """Return the calls that look an object up and may create it."""
    return [
        lambda: model.get_or_create(**kw),
        lambda: model.get_or_create(params=kw),
    ]


def lookups(model: type[JSONObject], **kw: Any) -> list[Callable[[], Any]]:
    """Return the calls that take one lookup: filter, get, and the others."""
    return [*queries(model, **kw), *creations(model, **kw)]


def sends_query(
    world: World, model: type[JSONObject], expected: str, **kw: Any
) -> None:
    """Assert that every form of a lookup sends exactly one query string."""
    for call in lookups(model, **kw):
        assert world.query(call) == expected


def sends_query_but_refuses_creation(
    world: World,
    model: type[JSONObject],
    expected: str,
    exc: type[Exception],
    *named: str,
    **kw: Any,
) -> None:
    """Assert that a lookup is sent, unless get_or_create() takes it.

    A lookup sends a value that the creation could not send, so
    ``get_or_create()`` raises ``exc`` before any request.
    """
    for call in queries(model, **kw):
        assert world.query(call) == expected
    for call in creations(model, **kw):
        raises(world, exc, call, *named)


def refuses_query(
    world: World,
    model: type[JSONObject],
    exc: type[Exception],
    *named: str,
    **kw: Any,
) -> None:
    """Assert that every form of a lookup raises ``exc`` before a request."""
    for call in lookups(model, **kw):
        raises(world, exc, call, *named)


def declare(namespace_annotations: dict[str, Any], **extra: Any) -> Any:
    """Decorate a model with the given annotations, or raise ``TypeError``."""
    namespace: dict[str, Any] = {
        "class_url": "/t/",
        "__annotations__": namespace_annotations,
        **extra,
    }
    return json_object(type("T", (Base,), namespace))


def declare_key(annotation: Any, **extra: Any) -> Any:
    """Declare a model whose key field has the given annotation."""
    return declare({"id": annotation}, **extra)


def declare_field(annotation: Any) -> Any:
    """Declare a model with a key and one field of the given annotation."""
    return declare({"id": int, "items": annotation})


@dataclass(frozen=True)
class Row:
    """One cell of the contract."""

    section: str
    name: str
    check: Callable[[World], None]
    pending: str | None = None
    """Why the row does not hold yet, if it does not."""

    @property
    def id(self) -> str:
        return f"{self.section}/{self.name}"


ROWS: list[Row] = []


def row(
    section: str, pending: str | None = None
) -> Callable[[Callable[[World], None]], Callable[[World], None]]:
    """Register a function as a row of a section."""

    def register(check: Callable[[World], None]) -> Callable[[World], None]:
        ROWS.append(Row(section, check.__name__, check, pending))
        return check

    return register


KEY = "declaring-a-key"

UID = NewType("UID", int)


class Colour(enum.Enum):
    RED = "red"


Wrapped = NewType("Wrapped", int)


def refused_key(world: World, annotation: Any, **extra: Any) -> None:
    """Assert that a key type is refused, and the types to declare named.

    The message names the field and offers ``str``, ``int``, and ``UUID``, and
    no type that a key may not be.
    """
    error = raises(
        world,
        TypeError,
        lambda: declare_key(annotation, **extra),
        "T.id",
        "str",
        "int",
        "UUID",
    )
    for other in ("datetime", "Decimal", "enum"):
        if other.lower() not in repr(annotation).lower():
            assert other not in str(error), (other, str(error))


@row(KEY)
def int_str_uuid_admitted(world: World) -> None:
    for annotation in (int, str, uuid.UUID):
        declare_key(annotation)


@row(KEY)
def optional_keys_admitted(world: World) -> None:
    declare_key(Optional[int])
    declare_key(Optional[str])
    declare_key(Union[int, None])
    declare_key(int | None)


@row(KEY)
def newtype_without_conversion_admitted(world: World) -> None:
    declare_key(UID)


@row(KEY)
def annotated_key_admitted(world: World) -> None:
    declare_key(Annotated[int, "doc"])


@row(KEY)
def converted_newtype_refused(world: World) -> None:
    conversions = {
        Wrapped: Conversion(
            decode=lambda value, _: Wrapped(value),
            encode=lambda value, _: int(value),
        )
    }
    refused_key(world, Wrapped, conversions=conversions)


@row(KEY)
def literal_key_refused(world: World) -> None:
    for annotation in (Literal["a", "b"], Literal[1, 2]):
        refused_key(world, annotation)


@row(KEY)
def enum_decimal_temporal_key_refused(world: World) -> None:
    for annotation in (
        Colour,
        Decimal,
        datetime.datetime,
        datetime.date,
        datetime.time,
        datetime.timedelta,
    ):
        refused_key(world, annotation)


@row(KEY)
def other_key_types_refused(world: World) -> None:
    class Slug(str):
        pass

    class Count(int):
        pass

    for annotation in (
        bool,
        float,
        Slug,
        Count,
        list[int],
        dict[str, int],
        Union[int, str],
    ):
        raises(world, TypeError, lambda: declare_key(annotation), "T.id")


@row(KEY)
def relation_key_refused(world: World) -> None:
    def build() -> None:
        json_object(
            pk="user",
        )(
            type(
                "Profile",
                (Base,),
                {
                    "class_url": "/profiles/",
                    "__annotations__": {"user": User},
                },
            )
        )

    raises(world, TypeError, build, "Profile.user", "int")


@row(KEY)
def hyperlinked_does_not_exist(world: World) -> None:
    assert not hasattr(eazyrest, "Hyperlinked")


DECL = "declaring-a-collection-of-relations"


def reads_as(annotation: Any, expected: type) -> None:
    model = declare_field(annotation)
    items: Any = model(json={"id": 1, "items": ["a", "b"]}).items
    keys = [item.pk for item in items]
    assert type(items) is expected
    assert sorted(keys) == ["a", "b"]
    if expected in (list, tuple, deque):
        assert keys == ["a", "b"]


@row(DECL)
def sequence_forms_read_as_a_list(world: World) -> None:
    for annotation in (
        list[Tag],
        MutableSequence[Tag],
        Optional[list[Tag]],
        Optional[MutableSequence[Tag]],
        list[Tag] | None,
    ):
        reads_as(annotation, list)


@row(DECL)
def tuple_forms_read_as_a_tuple(world: World) -> None:
    for annotation in (
        tuple[Tag, ...],
        Sequence[Tag],
        Optional[tuple[Tag, ...]],
        Optional[Sequence[Tag]],
    ):
        reads_as(annotation, tuple)


@row(DECL)
def deque_reads_as_a_deque(world: World) -> None:
    reads_as(deque[Tag], collections.deque)
    reads_as(Optional[deque[Tag]], collections.deque)


@row(DECL)
def mutable_set_forms_read_as_a_set(world: World) -> None:
    for annotation in (
        set[Tag],
        MutableSet[Tag],
        Optional[set[Tag]],
        Optional[MutableSet[Tag]],
    ):
        reads_as(annotation, set)


@row(DECL)
def frozen_set_forms_read_as_a_frozenset(world: World) -> None:
    for annotation in (
        frozenset[Tag],
        AbstractSet[Tag],
        Optional[frozenset[Tag]],
        Optional[AbstractSet[Tag]],
    ):
        reads_as(annotation, frozenset)


def refused_form(annotation: Any, *named: str) -> str:
    """Assert that a field type is refused, naming ``items`` and the texts.

    Returns:
        The message of the error.
    """
    error = None
    try:
        declare_field(annotation)
    except TypeError as caught:
        if type(caught) is not TypeError:
            raise AssertionError(
                f"{type(caught).__name__} was raised"
            ) from caught
        error = caught
    assert error is not None, "the declaration was admitted"
    names(str(error), "T.items", *named)
    return str(error)


@row(DECL)
def collection_refused(world: World) -> None:
    refused_form(Collection[Tag], "sequence", "set", "list[Tag]", "set[Tag]")


@row(DECL)
def iterable_refused(world: World) -> None:
    message = refused_form(Iterable[Tag], "Sequence[Tag]", "set[Tag]")
    assert message.index("Sequence[Tag]") < message.index("set[Tag]")


@row(DECL)
def wrapped_refused_forms_refused(world: World) -> None:
    refused_form(Optional[Collection[Tag]], "sequence", "set")
    refused_form(Optional[Iterable[Tag]], "Sequence[Tag]")
    refused_form(NewType("Tags", Collection[Tag]), "sequence", "set")
    refused_form(NewType("TagIter", Iterable[Tag]), "Sequence[Tag]")


@row(DECL)
def optional_items_refused(world: World) -> None:
    for annotation in (
        list[Optional[Tag]],
        list[Tag | None],
        set[Optional[Tag]],
        tuple[Tag | None, ...],
    ):
        refused_form(annotation, "every item has a key")


@row(DECL)
def collections_of_relation_collections_refused(world: World) -> None:
    for annotation in (
        list[list[Tag]],
        list[set[Tag]],
        tuple[list[Tag], ...],
        Collection[list[Tag]],
        Iterable[set[Tag]],
        list[Collection[Tag]],
    ):
        refused_form(annotation)


@row(DECL)
def collections_of_values_admitted(world: World) -> None:
    model = declare_field(list[int])
    assert model(json={"id": 1, "items": [1, 2]}).items == [1, 2]
    model = declare_field(set[str])
    assert model(json={"id": 1, "items": ["a"]}).items == {"a"}
    declare_field(Iterable[int])
    declare_field(Collection[int])


@row(DECL)
def fixed_tuples_and_mappings_refused(world: World) -> None:
    for annotation in (tuple[Tag, Tag], dict[str, Tag]):
        raises(world, TypeError, lambda: declare_field(annotation), "T.items")


BUILD = "building-an-object"


@row(BUILD)
def key_given_builds_the_url(world: World) -> None:
    user = User(id=7)
    assert user.pk == 7
    assert user.url == "/users/7/"
    assert world.count == 0


@row(BUILD)
def key_of_the_wrong_json_type_refused(world: World) -> None:
    for value in ("7", True, 7.0, [7], {"id": 7}):
        raises(world, FieldConversionError, lambda: User(id=value), "User.id")


@row(BUILD)
def a_model_object_as_the_key_refused(world: World) -> None:
    for value in (User(id=7), Admin(id=7), Other(id=7)):
        raises(world, FieldConversionError, lambda: User(id=value), "User.id")


@row(BUILD)
def key_read_from_json(world: World) -> None:
    assert User(json={"id": 7}).pk == 7


@row(BUILD)
def a_wrong_key_in_json_is_refused(world: World) -> None:
    raises(
        world,
        FieldConversionError,
        lambda: User(json={"id": "7"}),
        "User.id",
    )


@row(BUILD)
def a_missing_key_is_refused(world: World) -> None:
    for build in (lambda: User(), lambda: User(id=None)):
        raises(world, TypeError, build, "User.id", "create()")


@row(BUILD)
def fields_without_the_key_are_refused(world: World) -> None:
    raises(world, TypeError, lambda: User(name="a"), "User.id", "create()")


@row(BUILD)
def json_without_the_key_is_refused(world: World) -> None:
    for body in ({}, {"id": None}):
        raises(
            world,
            FieldConversionError,
            lambda: User(json=body),
            "User.id",
        )


@row(BUILD)
def a_slug_key_is_quoted_in_the_url(world: World) -> None:
    assert Tag(slug="a/b").url == "/tags/a%2Fb/"


@row(BUILD)
def a_slug_that_makes_no_path_has_no_url(world: World) -> None:
    for slug in ("", ".", ".."):
        raises(world, ValueError, lambda: Tag(slug=slug).url)


@row(BUILD)
def a_slug_key_of_the_wrong_type_refused(world: World) -> None:
    raises(world, FieldConversionError, lambda: Tag(slug=7), "Tag.slug")


@row(BUILD)
def a_uuid_key_is_read_from_any_spelling(world: World) -> None:
    for doc in (
        Doc(id=U),
        Doc(id=LOWER),
        Doc(id=UP),
        Doc(id=UC),
        Doc(json={"id": UP}),
    ):
        assert doc.pk == U
        assert doc.url == f"/docs/{LOWER}/"


@row(BUILD)
def a_uuid_key_is_read_from_the_braced_and_urn_forms(world: World) -> None:
    for text in (f"{{{LOWER}}}", f"urn:uuid:{LOWER}"):
        # Uppercase hex digits, with the "urn:uuid:" prefix as it is.
        for spelling in (text, text.replace(LOWER, UP)):
            doc = Doc(id=spelling)
            assert doc.pk == U
            assert doc.url == f"/docs/{LOWER}/"


@row(BUILD)
def a_uuid_key_refuses_a_spelling_outside_the_four_forms(
    world: World,
) -> None:
    digits = U.hex
    for text in (
        "0x" + digits[2:],
        "+" + digits[1:],
        digits[:15] + "_" + digits[16:],
        "-".join(digits[i : i + 4] for i in range(0, 32, 4)),
        " " + digits[1:],
        digits[:-1] + "\n",
        digits[:-1] + chr(0xFF17),
    ):
        raises(world, FieldConversionError, lambda: Doc(id=text), "Doc.id")
        raises(
            world,
            FieldConversionError,
            lambda: Doc(json={"id": text}),
            "Doc.id",
        )
        for call in queries(Doc, id=text):
            raises(world, FieldConversionError, call, "Doc.id")


SHELF: dict[str, Any] = {
    "id": 1,
    "cover": LOWER,
    "papers": [LOWER],
    "docset": [LOWER],
}


def shelf(**over: Any) -> Shelf:
    """Return a shelf read from JSON with the given keys replaced."""
    return Shelf(json={**SHELF, **over})


@row(BUILD)
def a_uuid_key_of_a_relation_is_read_from_any_spelling(
    world: World,
) -> None:
    spellings = (
        UP,
        UC,
        f"{{{LOWER}}}",
        f"urn:uuid:{LOWER}",
        f"{{{UP}}}",
        f"urn:uuid:{UP}",
    )
    for text in spellings:
        obj = shelf(cover=text, papers=[text], docset=[text])
        assert obj.cover == Doc(id=U)
        assert pks(obj.papers) == [U]
        assert pks(obj.docset) == [U]
    for text in spellings:
        values: tuple[Any, ...] = (text, Doc(id=text))
        for value in values:
            obj = shelf()
            obj.cover = value
            obj.papers = [value]
            obj.docset = {value}
            assert pks([obj.cover]) == [U]
            assert pks(obj.papers) == [U]
            assert pks(obj.docset) == [U]


@row(BUILD)
def a_uuid_key_of_a_relation_is_sent_in_its_canonical_form(
    world: World,
) -> None:
    for text in (UP, UC, f"{{{UP}}}", f"urn:uuid:{UP}"):
        values: list[tuple[str, Any]] = [
            ("cover", text),
            ("papers", [text]),
            ("docset", {text}),
        ]
        for field, value in values:
            body = world.body(
                lambda: Shelf.create(**{field: value}),
                "POST",
            )
            assert body == {field: [LOWER] if field != "cover" else LOWER}


@row(BUILD)
def a_uuid_key_of_a_relation_refuses_a_spelling_outside_the_four_forms(
    world: World,
) -> None:
    digits = U.hex
    for text in (
        "0x" + digits[2:],
        "+" + digits[1:],
        digits[:15] + "_" + digits[16:],
        "-".join(digits[i : i + 4] for i in range(0, 32, 4)),
        " " + digits[1:],
        digits[:-1] + "\n",
        digits[:-1] + chr(0xFF17),
    ):
        given: list[tuple[str, Any, str]] = [
            ("cover", text, "Shelf.cover"),
            ("papers", [text], "Shelf.papers"),
            ("docset", {text}, "Shelf.docset"),
        ]
        for field, value, bare in given:
            refuses_assignment(world, shelf(), field, value, bare)
            raises(
                world,
                FieldConversionError,
                lambda: Shelf.create(**{field: value}),
                bare,
            )
            for call in queries(Shelf, **{field: value}):
                raises(world, FieldConversionError, call, bare)
            obj = shelf(**{field: value})
            raises(
                world,
                FieldConversionError,
                lambda: getattr(obj, field),
                bare,
            )


def shelf_field(obj: Shelf, field: str) -> Any:
    """Return the object that a shelf's relation holds, or its only item."""
    value = getattr(obj, field)
    return value if field == "cover" else next(iter(value))


@row(BUILD)
def a_relation_keeps_its_objects_when_assigned_its_own_value(
    world: World,
) -> None:
    for text in (UP, UC):
        for field in ("cover", "papers", "docset"):
            obj = shelf(cover=text, papers=[text], docset=[text])
            held = shelf_field(obj, field)
            setattr(obj, field, getattr(obj, field))
            assert shelf_field(obj, field) is held
            before = world.count
            obj.save()
            assert world.count == before


@row(BUILD)
def a_relation_keeps_its_objects_when_the_server_spells_the_key_otherwise(
    world: World,
) -> None:
    mine = Doc(id=U)
    obj = shelf(cover=str(uuid.uuid4()))
    obj.cover = mine
    world.mocker.patch("https://rows.example/shelves/1/", json={"cover": UP})
    obj.save()
    assert obj.cover is mine
    partial = Shelf(id=1)
    partial.update_from_json({"cover": LOWER})
    held = partial.cover
    world.mocker.get(
        "https://rows.example/shelves/1/",
        json={"id": 1, "cover": UP, "papers": [], "docset": []},
    )
    assert partial.papers == []
    assert partial.cover is held


@row(BUILD)
def a_bad_uuid_refused(world: World) -> None:
    for value in ("not-a-uuid", 7):
        raises(world, FieldConversionError, lambda: Doc(id=value), "Doc.id")


@row(BUILD)
def a_model_without_a_key_refused(world: World) -> None:
    raises(world, UsageError, lambda: Entry(name="x"))


EQ = "equality-hashing-and-copies"


@row(EQ)
def same_class_and_key_are_equal(world: World) -> None:
    assert User(id=7) == User(id=7)
    assert hash(User(id=7)) == hash(User(id=7))
    assert world.count == 0


@row(EQ)
def another_class_is_not_equal(world: World) -> None:
    assert User(id=7) != Admin(id=7)
    assert User(id=7) != Other(id=7)


@row(EQ)
def a_uuid_key_is_equal_in_every_spelling(world: World) -> None:
    for other in (UP, UC):
        assert Doc(id=U) == Doc(id=other)
        assert hash(Doc(id=U)) == hash(Doc(id=other))


@row(EQ)
def a_str_key_is_case_sensitive(world: World) -> None:
    assert Tag(slug="A") != Tag(slug="a")


@row(EQ)
def a_model_with_no_key_is_equal_only_to_itself(world: World) -> None:
    entry = Entry(json={"name": "x"})
    same = entry
    assert entry == same
    assert entry != Entry(json={"name": "x"})
    assert hash(entry) == object.__hash__(entry)


def assert_copy_as_deepcopy(original: Post, duplicate: Post) -> None:
    assert duplicate is not original
    assert duplicate.owner is not original.owner
    assert duplicate.owner == original.owner
    original.tags.append(Tag(slug="z"))
    assert [t.pk for t in duplicate.tags] == ["a"]
    original.tags.pop()
    assert duplicate.api is original.api
    assert duplicate == original


@row(EQ)
def a_shallow_copy_is_a_deep_copy(world: World) -> None:
    original = post()
    assert original.owner is not None
    assert_copy_as_deepcopy(original, copy.copy(original))


@row(EQ)
def a_deep_copy_shares_only_the_api(world: World) -> None:
    original = post()
    assert original.owner is not None
    assert_copy_as_deepcopy(original, copy.deepcopy(original))


@row(EQ)
def a_copy_shares_the_api_given_as_api(world: World) -> None:
    api = API("https://given.example/")
    original = Post(json=BODY, api=api)
    assert original.owner is not None
    assert original.owner.api is api
    for duplicate in (copy.copy(original), copy.deepcopy(original)):
        assert_copy_as_deepcopy(original, duplicate)
        assert duplicate.api is api
        assert duplicate.owner is not None
        assert duplicate.owner.api is api


@row(EQ)
def a_pickle_round_trip_is_a_deep_copy(world: World) -> None:
    original = post()
    assert original.owner is not None
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        duplicate = pickle.loads(pickle.dumps(original, protocol=protocol))
        assert_copy_as_deepcopy(original, duplicate)


def copies_of(original: Post) -> list[Post]:
    """Return a shallow copy, a deep copy, and a pickle round trip."""
    return [
        copy.copy(original),
        copy.deepcopy(original),
        *(
            pickle.loads(pickle.dumps(original, protocol=protocol))
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1)
        ),
    ]


@row(EQ)
def a_copy_after_a_write_options_block_is_a_deep_copy(world: World) -> None:
    original = post()
    assert original.owner is not None
    with original.write_options(headers={"If-Match": "x"}):
        pass
    with original.owner.write_options(headers={"If-Match": "y"}):
        pass
    for duplicate in copies_of(original):
        assert_copy_as_deepcopy(original, duplicate)


@row(EQ)
def a_copy_made_in_a_write_options_block_sends_none_of_its_options(
    world: World,
) -> None:
    original = post()
    with original.write_options(headers={"If-Match": "x"}):
        duplicates = copies_of(original)
    for duplicate in duplicates:
        duplicate.title = "new"
        world.body(duplicate.save, "PATCH")
        assert "If-Match" not in world.requests[-1].headers


@row(EQ)
def objects_that_hold_each_other_in_sets_survive_a_pickle(
    world: World,
) -> None:
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        m = Member(json={"id": 1, "friends": [2]})
        n = Member(json={"id": 2, "friends": [1]})
        m.friends = {n}
        n.friends = {m}
        duplicate = pickle.loads(pickle.dumps(m, protocol=protocol))
        expected = copy.deepcopy(m)
        assert duplicate == expected
        assert duplicate.friends == expected.friends
        (friend,) = duplicate.friends
        assert friend.pk == 2
        assert friend.friends == {duplicate}
        assert friend.friends.pop() is duplicate
        # A deep copy of a cycle keeps the cycle, so the copy's friend holds
        # the copy itself, not a third object.
        (copied_friend,) = expected.friends
        (back,) = copied_friend.friends
        assert back is expected
        assert copied_friend is not n


REFUSED_ASSIGNMENTS: list[tuple[str, Callable[[], Any], tuple[str, ...]]] = [
    ("owner", lambda: "8", ("Post.owner",)),
    ("owner", lambda: True, ("Post.owner",)),
    ("owner", lambda: 8.0, ("Post.owner",)),
    ("owner", lambda: Other(id=8), ("Post.owner", "User", "Other")),
    ("owner", lambda: {"id": 8}, ("Post.owner",)),
    ("readers", lambda: [Other(id=1)], ("Post.readers",)),
    ("readers", lambda: [None], ("Post.readers", "[0]")),
    ("readers", lambda: [{"id": 8}], ("Post.readers",)),
    ("readers", lambda: ["8"], ("Post.readers",)),
    ("readers", lambda: User(id=8), ("Post.readers", "collections")),
    ("tags", lambda: Crew(slug="a"), ("Post.tags", "collections")),
    ("tagset", lambda: {None}, ("Post.tagset",)),
    ("watchers", lambda: [Other(id=1)], ("Post.watchers",)),
]


@row("assigning-a-relation")
def a_refused_assignment_to_an_unloaded_object_sends_nothing(
    world: World,
) -> None:
    for status in (200, 404):
        world.mocker.get(requests_mock.ANY, status_code=status, json={})
        for field, make, named in REFUSED_ASSIGNMENTS:
            obj = Post(id=1)
            raises(
                world,
                FieldConversionError,
                lambda: setattr(obj, field, make()),
                *named,
            )
            assert obj._json is None


ASSIGN = "assigning-the-key"


@row(ASSIGN)
def another_key_is_refused_for_a_keyed_object(world: World) -> None:
    for attribute in ("id", "pk"):
        user = User(json={"id": 7, "name": "a"})
        refuses_assignment(world, user, attribute, 8, exc=AttributeError)
        assert user.pk == 7


@row(ASSIGN)
def the_same_key_does_nothing(world: World) -> None:
    user = User(id=7)
    user.id = 7
    assert user.pk == 7
    user.save()
    assert world.count == 0


OWNER = "a-single-relation"


def assigned_owner_update(value: Any, world: World) -> Any:
    obj = post()
    obj.owner = value
    return world.body(obj.save, "PATCH")


def created_owner_body(value: Any, world: World) -> Any:
    return world.body(lambda: Post.create(owner=value), "POST")


@row(OWNER)
def a_key_or_object_of_the_model_is_sent_as_its_key(world: World) -> None:
    for value in (8, User(id=8), Admin(id=8)):
        assert assigned_owner_update(value, world) == {"owner": 8}
        assert created_owner_body(value, world) == {"owner": 8}


@row(OWNER)
def none_is_sent_as_null(world: World) -> None:
    assert assigned_owner_update(None, world) == {"owner": None}
    assert created_owner_body(None, world) == {"owner": None}


@row(OWNER)
def a_key_of_the_wrong_type_is_refused_for_a_relation(world: World) -> None:
    for value in ("8", True, 8.0):
        obj = post()
        refuses_assignment(world, obj, "owner", value, "Post.owner")
        raises(
            world,
            FieldConversionError,
            lambda: Post.create(owner=value),
            "Post.owner",
        )


@row(OWNER)
def an_object_of_another_model_is_refused(world: World) -> None:
    obj = post()
    names_ = ("Post.owner", "User", "Other")
    refuses_assignment(world, obj, "owner", Other(id=8), *names_)
    raises(
        world,
        FieldConversionError,
        lambda: Post.create(owner=Other(id=8)),
        *names_,
    )


@row(OWNER)
def a_mapping_is_refused_for_a_relation(world: World) -> None:
    obj = post()
    refuses_assignment(world, obj, "owner", {"id": 8}, "Post.owner")
    raises(
        world,
        FieldConversionError,
        lambda: Post.create(owner={"id": 8}),
        "Post.owner",
    )


@row(OWNER)
def a_key_in_json_reads_as_an_object(world: World) -> None:
    obj = post(owner=7)
    assert obj.owner == User(id=7)
    assert world.count == 0


@row(OWNER)
def a_key_of_the_wrong_type_in_json_is_refused_when_read(
    world: World,
) -> None:
    obj = post(owner="7")
    raises(world, FieldConversionError, lambda: obj.owner, "Post.owner")


@row(OWNER)
def an_embedded_object_is_read_without_a_request(world: World) -> None:
    obj = post(owner={"id": 7, "name": "a"})
    assert obj.owner is not None
    assert obj.owner.name == "a"
    assert world.count == 0


@row(OWNER)
def an_embedded_object_without_a_key_is_refused_when_read(
    world: World,
) -> None:
    obj = post(owner={"name": "a"})
    raises(world, FieldConversionError, lambda: obj.owner, "Post.owner", "key")


@row(OWNER)
def a_full_save_sends_an_embedded_owner_as_its_key(world: World) -> None:
    obj = post(owner={"id": 7})
    body = world.body(lambda: obj.save(full=True), "PUT")
    assert body["owner"] == 7


@row(OWNER)
def an_embedded_owner_assigned_its_own_object_sends_nothing(
    world: World,
) -> None:
    obj = post(owner={"id": 7, "name": "a"})
    held = obj.owner

    def assign_own_object(p: Post) -> None:
        p.owner = p.owner

    assert patch_after(world, obj, assign_own_object) is None
    assert obj.owner is held

    def assign_another(p: Post) -> None:
        p.owner = User(id=8)

    assert patch_after(world, obj, assign_another) == {"owner": 8}


COLL = "a-collection-of-relations"

SEQUENCES = ("tags", "tagt", "tagq")
"""The sequence forms of a collection of tags: a list, a tuple, a deque."""

CHANGED_IN_PLACE = ("tags", "tagq")
"""The sequence forms that change in place."""

SETS = ("tagset", "tagfs")
"""The set forms of a collection of tags: a set and a frozenset."""

FORM: dict[str, type] = {
    "tags": list,
    "tagt": tuple,
    "tagq": deque,
    "tagset": set,
    "tagfs": frozenset,
}
"""The type that each collection field reads as."""


def patch_after(world: World, obj: Post, change: Callable[[Post], Any]) -> Any:
    change(obj)
    before = world.count
    obj.save()
    sent = world.requests[before:]
    if not sent:
        return None
    assert [r.method for r in sent] == ["PATCH"]
    return json.loads(sent[0].text)


def pks(items: Iterable[Any]) -> list[Any]:
    return [item.pk for item in items]


def tag(slug: str) -> Tag:
    return Tag(slug=slug)


@row(COLL)
def a_null_item_is_refused_on_read(world: World) -> None:
    raises(
        world,
        FieldConversionError,
        lambda: post(readers=[None]).readers,
        "Post.readers",
        "[0]",
    )
    raises(
        world,
        FieldConversionError,
        lambda: post(watchers=[None, 7]).watchers,
        "Post.watchers",
        "[0]",
    )


@row(COLL)
def a_null_item_is_refused_on_write(world: World) -> None:
    p = post()
    refuses_assignment(world, p, "readers", [None], "Post.readers", "[0]")
    p.readers.append(None)  # type: ignore[arg-type]
    raises(world, FieldConversionError, p.save, "Post.readers", "[1]")
    raises(
        world,
        FieldConversionError,
        lambda: Post.create(readers=[None]),
        "Post.readers",
        "[0]",
    )
    # A set has no positions, so its message names none.
    error = refuses_assignment(
        world, post(), "watchers", {None}, "Post.watchers"
    )
    assert "[0]" not in str(error)
    q = post(watchers=[])
    q.watchers.add(None)  # type: ignore[arg-type]
    error = raises(world, FieldConversionError, q.save, "Post.watchers")
    assert "[0]" not in str(error)
    raises(
        world,
        FieldConversionError,
        lambda: Post.create(watchers={None, User(id=7)}),
        "Post.watchers",
    )


@row(COLL)
def sequences_are_read_as_the_declared_type(world: World) -> None:
    obj = post(tags=["a", "b"], tagt=["a", "b"], tagq=["a", "b"])
    for field in SEQUENCES:
        value: Any = getattr(obj, field)
        items = list(value)
        assert type(value) is FORM[field]
        assert items == [tag("a"), tag("b")]


@row(COLL)
def an_embedded_item_is_read_by_its_key(world: World) -> None:
    for field in SEQUENCES:
        obj = post(**{field: [{"slug": "a"}]})
        assert pks(getattr(obj, field)) == ["a"]


@row(COLL)
def an_item_without_a_key_is_refused_when_read(world: World) -> None:
    for field in SEQUENCES:
        obj = post(**{field: ["a", {"x": 1}]})
        raises(
            world,
            FieldConversionError,
            lambda: getattr(obj, field),
            f"Post.{field}",
            "[1]",
            "TypedDict",
            "dataclass",
        )


@row(COLL)
def an_assigned_collection_is_sent_as_keys(world: World) -> None:
    for field in SEQUENCES:
        for value in ([tag("b")], (tag("b"),)):
            obj = post()
            assert patch_after(
                world, obj, lambda p: setattr(p, field, value)
            ) == {field: ["b"]}
            assert type(getattr(obj, field)) is FORM[field]


@row(COLL)
def an_assigned_item_of_another_model_is_refused(world: World) -> None:
    for field in SEQUENCES:
        obj = post()
        refuses_assignment(world, obj, field, [Other(id=1)], f"Post.{field}")


@row(COLL)
def an_assigned_object_that_is_no_collection_is_refused(
    world: World,
) -> None:
    for field in SEQUENCES:
        obj = post()
        refuses_assignment(
            world, obj, field, tag("b"), f"Post.{field}", "collections"
        )


@row(COLL)
def an_iterable_model_is_refused_for_a_collection_and_kept_in_a_lookup(
    world: World,
) -> None:
    crew = Crew(slug="a")
    for field in (*SEQUENCES, *SETS):
        given: dict[str, Any] = {field: crew}
        obj = post()
        refuses_assignment(
            world, obj, field, crew, f"Post.{field}", "collections"
        )
        raises(
            world,
            FieldConversionError,
            lambda: Post.create(**given),
            f"Post.{field}",
            "collections",
        )
        sends_query_but_refuses_creation(
            world,
            Post,
            f"{field}=a",
            FieldConversionError,
            f"Post.{field}",
            **given,
        )


@row(COLL)
def embedded_items_assigned_their_own_collection_send_nothing(
    world: World,
) -> None:
    for field in SEQUENCES:
        obj = post(**{field: [{"slug": "a"}]})
        held = getattr(obj, field)

        def assign_own_collection(p: Post) -> None:
            setattr(p, field, getattr(p, field))

        assert patch_after(world, obj, assign_own_collection) is None
        assert getattr(obj, field) is not None
        assert pks(getattr(obj, field)) == ["a"]
        assert type(getattr(obj, field)) is type(held)

        def assign_another(p: Post) -> None:
            setattr(p, field, [tag("b")])

        assert patch_after(world, obj, assign_another) == {field: ["b"]}


@row(COLL)
def a_bare_key_added_in_place_is_refused_at_save(world: World) -> None:
    cases: list[tuple[str, Callable[[Post], Any], str]] = [
        ("tags", lambda p: p.tags.append("b"), "[1]"),  # type: ignore[arg-type]
        ("readers", lambda p: p.readers.append(8), "[1]"),  # type: ignore[arg-type]
        ("tagq", lambda p: p.tagq.append("b"), "[1]"),  # type: ignore[arg-type]
    ]
    for field, add, position in cases:
        for full in (False, True):
            obj = post()
            add(obj)
            raises(
                world,
                FieldConversionError,
                lambda: obj.save(full=full),
                f"Post.{field}",
                position,
            )


@row(COLL)
def an_appended_item_is_sent(world: World) -> None:
    for field in CHANGED_IN_PLACE:
        obj = post()
        sent = patch_after(
            world, obj, lambda p: getattr(p, field).append(tag("b"))
        )
        assert sent == {field: ["a", "b"]}


@row(COLL)
def a_replaced_item_is_sent(world: World) -> None:
    for field in CHANGED_IN_PLACE:
        obj = post()

        def change(p: Post) -> None:
            getattr(p, field)[0] = tag("b")

        assert patch_after(world, obj, change) == {field: ["b"]}


@row(COLL)
def a_reordered_collection_is_sent(world: World) -> None:
    for field in CHANGED_IN_PLACE:
        obj = post(**{field: ["a", "b"]})
        sent = patch_after(world, obj, lambda p: getattr(p, field).reverse())
        assert sent == {field: ["b", "a"]}


@row(COLL)
def an_item_added_to_the_left_of_a_deque_is_sent(world: World) -> None:
    obj = post(tagq=["a"])
    assert patch_after(world, obj, lambda p: p.tagq.appendleft(tag("b"))) == {
        "tagq": ["b", "a"]
    }


@row(COLL)
def a_new_object_of_the_same_key_sends_nothing(world: World) -> None:
    for field in CHANGED_IN_PLACE:
        obj = post()

        def change(p: Post) -> None:
            getattr(p, field)[0] = tag("a")

        assert patch_after(world, obj, change) is None


@row(COLL)
def a_subclass_object_of_the_same_key_sends_nothing(world: World) -> None:
    obj = post()

    def change(p: Post) -> None:
        p.readers[0] = Admin(id=7)

    assert patch_after(world, obj, change) is None


@row(COLL)
def a_saved_change_is_not_sent_again(world: World) -> None:
    for field in CHANGED_IN_PLACE:
        obj = post()
        getattr(obj, field).append(tag("b"))
        obj.save()
        before = world.count
        obj.save()
        assert world.count == before


@row(COLL)
def an_assigned_collection_is_copied(world: World) -> None:
    for field in CHANGED_IN_PLACE:
        first, second = post(), post(postId=2)
        setattr(second, field, getattr(first, field))
        getattr(first, field).append(tag("c"))
        assert patch_after(world, first, lambda p: None) == {field: ["a", "c"]}
        assert patch_after(world, second, lambda p: None) is None


@row(COLL)
def an_unread_collection_without_keys_is_not_sent(world: World) -> None:
    for field in SEQUENCES:
        obj = post(**{field: [{"x": 1}]})

        def change(p: Post) -> None:
            p.title = "x"

        assert patch_after(world, obj, change) == {"title": "x"}


@row(COLL)
def a_full_save_refuses_a_collection_without_keys(world: World) -> None:
    for field in SEQUENCES:
        obj = post(**{field: [{"x": 1}]})
        raises(
            world,
            FieldConversionError,
            lambda: obj.save(full=True),
            f"Post.{field}",
        )


SET = "a-set-of-relations"


@row(SET)
def sets_are_read_as_the_declared_type(world: World) -> None:
    obj = post()
    for field in SETS:
        value = getattr(obj, field)
        assert type(value) is FORM[field]
        assert value == {tag("a"), tag("b")}


@row(SET)
def a_repeated_key_is_one_member(world: World) -> None:
    for field in SETS:
        for items in (["a", "a"], [{"slug": "a"}, {"slug": "a"}]):
            obj = post(**{field: items})
            assert len(getattr(obj, field)) == 1
            assert pks(getattr(obj, field)) == ["a"]


@row(SET)
def a_set_item_without_a_key_is_refused_when_read(world: World) -> None:
    for field in SETS:
        obj = post(**{field: ["a", {"x": 1}]})
        raises(
            world,
            FieldConversionError,
            lambda: getattr(obj, field),
            f"Post.{field}",
            "[1]",
            "TypedDict",
            "dataclass",
        )


@row(SET)
def an_equal_set_sends_nothing(world: World) -> None:
    for field in SETS:
        for kind in (set, frozenset):
            obj = post()
            same = kind({tag("a"), tag("b")})
            assert (
                patch_after(world, obj, lambda p: setattr(p, field, same))
                is None
            )


@row(SET)
def another_set_is_sent_sorted(world: World) -> None:
    for field in SETS:
        for kind in (set, frozenset):
            obj = post()
            other = kind({tag("c"), tag("a")})
            sent = patch_after(world, obj, lambda p: setattr(p, field, other))
            assert sent == {field: ["a", "c"]}


@row(SET)
def a_list_assigned_to_a_set_field_is_deduplicated(world: World) -> None:
    for field in SETS:
        obj = post()
        sent = patch_after(
            world, obj, lambda p: setattr(p, field, [tag("c"), tag("c")])
        )
        assert sent == {field: ["c"]}
        assert type(getattr(obj, field)) is FORM[field]


@row(SET)
def a_tuple_assigned_to_a_set_field_reads_as_the_declared_type(
    world: World,
) -> None:
    for field in SETS:
        obj = post()
        setattr(obj, field, (tag("c"),))
        assert type(getattr(obj, field)) is FORM[field]


@row(SET)
def an_added_member_is_sent_sorted(world: World) -> None:
    obj = post()
    assert patch_after(world, obj, lambda p: p.tagset.add(tag("c"))) == {
        "tagset": ["a", "b", "c"]
    }


@row(SET)
def a_discarded_member_is_sent(world: World) -> None:
    obj = post()
    assert patch_after(world, obj, lambda p: p.tagset.discard(tag("a"))) == {
        "tagset": ["b"]
    }


@row(SET)
def a_new_object_of_a_present_key_sends_nothing(world: World) -> None:
    obj = post()
    assert patch_after(world, obj, lambda p: p.tagset.add(tag("a"))) is None


@row(SET)
def a_repeated_stored_key_is_not_a_change(world: World) -> None:
    for field in SETS:
        obj = post(**{field: ["a", "a"]})

        def assign_own_value(p: Post) -> None:
            setattr(p, field, getattr(p, field))

        assert patch_after(world, obj, assign_own_value) is None


@row(SET)
def a_bare_key_added_to_a_set_in_place_is_refused_at_save(
    world: World,
) -> None:
    for full in (False, True):
        obj = post()
        obj.tagset.add("c")  # type: ignore[arg-type]
        raises(
            world,
            FieldConversionError,
            lambda: obj.save(full=full),
            "Post.tagset",
        )


@row(SET)
def a_saved_set_change_is_not_sent_again(world: World) -> None:
    obj = post()
    obj.tagset.add(tag("c"))
    obj.save()
    before = world.count
    obj.save()
    assert world.count == before


@row(SET)
def members_of_one_key_are_sent_once(world: World) -> None:
    obj = post(watchers=[])
    both = {User(id=7), Admin(id=7)}
    assert patch_after(world, obj, lambda p: setattr(p, "watchers", both)) == {
        "watchers": [7]
    }
    assert len(obj.watchers) == 2


@row(SET)
def replacing_a_member_by_a_subclass_object_sends_nothing(
    world: World,
) -> None:
    obj = post()

    def change(p: Post) -> None:
        p.watchers.discard(User(id=7))
        p.watchers.add(Admin(id=7))

    assert patch_after(world, obj, change) is None


@row(SET)
def an_assigned_set_is_copied(world: World) -> None:
    first, second = post(), post(postId=2)
    second.tagset = first.tagset
    first.tagset.add(tag("c"))
    assert patch_after(world, first, lambda p: None) == {
        "tagset": ["a", "b", "c"]
    }
    assert patch_after(world, second, lambda p: None) is None


@row(SET)
def a_full_save_sends_sets_sorted(world: World) -> None:
    obj = post()
    body = world.body(lambda: obj.save(full=True), "PUT")
    assert body["tagset"] == ["a", "b"]
    assert body["tagfs"] == ["a", "b"]


OWN = "queries-on-the-models-own-key"


@row(OWN)
def the_key_by_its_python_name_is_sent_by_its_json_name(
    world: World,
) -> None:
    sends_query(world, Post, "postId=7", id=7)


@row(OWN)
def a_lookup_on_the_key_is_sent_by_its_json_name(world: World) -> None:
    sends_query(world, Post, "postId__gt=7", id__gt=7)


@row(OWN)
def pk_is_sent_as_pk(world: World) -> None:
    sends_query(world, Post, "pk=7", pk=7)


@row(OWN)
def pk_lookups_are_sent_as_pk(world: World) -> None:
    sends_query(world, Post, "pk__in=7,8", pk__in=[7, 8])
    sends_query(world, Post, "pk__range=1,5", pk__range=(1, 5))
    api = API("https://rows.example/")
    api.list_lookup_style = "repeat"
    for call in (
        lambda: list(Post.filter(pk__in=[7, 8], api=api)),
        lambda: Post.get(pk__in=[7, 8], api=api),
        lambda: Post.get_or_create(pk__in=[7, 8], api=api),
    ):
        assert world.query(call) == "pk__in=7&pk__in=8"


@row(OWN)
def a_key_of_the_wrong_type_is_refused_in_a_query(world: World) -> None:
    for kw in ({"id": "7"}, {"pk": "7"}, {"id__gt": "7"}, {"id": True}):
        refuses_query(world, Post, FieldConversionError, "Post.id", **kw)


@row(OWN)
def an_object_of_the_model_is_sent_as_the_key(world: World) -> None:
    other = Post(json={"postId": 3})
    sends_query_but_refuses_creation(
        world, Post, "postId=3", FieldConversionError, "Post.id", id=other
    )
    sends_query_but_refuses_creation(
        world, Post, "pk=3", FieldConversionError, "Post.id", pk=other
    )
    sends_query(world, Post, "pk__in=3", pk__in=[other])


@row(OWN)
def an_iterable_model_is_sent_as_the_key(world: World) -> None:
    crew = Crew(slug="a")
    for name, expected in (("slug", "slug=a"), ("pk", "pk=a")):
        sends_query_but_refuses_creation(
            world,
            Tag,
            expected,
            FieldConversionError,
            "Tag.slug",
            **{name: crew},
        )


@row(OWN)
def a_model_is_refused_by_a_lookup_of_several_values(world: World) -> None:
    refuses_query(world, Tag, ValueError, "pk__in", pk__in=Crew(slug="a"))
    refuses_query(world, Post, ValueError, "id__range", id__range=Crowd(id=5))
    refuses_query(world, Post, ValueError, "title__in", title__in=Crowd(id=5))


@row(OWN)
def a_subclass_object_is_sent_as_its_key(world: World) -> None:
    sends_query_but_refuses_creation(
        world, User, "id=7", FieldConversionError, "User.id", id=Admin(id=7)
    )


@row(OWN)
def an_object_of_another_model_is_refused_in_a_query(world: World) -> None:
    refuses_query(
        world,
        Post,
        FieldConversionError,
        "Post.id",
        "Post",
        "User",
        id=User(id=8),
    )


@row(OWN)
def an_object_of_a_base_class_is_refused_as_the_key_of_a_subclass(
    world: World,
) -> None:
    for name in ("id", "pk"):
        for call in lookups(Admin, **{name: User(id=7)}):
            error = raises(
                world, FieldConversionError, call, "Admin.id", "Admin", "User"
            )
            assert "relation" not in str(error)
            assert "key" in str(error)


@row(OWN)
def two_parameters_naming_the_key_are_refused(world: World) -> None:
    refuses_query(world, Post, ValueError, "pk", "id", pk=7, id=8)
    refuses_query(
        world, Post, ValueError, "pk__exact", "id", pk__exact=7, id=8
    )
    refuses_query(
        world, Post, ValueError, "id__exact", "pk", id__exact=7, pk=8
    )
    refuses_query(world, Post, ValueError, "pk", "postId", pk=7, postId=8)
    refuses_query(
        world, Post, ValueError, "pk__gt", "postId__gt", pk__gt=7, postId__gt=8
    )
    refuses_query(
        world,
        Post,
        ValueError,
        "id__in",
        "postId__in",
        id__in=[1],
        postId__in=[2],
    )
    sends_query(
        world, Post, "pk=7&postId__icontains=8", pk=7, postId__icontains="8"
    )
    refuses_query(world, Post, ValueError, "postId", "pk", postId=7, pk=8)
    refuses_query(
        world,
        Post,
        ValueError,
        "pk__exact",
        "postId",
        pk__exact=7,
        postId=8,
    )


@row(OWN)
def a_collection_for_a_lookup_of_one_value_is_refused(world: World) -> None:
    for name in (
        "id",
        "id__exact",
        "id__gt",
        "id__gte",
        "id__lt",
        "id__lte",
        "pk",
        "pk__exact",
        "pk__gt",
    ):
        for value in ([7, 8], (7, 8), {7, 8}):
            refuses_query(world, Post, ValueError, name, **{name: value})


@row(OWN)
def a_collection_for_the_json_name_of_the_key_is_refused(
    world: World,
) -> None:
    for name in ("postId", "postId__exact", "postId__gt"):
        for value in ([7, 8], (7, 8), {7, 8}):
            refuses_query(world, Post, ValueError, name, **{name: value})
    sends_query(world, Post, "postId__in=7,8", postId__in=[7, 8])


@row(OWN)
def a_collection_for_a_key_json_name_with_two_underscores_is_refused(
    world: World,
) -> None:
    for name in ("doc__id", "doc__id__exact", "doc__id__gt"):
        for value in ([7, 8], (7, 8), {7, 8}):
            refuses_query(world, DK, ValueError, name, **{name: value})
    world.mocker.get(requests_mock.ANY, json=[{"doc__id": 7}])
    sends_query(world, DK, "doc__id__in=7,8", doc__id__in=[7, 8])


@row(OWN)
def two_parameters_naming_the_key_by_its_write_key_are_refused(
    world: World,
) -> None:
    refuses_query(world, WK, ValueError, "pk", "ident", pk=7, ident=8)
    refuses_query(world, WK, ValueError, "id", "ident", id=7, ident=8)
    refuses_query(
        world, WK, ValueError, "pk__gt", "ident__gt", pk__gt=7, ident__gt=8
    )
    sends_query(world, WK, "ident=8", ident=8)


@row(OWN)
def the_json_name_is_an_ordinary_name(world: World) -> None:
    sends_query(world, Post, "postId=7", postId=7)


@row(OWN)
def an_ordinary_name_is_sent_as_given(world: World) -> None:
    sends_query_but_refuses_creation(
        world, Post, "postId=7", FieldConversionError, "Post.id", postId="7"
    )


@row(OWN)
def two_parameters_sending_one_name_are_refused(world: World) -> None:
    refuses_query(world, Post, ValueError, "postId", id=7, postId=8)


@row(OWN)
def a_uuid_key_is_sent_in_its_canonical_form(world: World) -> None:
    sends_query(world, Doc, f"id={LOWER}", id=UP)
    sends_query(world, Doc, f"id={LOWER}", id=U)


@row(OWN)
def a_str_key_of_the_wrong_type_is_refused_in_a_query(world: World) -> None:
    refuses_query(world, Tag, FieldConversionError, "Tag.slug", slug=7)


REL = "queries-on-a-relation"


@row(REL)
def a_key_or_object_is_sent_as_the_key(world: World) -> None:
    for value in (7, User(id=7), Admin(id=7)):
        sends_query(world, Post, "owner=7", owner=value)


@row(REL)
def a_key_of_the_wrong_type_is_refused_for_the_relation(
    world: World,
) -> None:
    for value in ("7", True):
        refuses_query(
            world, Post, FieldConversionError, "Post.owner", owner=value
        )


@row(REL)
def an_object_of_another_model_is_refused_for_the_relation(
    world: World,
) -> None:
    refuses_query(
        world, Post, FieldConversionError, "Post.owner", owner=Other(id=7)
    )


@row(REL)
def a_collection_for_a_single_relation_is_refused(world: World) -> None:
    for name in ("owner", "owner__exact", "owner__gt"):
        refuses_query(world, Post, ValueError, name, **{name: [7, 8]})


@row(REL)
def several_keys_are_joined(world: World) -> None:
    sends_query(world, Post, "owner__in=7,8", owner__in=[7, User(id=8)])


@row(REL)
def a_key_of_the_wrong_type_among_several_is_refused(world: World) -> None:
    refuses_query(
        world, Post, FieldConversionError, "Post.owner", owner__in=["7"]
    )


@row(REL)
def a_relation_is_queried_under_its_json_name(world: World) -> None:
    sends_query(world, Task, "userId=3", user=3)
    sends_query(world, Task, "userId__in=3,5", user__in=[3, User(id=5)])


@row(REL)
def a_set_field_is_queried_by_its_distinct_keys_sorted(world: World) -> None:
    sends_query(
        world,
        Post,
        "watchers=7",
        watchers={User(id=7), Admin(id=7)},
    )
    sends_query(
        world,
        Post,
        "tagset=a&tagset=b",
        tagset=[Tag(slug="b"), Tag(slug="a"), Tag(slug="b")],
    )


@row(REL)
def a_set_for_a_sequence_relation_is_sent_sorted(world: World) -> None:
    sends_query(
        world,
        Post,
        "tags=a&tags=b",
        tags={Tag(slug="b"), Tag(slug="a")},
    )
    sends_query(
        world,
        Post,
        "readers=3&readers=7",
        readers=frozenset({User(id=7), User(id=3)}),
    )


@row(REL)
def any_set_is_sent_sorted(world: World) -> None:
    keys = {"b": 1, "a": 2}.keys()
    assert world.query(lambda: list(Post.filter(tags=keys))) == (
        "tags=a&tags=b"
    )
    assert world.query(lambda: list(Post.filter(tags__in=keys))) == (
        "tags__in=a,b"
    )
    assert world.query(lambda: list(Post.filter(x=keys))) == "x=a&x=b"


@row(REL)
def a_collection_relation_takes_a_key_or_an_object(world: World) -> None:
    sends_query_but_refuses_creation(
        world,
        Post,
        "tags=a",
        FieldConversionError,
        "Post.tags",
        tags=Tag(slug="a"),
    )
    sends_query_but_refuses_creation(
        world, Post, "tags=a", FieldConversionError, "Post.tags", tags="a"
    )


@row(REL)
def a_list_of_keys_repeats_the_parameter(world: World) -> None:
    sends_query(world, Post, "tags=a&tags=b", tags=["a", "b"])


@row(REL)
def a_list_of_keys_for_an_exact_lookup_is_refused(world: World) -> None:
    refuses_query(
        world, Post, ValueError, "tags__exact", tags__exact=["a", "b"]
    )


THROUGH = "queries-through-a-relation"


@row(THROUGH)
def a_path_through_a_relation_is_sent(world: World) -> None:
    sends_query(world, Post, "owner__id=7", owner__id=7)


@row(THROUGH)
def a_string_through_a_relation_is_sent_as_given(world: World) -> None:
    sends_query(world, Post, "owner__id=7", owner__id="7")


@row(THROUGH)
def a_bool_through_a_relation_is_sent_as_given(world: World) -> None:
    sends_query(world, Post, "owner__id=True", owner__id=True)


@row(THROUGH)
def several_values_through_a_relation_are_sent_as_given(
    world: World,
) -> None:
    sends_query(world, Post, "owner__id__in=7,8", owner__id__in=["7", 8])


@row(THROUGH)
def an_object_through_a_relation_is_sent_as_its_key(world: World) -> None:
    sends_query(world, Post, "owner__pk=8", owner__pk=User(id=8))


@row(THROUGH)
def an_object_of_another_model_through_a_relation_is_sent(
    world: World,
) -> None:
    sends_query(world, Post, "owner__id=8", owner__id=Other(id=8))


@row(THROUGH)
def another_field_through_a_relation_is_sent(world: World) -> None:
    sends_query(world, Post, "owner__name=x", owner__name="x")


@row(THROUGH)
def an_empty_or_null_collection_through_a_relation_is_refused(
    world: World,
) -> None:
    for value in ([], "7,8", [None]):
        refuses_query(
            world, Post, ValueError, "owner__id__in", owner__id__in=value
        )


@row(THROUGH)
def the_first_segment_of_a_path_is_sent_under_the_json_name(
    world: World,
) -> None:
    sends_query(world, Task, "userId__id=3", user__id=3)
    sends_query(world, Task, "userId__name=x", user__name="x")


@row(THROUGH)
def a_collection_for_a_lookup_of_one_value_through_a_relation_is_refused(
    world: World,
) -> None:
    for name in ("owner__id", "owner__id__gt", "owner__id__exact"):
        for value in ([7, 8], (7, 8), {7, 8}):
            refuses_query(world, Post, ValueError, name, **{name: value})


@row(THROUGH)
def a_collection_through_the_json_name_of_a_relation_is_refused(
    world: World,
) -> None:
    for name in ("userId__id", "userId__id__gt", "userId__pk"):
        for value in ([3, 4], (3, 4), {3, 4}):
            refuses_query(world, Task, ValueError, name, **{name: value})
    sends_query(world, Task, "userId__id__in=3,4", userId__id__in=[3, 4])


JSON_NAME = "queries-by-a-fields-json-name"


@row(JSON_NAME)
def a_collection_for_the_json_name_of_a_field_is_refused(
    world: World,
) -> None:
    for name in ("title", "Title", "Title__exact", "Title__gt"):
        for value in (["a", "b"], ("a", "b"), {"a", "b"}):
            refuses_query(world, Titled, ValueError, name, **{name: value})


@row(JSON_NAME)
def a_collection_for_the_json_name_of_a_relation_is_refused(
    world: World,
) -> None:
    for name in ("user", "userId", "userId__exact"):
        for value in ([3, 4], (3, 4), {3, 4}):
            refuses_query(world, Task, ValueError, name, **{name: value})


@row(JSON_NAME)
def a_lookup_of_several_values_or_one_value_by_json_name_is_sent(
    world: World,
) -> None:
    sends_query(world, Titled, "Title__in=a,b", Title__in=["a", "b"])
    sends_query(world, Task, "userId__in=3,4", userId__in=[3, 4])
    sends_query(world, Titled, "Title=a", Title="a")


@row(JSON_NAME)
def a_collection_for_a_json_name_with_two_underscores_is_refused(
    world: World,
) -> None:
    for name in ("the__title", "the__title__exact", "the__title__gt"):
        for value in (["a", "b"], ("a", "b"), {"a", "b"}):
            refuses_query(world, DT, ValueError, name, **{name: value})
    for call in queries(DT, the__title="a"):
        assert world.query(call) == "the__title=a"
    sends_query(world, DT, "the__title__in=a,b", the__title__in=["a", "b"])


@row(JSON_NAME)
def a_field_json_name_with_two_underscores_is_no_lookup_of_get_or_create(
    world: World,
) -> None:
    for call in creations(DT, the__title="a"):
        raises(world, ValueError, call, "the__title", "title")
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json={"id": 9})
    assert world.bodies(lambda: DT.get_or_create(title="a")) == [
        ("GET", None),
        ("POST", {"the__title": "a"}),
    ]
    lookup = world.requests[-2]
    assert unquote(urlsplit(lookup.url).query) == "the__title=a"


@row(JSON_NAME)
def a_value_that_a_conversion_takes_whole_is_refused_by_json_name(
    world: World,
) -> None:
    span = Span(1, 2)
    for name in ("Span__in", "Spans", "span__in"):
        refuses_query(world, Spanned, ValueError, name, **{name: span})
    assert world.query(lambda: list(Spanned.filter(spans=span))) == "Spans=1-2"


@row(JSON_NAME)
def a_value_that_is_no_json_scalar_is_refused_by_json_name(
    world: World,
) -> None:
    for name, advice, value in (
        ("Color", "color", Color.RED),
        ("Color__exact", "color__exact", Color.RED),
        ("Color__in", "color__in", [Color.RED]),
        ("When", "when", W),
        ("When_w__gt", "when__gt", W),
        ("When__range", "when__range", [W, W]),
        ("Color", "color", User(id=3)),
    ):
        refuses_query(
            world, Colored, ValueError, name, advice, **{name: value}
        )


@row(JSON_NAME)
def a_boolean_is_refused_by_json_name(world: World) -> None:
    for name, advice, value in (
        ("Flag", "flag", True),
        ("Flag__in", "flag__in", [True, False]),
        ("Color", "color", False),
    ):
        refuses_query(
            world, Colored, ValueError, name, advice, **{name: value}
        )


@row(JSON_NAME)
def a_json_scalar_is_sent_by_json_name_as_given(world: World) -> None:
    sends_query(world, Colored, "Color=red", Color="red")
    sends_query(
        world, Colored, "Color__in=red,blue", Color__in=["red", "blue"]
    )
    sends_query(
        world,
        Colored,
        "When=2024-01-02T03:04:05Z",
        When="2024-01-02T03:04:05Z",
    )
    for call in queries(Task, userId=User(id=3)):
        assert world.query(call) == "userId=3"


STRUCT = "value-suffix-lookups-on-a-structured-field"


@row(STRUCT)
def a_structured_value_is_refused_as_the_fields_own_parameter(
    world: World,
) -> None:
    refuses_query(world, Post, ValueError, "point", point=(1, 2))


@row(STRUCT)
def a_structured_value_is_refused_by_a_comparing_suffix(
    world: World,
) -> None:
    for name in ("point__exact", "point__gt"):
        refuses_query(world, Post, ValueError, name, **{name: (1, 2)})


@row(STRUCT)
def a_collection_of_structured_values_is_refused(world: World) -> None:
    refuses_query(
        world,
        Post,
        ValueError,
        "point__gt",
        point__gt=[(1, 2), (3, 4)],
    )


@row(STRUCT)
def a_string_operand_is_refused_as_the_field_encodes_it(
    world: World,
) -> None:
    refuses_query(
        world, Post, FieldConversionError, "Post.point", point__exact="1,2"
    )


@row(STRUCT)
def other_suffixes_are_sent_as_given(world: World) -> None:
    sends_query(world, Post, "point__iexact=1,2", point__iexact="1,2")


@row(STRUCT)
def a_name_that_names_no_field_is_sent_as_given(world: World) -> None:
    sends_query(world, Post, "title__iexact=x", title__iexact="x")
    sends_query_but_refuses_creation(
        world, Post, "x=8", TypeError, "User", x=User(id=8)
    )


@json_object
class Crowd(Base):
    """A model that can be iterated."""

    class_url = "/crowds/"
    id: int

    def __iter__(self) -> Iterator[int]:
        """Return members that are not the model's key."""
        return iter([1, 2, 3])


@row(STRUCT)
def a_model_that_can_be_iterated_is_sent_as_its_key_under_any_name(
    world: World,
) -> None:
    sends_query_but_refuses_creation(
        world, Post, "x=5", TypeError, "Crowd", x=Crowd(id=5)
    )
    sends_query_but_refuses_creation(
        world, Post, "x=5", TypeError, "Crowd", x=[Crowd(id=5)]
    )


CREATE = "creating"


def creating(model: type[JSONObject], **kw: Any) -> list[Callable[[], Any]]:
    """Return the calls that create an object: create and get_or_create."""
    return [
        lambda: model.create(**kw),
        lambda: model.get_or_create(**kw),
    ]


def posted(world: World, model: type[JSONObject], **kw: Any) -> list[Any]:
    """Return the body that each way of creating an object posts.

    ``get_or_create()`` finds nothing, so it creates the object, and its
    ``POST`` is the last request.
    """
    world.mocker.get(requests_mock.ANY, json=[])
    bodies = []
    for call in creating(model, **kw):
        sent = world.bodies(call)
        assert sent[-1][0] == "POST", sent
        bodies.append(sent[-1][1])
    return bodies


def refuses_creation(
    world: World,
    model: type[JSONObject],
    exc: type[Exception],
    *named: str,
    **kw: Any,
) -> None:
    """Assert that both ways of creating an object raise before a request."""
    for call in creating(model, **kw):
        raises(world, exc, call, *named)


@row(CREATE)
def a_key_and_a_field_are_posted(world: World) -> None:
    world.mocker.post(requests_mock.ANY, json={"id": 7, "name": "a"})
    for body in posted(world, User, id=7, name="a"):
        assert body == {"id": 7, "name": "a"}
    assert world.requests[-1].path == "/users/"


@row(CREATE)
def pk_is_posted_as_the_key(world: World) -> None:
    world.mocker.post(requests_mock.ANY, json={"id": 7})
    for body in posted(world, User, pk=7):
        assert body == {"id": 7}
    assert world.requests[-1].path == "/users/"


@row(CREATE)
def a_key_of_the_wrong_type_is_refused_on_create(world: World) -> None:
    refuses_creation(world, User, FieldConversionError, "User.id", id="7")


@row(CREATE)
def a_json_name_is_posted_as_given(world: World) -> None:
    world.mocker.post(requests_mock.ANY, json={"postId": 7})
    for body in posted(world, Post, postId=7):
        assert body == {"postId": 7}


@row(CREATE)
def a_json_name_of_the_wrong_type_is_refused_on_create(world: World) -> None:
    refuses_creation(world, Post, FieldConversionError, "Post.id", postId="7")


@row(CREATE)
def two_names_of_one_key_are_refused_on_create(world: World) -> None:
    # get_or_create() takes the two names as a lookup, which refuses them as a
    # ValueError, the class of the rows on a lookup.
    raises(
        world,
        TypeError,
        lambda: Post.create(id=7, postId=8),
        "id",
        "postId",
    )


@row(CREATE)
def a_uuid_key_is_posted_in_its_canonical_form(world: World) -> None:
    for body in posted(world, Doc, id=UP):
        assert body == {"id": LOWER}


@row(CREATE)
def a_related_object_is_posted_as_its_key(world: World) -> None:
    for body in posted(world, Post, owner=User(id=8)):
        assert body == {"owner": 8}


@row(CREATE)
def related_objects_are_posted_as_keys(world: World) -> None:
    for body in posted(world, Post, tags=[Tag(slug="a")]):
        assert body == {"tags": ["a"]}


@row(CREATE)
def get_or_create_posts_the_key_when_nothing_is_found(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json={"id": 7})
    sent = world.bodies(lambda: User.get_or_create(pk=7))
    assert sent == [("GET", None), ("POST", {"id": 7})]
    assert [r.path for r in world.requests] == ["/users/", "/users/"]
    assert unquote(urlsplit(world.requests[-2].url).query) == "pk=7"


@row(CREATE)
def get_or_create_by_id_posts_the_key(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json={"id": 7})
    sent = world.bodies(lambda: User.get_or_create(id=7))
    assert sent == [("GET", None), ("POST", {"id": 7})]
    assert [r.path for r in world.requests] == ["/users/", "/users/"]
    assert unquote(urlsplit(world.requests[-2].url).query) == "id=7"


@row(CREATE)
def get_or_create_looks_up_a_uuid_key_under_its_json_name_as_built(
    world: World,
) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json={"docId": LOWER})
    sent = world.bodies(lambda: Note.get_or_create(docId=UP))
    assert sent == [("GET", None), ("POST", {"docId": LOWER})]
    assert unquote(urlsplit(world.requests[-2].url).query) == f"docId={LOWER}"
    assert world.query(lambda: list(Note.filter(docId=UP))) == f"docId={UP}"


@row(CREATE)
def a_response_that_names_another_key_is_refused(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json={"id": 8, "name": "a"})
    for call, requests in (
        (lambda: User.create(id=7, name="a"), 1),
        (lambda: User.get_or_create(id=7, name="a"), 2),
    ):
        error = raises(
            world,
            ResponseShapeError,
            call,
            "another",
            "key",
            requests=requests,
        )
        assert isinstance(error, ResponseShapeError)
        # The keys can be secrets, so the message quotes neither.
        assert not re.search(r"[78]", str(error))
        assert error.response is not None
        assert error.response.json() == {"id": 8, "name": "a"}


RESPONSES_WITHOUT_THE_KEY: list[dict[str, Any]] = [
    {},
    {"id": None, "name": "a"},
]


@row(CREATE)
def a_response_without_the_key_is_refused_after_create(
    world: World,
) -> None:
    for body in RESPONSES_WITHOUT_THE_KEY:
        world.mocker.post(requests_mock.ANY, json=body)
        error = raises(
            world,
            ResponseShapeError,
            lambda: User.create(name="a"),
            "created",
            requests=1,
        )
        names(str(error), "User.id")
        assert isinstance(error, ResponseShapeError)
        assert error.response is not None
        assert error.response.json() == body


@row(CREATE)
def a_response_without_the_key_is_refused_after_get_or_create(
    world: World,
) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    for body in RESPONSES_WITHOUT_THE_KEY:
        world.mocker.post(requests_mock.ANY, json=body)
        error = raises(
            world,
            ResponseShapeError,
            lambda: User.get_or_create(name="a"),
            "created",
            "User.id",
            requests=2,
        )
        assert isinstance(error, ResponseShapeError)
        assert error.response is not None
        assert error.response.json() == body


@row(CREATE)
def a_none_key_is_refused_on_create(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    for call, name in (
        (lambda: User.create(id=None, name="a"), "User.id"),
        (lambda: User.create(pk=None), "User.id"),
        (lambda: User.create(fields={"id": None}), "User.id"),
        (lambda: Post.create(postId=None), "Post.id"),
        (lambda: Post.create(id=None), "Post.id"),
        (
            lambda: User.get_or_create(name="a", defaults={"id": None}),
            "User.id",
        ),
    ):
        error = raises(world, FieldConversionError, call, name, "out")
        assert "leave the key out" in str(error).lower()


@row(CREATE)
def a_json_name_with_two_underscores_is_no_lookup_of_get_or_create(
    world: World,
) -> None:
    world.mocker.get(requests_mock.ANY, json=[{"doc__id": 7}])
    for value in (7, "7"):
        for call in creations(DK, doc__id=value):
            raises(world, ValueError, call, "doc__id")
    assert world.query(lambda: list(DK.filter(doc__id=7))) == "doc__id=7"
    assert world.query(lambda: DK.get_or_create(id=7)) == "doc__id=7"


@row(CREATE)
def a_key_field_name_with_two_underscores_is_looked_up_as_the_field(
    world: World,
) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json={"doc__id": 7})
    assert world.bodies(lambda: DU.get_or_create(doc__id=7)) == [
        ("GET", None),
        ("POST", {"doc__id": 7}),
    ]
    lookup = world.requests[-2]
    assert unquote(urlsplit(lookup.url).query) == "doc__id=7"
    assert world.query(lambda: list(DU.filter(doc__id=7))) == "doc__id=7"


@row(CREATE)
def an_extra_that_json_would_change_is_refused_by_create(
    world: World,
) -> None:
    for value in ((1, 2), {1: "a"}):
        for call in (
            lambda: User.create(id=7, x=value),
            lambda: User.create(fields={"id": 7, "x": value}),
            lambda: User.get_or_create(id=7, defaults={"x": value}),
        ):
            raises(world, TypeError, call)


@row(CREATE)
def an_extra_given_as_a_lookup_is_refused_as_a_query_value(
    world: World,
) -> None:
    raises(world, TypeError, lambda: User.get_or_create(id=7, x=(1, 2)))
    raises(
        world, ValueError, lambda: User.get_or_create(id=7, x={1: "a"}), "x"
    )


@row(CREATE)
def a_keyed_model_without_class_url_is_a_usage_error(world: World) -> None:
    for call in (
        lambda: Bare(id=1).url,
        lambda: Bare(id=1).name,
        lambda: Bare(id=1).refresh(),
        lambda: Bare(id=1).delete(),
        lambda: Bare.create(name="x"),
        lambda: Bare.filter(),
        lambda: Bare.all(),
        lambda: Bare.get(id=1),
        lambda: Bare.get_page(),
        lambda: Bare.get_or_create(id=1),
    ):
        raises(world, UsageError, call, "Bare", "class_url")
    # hasattr() swallows an AttributeError only, so it raises too.
    with pytest.raises(UsageError):
        hasattr(Bare(id=1), "name")


@row(CREATE)
def a_model_without_a_key_cannot_be_created(world: World) -> None:
    raises(world, UsageError, lambda: Entry.create(name="x"))


FULL = "full-saves-and-server-state"


@row(FULL)
def a_full_save_writes_the_canonical_key(world: World) -> None:
    doc = Doc(json={"id": UP})
    assert world.body(lambda: doc.save(full=True), "PUT") == {"id": LOWER}
    assert world.requests[-1].path == f"/docs/{LOWER}/"


@row(FULL)
def a_matching_key_merges(world: World) -> None:
    user = User(json={"id": 7})
    user.update_from_json({"id": 7, "name": "a"})
    assert user.name == "a"
    assert user.pk == 7
    user.update_from_json({"name": "b"})
    assert user.name == "b"


@row(FULL)
def another_key_is_refused_when_merging(world: World) -> None:
    for key in (8, None, "7"):
        for body in ({"id": key}, {"id": key, "name": "b"}):
            user = User(json={"id": 7, "name": "a"})
            raises(
                world,
                ResponseShapeError,
                lambda: user.update_from_json(body),
            )
            assert user.pk == 7
            assert user.name == "a"


@row(FULL)
def a_query_item_without_the_key_is_refused(world: World) -> None:
    items = [{"id": 1, "name": "a"}, {"name": "a"}]
    world.mocker.get(requests_mock.ANY, json=items)
    for call in (
        lambda: list(User.filter()),
        lambda: list(User.all()),
        lambda: User.get_page(),
    ):
        raises(
            world,
            ResponseShapeError,
            call,
            "User.id",
            "[1]",
            requests=1,
        )
    world.mocker.get(requests_mock.ANY, json=items[1:])
    raises(
        world,
        ResponseShapeError,
        lambda: User.get(),
        "User.id",
        "[0]",
        requests=1,
    )


@row(FULL)
def a_query_item_that_is_no_object_is_refused(world: World) -> None:
    bad: tuple[Any, ...] = (None, 7, [])
    for item in bad:
        world.mocker.get(
            requests_mock.ANY, json=[{"id": 1, "name": "a"}, item]
        )
        for call in (
            lambda: list(User.filter()),
            lambda: list(User.all()),
            lambda: User.get_page(),
        ):
            raises(world, ResponseShapeError, call, "[1]", requests=1)
        world.mocker.get(requests_mock.ANY, json=[item])
        raises(world, ResponseShapeError, User.get, "[0]", requests=1)


@row(FULL)
def a_response_key_of_the_wrong_type_is_refused(world: World) -> None:
    body = {"id": "7", "name": "a"}
    world.mocker.post(requests_mock.ANY, json=body)
    raises(
        world,
        ResponseShapeError,
        lambda: User.create(name="a"),
        "User.id",
        requests=1,
    )
    world.mocker.get(requests_mock.ANY, json=[])
    raises(
        world,
        ResponseShapeError,
        lambda: User.get_or_create(name="a"),
        "User.id",
        requests=2,
    )
    world.mocker.get(requests_mock.ANY, json=[body])
    raises(
        world,
        ResponseShapeError,
        lambda: list(User.filter()),
        "User.id",
        "[0]",
        requests=1,
    )
    # A creation that sent a key is answered with one that cannot be read.
    world.mocker.get(requests_mock.ANY, json=[])
    world.mocker.post(requests_mock.ANY, json=body)
    for call, requests in (
        (lambda: User.create(id=7, name="a"), 1),
        (lambda: User.get_or_create(id=7, name="a"), 2),
    ):
        error = raises(
            world,
            ResponseShapeError,
            call,
            "User.id",
            requests=requests,
        )
        assert isinstance(error, ResponseShapeError)
        assert error.response is not None
        assert error.response.json() == body


@row(FULL)
def a_loaded_response_without_the_key_keeps_the_key(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json={"name": "a"})
    user = User(id=7)
    assert user.name == "a"
    assert user.pk == 7
    assert world.requests[-1].path == "/users/7/"


@row(FULL)
def a_loaded_response_with_a_null_key_is_refused(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json={"id": None, "name": "a"})
    user = User(id=7)
    raises(world, ResponseShapeError, lambda: user.name, requests=1)


@row(FULL)
def a_merge_keeps_a_related_object_whose_key_is_unchanged(
    world: World,
) -> None:
    embedded = {"id": 7, "name": "a"}
    obj = post(owner=embedded)
    owner = obj.owner
    assert owner is not None
    owner.name = "b"
    obj.update_from_json({"owner": {"id": 7, "name": "a"}})
    assert obj.owner is owner
    assert world.body(owner.save, "PATCH") == {"name": "b"}
    obj.update_from_json({"owner": 8})
    assert obj.owner is not owner
    assert obj.owner is not None
    assert obj.owner.pk == 8


@row(FULL)
def a_response_with_a_bare_key_replaces_an_embedded_owner(
    world: World,
) -> None:
    obj = post(owner={"id": 7, "name": "a"})
    held = obj.owner
    assert held is not None
    held.name = "b"
    obj.title = "x"
    world.mocker.patch(requests_mock.ANY, json={"owner": 7})
    obj.save()
    # The field's JSON changed from an embedded object to a bare key, so the
    # field reads a new object, without the unsaved change.
    assert obj.owner is not held
    assert obj.owner is not None
    assert obj.owner.pk == 7
    sent = world.count
    obj.owner.save()
    assert world.count == sent


LISTS: list[list[dict[str, Any]]] = [[], [{"id": 7}, {"id": 8}]]


@row(FULL)
def a_write_answered_with_a_list_is_a_shape_error(world: World) -> None:
    for body in LISTS:
        for method, mock in (("PATCH", world.mocker.patch), ("PUT", None)):
            if mock is None:
                mock = world.mocker.put
            mock(requests_mock.ANY, json=body)
            user = User(json={"id": 7, "name": "a"})
            user.name = "b"
            call = (
                user.save
                if method == "PATCH"
                else lambda: user.save(full=True)
            )
            error = raises(world, ResponseShapeError, call, requests=1)
            assert isinstance(error, ResponseShapeError)
            assert error.response is not None
            assert error.response.json() == body
            # The change is still pending, so a save that works sends it.
            world.mocker.patch(requests_mock.ANY, json={})
            assert world.body(user.save, "PATCH") == {"name": "b"}
        eager = User(json={"id": 7, "name": "a"}, write_mode="eager")
        world.mocker.patch(requests_mock.ANY, json=body)
        error = raises(
            world,
            ResponseShapeError,
            lambda: setattr(eager, "name", "b"),
            requests=1,
        )
        assert isinstance(error, ResponseShapeError)
        assert error.response is not None
        assert error.response.json() == body


@row(FULL)
def a_response_that_is_a_list_of_one_object_is_read_as_that_object(
    world: World,
) -> None:
    body = [{"id": 7, "name": "b"}]
    world.mocker.patch(requests_mock.ANY, json=body)
    world.mocker.put(requests_mock.ANY, json=body)
    world.mocker.get(requests_mock.ANY, json=body)
    world.mocker.post(requests_mock.ANY, json=body)
    saved = User(json={"id": 7, "name": "a"})
    saved.name = "x"
    saved.save()
    assert saved.json == {"id": 7, "name": "b"}
    replaced = User(json={"id": 7, "name": "a"})
    replaced.save(full=True)
    assert replaced.json == {"id": 7, "name": "b"}
    assert User(id=7).name == "b"
    assert User.create(name="a").json == {"id": 7, "name": "b"}


@row(FULL)
def a_load_answered_with_a_list_is_a_shape_error(world: World) -> None:
    def refreshed() -> User:
        obj = User(json={"id": 7, "name": "a"})
        obj.refresh()
        return obj

    for body in LISTS:
        world.mocker.get(requests_mock.ANY, json=body)
        for read in (
            lambda: User(id=7).name,
            lambda: User(id=7).json,
            lambda: refreshed().name,
        ):
            error = raises(world, ResponseShapeError, read, requests=1)
            assert isinstance(error, ResponseShapeError)
            assert error.response is not None
            assert error.response.json() == body


PREFETCH = "prefetch"

hook_calls: list[Any] = []
hook_results: dict[str, Any] = {}


@json_object
class PUser(Base):
    class_url = "/pusers/"
    id: int

    @classmethod
    def bulk_get_by_pks(cls, pks: set[Any], **kwargs: Any) -> dict[Any, Any]:
        hook_calls.append(set(pks))
        make = hook_results.get("make", lambda pk: cls(json={"id": pk}))
        return {pk: make(pk) for pk in pks}

    @classmethod
    def bulk_get_by_urls(cls, urls: set[Any], **kwargs: Any) -> dict[Any, Any]:
        hook_calls.append(("urls", set(urls)))
        return {}


@json_object
class PAdmin(PUser):
    pass


@json_object
class POther(Base):
    class_url = "/pothers/"
    id: int


@json_object
class PPost(Base):
    class_url = "/pposts/"
    id: int
    owner: PUser


def prefetched(world: World, *owners: Any) -> list[PPost]:
    hook_calls.clear()
    hook_results.clear()
    world.mocker.get(
        requests_mock.ANY,
        json=[{"id": i, "owner": owner} for i, owner in enumerate(owners, 1)],
    )
    return list(PPost.filter(prefetch=["owner"]))


@row(PREFETCH)
def the_hook_receives_the_keys(world: World) -> None:
    prefetched(world, 7)
    assert hook_calls == [{7}]


@row(PREFETCH)
def a_key_of_the_wrong_type_is_refused_before_the_hook(world: World) -> None:
    hook_calls.clear()
    world.mocker.get(requests_mock.ANY, json=[{"id": 1, "owner": "7"}])
    raises(
        world,
        FieldConversionError,
        lambda: list(PPost.filter(prefetch=["owner"])),
        "PPost.owner",
        requests=1,
    )
    assert hook_calls == []


@row(PREFETCH)
def parents_share_the_object_of_a_subclass(world: World) -> None:
    hook_calls.clear()
    hook_results.clear()
    hook_results["make"] = lambda pk: PAdmin(json={"id": pk})
    world.mocker.get(
        requests_mock.ANY,
        json=[{"id": 1, "owner": 7}, {"id": 2, "owner": 7}],
    )
    first, second = list(PPost.filter(prefetch=["owner"]))
    assert first.owner is second.owner


@row(PREFETCH)
def an_object_of_another_model_from_the_hook_is_refused(
    world: World,
) -> None:
    hook_calls.clear()
    hook_results.clear()
    hook_results["make"] = lambda pk: POther(json={"id": pk})
    world.mocker.get(requests_mock.ANY, json=[{"id": 1, "owner": 7}])
    raises(
        world,
        TypeError,
        lambda: list(PPost.filter(prefetch=["owner"])),
        "PUser.bulk_get_by_pks",
        "PUser",
        "PPost.owner",
        "POther",
        requests=1,
    )


@row(PREFETCH)
def a_field_named_twice_is_refused(world: World) -> None:
    hook_calls.clear()
    raises(
        world,
        ValueError,
        lambda: list(PPost.filter(prefetch=["owner", "owner"])),
        "owner",
        "twice",
    )
    assert hook_calls == []


@row(PREFETCH)
def bulk_get_by_urls_is_not_a_hook(world: World) -> None:
    prefetched(world, 7)
    assert hook_calls == [{7}]
    assert not any(isinstance(call, tuple) for call in hook_calls)
    assert not hasattr(JSONObject, "bulk_get_by_urls")


@row(PREFETCH)
def a_batch_size_that_no_batch_can_have_is_refused(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[{"id": 1, "owner": 7}])
    calls: list[Callable[[], Any]] = [
        lambda: list(PPost.filter(prefetch="owner")),
        lambda: list(PPost.all(prefetch="owner")),
        lambda: PPost.get_page(prefetch="owner"),
        lambda: PPost.get(prefetch="owner"),
    ]
    saved = PPost.prefetch_batch_size
    try:
        for size, exc in (
            (0, ValueError),
            (-1, ValueError),
            (1.5, TypeError),
            (True, TypeError),
            ("2", TypeError),
        ):
            PPost.prefetch_batch_size = size  # type: ignore[assignment]
            for call in calls:
                raises(world, exc, call, "prefetch_batch_size", "PPost")
        PPost.prefetch_batch_size = None
        hook_calls.clear()
        assert [p.pk for p in PPost.filter(prefetch="owner")] == [1]
        assert hook_calls == [{7}]
    finally:
        PPost.prefetch_batch_size = saved


OVERRIDE = "a-class-that-overrides-url"


@json_object
class Slot(Base):
    class_url = "/slots/"
    id: int
    name: str

    @property
    def url(self) -> str:
        return f"/slots/{self.pk}/x/"


@row(OVERRIDE)
def a_url_may_use_the_key(world: World) -> None:
    assert Slot(id=3).url == "/slots/3/x/"


@row(OVERRIDE)
def a_body_with_another_key_is_refused(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json={"id": 4, "name": "a"})
    slot = Slot(id=3)
    raises(world, ResponseShapeError, lambda: slot.name, requests=1)


SCHEMA = "keys-in-the-json-schema"


def schema_property(model: type[Any], mode: str, name: str) -> Any:
    return model.json_schema(mode)["properties"][name]


@row(SCHEMA)
def key_types_have_their_schema(world: World) -> None:
    for mode in ("read", "write"):
        assert schema_property(User, mode, "id") == {"type": "integer"}
        assert schema_property(Post, mode, "postId") == {"type": "integer"}
        assert schema_property(Tag, mode, "slug") == {"type": "string"}
        assert schema_property(Doc, mode, "id") == {
            "type": "string",
            "format": "uuid",
        }


def alternatives(schema: Any) -> list[Any]:
    """Return the schemas that a schema allows, through nested ``anyOf``."""
    if not isinstance(schema, dict) or "anyOf" not in schema:
        return [schema]
    return [alt for item in schema["anyOf"] for alt in alternatives(item)]


@row(SCHEMA)
def a_relation_is_read_as_a_key_or_an_object_with_its_key(
    world: World,
) -> None:
    keyed_object = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "integer"}},
    }
    found = alternatives(schema_property(Post, "read", "owner"))
    expected = [{"type": "integer"}, keyed_object, {"type": "null"}]
    assert sorted(found, key=json.dumps) == sorted(expected, key=json.dumps)


@row(SCHEMA)
def a_relation_is_written_as_a_key(world: World) -> None:
    assert schema_property(Post, "write", "owner") == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }


@row(SCHEMA)
def a_collection_item_read_has_a_key(world: World) -> None:
    assert schema_property(Post, "read", "tags") == {
        "type": "array",
        "items": {
            "anyOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "required": ["slug"],
                    "properties": {"slug": {"type": "string"}},
                },
            ]
        },
    }


@row(SCHEMA)
def a_collection_is_written_as_keys(world: World) -> None:
    assert schema_property(Post, "write", "tags") == {
        "type": "array",
        "items": {"type": "string"},
    }


@row(SCHEMA)
def the_read_schema_requires_the_key(world: World) -> None:
    for model, required in (
        (User, ["id"]),
        (Tag, ["slug"]),
        (Doc, ["id"]),
        (Post, ["postId"]),
    ):
        assert model.json_schema("read")["required"] == required


@row(SCHEMA)
def the_write_schema_requires_nothing(world: World) -> None:
    for model in (User, Tag, Doc, Post):
        assert "required" not in model.json_schema("write")


@row(PREFETCH)
def a_parent_without_its_key_is_refused_before_the_hook(
    world: World,
) -> None:
    hook_calls.clear()
    world.mocker.get(
        requests_mock.ANY, json=[{"id": 1, "owner": 7}, {"owner": 7}]
    )
    raises(
        world,
        ResponseShapeError,
        lambda: list(PPost.filter(prefetch=["owner"])),
        "[1]",
        "PPost.id",
        requests=1,
    )
    assert hook_calls == []
    world.mocker.get(requests_mock.ANY, json=[{"id": 1, "owner": 7}, None])
    raises(
        world,
        ResponseShapeError,
        lambda: list(PPost.filter(prefetch=["owner"])),
        "[1]",
        requests=1,
    )
    assert hook_calls == []
    # An item of a later batch is named by its place in the whole collection.
    world.mocker.get(
        requests_mock.ANY,
        json=[{"id": 1, "owner": 7}, {"id": 2, "owner": 8}, {"owner": 9}],
    )
    saved = PPost.prefetch_batch_size
    PPost.prefetch_batch_size = 2
    try:
        raises(
            world,
            ResponseShapeError,
            lambda: list(PPost.filter(prefetch=["owner"])),
            "[2]",
            "PPost.id",
            requests=1,
        )
    finally:
        PPost.prefetch_batch_size = saved
    assert hook_calls == [{7, 8}]
