"""The 1.0 contract for keys, queries, and relations, one row per cell.

Each row is one input at one entry point and the outcome that it must have: the
exact query string or body sent, the value read, or the exception raised, by
class and by the field or parameter that it names. ``test_rows.py`` runs every
row as one assertion.

A row that raises means that it raises before any request, with nothing changed
on the object. A row that names an exception class asserts that class by
``isinstance``, so a ``FieldConversionError`` meets a row that asks for a
``ValueError``.
"""

from __future__ import annotations

import collections
import copy
import datetime
import enum
import json
import pickle
import uuid
from collections import deque
from collections.abc import (
    Callable,
    Collection,
    Iterable,
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
    Literal,
    NewType,
    Optional,
    Union,
)
from urllib.parse import unquote, urlsplit

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

U = uuid.UUID("12345678-1234-5678-1234-567812345678")
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
class Doc(Base):
    class_url = "/docs/"
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
class Entry(Base):
    """A model with no key."""

    name: str


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


def raises(
    world: World,
    exc: type[BaseException],
    call: Callable[[], Any],
    *named: str,
    requests: int = 0,
) -> BaseException:
    """Assert that a call raises ``exc``, naming each given text.

    The error is raised before any request, unless ``requests`` says how many
    have been sent by then.
    """
    before = world.count
    caught: BaseException | None = None
    try:
        call()
    except exc as error:
        caught = error
    if caught is None:
        raise AssertionError(f"{exc.__name__} was not raised")
    for text in named:
        assert text in str(caught), (text, str(caught))
    assert world.count - before == requests
    return caught


def lookups(model: type[JSONObject], **kw: Any) -> list[Callable[[], Any]]:
    """Return the calls that take one lookup: filter, get, and the others."""
    return [
        lambda: list(model.filter(**kw)),
        lambda: model.get(**kw),
        lambda: model.get_or_create(**kw),
        lambda: list(model.filter(params=kw)),
    ]


def sends_query(
    world: World, model: type[JSONObject], expected: str, **kw: Any
) -> None:
    """Assert that every form of a lookup sends exactly one query string."""
    for call in lookups(model, **kw):
        assert world.query(call) == expected


def refuses_query(
    world: World,
    model: type[JSONObject],
    exc: type[BaseException],
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


@row(KEY, pending="a NewType that a conversion converts is admitted as a key")
def converted_newtype_refused(world: World) -> None:
    conversions = {
        Wrapped: Conversion(
            decode=lambda value, _: Wrapped(value),
            encode=lambda value, _: int(value),
        )
    }
    error = raises(
        world,
        TypeError,
        lambda: declare_key(Wrapped, conversions=conversions),
        "id",
    )
    assert "UUID" in str(error)


@row(KEY, pending="a Literal is admitted as a key")
def literal_key_refused(world: World) -> None:
    for annotation in (Literal["a", "b"], Literal[1, 2]):
        error = raises(world, TypeError, lambda: declare_key(annotation), "id")
        assert "UUID" in str(error)


@row(KEY, pending="an enum, Decimal, or temporal type is admitted as a key")
def enum_decimal_temporal_key_refused(world: World) -> None:
    for annotation in (
        Colour,
        Decimal,
        datetime.datetime,
        datetime.date,
        datetime.time,
        datetime.timedelta,
    ):
        error = raises(world, TypeError, lambda: declare_key(annotation), "id")
        assert "UUID" in str(error)


@row(KEY)
def other_key_types_refused(world: World) -> None:
    class Slug(str):
        pass

    for annotation in (
        bool,
        float,
        Slug,
        list[int],
        dict[str, int],
        Union[int, str],
    ):
        raises(world, TypeError, lambda: declare_key(annotation), "id")


@row(KEY, pending="a relation is admitted as the key")
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

    error = raises(world, TypeError, build, "user")
    assert "key" in str(error)


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


def refused_form(annotation: Any, *named: str) -> None:
    error = None
    try:
        declare_field(annotation)
    except TypeError as caught:
        error = caught
    assert error is not None, "the declaration was admitted"
    for text in named:
        assert text in str(error), (text, str(error))
    assert "items" in str(error)


@row(DECL)
def collection_refused(world: World) -> None:
    refused_form(Collection[Tag], "sequence", "set", "list[Tag]", "set[Tag]")


@row(DECL)
def iterable_refused(world: World) -> None:
    refused_form(Iterable[Tag], "Sequence[Tag]", "set[Tag]")


@row(DECL)
def wrapped_refused_forms_refused(world: World) -> None:
    refused_form(Optional[Collection[Tag]], "sequence", "set")
    refused_form(Optional[Iterable[Tag]], "Sequence[Tag]")
    refused_form(NewType("Tags", Collection[Tag]), "sequence", "set")
    refused_form(NewType("TagIter", Iterable[Tag]), "Sequence[Tag]")


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
        raises(world, TypeError, lambda: declare_field(annotation), "items")


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
    for value in (User(id=7), Admin(id=7), Other(id=7), User()):
        raises(world, FieldConversionError, lambda: User(id=value), "User.id")


@row(BUILD)
def a_missing_key_gives_a_keyless_object(world: World) -> None:
    for user in (User(), User(id=None)):
        assert user.pk is None
        for operation in (
            lambda: user.url,
            lambda: user.save(full=True),
            lambda: user.refresh(),
        ):
            raises(world, ValueError, operation)
        raises(world, TypeError, lambda: hash(user))
        same = user
        assert user == same
        assert user != User()


@row(BUILD)
def key_read_from_json(world: World) -> None:
    assert User(json={"id": 7}).pk == 7


@row(
    BUILD,
    pending="a key of the wrong JSON type is built without an error",
)
def a_wrong_key_in_json_is_refused_when_read(world: World) -> None:
    user = User(json={"id": "7"})
    error = raises(world, FieldConversionError, lambda: user.pk, "User.id")
    assert "json" in str(error)
    raises(world, FieldConversionError, lambda: user.id, "User.id")
    same = user
    assert user == same
    assert user != User(json={"id": "7"})
    raises(world, TypeError, lambda: hash(user))


@row(BUILD)
def a_missing_or_null_key_in_json_is_keyless(world: World) -> None:
    for body in ({}, {"id": None}):
        assert User(json=body).pk is None


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
def keyless_objects_are_unequal_and_unhashable(world: World) -> None:
    assert User() != User()
    raises(world, TypeError, lambda: hash(User()))


@row(EQ)
def a_model_with_no_key_is_equal_only_to_itself(world: World) -> None:
    entry = Entry(json={"name": "x"})
    same = entry
    assert entry == same
    assert entry != Entry(json={"name": "x"})
    assert hash(entry) == hash(entry)


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
def a_pickle_round_trip_is_a_deep_copy(world: World) -> None:
    original = post()
    assert original.owner is not None
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        duplicate = pickle.loads(pickle.dumps(original, protocol=protocol))
        assert_copy_as_deepcopy(original, duplicate)


ASSIGN = "assigning-the-key"


@row(ASSIGN)
def a_key_assigned_to_a_keyless_object_is_kept(world: World) -> None:
    for attribute in ("id", "pk"):
        user = User()
        setattr(user, attribute, 7)
        assert user.pk == 7
    assert world.count == 0


@row(ASSIGN)
def a_key_of_the_wrong_type_is_refused(world: World) -> None:
    raises(
        world,
        FieldConversionError,
        lambda: setattr(User(), "id", "7"),
        "User.id",
    )


@row(ASSIGN)
def another_key_is_refused_for_a_keyed_object(world: World) -> None:
    for attribute in ("id", "pk"):
        user = User(id=7)
        raises(
            world,
            AttributeError,
            lambda: setattr(user, attribute, 8),
        )
        assert user.pk == 7


@row(ASSIGN)
def the_same_key_does_nothing(world: World) -> None:
    user = User(id=7)
    user.id = 7
    assert user.pk == 7
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
        raises(
            world,
            FieldConversionError,
            lambda: setattr(obj, "owner", value),
            "Post.owner",
        )
        raises(
            world,
            FieldConversionError,
            lambda: Post.create(owner=value),
            "Post.owner",
        )


@row(OWNER)
def an_object_of_another_model_is_refused(world: World) -> None:
    obj = post()
    for call in (
        lambda: setattr(obj, "owner", Other(id=8)),
        lambda: Post.create(owner=Other(id=8)),
    ):
        raises(
            world, FieldConversionError, call, "Post.owner", "User", "Other"
        )


@row(OWNER)
def an_object_without_a_key_is_refused(world: World) -> None:
    obj = post()
    for call in (
        lambda: setattr(obj, "owner", User()),
        lambda: Post.create(owner=User()),
    ):
        raises(world, FieldConversionError, call, "Post.owner", "None")


@row(OWNER)
def a_mapping_is_refused_for_a_relation(world: World) -> None:
    obj = post()
    for call in (
        lambda: setattr(obj, "owner", {"id": 8}),
        lambda: Post.create(owner={"id": 8}),
    ):
        raises(world, FieldConversionError, call, "Post.owner")


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
def an_embedded_object_without_a_key_has_no_key(world: World) -> None:
    obj = post(owner={"name": "a"})
    assert obj.owner is not None
    assert obj.owner.pk is None


@row(OWNER)
def a_full_save_sends_an_embedded_owner_as_its_key(world: World) -> None:
    obj = post(owner={"id": 7})
    body = world.body(lambda: obj.save(full=True), "PUT")
    assert body["owner"] == 7


COLL = "a-collection-of-relations"


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


@row(COLL)
def a_list_is_read_as_a_list_and_a_tuple_as_a_tuple(world: World) -> None:
    obj = post(tags=["a", "b"], tagt=["a", "b"])
    assert obj.tags == [Tag(slug="a"), Tag(slug="b")]
    assert type(obj.tags) is list
    assert obj.tagt == (Tag(slug="a"), Tag(slug="b"))
    assert type(obj.tagt) is tuple


@row(COLL)
def an_embedded_item_is_read_by_its_key(world: World) -> None:
    obj = post(tags=[{"slug": "a"}])
    assert pks(obj.tags) == ["a"]


@row(COLL)
def an_item_without_a_key_is_refused_when_read(world: World) -> None:
    obj = post(tags=["a", {"x": 1}])
    error = raises(
        world,
        FieldConversionError,
        lambda: obj.tags,
        "Post.tags",
        "[1]",
        "TypedDict",
        "dataclass",
    )
    assert error is not None


@row(COLL)
def an_assigned_collection_is_sent_as_keys(world: World) -> None:
    for value in ([Tag(slug="b")], (Tag(slug="b"),)):
        obj = post()
        assert patch_after(
            world,
            obj,
            lambda p: setattr(p, "tags", value),
        ) == {"tags": ["b"]}
        assert type(obj.tags) is list


@row(COLL)
def an_assigned_collection_reads_as_the_declared_type(world: World) -> None:
    obj = post()
    obj.tagt = [Tag(slug="b")]  # type: ignore[assignment]
    assert type(obj.tagt) is tuple


@row(COLL)
def an_assigned_item_without_a_key_is_refused(world: World) -> None:
    obj = post()
    raises(
        world,
        FieldConversionError,
        lambda: setattr(obj, "tags", [Tag()]),
        "Post.tags",
        "[0]",
    )


@row(COLL)
def an_assigned_item_of_another_model_is_refused(world: World) -> None:
    obj = post()
    raises(
        world,
        FieldConversionError,
        lambda: setattr(obj, "tags", [Other(id=1)]),
        "Post.tags",
    )


@row(COLL)
def an_assigned_object_that_is_no_collection_is_refused(
    world: World,
) -> None:
    obj = post()
    raises(
        world,
        FieldConversionError,
        lambda: setattr(obj, "tags", Tag(slug="b")),
        "Post.tags",
        "collection",
    )


@row(COLL)
def an_appended_item_is_sent(world: World) -> None:
    obj = post()
    assert patch_after(world, obj, lambda p: p.tags.append(Tag(slug="b"))) == {
        "tags": ["a", "b"]
    }


@row(COLL)
def a_replaced_item_is_sent(world: World) -> None:
    obj = post()

    def change(p: Post) -> None:
        p.tags[0] = Tag(slug="b")

    assert patch_after(world, obj, change) == {"tags": ["b"]}


@row(COLL)
def a_reordered_list_is_sent(world: World) -> None:
    obj = post(tags=["a", "b"])
    assert patch_after(world, obj, lambda p: p.tags.reverse()) == {
        "tags": ["b", "a"]
    }


@row(COLL)
def an_item_added_to_the_left_of_a_deque_is_sent(world: World) -> None:
    obj = post(tagq=["a"])
    assert patch_after(
        world, obj, lambda p: p.tagq.appendleft(Tag(slug="b"))
    ) == {"tagq": ["b", "a"]}


@row(COLL)
def a_new_object_of_the_same_key_sends_nothing(world: World) -> None:
    obj = post()

    def change(p: Post) -> None:
        p.tags[0] = Tag(slug="a")

    assert patch_after(world, obj, change) is None


@row(COLL)
def a_subclass_object_of_the_same_key_sends_nothing(world: World) -> None:
    obj = post()

    def change(p: Post) -> None:
        p.readers[0] = Admin(id=7)

    assert patch_after(world, obj, change) is None


@row(COLL)
def an_appended_item_without_a_key_is_refused_at_save(world: World) -> None:
    obj = post()
    obj.tags.append(Tag())
    raises(world, FieldConversionError, obj.save, "Post.tags", "[1]")


@row(COLL)
def a_saved_change_is_not_sent_again(world: World) -> None:
    obj = post()
    obj.tags.append(Tag(slug="b"))
    obj.save()
    before = world.count
    obj.save()
    assert world.count == before


@row(COLL)
def an_assigned_collection_is_copied(world: World) -> None:
    first, second = post(), post(postId=2)
    second.tags = first.tags
    first.tags.append(Tag(slug="c"))
    assert patch_after(world, first, lambda p: None) == {"tags": ["a", "c"]}
    assert patch_after(world, second, lambda p: None) is None


@row(COLL)
def an_unread_collection_without_keys_is_not_sent(world: World) -> None:
    obj = post(tags=[{"x": 1}])

    def change(p: Post) -> None:
        p.title = "x"

    assert patch_after(world, obj, change) == {"title": "x"}


@row(COLL)
def a_full_save_refuses_a_collection_without_keys(world: World) -> None:
    obj = post(tags=[{"x": 1}])
    raises(
        world,
        FieldConversionError,
        lambda: obj.save(full=True),
        "Post.tags",
    )


SET = "a-set-of-relations"


@row(SET)
def a_set_is_read_as_a_set_and_a_frozenset_as_a_frozenset(
    world: World,
) -> None:
    obj = post()
    assert obj.tagset == {Tag(slug="a"), Tag(slug="b")}
    assert type(obj.tagset) is set
    assert obj.tagfs == frozenset({Tag(slug="a"), Tag(slug="b")})
    assert type(obj.tagfs) is frozenset


@row(SET)
def a_repeated_key_is_one_member(world: World) -> None:
    for items in (["a", "a"], [{"slug": "a"}, {"slug": "a"}]):
        obj = post(tagset=items)
        assert len(obj.tagset) == 1
        assert pks(obj.tagset) == ["a"]


@row(SET)
def a_set_item_without_a_key_is_refused_when_read(world: World) -> None:
    obj = post(tagset=["a", {"x": 1}])
    raises(
        world,
        FieldConversionError,
        lambda: obj.tagset,
        "Post.tagset",
        "[1]",
        "TypedDict",
        "dataclass",
    )


@row(SET)
def an_equal_frozenset_sends_nothing(world: World) -> None:
    obj = post()
    assert (
        patch_after(
            world,
            obj,
            lambda p: setattr(
                p, "tagfs", frozenset({Tag(slug="a"), Tag(slug="b")})
            ),
        )
        is None
    )


@row(SET)
def another_frozenset_is_sent_sorted(world: World) -> None:
    obj = post()
    assert patch_after(
        world,
        obj,
        lambda p: setattr(
            p, "tagfs", frozenset({Tag(slug="c"), Tag(slug="a")})
        ),
    ) == {"tagfs": ["a", "c"]}


@row(SET)
def a_list_assigned_to_a_set_field_is_deduplicated(world: World) -> None:
    obj = post()
    assert patch_after(
        world,
        obj,
        lambda p: setattr(p, "tagfs", [Tag(slug="c"), Tag(slug="c")]),
    ) == {"tagfs": ["c"]}
    assert type(obj.tagfs) is frozenset


@row(SET)
def a_tuple_assigned_to_a_set_field_reads_as_a_set(world: World) -> None:
    obj = post()
    obj.tagset = (Tag(slug="c"),)  # type: ignore[assignment]
    assert type(obj.tagset) is set


@row(SET)
def an_assigned_set_item_without_a_key_is_refused(world: World) -> None:
    obj = post()
    raises(
        world,
        FieldConversionError,
        lambda: setattr(obj, "tagset", [Tag()]),
        "Post.tagset",
        "[0]",
    )


@row(SET)
def an_added_member_is_sent_sorted(world: World) -> None:
    obj = post()
    assert patch_after(world, obj, lambda p: p.tagset.add(Tag(slug="c"))) == {
        "tagset": ["a", "b", "c"]
    }


@row(SET)
def a_discarded_member_is_sent(world: World) -> None:
    obj = post()
    assert patch_after(
        world, obj, lambda p: p.tagset.discard(Tag(slug="a"))
    ) == {"tagset": ["b"]}


@row(SET)
def a_new_object_of_a_present_key_sends_nothing(world: World) -> None:
    obj = post()
    assert (
        patch_after(world, obj, lambda p: p.tagset.add(Tag(slug="a"))) is None
    )


@row(SET)
def a_member_without_a_key_cannot_be_added(world: World) -> None:
    obj = post()
    raises(world, TypeError, lambda: obj.tagset.add(Tag()))


@row(SET)
def a_saved_set_change_is_not_sent_again(world: World) -> None:
    obj = post()
    obj.tagset.add(Tag(slug="c"))
    obj.save()
    before = world.count
    obj.save()
    assert world.count == before


@row(SET)
def members_of_one_key_are_sent_once(world: World) -> None:
    obj = post(watchers=[])
    assert patch_after(
        world,
        obj,
        lambda p: setattr(p, "watchers", {User(id=7), Admin(id=7)}),
    ) == {"watchers": [7]}


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
    first.tagset.add(Tag(slug="c"))
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


@row(OWN)
def a_key_of_the_wrong_type_is_refused_in_a_query(world: World) -> None:
    for kw in ({"id": "7"}, {"pk": "7"}, {"id__gt": "7"}, {"id": True}):
        refuses_query(world, Post, FieldConversionError, "Post.id", **kw)


@row(OWN)
def an_object_of_the_model_is_refused_as_the_key(world: World) -> None:
    other = Post(json={"postId": 3})
    sends_query(world, Post, "postId=3", id=other)
    sends_query(world, Post, "pk=3", pk=other)
    sends_query(world, Post, "pk__in=3", pk__in=[other])


@row(OWN)
def a_subclass_object_is_sent_as_its_key(world: World) -> None:
    sends_query(world, User, "id=7", id=Admin(id=7))


@row(OWN)
def an_object_without_a_key_is_refused_in_a_query(world: World) -> None:
    refuses_query(world, Post, FieldConversionError, "Post.id", id=Post())


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
def two_parameters_naming_the_key_are_refused(world: World) -> None:
    refuses_query(world, Post, ValueError, "pk", "id", pk=7, id=8)


@row(OWN)
def the_json_name_is_an_ordinary_name(world: World) -> None:
    sends_query(world, Post, "postId=7", postId=7)


@row(OWN)
def an_ordinary_name_is_sent_as_given(world: World) -> None:
    sends_query(world, Post, "postId=7", postId="7")


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
def an_object_of_another_model_or_without_a_key_is_refused(
    world: World,
) -> None:
    for value in (Other(id=7), User()):
        refuses_query(
            world, Post, FieldConversionError, "Post.owner", owner=value
        )


@row(REL)
def several_keys_are_joined(world: World) -> None:
    sends_query(world, Post, "owner__in=7,8", owner__in=[7, User(id=8)])


@row(REL)
def a_key_of_the_wrong_type_among_several_is_refused(world: World) -> None:
    refuses_query(
        world, Post, FieldConversionError, "Post.owner", owner__in=["7"]
    )


@row(REL)
def a_collection_relation_takes_a_key_or_an_object(world: World) -> None:
    sends_query(world, Post, "tags=a", tags=Tag(slug="a"))
    sends_query(world, Post, "tags=a", tags="a")


@row(REL)
def a_list_of_keys_repeats_the_parameter(world: World) -> None:
    sends_query(world, Post, "tags=a&tags=b", tags=["a", "b"])


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
def an_object_without_a_key_through_a_relation_is_refused(
    world: World,
) -> None:
    refuses_query(world, Post, ValueError, "owner__id", owner__id=User())


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
    sends_query(world, Post, "x=8", x=User(id=8))


CREATE = "creating"


@row(CREATE)
def a_key_and_a_field_are_posted(world: World) -> None:
    assert world.body(lambda: User.create(id=7, name="a"), "POST") == {
        "id": 7,
        "name": "a",
    }


@row(CREATE, pending="create() sends pk under its own name in the body")
def pk_is_posted_as_the_key(world: World) -> None:
    assert world.body(lambda: User.create(pk=7), "POST") == {"id": 7}


@row(CREATE)
def a_key_of_the_wrong_type_is_refused_on_create(world: World) -> None:
    raises(world, FieldConversionError, lambda: User.create(id="7"), "User.id")


@row(CREATE)
def a_json_name_is_posted_as_given(world: World) -> None:
    assert world.body(lambda: Post.create(postId=7), "POST") == {"postId": 7}


@row(CREATE)
def a_json_name_of_the_wrong_type_is_refused_on_create(world: World) -> None:
    raises(
        world,
        FieldConversionError,
        lambda: Post.create(postId="7"),
        "Post.id",
    )


@row(CREATE)
def two_names_of_one_key_are_refused_on_create(world: World) -> None:
    raises(
        world,
        TypeError,
        lambda: Post.create(id=7, postId=8),
        "id",
        "postId",
    )


@row(CREATE)
def a_uuid_key_is_posted_in_its_canonical_form(world: World) -> None:
    assert world.body(lambda: Doc.create(id=UP), "POST") == {"id": LOWER}


@row(CREATE)
def a_related_object_is_posted_as_its_key(world: World) -> None:
    assert world.body(lambda: Post.create(owner=User(id=8)), "POST") == {
        "owner": 8
    }


@row(CREATE)
def related_objects_are_posted_as_keys(world: World) -> None:
    assert world.body(lambda: Post.create(tags=[Tag(slug="a")]), "POST") == {
        "tags": ["a"]
    }


@row(CREATE, pending="get_or_create(pk=) posts pk under its own name")
def get_or_create_posts_the_key_when_nothing_is_found(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    sent = world.bodies(lambda: User.get_or_create(pk=7))
    assert sent == [("GET", None), ("POST", {"id": 7})]
    assert unquote(urlsplit(world.requests[-2].url).query) == "pk=7"


@row(CREATE)
def get_or_create_by_id_posts_the_key(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json=[])
    sent = world.bodies(lambda: User.get_or_create(id=7))
    assert sent == [("GET", None), ("POST", {"id": 7})]
    assert unquote(urlsplit(world.requests[-2].url).query) == "id=7"


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


@row(FULL)
def another_key_is_refused_when_merging(world: World) -> None:
    for key in (8, None, "7"):
        user = User(json={"id": 7})
        raises(
            world,
            ResponseShapeError,
            lambda: user.update_from_json({"id": key}),
        )
        assert user.pk == 7


@row(FULL)
def a_key_is_refused_for_a_keyless_object_when_merging(world: World) -> None:
    raises(
        world, ResponseShapeError, lambda: User().update_from_json({"id": 7})
    )


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
        "bulk_get_by_pks",
        "PUser",
        "owner",
        "POther",
        requests=1,
    )


@row(PREFETCH)
def an_object_without_a_key_from_the_hook_is_ignored(world: World) -> None:
    hook_calls.clear()
    hook_results.clear()
    hook_results["make"] = lambda pk: PUser()
    world.mocker.get(requests_mock.ANY, json=[{"id": 1, "owner": 7}])
    (parent,) = list(PPost.filter(prefetch=["owner"]))
    assert parent.owner is not None
    assert parent.owner.pk == 7


@row(PREFETCH)
def bulk_get_by_urls_is_not_a_hook(world: World) -> None:
    prefetched(world, 7)
    assert ("urls", {7}) not in hook_calls
    assert not hasattr(JSONObject, "bulk_get_by_urls")


OVERRIDE = "a-class-that-overrides-url"


@json_object
class Me(Base):
    id: int
    name: str

    @property
    def url(self) -> str:
        return "/me/"


@json_object
class Slot(Base):
    class_url = "/slots/"
    id: int
    name: str

    @property
    def url(self) -> str:
        return f"/slots/{self.pk}/x/"


@json_object
class Card(Base):
    class_url = "/cards/"
    id: int
    me: Me


@row(OVERRIDE)
def the_url_is_the_override(world: World) -> None:
    assert Me().url == "/me/"


@row(OVERRIDE)
def a_field_read_loads_the_override_url(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json={"id": 5, "name": "a"})
    me = Me()
    assert me.name == "a"
    assert world.requests[-1].path == "/me/"
    assert me.pk == 5


@row(OVERRIDE)
def an_object_not_yet_loaded_cannot_be_hashed(world: World) -> None:
    raises(world, TypeError, lambda: hash(Me()))


@row(OVERRIDE)
def two_unloaded_objects_are_unequal(world: World) -> None:
    assert Me() != Me()


@row(OVERRIDE)
def an_unloaded_object_cannot_be_given_a_key(world: World) -> None:
    raises(world, AttributeError, lambda: setattr(Me(), "id", 5))


@row(OVERRIDE)
def a_keyed_object_keeps_the_override_url(world: World) -> None:
    assert Me(id=5).url == "/me/"


@row(OVERRIDE)
def a_loaded_object_saves_to_the_override_url(world: World) -> None:
    me = Me(json={"id": 5, "name": "a"})
    me.name = "b"
    assert world.body(me.save, "PATCH") == {"name": "b"}
    assert world.requests[-1].path == "/me/"


@row(OVERRIDE)
def a_url_may_use_the_key(world: World) -> None:
    assert Slot(id=3).url == "/slots/3/x/"


@row(OVERRIDE)
def a_body_with_another_key_is_refused(world: World) -> None:
    world.mocker.get(requests_mock.ANY, json={"id": 4, "name": "a"})
    slot = Slot(id=3)
    raises(world, ResponseShapeError, lambda: slot.name, requests=1)


@row(OVERRIDE)
def a_relation_to_the_class_reads_its_key(world: World) -> None:
    card = Card(json={"id": 1, "me": 5})
    assert card.me.url == "/me/"
    assert card.me.pk == 5


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


@row(SCHEMA)
def a_relation_is_read_as_a_key_or_an_object(world: World) -> None:
    assert schema_property(Post, "read", "owner") == {
        "anyOf": [
            {"anyOf": [{"type": "integer"}, {"type": "object"}]},
            {"type": "null"},
        ]
    }


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
