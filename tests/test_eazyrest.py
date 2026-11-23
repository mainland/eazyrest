"""Tests for JSON-backed REST objects."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import decimal
import email
import email.message
import enum
import inspect
import math
import re
import sys
import types
import typing
import uuid
from collections import deque
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from copy import deepcopy
from typing import (
    Annotated,
    Any,
    ClassVar,
    NewType,
    Optional,
    TypeVar,
    cast,
)
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st
from requests_mock import ANY as requests_mock_ANY
from typing_extensions import TypeAliasType, TypedDict

from eazyrest import (
    API,
    Client,
    Conversion,
    DoesNotExist,
    EazyrestError,
    FieldConversionError,
    JSONObject,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    RedirectError,
    Response,
    ResponseShapeError,
    UsageError,
    WriteMode,
    encode_json,
    json_object,
)
from eazyrest._models import _api_registry

if typing.TYPE_CHECKING:
    # Imported only for type checkers, so the name is unbound at run time.
    from fractions import Fraction


class ModelBase(JSONObject):
    """Base object for test-only models."""


test_api = API("https://example.com/v1/")
ModelBase.register_api(test_api)


@json_object
class User(ModelBase):
    """User model used in tests."""

    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls,
        pks: set[Any],
        *,
        api: Client | None = None,
        **kwargs: Any,
    ) -> dict[Any, User]:
        """Load users in one request for explicit prefetch support.

        Args:
            pks: Primary keys of the users to load.
            api: API to load the users with, or ``None`` for the model's own.

        Returns:
            The loaded users, keyed by primary key.
        """
        users = list(cls.filter(id__in=sorted(pks), api=api))
        return {user.pk: user for user in users}


@json_object(field_map={"user": "userId"})
class Todo(ModelBase):
    """Todo model used in tests."""

    class_url = "/todos/"

    id: int
    user: User
    title: str
    completed: bool


def test_absolute_url_resolves_against_api_base_url() -> None:
    """absolute_url should include the API base URL and path prefix."""
    todo = Todo(id=1)

    assert todo.url == "/todos/1/"
    assert todo.absolute_url == "https://example.com/v1/todos/1/"


def test_absolute_url_keeps_absolute_class_url() -> None:
    """absolute_url should not rewrite already absolute model URLs."""

    @json_object
    class ExternalTodo(ModelBase):
        """Model with an absolute class URL."""

        class_url = "https://other.example.com/todos/"

        id: int

    todo = ExternalTodo(id=1)

    assert todo.url == "https://other.example.com/todos/1/"
    assert todo.absolute_url == "https://other.example.com/todos/1/"


def test_detail_response_without_primary_key_keeps_identity(
    requests_mock: Any,
) -> None:
    """A loaded object keeps the key it was loaded by."""
    requests_mock.get("https://example.com/v1/users/3/", json={"name": "Ada"})
    patch = requests_mock.patch(
        "https://example.com/v1/users/3/", status_code=204
    )
    user = User(id=3)

    assert user.name == "Ada"
    assert user.pk == 3
    user.name = "Grace"
    user.save()
    assert patch.last_request.json() == {"name": "Grace"}


def test_a_resource_at_a_fixed_url_is_read_with_its_key(
    requests_mock: Any,
) -> None:
    """A resource at a fixed URL is read as an object of its model.

    The object has the key that the response names, and its own URL. A class
    that overrides ``url`` still needs its key from construction.
    """

    @json_object
    class CurrentUser(ModelBase):
        """Resource addressed by a fixed URL."""

        id: int
        name: str

        @property
        def url(self) -> str:
            """Return the fixed resource URL."""
            return "/me/"

    requests_mock.get(
        "https://example.com/v1/me/", json={"id": 7, "name": "Ada"}
    )

    me = User(json=test_api.json(test_api.get("/me/")))

    assert me.pk == 7
    assert me.name == "Ada"
    assert me.url == "/users/7/"
    assert requests_mock.call_count == 1
    with pytest.raises(TypeError, match=r"CurrentUser\.id"):
        CurrentUser()


def test_url_override_that_extends_default_reads_the_key_without_url(
    requests_mock: Any,
) -> None:
    """A key read never calls ``url``, so an override cannot recurse."""

    @json_object
    class Extended(ModelBase):
        """Resource whose ``url`` extends the default."""

        class_url = "/extended/"

        id: int
        name: str

        @property
        def url(self) -> str:
            """Return the default URL with a suffix."""
            return super().url + "detail/"

    obj = Extended(id=3)

    assert obj.pk == 3
    assert obj.url == "/extended/3/detail/"
    assert requests_mock.call_count == 0


@json_object
class SearchResult(ModelBase):
    """Model that extracts results from envelope responses."""

    class_url = "/search/"

    id: int
    name: str

    @classmethod
    def collection_items(
        cls, payload: Any, *, api: Client, **kwargs: Any
    ) -> Any:
        """Extract wrapped collection items for this test model.

        Args:
            payload: Decoded JSON body of a collection response.
            api: The query's client.

        Returns:
            The items under the body's ``results`` key.
        """
        return payload["results"]

    @classmethod
    def object_json(cls, payload: Any, **kwargs: Any) -> Any:
        """Extract a wrapped single object for this test model.

        Args:
            payload: Decoded JSON body of a single-object response.

        Returns:
            The object under the body's ``result`` key.
        """
        return payload["result"]


@json_object
class PaginatedSearchResult(ModelBase):
    """Model that follows paginated collection responses."""

    class_url = "/paginated-search/"

    id: int
    name: str

    @classmethod
    def collection_items(
        cls, payload: Any, *, api: Client, **kwargs: Any
    ) -> Any:
        """Follow ``next`` links and yield items across all pages.

        Args:
            payload: Decoded JSON body of the first page.
            api: The query's client.

        Yields:
            Each item in the ``results`` list of each page, fetching the page
            that ``next`` names until ``next`` is ``None``.
        """
        while True:
            yield from payload["results"]
            next_url = payload["next"]
            if next_url is None:
                return
            payload = api.get(next_url).json()


@json_object
class MaybeTodo(ModelBase):
    """Model with an optional related user field."""

    class_url = "/maybe-todos/"

    id: int
    user: User | None


@json_object
class Team(ModelBase):
    """Model with a lazy collection of related users."""

    class_url = "/teams/"

    id: int
    members: Sequence[User]


@json_object
class TupleTeam(ModelBase):
    """Model with a tuple of related users."""

    class_url = "/tuple-teams/"

    id: int
    members: tuple[User, ...]


@json_object
class SetTeam(ModelBase):
    """Model with a set of related users."""

    class_url = "/set-teams/"

    id: int
    members: set[User]


@json_object
class NamedSetTeam(ModelBase):
    """Model with a set of related users and a plain field."""

    class_url = "/named-set-teams/"

    id: int
    members: set[User]
    name: str


@json_object
class Event(ModelBase):
    """Model with a datetime field used to verify caching."""

    class_url = "/events/"

    id: int
    starts_at: datetime.datetime


@json_object
class SequenceTeam(ModelBase):
    """Model with a sequence of related objects."""

    class_url = "/sequence-teams/"

    id: int
    members: Sequence[User]


@json_object
class Booking(ModelBase):
    """Model with date and UUID fields."""

    class_url = "/bookings/"

    id: int
    day: datetime.date
    token: uuid.UUID


@json_object
class Timeline(ModelBase):
    """Model with a list of datetimes."""

    class_url = "/timelines/"

    id: int
    starts_at: list[datetime.datetime]


@json_object
class Readings(ModelBase):
    """Model with a list that may hold nulls and a list of lists."""

    class_url = "/readings/"

    id: int
    values: list[int | None]
    rows: list[list[int]]


T = TypeVar("T")
Dates = TypeAliasType("Dates", list[datetime.datetime])
ListOf = TypeAliasType("ListOf", list[T], type_params=(T,))
MaybeDates = TypeAliasType("MaybeDates", Dates | None)
QuotedDates = TypeAliasType("QuotedDates", list["datetime.datetime"])
NotedQuotedDates = TypeAliasType(
    "NotedQuotedDates",
    Annotated[list["datetime.datetime"], "a note, not a name"],
)
# Build list["..."] at run time, since mypy rejects what these aliases test: a
# quoted self-reference and an unknown name.
QuotedRecursive = TypeAliasType(
    "QuotedRecursive",
    types.GenericAlias(list, ("QuotedRecursive",)),  # type: ignore[valid-type]
)
QuotedMissing = TypeAliasType(
    "QuotedMissing",
    types.GenericAlias(list, ("NoSuchType",)),  # type: ignore[valid-type]
)


@json_object
class AliasedTimeline(ModelBase):
    """Model whose field types are named type aliases."""

    class_url = "/aliased-timelines/"

    id: int
    starts_at: Dates
    ends_at: ListOf[datetime.datetime]
    paused_at: MaybeDates


@json_object
class AnnotatedTimeline(ModelBase):
    """Model whose field types carry metadata that eazyrest does not use."""

    class_url = "/annotated-timelines/"

    id: int
    owner: Annotated[User, "owner"] | None
    starts_at: Annotated[list[Annotated[datetime.datetime, "start"]], "all"]


# Enums that mix str into Enum are a common form in user code, and they format
# differently from StrEnum, so the tests keep them.
class IssueStatus(str, enum.Enum):  # noqa: UP042
    """Status values for enum conversion tests."""

    OPEN = "open"
    CLOSED = "closed"


class Priority(str, enum.Enum):  # noqa: UP042
    """Priority values whose enum maps unknown values to ``UNKNOWN``."""

    LOW = "low"
    HIGH = "high"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> Priority:
        """Map a value the enum lacks to ``UNKNOWN``.

        Args:
            value: Value that matches no member. It is not inspected.

        Returns:
            The ``UNKNOWN`` member.
        """
        return cls.UNKNOWN


@json_object
class PrioritizedTask(ModelBase):
    """Model with an enum field that accepts unknown values."""

    class_url = "/prioritized-tasks/"

    id: int
    title: str
    priority: Priority


@json_object
class Issue(ModelBase):
    """Model with an enum field."""

    class_url = "/issues/"

    id: int
    status: IssueStatus


@json_object
class MaybeIssue(ModelBase):
    """Model with an optional enum field."""

    class_url = "/maybe-issues/"

    id: int
    status: IssueStatus | None


@json_object
class IssueSnapshot(ModelBase):
    """Model with a tuple of enums."""

    class_url = "/issue-snapshots/"

    id: int
    statuses: tuple[IssueStatus, ...]


@json_object
class NoBulkUser(ModelBase):
    """Related model without bulk prefetch support."""

    class_url = "/nobulk-users/"

    id: int
    name: str


@json_object
class IssueHistory(ModelBase):
    """Model with a variable-length tuple of enums."""

    class_url = "/issue-histories/"

    id: int
    statuses: tuple[IssueStatus, ...]


@json_object
class Task(ModelBase):
    """Model with a duration field."""

    class_url = "/tasks/"

    id: int
    duration: datetime.timedelta


@json_object
class Admin(User):
    """Decorated subclass of a decorated model."""

    role: str


@json_object(pk="slug")
class Org(ModelBase):
    """Model with a string primary key."""

    class_url = "/orgs/"

    slug: str
    name: str


@json_object
class Repo(ModelBase):
    """Model related to a model with a string primary key."""

    class_url = "/repos/"

    id: int
    org: Org


@json_object(field_map={"id": "ID"})
class LegacyUser(ModelBase):
    """Model whose primary key has a different JSON name."""

    class_url = "/legacy-users/"

    id: int
    name: str


@json_object
class LegacyTodo(ModelBase):
    """Model related to a model with a renamed primary key."""

    class_url = "/legacy-todos/"

    id: int
    owner: LegacyUser


class AccountBase(JSONObject):
    """Base object for test models served by a second API."""


accounts_api = API("https://accounts.example.com/")
AccountBase.register_api(accounts_api)


@json_object
class Account(AccountBase):
    """Model served by the second API."""

    class_url = "/accounts/"

    id: int
    name: str


@json_object
class Invoice(ModelBase):
    """Model served by the first API with a related account."""

    class_url = "/invoices/"

    id: int
    account: Account


@json_object(field_map={"user": "userId"})
class NoBulkTodo(ModelBase):
    """Todo model whose relation does not opt into bulk prefetch."""

    class_url = "/nobulk-todos/"

    id: int
    user: NoBulkUser
    title: str


def test_a_load_answered_with_an_empty_list_raises_a_shape_error(
    requests_mock: Any,
) -> None:
    """An empty list is not the object that a load reads."""
    requests_mock.get("https://example.com/v1/todos/1/", json=[])

    todo = Todo(id=1)

    with pytest.raises(ResponseShapeError) as caught:
        _ = todo.json
    assert caught.value.response is not None
    assert caught.value.response.json() == []


def test_a_load_answered_with_a_list_of_several_objects_raises(
    requests_mock: Any,
) -> None:
    """Only a singleton list holds one object."""
    requests_mock.get(
        "https://example.com/v1/users/1/",
        json=[{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}],
    )

    with pytest.raises(ResponseShapeError) as caught:
        _ = User(id=1).name
    assert caught.value.response is not None
    assert len(caught.value.response.json()) == 2


def test_filter_returns_iterable_and_preserves_base_path_prefix(
    requests_mock: Any,
) -> None:
    """Collection queries should stream objects from the prefixed base URL."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "userId": 2, "title": "a", "completed": False},
            {"id": 2, "userId": 2, "title": "b", "completed": True},
        ],
    )

    todos = list(Todo.filter())

    assert [todo.id for todo in todos] == [1, 2]


def test_default_filter_reads_every_page(requests_mock: Any) -> None:
    """By default, ``filter()`` follows next links from the collection URL.

    A query-only link resolves against the collection's URL, not the base URL,
    so the default passes the response's URL to ``follow_next_links()``.
    """
    requests_mock.get(
        "https://example.com/v1/todos/",
        complete_qs=True,
        json={
            "count": 2,
            "next": "?page=2",
            "results": [{"id": 1, "userId": 2, "title": "a"}],
        },
    )
    requests_mock.get(
        "https://example.com/v1/todos/?page=2",
        json={"next": None, "results": [{"id": 2, "userId": 2, "title": "b"}]},
    )

    todos = Todo.filter()

    assert todos.count == 2
    assert [todo.id for todo in todos] == [1, 2]


def test_default_filter_rejects_other_payloads(requests_mock: Any) -> None:
    """A payload that is neither a list nor a page raises."""
    requests_mock.get("https://example.com/v1/todos/", json={"items": []})

    with pytest.raises(ResponseShapeError):
        list(Todo.filter())


def test_default_collection_items_does_not_page() -> None:
    """The default ``collection_items()`` itself accepts only a list."""
    with pytest.raises(ResponseShapeError, match="expected a list"):
        Todo.collection_items({"results": []}, api=test_api)


def test_collection_and_object_hooks_support_envelope_responses(
    requests_mock: Any,
) -> None:
    """Models can override extraction hooks for wrapped responses."""
    requests_mock.get(
        "https://example.com/v1/search/",
        json={"results": [{"id": 1, "name": "Ada"}]},
    )
    requests_mock.get(
        "https://example.com/v1/search/1/",
        json={"result": {"id": 1, "name": "Ada"}},
    )

    results = list(SearchResult.filter())
    result = SearchResult(id=1)

    assert [item.name for item in results] == ["Ada"]
    assert result.name == "Ada"


def test_iter_collection_items_can_follow_paginated_responses(
    requests_mock: Any,
) -> None:
    """Collection iterators can follow ``next`` links across pages."""
    requests_mock.get(
        "https://example.com/v1/paginated-search/",
        json={
            "next": "https://example.com/v1/paginated-search/?cursor=abc",
            "results": [{"id": 1, "name": "Ada"}],
        },
    )
    requests_mock.get(
        "https://example.com/v1/paginated-search/?cursor=abc",
        json={
            "next": None,
            "results": [{"id": 2, "name": "Grace"}],
        },
    )

    results = list(PaginatedSearchResult.filter())

    assert [item.name for item in results] == ["Ada", "Grace"]


def test_get_or_create_returns_existing_object_without_patch(
    requests_mock: Any,
) -> None:
    """Lookup hits in ``get_or_create`` should not mutate remote objects."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "keep", "completed": False}],
    )

    obj, created = Todo.get_or_create(id=1)

    assert not created
    assert obj.id == 1
    assert requests_mock.call_count == 1


def test_create_encodes_json_backed_fields(requests_mock: Any) -> None:
    """``create()`` should map and encode declared JSON-backed fields."""
    post = requests_mock.post(
        "https://example.com/v1/todos/",
        json={"id": 1, "userId": 2, "title": "a", "completed": False},
    )

    todo = Todo.create(
        user=User(id=2),
        title="a",
        completed=False,
        metadata={"source": "test"},
    )

    assert todo.id == 1
    assert post.last_request.json() == {
        "userId": 2,
        "title": "a",
        "completed": False,
        "metadata": {"source": "test"},
    }


def test_create_encodes_datetime_fields(requests_mock: Any) -> None:
    """``create()`` should use typed JSON encoding for scalar fields."""
    starts_at = datetime.datetime(
        2024,
        4,
        1,
        12,
        0,
        tzinfo=datetime.UTC,
    )
    post = requests_mock.post(
        "https://example.com/v1/events/",
        json={"id": 1, "starts_at": "2024-04-01T12:00:00Z"},
    )

    event = Event.create(starts_at=starts_at)

    assert event.id == 1
    assert event.starts_at == starts_at
    assert post.last_request.json() == {
        "starts_at": "2024-04-01T12:00:00+00:00"
    }


def test_create_encodes_enum_fields(requests_mock: Any) -> None:
    """``create()`` should encode enum fields using their JSON values."""
    post = requests_mock.post(
        "https://example.com/v1/issues/",
        json={"id": 1, "status": "open"},
    )

    issue = Issue.create(status=IssueStatus.OPEN)

    assert issue.status is IssueStatus.OPEN
    assert post.last_request.json() == {"status": "open"}


def test_create_uses_object_json_hook_for_response(
    requests_mock: Any,
) -> None:
    """``create()`` should unwrap custom single-object response envelopes."""
    post = requests_mock.post(
        "https://example.com/v1/search/",
        json={"result": {"id": 1, "name": "Ada"}},
    )

    result = SearchResult.create(name="Ada")

    assert result.id == 1
    assert result.name == "Ada"
    assert post.last_request.json() == {"name": "Ada"}


def test_related_collection_is_loaded_lazily(
    requests_mock: Any,
) -> None:
    """Related objects are not fetched until a field of one is read."""
    requests_mock.get(
        "https://example.com/v1/teams/1/",
        json={"id": 1, "members": [2, 3]},
    )
    requests_mock.get(
        "https://example.com/v1/users/2/", json={"id": 2, "name": "Ada"}
    )
    requests_mock.get(
        "https://example.com/v1/users/3/",
        json={"id": 3, "name": "Grace"},
    )

    team = Team(id=1)
    members = team.members

    assert requests_mock.call_count == 1
    assert isinstance(members, tuple)

    loaded = list(members)

    assert [member.name for member in loaded] == ["Ada", "Grace"]
    assert requests_mock.call_count == 3


def test_related_tuple_is_materialized(
    requests_mock: Any,
) -> None:
    """Concrete tuple annotations should materialize related collections."""
    requests_mock.get(
        "https://example.com/v1/tuple-teams/1/",
        json={"id": 1, "members": [2, 3]},
    )
    requests_mock.get(
        "https://example.com/v1/users/2/",
        json={"id": 2, "name": "Ada"},
    )
    requests_mock.get(
        "https://example.com/v1/users/3/",
        json={"id": 3, "name": "Grace"},
    )

    team = TupleTeam(id=1)
    members = team.members

    assert isinstance(members, tuple)
    assert [member.name for member in members] == ["Ada", "Grace"]
    assert requests_mock.call_count == 3


def test_related_set_is_materialized(
    requests_mock: Any,
) -> None:
    """Concrete set annotations should materialize related collections."""
    requests_mock.get(
        "https://example.com/v1/set-teams/1/",
        json={"id": 1, "members": [2, 3]},
    )
    requests_mock.get(
        "https://example.com/v1/users/2/",
        json={"id": 2, "name": "Ada"},
    )
    requests_mock.get(
        "https://example.com/v1/users/3/",
        json={"id": 3, "name": "Grace"},
    )

    team = SetTeam(id=1)
    members = team.members

    assert isinstance(members, set)
    assert {member.name for member in members} == {"Ada", "Grace"}
    assert requests_mock.call_count == 3


@pytest.mark.parametrize(
    ("payload", "value_type"),
    [
        ("23", "str"),
        ({"id": 2}, "dict"),
    ],
)
def test_invalid_related_collection_payload_raises_type_error(
    requests_mock: Any,
    payload: Any,
    value_type: str,
) -> None:
    """Malformed related collections should fail fast.

    Args:
        payload: JSON value of the related collection, which is not a list.
        value_type: Name of the type of ``payload`` that the error reports.
    """
    requests_mock.get(
        "https://example.com/v1/teams/1/",
        json={"id": 1, "members": payload},
    )

    team = Team(id=1)

    with pytest.raises(
        TypeError,
        match=rf"Related collection values must be iterable, got {value_type}",
    ):
        _ = team.members


def test_optional_pep604_related_object_is_converted(
    requests_mock: Any,
) -> None:
    """``User | None`` fields should still use related-object conversion."""
    requests_mock.get(
        "https://example.com/v1/maybe-todos/1/",
        json={"id": 1, "user": 2},
    )
    requests_mock.get(
        "https://example.com/v1/users/2/",
        json={"id": 2, "name": "Ada"},
    )
    requests_mock.get(
        "https://example.com/v1/maybe-todos/2/",
        json={"id": 2, "user": None},
    )

    with_user = MaybeTodo(id=1)
    without_user = MaybeTodo(id=2)

    assert with_user.user is not None
    assert with_user.user.name == "Ada"
    assert without_user.user is None


def test_repeated_related_field_access_returns_cached_instance(
    requests_mock: Any,
) -> None:
    """Repeated reads of a related field should reuse one instance."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "a", "completed": False},
    )

    todo = Todo(id=1)

    first = todo.user
    second = todo.user

    assert first is second
    assert requests_mock.call_count == 1


def test_repeated_datetime_access_returns_cached_instance(
    requests_mock: Any,
) -> None:
    """Parsed datetime fields should be cached after first access."""
    requests_mock.get(
        "https://example.com/v1/events/1/",
        json={"id": 1, "starts_at": 1711972800.0},
    )

    event = Event(id=1)

    first = event.starts_at
    second = event.starts_at

    assert first is second
    assert first == datetime.datetime(2024, 4, 1, 12, 0, tzinfo=datetime.UTC)


def test_datetime_list_field_decodes_nested_items() -> None:
    """Collection items should use nested datetime conversion."""
    timeline = Timeline(
        json={"id": 1, "starts_at": [1711972800.0, 1711976400.0]}
    )

    assert timeline.starts_at == [
        datetime.datetime(2024, 4, 1, 12, 0, tzinfo=datetime.UTC),
        datetime.datetime(2024, 4, 1, 13, 0, tzinfo=datetime.UTC),
    ]


def test_annotated_field_types_convert_like_their_types(
    requests_mock: Any,
) -> None:
    """``Annotated[T, ...]`` converts as ``T`` and keeps its metadata."""
    patch = requests_mock.patch(
        "https://example.com/v1/annotated-timelines/1/", status_code=204
    )
    timeline = AnnotatedTimeline(
        json={"id": 1, "owner": {"id": 2}, "starts_at": [1711972800]}
    )
    start = datetime.datetime(2024, 4, 1, 12, tzinfo=datetime.UTC)

    assert isinstance(timeline.owner, User)
    assert timeline.owner.id == 2
    assert timeline.starts_at == [start]
    assert typing.get_args(
        vars(AnnotatedTimeline)["starts_at"]._resolve_type()
    )[1:] == ("all",)

    timeline.owner = None
    timeline.starts_at = [start]
    timeline.save()

    assert patch.last_request.json() == {
        "owner": None,
        "starts_at": ["2024-04-01T12:00:00+00:00"],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "2024-04-01T12:00:00Z",
            datetime.datetime(2024, 4, 1, 12, tzinfo=datetime.UTC),
        ),
        (
            "2024-04-01T14:00:00+02:00",
            datetime.datetime(
                2024,
                4,
                1,
                14,
                tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
            ),
        ),
        (
            "2024-04-01T12:00:00.5",
            # A string without an offset decodes to a naive datetime.
            datetime.datetime(2024, 4, 1, 12, 0, 0, 500000),  # noqa: DTZ001
        ),
        (
            1711972800,
            datetime.datetime(2024, 4, 1, 12, tzinfo=datetime.UTC),
        ),
    ],
)
def test_datetime_field_decodes_iso_8601_and_timestamps(
    value: Any, expected: datetime.datetime
) -> None:
    """Datetime fields decode ISO 8601 strings and Unix timestamps.

    Args:
        value: JSON value of the datetime field, an ISO 8601 string or a Unix
            timestamp.
        expected: Datetime that ``value`` decodes to, including its time zone.
    """
    event = Event(json={"id": 1, "starts_at": value})

    assert event.starts_at == expected
    assert event.starts_at.tzinfo == expected.tzinfo


def test_naive_datetime_encodes_without_offset(requests_mock: Any) -> None:
    """A naive datetime is written without inventing a UTC offset."""
    post = requests_mock.post(
        "https://example.com/v1/events/",
        json={"id": 1, "starts_at": "2024-04-01T12:00:00"},
    )

    Event.create(
        starts_at=datetime.datetime(2024, 4, 1, 12)  # noqa: DTZ001
    )

    assert post.last_request.json() == {"starts_at": "2024-04-01T12:00:00"}


def test_datetime_with_sub_minute_offset_is_rejected(
    requests_mock: Any,
) -> None:
    """A UTC offset with seconds raises rather than sending unreadable JSON.

    Python writes such an offset, as ``+00:19:32``, but ISO 8601 has none, so
    neither the field nor a server would read the value back.
    """
    offset = datetime.timezone(datetime.timedelta(minutes=19, seconds=32))
    value = datetime.datetime(1900, 1, 1, tzinfo=offset)
    event = Event(json={"id": 1, "starts_at": "2024-04-01T12:00:00Z"})

    with pytest.raises(FieldConversionError, match="whole minutes"):
        event.starts_at = value
    with pytest.raises(FieldConversionError, match="whole minutes"):
        Event.create(starts_at=value)

    assert event.starts_at == datetime.datetime(
        2024, 4, 1, 12, tzinfo=datetime.UTC
    )
    assert event._pending_updates == {}
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("value", [True, {"seconds": 1}])
def test_datetime_field_rejects_other_json_types(value: Any) -> None:
    """Only strings and numbers decode as datetimes.

    Args:
        value: JSON value of the datetime field that is neither a string nor a
            number.
    """
    event = Event(json={"id": 1, "starts_at": value})

    with pytest.raises(TypeError, match="ISO 8601"):
        _ = event.starts_at


@pytest.mark.parametrize("value", [1e20, -1e20, float("inf")])
def test_datetime_field_rejects_out_of_range_timestamps(value: float) -> None:
    """A timestamp that ``datetime`` cannot represent is a ``ValueError``.

    Args:
        value: Timestamp outside the range that ``datetime`` represents.
    """
    event = Event(json={"id": 1, "starts_at": value})

    with pytest.raises(ValueError, match="out of range"):
        _ = event.starts_at


@json_object
class Shop(ModelBase):
    """Model with decimal and time fields, as DRF serializes them."""

    class_url = "/shops/"

    id: int
    price: decimal.Decimal
    prices: list[decimal.Decimal]
    opens: datetime.time


SHOP_URL = "https://example.com/v1/shops/1/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.10", decimal.Decimal("1.10")),
        ("-12", decimal.Decimal(-12)),
        ("1.5e3", decimal.Decimal("1.5e3")),
        ("1e4300", decimal.Decimal("1e4300")),
        ("-1e-4300", decimal.Decimal("-1e-4300")),
        ("0e999999", decimal.Decimal("0e999999")),
        (0.1, decimal.Decimal("0.1")),
        (2, decimal.Decimal(2)),
    ],
)
def test_decimal_field_reads_strings_and_numbers(
    value: Any, expected: decimal.Decimal
) -> None:
    """A decimal reads from a decimal string or a JSON number.

    Args:
        value: JSON value of the decimal field.
        expected: Decimal that the field reads, with the same digits.
    """
    shop = Shop(json={"id": 1, "price": value})

    assert shop.price == expected
    assert str(shop.price) == str(expected)


@pytest.mark.parametrize("value", ["1 000", "1_000", " 1.5", "NaN", "Inf", ""])
def test_decimal_field_rejects_other_strings(value: str) -> None:
    """Only a decimal number reads, not the other strings ``Decimal`` reads.

    Args:
        value: String that is not a decimal number.
    """
    shop = Shop(json={"id": 1, "price": value})

    with pytest.raises(FieldConversionError, match=r"^Shop\.price: Decimal"):
        _ = shop.price


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_decimal_field_rejects_a_float_that_is_not_finite(
    value: float,
) -> None:
    """A NaN or an infinity in JSON that the caller passes does not read.

    A response cannot carry one, but ``json=`` and a hook's return value can.

    Args:
        value: Float that is not a number.
    """
    shop = Shop(json={"id": 1, "price": value})

    with pytest.raises(FieldConversionError, match=r"^Shop\.price: Decimal"):
        _ = shop.price


@pytest.mark.parametrize(
    "value", ["1E+20000000", "1e-4302", "12.5e4300", "0e-20000", "-0E-4302"]
)
def test_decimal_field_rejects_exponents_too_long_to_write(
    requests_mock: Any, value: str
) -> None:
    """A decimal whose positional form would be very long does not read.

    So, 11 bytes of JSON cannot become megabytes when the value is written
    back. Assigning such a decimal raises too, and sends nothing.

    Args:
        requests_mock: Mocked HTTP transport.
        value: Decimal string with a large exponent.
    """
    shop = Shop(json={"id": 1, "price": value, "prices": [value]})

    with pytest.raises(FieldConversionError, match="exponent beyond"):
        _ = shop.price
    with pytest.raises(FieldConversionError, match=r"^Shop\.prices: \[0\]"):
        _ = shop.prices
    with pytest.raises(FieldConversionError, match="exponent beyond"):
        shop.price = decimal.Decimal(value)
    assert requests_mock.call_count == 0


def test_decimal_field_rejects_exponents_beyond_decimals_own() -> None:
    """An exponent that Decimal itself refuses raises a conversion error."""
    shop = Shop(json={"id": 1, "price": "1e9999999999999999999"})

    with pytest.raises(FieldConversionError, match="exponent beyond"):
        _ = shop.price


def test_zero_decimal_with_a_large_exponent_writes_one_digit() -> None:
    """A zero with a positive exponent writes ``"0"``, so it reads."""
    shop = Shop(json={"id": 1, "price": "0E+20000"})

    assert shop.price == 0
    assert encode_json(shop.price, decimal.Decimal) == "0"


@pytest.mark.parametrize("value", [True, [1], {"amount": 1}])
def test_decimal_field_rejects_other_json_types(value: Any) -> None:
    """Only strings and numbers read as decimals. A boolean is not a number.

    Args:
        value: JSON value that is neither a string nor a number.
    """
    shop = Shop(json={"id": 1, "price": value})

    with pytest.raises(FieldConversionError, match="strings or numbers"):
        _ = shop.price


def test_decimal_field_writes_strings_with_their_digits(
    requests_mock: Any,
) -> None:
    """A decimal is written as DRF writes one: a string in positional form."""
    patch = requests_mock.patch(SHOP_URL, status_code=204)
    shop = Shop(json={"id": 1, "price": "1.10", "prices": ["1"]})

    shop.price = decimal.Decimal("2.50")
    shop.prices = [decimal.Decimal("1E+2"), decimal.Decimal("-0.001")]
    shop.save()

    assert patch.last_request.json() == {
        "price": "2.50",
        "prices": ["100", "-0.001"],
    }


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_field_refuses_to_write_non_finite_values(
    requests_mock: Any, value: str
) -> None:
    """A decimal that JSON and DRF cannot read is refused and not sent.

    Args:
        value: Text of a decimal that is not finite.
    """
    shop = Shop(json={"id": 1, "price": "1.10"})

    with pytest.raises(FieldConversionError, match="finite"):
        shop.price = decimal.Decimal(value)

    assert shop.price == decimal.Decimal("1.10")
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("09:30", datetime.time(9, 30)),
        ("09:30:15", datetime.time(9, 30, 15)),
        ("09:30:15.5", datetime.time(9, 30, 15, 500000)),
        ("09:30:15.123456", datetime.time(9, 30, 15, 123456)),
        ("09:30Z", datetime.time(9, 30, tzinfo=datetime.UTC)),
        (
            "09:30:00-05:30",
            datetime.time(
                9,
                30,
                tzinfo=datetime.timezone(
                    -datetime.timedelta(hours=5, minutes=30)
                ),
            ),
        ),
    ],
)
def test_time_field_reads_iso_8601(
    value: str, expected: datetime.time
) -> None:
    """A time reads from ISO 8601 as documented, whatever Python reads.

    ``time.fromisoformat()`` accepts different forms on different Python
    versions, so the field checks the format itself.

    Args:
        value: ISO 8601 time.
        expected: Time that the field reads.
    """
    shop = Shop(json={"id": 1, "opens": value})

    assert shop.opens == expected
    assert shop.opens.tzinfo == expected.tzinfo


@pytest.mark.parametrize(
    "value",
    ["9:30", "0930", "24:00", "09:60", "09:30:00.1234567", "09:30+24:00", ""],
)
def test_time_field_rejects_other_strings(value: str) -> None:
    """A string that is not an ISO 8601 time, or is out of range, raises.

    Args:
        value: String that the field cannot read.
    """
    shop = Shop(json={"id": 1, "opens": value})

    with pytest.raises(FieldConversionError, match=r"^Shop\.opens: "):
        _ = shop.opens


def test_time_field_rejects_numbers() -> None:
    """A number is not read as seconds since midnight."""
    shop = Shop(json={"id": 1, "opens": 3600})

    with pytest.raises(FieldConversionError, match="must be strings"):
        _ = shop.opens


def test_time_field_writes_iso_8601(requests_mock: Any) -> None:
    """A time is written in ISO 8601, and an offset with seconds is refused."""
    patch = requests_mock.patch(SHOP_URL, status_code=204)
    shop = Shop(json={"id": 1, "opens": "09:30:00"})
    odd = datetime.timezone(datetime.timedelta(minutes=19, seconds=32))

    with pytest.raises(FieldConversionError, match="whole minutes"):
        shop.opens = datetime.time(8, tzinfo=odd)
    shop.opens = datetime.time(8, 15, 0, 500)
    shop.save()

    assert patch.last_request.json() == {"opens": "08:15:00.000500"}


def test_json_schema_describes_decimal_and_time_fields() -> None:
    """A decimal reads a string or a number and writes a string."""
    written = {"type": "string", "format": "decimal"}

    read = Shop.json_schema()["properties"]
    write = Shop.json_schema("write")["properties"]

    assert read["price"] == {"anyOf": [written, {"type": "number"}]}
    assert write["price"] == written
    assert write["prices"] == {"type": "array", "items": written}
    assert (
        read["opens"]
        == write["opens"]
        == {
            "type": "string",
            "format": "time",
        }
    )


def test_sequence_field_reads_as_tuple_of_converted_items(
    requests_mock: Any,
) -> None:
    """A ``Sequence`` field converts its items and reads as a tuple."""
    patch = requests_mock.patch("https://example.com/v1/sequence-teams/1/")
    team = SequenceTeam(json={"id": 1, "members": [{"id": 2}, 3]})

    assert isinstance(team.members, tuple)
    assert [member.pk for member in team.members] == [2, 3]

    team.members = [User(id=4)]
    team.save()

    assert patch.last_request.json() == {"members": [4]}


BOOKING_TOKEN = uuid.UUID("6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40")


@json_object(pk="key", field_map={"key": "ID"})
class UUIDUser(ModelBase):
    """Related model with a UUID primary key and a distinct JSON name."""

    class_url = "/uuid-users/"

    key: uuid.UUID
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, UUIDUser]:
        """Load users and key the result by their decoded primary keys.

        Args:
            pks: Primary keys of the users to load.
            api: Unused. The users load through the model's own API.

        Returns:
            The loaded users, keyed by decoded primary key.
        """
        del api
        return {user.pk: user for user in cls.filter(key__in=sorted(pks))}


@json_object
class UUIDTeam(ModelBase):
    """Model with scalar and collection relations to UUID-keyed users."""

    class_url = "/uuid-teams/"

    id: int
    owner: UUIDUser
    members: list[UUIDUser]


@pytest.mark.parametrize(
    "related",
    [
        BOOKING_TOKEN,
        UUIDUser(key=BOOKING_TOKEN),
        UUIDUser(json={"ID": str(BOOKING_TOKEN), "name": "Ada"}),
    ],
)
def test_uuid_relations_encode_primary_keys(
    requests_mock: Any, related: Any
) -> None:
    """Related UUID keys encode for creation and field assignments.

    Args:
        related: Related user, given as a UUID key or as a ``UUIDUser``.
    """
    payload = {
        "id": 1,
        "owner": str(BOOKING_TOKEN),
        "members": [str(BOOKING_TOKEN)],
    }
    post = requests_mock.post(
        "https://example.com/v1/uuid-teams/", json=payload
    )
    patch = requests_mock.patch(
        "https://example.com/v1/uuid-teams/1/", status_code=204
    )

    UUIDTeam.create(owner=related, members=[related])
    team = UUIDTeam(json={"id": 1, "owner": None, "members": []})
    team.owner = related
    team.members = [related]
    team.save()

    expected = {"owner": str(BOOKING_TOKEN), "members": [str(BOOKING_TOKEN)]}
    assert post.last_request.json() == expected
    assert patch.last_request.json() == expected


@pytest.mark.parametrize("loaded", [False, True])
def test_update_from_json_matches_decoded_uuid_primary_keys(
    requests_mock: Any, loaded: bool
) -> None:
    """Equivalent UUID spellings identify the same loaded or lazy object.

    Args:
        loaded: Whether the user starts from its JSON or from only its primary
            key.
    """
    user = (
        UUIDUser(json={"ID": str(BOOKING_TOKEN), "name": "Ada"})
        if loaded
        else UUIDUser(key=BOOKING_TOKEN)
    )

    user.update_from_json({"ID": BOOKING_TOKEN.hex.upper(), "name": "Grace"})

    assert user.pk == BOOKING_TOKEN
    assert user.name == "Grace"
    assert requests_mock.call_count == 0


def test_partial_update_seeds_uuid_primary_key_as_json(
    requests_mock: Any,
) -> None:
    """A partial payload leaves a lazy object's UUID key readable."""
    user = UUIDUser(key=BOOKING_TOKEN)

    user.update_from_json({"name": "Grace"})

    assert user.pk == BOOKING_TOKEN
    assert user.json["ID"] == str(BOOKING_TOKEN)
    assert user.name == "Grace"
    assert requests_mock.call_count == 0


def test_lazy_related_uuid_primary_keys_are_decoded(
    requests_mock: Any,
) -> None:
    """A related key read from JSON has the key field's type before loading."""
    team = UUIDTeam(
        json={
            "id": 1,
            "owner": str(BOOKING_TOKEN),
            "members": [BOOKING_TOKEN.hex.upper()],
        }
    )

    assert team.owner.key == BOOKING_TOKEN
    assert team.owner.pk == BOOKING_TOKEN
    assert [member.pk for member in team.members] == [BOOKING_TOKEN]
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "key", [BOOKING_TOKEN, str(BOOKING_TOKEN), BOOKING_TOKEN.hex]
)
def test_constructor_decodes_uuid_primary_key(
    requests_mock: Any, key: Any
) -> None:
    """Any accepted spelling of a key reads back as the decoded value.

    Args:
        key: Primary key given to the constructor, as a UUID or one of its
            string spellings.
    """
    user = UUIDUser(key=key)

    assert isinstance(user.key, uuid.UUID)
    assert user.pk == BOOKING_TOKEN
    assert user.url == f"/uuid-users/{BOOKING_TOKEN}/"
    assert requests_mock.call_count == 0


def test_constructor_rejects_invalid_uuid_primary_key() -> None:
    """A key that the field cannot decode fails when the object is made."""
    with pytest.raises(ValueError, match="UUID is not valid"):
        UUIDUser(key="not-a-uuid")


def test_update_from_json_rejects_different_uuid_primary_key() -> None:
    """Normalizing UUIDs must still reject updates for another object."""
    user = UUIDUser(json={"ID": str(BOOKING_TOKEN), "name": "Ada"})

    with pytest.raises(ValueError, match="primary key"):
        user.update_from_json({"ID": str(uuid.UUID(int=0)), "name": "Grace"})

    assert user.pk == BOOKING_TOKEN
    assert user.name == "Ada"


def test_uuid_relations_reuse_prefetched_objects(requests_mock: Any) -> None:
    """Scalar and collection UUID relations share bulk-loaded objects."""
    requests_mock.get(
        "https://example.com/v1/uuid-teams/",
        json=[
            {
                "id": 1,
                "owner": str(BOOKING_TOKEN),
                "members": [str(BOOKING_TOKEN)],
            },
            {
                "id": 2,
                "owner": BOOKING_TOKEN.hex.upper(),
                "members": [str(BOOKING_TOKEN)],
            },
        ],
    )
    requests_mock.get(
        "https://example.com/v1/uuid-users/",
        json=[{"ID": str(BOOKING_TOKEN), "name": "Ada"}],
    )

    first, second = UUIDTeam.filter(prefetch=["owner", "members"])
    calls_after_prefetch = requests_mock.call_count

    assert first.owner is second.owner
    assert first.members[0] is second.members[0]
    assert first.owner.name == first.members[0].name == "Ada"
    # Each field has its own bulk lookup, but no individual user is fetched.
    assert calls_after_prefetch == 3
    assert requests_mock.call_count == calls_after_prefetch


def test_date_and_uuid_fields_decode() -> None:
    """Date and UUID strings decode to ``date`` and ``UUID`` values."""
    booking = Booking(
        json={"id": 1, "day": "2026-01-02", "token": str(BOOKING_TOKEN)}
    )

    assert booking.day == datetime.date(2026, 1, 2)
    assert booking.token == BOOKING_TOKEN


def test_date_and_uuid_fields_encode(requests_mock: Any) -> None:
    """Assigned ``date`` and ``UUID`` values are sent as strings."""
    patch = requests_mock.patch("https://example.com/v1/bookings/1/")
    booking = Booking(
        json={"id": 1, "day": "2026-01-02", "token": str(uuid.UUID(int=0))}
    )

    booking.day = datetime.date(2026, 1, 3)
    booking.token = BOOKING_TOKEN
    booking.save()

    assert patch.last_request.json() == {
        "day": "2026-01-03",
        "token": "6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40",
    }


def test_enum_fallback_keeps_the_server_value(requests_mock: Any) -> None:
    """A value mapped by ``_missing_()`` is not written back."""
    patch = requests_mock.patch(
        "https://example.com/v1/prioritized-tasks/1/", status_code=204
    )
    task = PrioritizedTask(json={"id": 1, "title": "a", "priority": "urgent"})

    assert task.priority is Priority.UNKNOWN
    task.title = "b"
    task.save()

    assert patch.last_request.json() == {"title": "b"}
    assert task.json["priority"] == "urgent"


def test_conversion_errors_name_the_field(requests_mock: Any) -> None:
    """A conversion error names the model and field and keeps its cause."""
    booking = Booking(json={"id": 1, "day": "2026-13-01", "token": "x"})
    when = datetime.datetime(2026, 1, 3, tzinfo=datetime.UTC)

    with pytest.raises(FieldConversionError, match=r"^Booking\.day: ") as info:
        _ = booking.day
    with pytest.raises(TypeError, match=r"^Booking\.day: "):
        Booking.create(day=when)
    with pytest.raises(ValueError, match=r"^UUIDUser\.key: "):
        UUIDUser(key="not-a-uuid")

    error = info.value
    assert (error.model, error.field) == (Booking, "day")
    assert isinstance(error, TypeError)
    assert isinstance(error, ValueError)
    assert isinstance(error, EazyrestError)
    assert isinstance(error.__cause__, ValueError)
    assert requests_mock.call_count == 0


def test_date_field_rejects_datetime() -> None:
    """A ``datetime`` is not written to a ``date`` field."""
    booking = Booking(
        json={"id": 1, "day": "2026-01-02", "token": str(uuid.UUID(int=0))}
    )

    with pytest.raises(TypeError, match="must be dates, got datetime"):
        booking.day = datetime.datetime(
            2026, 1, 3, 10, 30, tzinfo=datetime.UTC
        )

    assert booking.day == datetime.date(2026, 1, 2)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("day", "2026-01-02T00:00:00", ValueError),
        ("day", "2026-01", ValueError),
        ("day", 20260102, TypeError),
        ("token", "not-a-uuid", ValueError),
        ("token", 5, TypeError),
    ],
)
def test_date_and_uuid_fields_reject_other_values(
    field: str, value: Any, error: type[Exception]
) -> None:
    """A date field holds a complete date, and a UUID field a UUID string.

    Args:
        field: Name of the field to read, ``"day"`` or ``"token"``.
        value: Invalid JSON value of the field.
        error: Exception type that reading the field should raise.
    """
    booking = Booking(
        json={"id": 1, "day": "2026-01-02", "token": str(BOOKING_TOKEN)}
        | {field: value}
    )

    with pytest.raises(error):
        getattr(booking, field)


def test_iterable_datetime_field_is_materialized_when_not_related() -> None:
    """Non-related iterable collections should not stay lazy."""
    timeline = Timeline(
        json={"id": 1, "starts_at": [1711972800.0, 1711976400.0]}
    )

    assert isinstance(timeline.starts_at, list)


def test_enum_field_decodes_from_json() -> None:
    """Enum fields should decode JSON values using the declared type."""
    issue = Issue(json={"id": 1, "status": "open"})

    assert issue.status is IssueStatus.OPEN


def test_optional_enum_field_decodes_from_json() -> None:
    """Optional enum fields should decode values and preserve ``None``."""
    with_status = MaybeIssue(json={"id": 1, "status": "closed"})
    without_status = MaybeIssue(json={"id": 2, "status": None})

    assert with_status.status is IssueStatus.CLOSED
    assert without_status.status is None


def test_enum_tuple_field_decodes_nested_items() -> None:
    """Collection items should use nested enum conversion."""
    snapshot = IssueSnapshot(json={"id": 1, "statuses": ["open", "closed"]})

    # eazyrest reads tuple[IssueStatus] as a tuple of any length, but

    # mypy reads it as a tuple of one item.

    statuses: tuple[IssueStatus, ...] = snapshot.statuses

    assert statuses == (IssueStatus.OPEN, IssueStatus.CLOSED)


def test_assigning_enum_field_encodes_value(requests_mock: Any) -> None:
    """Enum assignment should write the enum value to JSON."""
    requests_mock.get(
        "https://example.com/v1/issues/1/",
        json={"id": 1, "status": "open"},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/issues/1/",
        json={"id": 1, "status": "closed"},
    )

    issue = Issue(id=1, write_mode="eager")
    issue.status = IssueStatus.CLOSED

    assert patch.called
    assert patch.last_request.json() == {"status": "closed"}


def test_create_encodes_nested_datetime_and_enum_collections(
    requests_mock: Any,
) -> None:
    """``create()`` should recursively encode nested collection items."""
    starts_at = [
        datetime.datetime(
            2024,
            4,
            1,
            12,
            0,
            tzinfo=datetime.UTC,
        ),
        datetime.datetime(
            2024,
            4,
            1,
            13,
            0,
            tzinfo=datetime.UTC,
        ),
    ]
    timeline_post = requests_mock.post(
        "https://example.com/v1/timelines/",
        json={
            "id": 1,
            "starts_at": ["2024-04-01T12:00:00Z", "2024-04-01T13:00:00Z"],
        },
    )
    snapshot_post = requests_mock.post(
        "https://example.com/v1/issue-snapshots/",
        json={"id": 1, "statuses": ["open", "closed"]},
    )

    timeline = Timeline.create(starts_at=starts_at)
    snapshot = IssueSnapshot.create(
        statuses=(IssueStatus.OPEN, IssueStatus.CLOSED)
    )

    assert timeline.starts_at == starts_at
    assert timeline_post.last_request.json() == {
        "starts_at": ["2024-04-01T12:00:00+00:00", "2024-04-01T13:00:00+00:00"]
    }
    # eazyrest reads tuple[IssueStatus] as a tuple of any length, but mypy
    # reads it as a tuple of one item.
    statuses: tuple[IssueStatus, ...] = snapshot.statuses
    assert statuses == (
        IssueStatus.OPEN,
        IssueStatus.CLOSED,
    )
    assert snapshot_post.last_request.json() == {
        "statuses": ["open", "closed"]
    }


def test_assigning_field_invalidates_cached_related_value(
    requests_mock: Any,
) -> None:
    """Setting a related field should replace the cached converted value."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "a", "completed": False},
    )

    todo = Todo(id=1)
    original = todo.user

    todo.user = User(id=3)
    updated = todo.user

    assert original.pk == 2
    assert updated.pk == 3
    assert updated is not original


@pytest.mark.parametrize("failure", ["conversion", "write"])
def test_a_refused_assignment_keeps_the_cached_value(
    requests_mock: Any, failure: str
) -> None:
    """An assignment that raises leaves the field's cached value as it was."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "a", "completed": False},
    )
    requests_mock.patch("https://example.com/v1/todos/1/", status_code=500)
    todo = Todo(id=1, write_mode="eager")
    original = todo.user
    value = Todo(id=9) if failure == "conversion" else User(id=3)
    error = (
        FieldConversionError if failure == "conversion" else requests.HTTPError
    )

    with pytest.raises(error):
        todo.user = value  # type: ignore[assignment]

    assert todo.user is original
    assert todo._pending_updates == {}


def test_reassigning_related_object_keeps_its_unsaved_changes(
    requests_mock: Any,
) -> None:
    """Assigning a related object keeps that object, edits and all."""
    requests_mock.get(
        "https://example.com/v1/users/2/", json={"id": 2, "name": "Ada"}
    )
    patch = requests_mock.patch(
        "https://example.com/v1/users/2/", status_code=204
    )
    todo = Todo(json={"id": 1, "userId": 2, "title": "a", "completed": False})
    grace = User(json={"id": 3, "name": "Grace"})

    todo.user.name = "Hopper"
    todo.user = todo.user
    todo.user.save()
    todo.user = grace

    assert patch.last_request.json() == {"name": "Hopper"}
    assert todo.user is grace


def test_filter_prefetches_related_objects_via_bulk_get(
    requests_mock: Any,
) -> None:
    """Explicit prefetch should bulk-load related objects once."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "userId": 2, "title": "a", "completed": False},
            {"id": 2, "userId": 3, "title": "b", "completed": True},
        ],
    )
    users = requests_mock.get(
        "https://example.com/v1/users/",
        json=[
            {"id": 2, "name": "Ada"},
            {"id": 3, "name": "Grace"},
        ],
    )

    todos_iter = Todo.filter(prefetch=["user"])

    assert requests_mock.call_count == 1

    todos = list(todos_iter)

    assert [todo.user.name for todo in todos] == ["Ada", "Grace"]
    assert requests_mock.call_count == 2
    assert users.last_request.qs == {"id__in": ["2,3"]}


def test_prefetch_refuses_an_object_of_another_model_from_bulk_get_by_pks(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that returns an object of another model raises ``TypeError``.

    The message names the hook, the model, the field, and the class returned,
    and no parent reads the object.
    """

    def bulk_get_by_pks(
        cls: type[User], /, pks: set[Any], *, api: Client, **kwargs: Any
    ) -> dict[int, Any]:
        return {pk: Team(json={"id": pk}, api=api) for pk in pks}

    monkeypatch.setattr(User, "bulk_get_by_pks", classmethod(bulk_get_by_pks))
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )

    with pytest.raises(
        TypeError,
        match=(
            r"User\.bulk_get_by_pks\(\) returned an object of Team for "
            r"Todo\.user, which is not a User"
        ),
    ):
        list(Todo.filter(prefetch="user"))


@pytest.mark.parametrize("annotation", ["name", "alias"])
def test_prefetch_of_a_model_keyed_by_a_later_class_raises_and_keeps_nothing(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch, annotation: str
) -> None:
    """A prefetch cannot match objects by a key that cannot be read yet.

    The related model's key names a class that is defined later, directly or by
    a ``type`` alias. The prefetch raises the undefined name before it sends
    the bulk request, since the request cannot be matched to its parents, and
    keeps nothing. Once the class is defined, a new prefetch loads the related
    objects with the one bulk request and the parents read them without more.

    Args:
        requests_mock: Serves the parents.
        monkeypatch: Defines the class as a global, after the prefetch fails.
        annotation: Whether the key names the class, or an alias of it.
    """
    key: Any = "LaterRelKey"
    if annotation == "alias":
        if sys.version_info < (3, 12):
            pytest.skip("The type statement is new in 3.12.")
        namespace: dict[str, Any] = {}
        exec("type PendingRelKey = LaterRelKey", namespace)  # noqa: S102
        key = namespace["PendingRelKey"]
        later = namespace
    else:
        later = globals()
    slot: Any = json_object(
        type(
            "PrefetchSlot",
            (ModelBase,),
            {
                "__annotations__": {"id": key, "name": str},
                "class_url": "/slots/",
            },
        )
    )
    calls: list[set[Any]] = []

    def bulk_get_by_pks(
        cls: type[Any], /, pks: set[Any], *, api: Client, **kwargs: Any
    ) -> dict[int, Any]:
        calls.append(set(pks))
        return {
            pk: cls(json={"id": pk, "name": f"bulk{pk}"}, api=api)
            for pk in pks
        }

    monkeypatch.setattr(slot, "bulk_get_by_pks", classmethod(bulk_get_by_pks))
    parent: Any = json_object(
        type(
            "PrefetchParent",
            (ModelBase,),
            {
                "__annotations__": {"id": int, "m": slot},
                "class_url": "/parents/",
            },
        )
    )
    requests_mock.get(
        "https://example.com/v1/parents/", json=[{"id": 1, "m": 1}]
    )

    with pytest.raises(NameError, match="LaterRelKey"):
        list(parent.filter(prefetch="m"))
    assert calls == []
    assert "analyzed_type" not in vars(slot._pk)

    if annotation == "alias":
        later["LaterRelKey"] = int
    else:
        monkeypatch.setitem(later, "LaterRelKey", int)

    (found,) = parent.filter(prefetch="m")
    requests = requests_mock.call_count
    assert found.m.name == "bulk1"
    assert calls == [{1}]
    assert requests_mock.call_count == requests


def test_prefetch_accepts_a_subclass_from_bulk_get_by_pks(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An object of a subclass of the related model is a related object."""

    def bulk_get_by_pks(
        cls: type[User], /, pks: set[Any], *, api: Client, **kwargs: Any
    ) -> dict[int, User]:
        return {
            pk: Admin(json={"id": pk, "name": "root"}, api=api) for pk in pks
        }

    monkeypatch.setattr(User, "bulk_get_by_pks", classmethod(bulk_get_by_pks))
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )

    (todo,) = Todo.filter(prefetch="user")

    assert isinstance(todo.user, Admin)
    assert todo.user.name == "root"
    assert requests_mock.call_count == 1


def test_get_supports_prefetch_for_single_result(
    requests_mock: Any,
) -> None:
    """Single-object lookups should accept the same prefetch option."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )
    requests_mock.get(
        "https://example.com/v1/users/",
        json=[{"id": 2, "name": "Ada"}],
    )

    todo = Todo.get(id=1, prefetch=["user"])

    assert todo.user.name == "Ada"
    assert requests_mock.call_count == 2


def test_all_supports_prefetch(
    requests_mock: Any,
) -> None:
    """All-object lookups should accept the same prefetch option."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )
    requests_mock.get(
        "https://example.com/v1/users/",
        json=[{"id": 2, "name": "Ada"}],
    )

    todo = next(iter(Todo.all(prefetch=["user"])))

    assert todo.user.name == "Ada"
    assert requests_mock.call_count == 2


def test_prefetch_accepts_single_field_name(
    requests_mock: Any,
) -> None:
    """A single prefetch field string should not be split into characters."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )
    requests_mock.get(
        "https://example.com/v1/users/",
        json=[{"id": 2, "name": "Ada"}],
    )

    todo = next(iter(Todo.all(prefetch="user")))

    assert todo.user.name == "Ada"
    assert requests_mock.call_count == 2


def test_empty_prefetch_is_treated_like_no_prefetch(
    requests_mock: Any,
) -> None:
    """An empty prefetch list should behave the same as no prefetch."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )

    todos = list(Todo.filter(prefetch=[]))

    assert [todo.id for todo in todos] == [1]
    assert requests_mock.call_count == 1


def test_prefetch_reuses_related_instances_within_a_batch(
    requests_mock: Any,
) -> None:
    """Prefetch should share one related instance across matching parents."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "userId": 2, "title": "a", "completed": False},
            {"id": 2, "userId": 2, "title": "b", "completed": True},
        ],
    )
    requests_mock.get(
        "https://example.com/v1/users/",
        json=[{"id": 2, "name": "Ada"}],
    )

    first, second = list(Todo.filter(prefetch=["user"]))

    assert first.user is second.user


def test_reading_shared_prefetched_relation_keeps_its_write_mode(
    requests_mock: Any,
) -> None:
    """One parent's write mode does not change a relation it shares."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "userId": 2, "title": "a", "completed": False},
            {"id": 2, "userId": 2, "title": "b", "completed": True},
        ],
    )
    requests_mock.get(
        "https://example.com/v1/users/", json=[{"id": 2, "name": "Ada"}]
    )
    patch = requests_mock.patch(
        "https://example.com/v1/users/2/", status_code=204
    )

    first, second = list(Todo.filter(prefetch="user"))
    first.user.name = "Grace"
    second.set_write_mode("eager")
    _ = second.user
    first.user.name = "Hopper"

    assert first.user.write_mode == "lazy"
    assert patch.call_count == 0
    first.user.save()
    assert patch.last_request.json() == {"name": "Hopper"}


@pytest.mark.parametrize(
    ("parent_mode", "related_mode"), [("lazy", "eager"), ("eager", "lazy")]
)
@pytest.mark.parametrize("prefetch", [False, True])
def test_prefetch_preserves_related_write_mode(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    parent_mode: WriteMode,
    related_mode: WriteMode,
    prefetch: bool,
) -> None:
    """Prefetch must preserve a relation's inherited persistence behavior.

    Args:
        parent_mode: Default write mode of the API that serves todos.
        related_mode: Default write mode of the API that serves users.
        prefetch: Whether the todos are listed with the ``user`` relation
            prefetched.
    """
    parent_api = API(
        "https://parents.example.com/", default_write_mode=parent_mode
    )
    related_api = API(
        "https://users.example.com/", default_write_mode=related_mode
    )
    monkeypatch.setitem(_api_registry, Todo, parent_api)
    monkeypatch.setitem(_api_registry, User, related_api)
    requests_mock.get(
        "https://parents.example.com/todos/",
        json=[{"id": 1, "userId": 2, "title": "a", "completed": False}],
    )
    requests_mock.get(
        "https://users.example.com/users/", json=[{"id": 2, "name": "Ada"}]
    )
    requests_mock.get(
        "https://users.example.com/users/2/", json={"id": 2, "name": "Ada"}
    )
    patch = requests_mock.patch(
        "https://users.example.com/users/2/", status_code=204
    )

    todo = next(Todo.filter(prefetch="user" if prefetch else None))
    todo.user.name = "Grace"

    assert todo.user.write_mode == parent_mode
    assert todo.user.api is related_api
    assert patch.call_count == (1 if parent_mode == "eager" else 0)
    todo.user.save()
    assert patch.call_count == 1
    assert patch.last_request.json() == {"name": "Grace"}


def test_prefetches_related_collections_via_bulk_get(
    requests_mock: Any,
) -> None:
    """Collection relations should also use bulk prefetch when requested."""
    requests_mock.get(
        "https://example.com/v1/teams/",
        json=[{"id": 1, "members": [2, 3]}],
    )
    users = requests_mock.get(
        "https://example.com/v1/users/",
        json=[
            {"id": 2, "name": "Ada"},
            {"id": 3, "name": "Grace"},
        ],
    )

    team = next(iter(Team.filter(prefetch=["members"])))
    members = team.members

    assert isinstance(members, tuple)
    assert requests_mock.call_count == 2
    assert [member.name for member in members] == ["Ada", "Grace"]
    assert users.last_request.qs == {"id__in": ["2,3"]}


def test_prefetches_scalar_members_from_mixed_related_collection(
    requests_mock: Any,
) -> None:
    """Mixed embedded/scalar collections should still prefetch scalars."""
    requests_mock.get(
        "https://example.com/v1/teams/",
        json=[{"id": 1, "members": [{"id": 2, "name": "Ada"}, 3]}],
    )
    users = requests_mock.get(
        "https://example.com/v1/users/",
        json=[{"id": 3, "name": "Grace"}],
    )

    team = next(iter(Team.filter(prefetch=["members"])))

    assert [member.name for member in team.members] == ["Ada", "Grace"]
    assert requests_mock.call_count == 2
    assert users.last_request.qs == {"id__in": ["3"]}


@pytest.mark.parametrize(
    ("members", "expected_names", "expected_ids"),
    [
        ([{"id": 3, "name": "Grace"}], ["Grace"], []),
        (
            [2, {"id": 3, "name": "Grace"}, 2],
            ["Ada", "Grace", "Ada"],
            ["2"],
        ),
    ],
)
def test_prefetch_skips_embedded_collection_members(
    requests_mock: Any,
    members: list[Any],
    expected_names: list[str],
    expected_ids: list[str],
) -> None:
    """Embedded members need no primary-key lookup.

    Args:
        members: JSON value of the team's ``members`` collection.
        expected_names: Names of the loaded members.
        expected_ids: Primary keys that the prefetch request should ask for, as
            query-string values.
    """

    @json_object
    class MaybeTeam(ModelBase):
        """Model with a related collection that embeds some members."""

        class_url = "/maybe-teams/"

        id: int
        members: list[User]

    requests_mock.get(
        "https://example.com/v1/maybe-teams/",
        json=[{"id": 1, "members": members}],
    )
    users = requests_mock.get(
        "https://example.com/v1/users/", json=[{"id": 2, "name": "Ada"}]
    )

    team = next(MaybeTeam.filter(prefetch="members"))

    assert [member.name for member in team.members] == expected_names
    assert users.call_count == bool(expected_ids)
    if expected_ids:
        assert users.last_request.qs == {"id__in": [",".join(expected_ids)]}
        assert team.members[0] is team.members[-1]
    assert requests_mock.call_count == 1 + bool(expected_ids)


def test_prefetch_requires_explicit_bulk_support(
    requests_mock: Any,
) -> None:
    """Prefetch without bulk support fails before sending the request."""
    requests_mock.get("https://example.com/v1/nobulk-todos/", json=[])

    with pytest.raises(PrefetchNotSupported, match="bulk_get_by_pks"):
        NoBulkTodo.filter(prefetch=["user"])

    assert requests_mock.call_count == 0


def test_get_raises_multiple_objects_returned(requests_mock: Any) -> None:
    """``get()`` should raise when more than one object matches."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "userId": 2, "title": "a", "completed": False},
            {"id": 2, "userId": 2, "title": "b", "completed": False},
        ],
    )

    match = r"^More than one Todo matching userId exists$"
    with pytest.raises(MultipleObjectsReturned, match=match):
        Todo.get(userId=2)
    with pytest.raises(EazyrestError, match=match):
        Todo.get_or_create(userId=2)


def test_repr_shows_primary_key_without_a_request(
    requests_mock: Any,
) -> None:
    """``repr()`` names the class and key and never loads the object."""
    key = uuid.UUID(int=1)

    assert repr(User(id=1)) == "<User id=1>"
    assert repr(User(json={"id": 2, "name": "Ada"})) == "<User id=2>"
    assert repr(UUIDUser(key=key)) == f"<UUIDUser key={str(key)!r}>"
    assert repr(UUIDUser(json={"ID": str(key)})) == repr(UUIDUser(key=key))
    assert repr(ModelBase()) == "<ModelBase>"
    assert requests_mock.call_count == 0


def test_reading_lazy_primary_key_sends_no_request(requests_mock: Any) -> None:
    """A lazy object's primary key and URL are known without a ``GET``."""
    user = User(id=1)

    assert user.id == 1
    assert user.pk == 1
    assert user.url == "/users/1/"
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "body",
    [
        {"status_code": 204},
        {"json": {}},
        {"json": {"title": "a"}},
        {"json": {"id": None}},
        {"json": {"id": "5"}},
    ],
)
def test_create_without_a_returned_key_is_refused_with_its_response(
    requests_mock: Any, body: dict[str, Any]
) -> None:
    """A creation whose response names no readable key raises after the POST.

    The server may have made the resource, which only the response names, so
    the error holds the response. ``get_or_create()`` creates the same way.

    Args:
        body: Keyword arguments of the mocked ``POST`` response.
    """
    posted = requests_mock.post("https://example.com/v1/todos/", **body)
    requests_mock.get("https://example.com/v1/todos/", json=[])

    for create in (
        lambda: Todo.create(title="a"),
        lambda: Todo.get_or_create(title="a"),
    ):
        with pytest.raises(
            ResponseShapeError, match=r"created.*Todo\.id"
        ) as e:
            create()
        assert e.value.response is not None
        assert e.value.response.status_code == body.get("status_code", 200)
        assert e.value.response.request.method == "POST"
    assert posted.call_count == 2


@json_object
class TodoNote(ModelBase):
    """Model with a relation to a todo, for relation-write tests."""

    class_url = "/todo-notes/"

    id: int
    todo: Todo


def test_a_collection_item_without_its_key_is_refused(
    requests_mock: Any,
) -> None:
    """An item whose JSON lacks the key field names no object.

    The server's serializer may omit the key, such as one that names each
    object by its URL, and then the query raises, naming the item.
    """
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"url": "https://example.com/v1/todos/1/", "title": "t"}],
    )

    with pytest.raises(ResponseShapeError, match=r"Item \[0\].*Todo\.id"):
        next(Todo.filter())
    assert [r.method for r in requests_mock.request_history] == ["GET"]


def test_refresh_refuses_before_it_discards_pending_changes(
    requests_mock: Any,
) -> None:
    """An object that cannot reload keeps its pending changes."""
    obj = TextKeyed(json={"id": ".", "name": "a"})
    obj.name = "new"

    with pytest.raises(ValueError, match="so it has no URL"):
        obj.refresh()

    assert obj._pending_updates == {"name": "new"}
    assert obj.name == "new"
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("key", ["", ".", ".."])
def test_refresh_refuses_an_object_that_has_no_url(
    requests_mock: Any, key: str
) -> None:
    """An object whose key has no URL is refused.

    Args:
        key: Primary key that the object is built with.
    """
    obj = TextKeyed(id=key)

    with pytest.raises(ValueError, match="so it has no URL"):
        obj.refresh()
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_constructor_field_arguments_are_assignments(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """Fields other than the key are assigned after the key is set.

    Args:
        write_mode: Write mode of the user.
    """
    get = requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": 1, "name": "Ada"}
    )
    patch = requests_mock.patch(
        "https://example.com/v1/users/1/", status_code=204
    )

    user = User(id=1, name="Grace", write_mode=write_mode)

    assert get.call_count == 1
    assert patch.call_count == (1 if write_mode == "eager" else 0)
    user.save()
    assert patch.last_request.json() == {"name": "Grace"}


def test_lazy_write_mode_batches_updates_until_save(
    requests_mock: Any,
) -> None:
    """Lazy mode should queue changes and send them on ``save()``."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 3, "title": "new", "completed": False},
    )

    todo = Todo(id=1)
    todo.title = "new"
    todo.user = User(id=3)

    assert patch.call_count == 0
    assert todo.title == "new"
    assert todo.user.pk == 3

    todo.save()

    assert patch.called
    assert patch.last_request.json() == {"title": "new", "userId": 3}


def _saved_user(state: str, write_mode: WriteMode) -> User:
    """Return the user 1 in a loading state.

    Args:
        state: ``"unloaded"`` for a user known by its key, ``"loaded"`` for one
            built from its JSON, or ``"refreshed"`` for a loaded user that has
            discarded its JSON.
        write_mode: Write mode of the user.

    Returns:
        The user, with its key 1.
    """
    if state == "unloaded":
        return User(id=1, write_mode=write_mode)
    user = User(json={"id": 1, "name": "Ada"}, write_mode=write_mode)
    if state == "refreshed":
        user.refresh()
    return user


@pytest.mark.parametrize("how", ["field", "pk"])
@pytest.mark.parametrize("state", ["unloaded", "loaded", "refreshed"])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_assigning_another_key_to_an_object_that_has_its_key_is_refused(
    requests_mock: Any, write_mode: WriteMode, state: str, how: str
) -> None:
    """The key of a saved object cannot change, and nothing is sent.

    The key, the hash, the URL, and the pending updates stay as they were.

    Args:
        write_mode: Write mode of the user.
        state: Loading state of the user.
        how: Whether the key is assigned as the field or as ``pk``.
    """
    user = _saved_user(state, write_mode)
    hashed = hash(user)
    if state == "loaded" and write_mode == "lazy":
        user.name = "Grace"
    before = copy.deepcopy(user._json)
    pending = copy.deepcopy(user._pending_updates)
    requests_mock.reset_mock()

    name = "id" if how == "field" else "pk"
    with pytest.raises(AttributeError, match=r"key .* cannot change"):
        setattr(user, name, 2)

    assert requests_mock.call_count == 0
    assert user.pk == 1
    assert user.url == "/users/1/"
    assert hash(user) == hashed
    assert user == User(id=1)
    assert user._pending_updates == pending
    assert user._json == before


@pytest.mark.parametrize("how", ["field", "pk"])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_assigning_the_key_that_an_object_has_sends_nothing(
    requests_mock: Any, write_mode: WriteMode, how: str
) -> None:
    """A key that names the object's own key is not a change.

    Args:
        write_mode: Write mode of the user.
        how: Whether the key is assigned as the field or as ``pk``.
    """
    user = User(json={"id": 1, "name": "Ada"}, write_mode=write_mode)
    hashed = hash(user)

    if how == "field":
        user.id = 1
    else:
        user.pk = 1
    user.save()

    assert requests_mock.call_count == 0
    assert user.pk == 1
    assert hash(user) == hashed
    assert user._pending_updates == {}


def test_refresh_discards_pending_updates_and_reloads(
    requests_mock: Any,
) -> None:
    """``refresh()`` should drop unsaved changes and reload server state."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        [
            {
                "json": {
                    "id": 1,
                    "userId": 2,
                    "title": "old",
                    "completed": False,
                }
            },
            {
                "json": {
                    "id": 1,
                    "userId": 2,
                    "title": "server",
                    "completed": False,
                }
            },
        ],
    )

    todo = Todo(id=1)
    assert todo.title == "old"

    todo.title = "local"
    todo.refresh()

    assert todo.title == "server"


def test_eager_write_mode_patches_immediately(requests_mock: Any) -> None:
    """Eager mode should PATCH as soon as a field changes."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "new", "completed": False},
    )

    todo = Todo(id=1, write_mode="eager")
    todo.title = "new"

    assert patch.called
    assert patch.last_request.json() == {"title": "new"}


def test_eager_assignment_sends_value_pending_from_in_place_change(
    requests_mock: Any,
) -> None:
    """An eager assignment equal to an unsent in-place change is sent."""
    api = API("https://example.com/v3/")

    @json_object
    class Doc(JSONObject):
        """Model whose second field a partial object lacks."""

        class_url = "/docs/"

        id: int
        meta: dict[str, int]
        counts: dict[str, int]

    Doc.register_api(api)
    requests_mock.get(
        "https://example.com/v3/docs/1/",
        json={"id": 1, "meta": {}, "counts": {}},
    )
    patch = requests_mock.patch(
        "https://example.com/v3/docs/1/",
        json={"id": 1, "meta": {"": 0}, "counts": {}},
    )

    doc = Doc(id=1)
    doc.update_from_json({"id": 1, "meta": {}})
    doc.meta[""] = 0
    doc.set_write_mode("eager")
    # Reading a field the object lacks loads it and queues the change.
    assert doc.counts == {}
    assert not patch.called

    doc.meta = {"": 0}

    assert patch.call_count == 1
    assert patch.last_request.json() == {"meta": {"": 0}}


def test_eager_assignment_sends_value_queued_in_lazy_mode(
    requests_mock: Any,
) -> None:
    """An eager assignment equal to an update queued in lazy mode is sent."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "new", "completed": False},
    )

    todo = Todo(id=1)
    todo.title = "new"
    todo.set_write_mode("eager")
    assert not patch.called

    todo.title = "new"

    assert patch.call_count == 1
    assert patch.last_request.json() == {"title": "new"}


def test_lazy_assignment_equal_to_stored_value_queues_nothing(
    requests_mock: Any,
) -> None:
    """A lazy assignment equal to the stored value is not a change."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )

    todo = Todo(id=1)
    todo.title = "old"

    assert todo._pending_updates == {}


def test_api_default_write_mode_is_used_for_new_objects(
    requests_mock: Any,
) -> None:
    """Objects should inherit eager writes from their API by default."""
    api = API("https://example.com/v2/", default_write_mode="eager")

    @json_object
    class EagerTodo(JSONObject):
        """Model bound to an API with eager writes by default."""

        class_url = "/todos/"

        id: int
        title: str

    EagerTodo.register_api(api)

    requests_mock.get(
        "https://example.com/v2/todos/1/",
        json={"id": 1, "title": "old"},
    )
    patch = requests_mock.patch(
        "https://example.com/v2/todos/1/",
        json={"id": 1, "title": "new"},
    )

    todo = EagerTodo(id=1)
    todo.title = "new"

    assert patch.called
    assert patch.last_request.json() == {"title": "new"}


def test_object_write_mode_overrides_api_default(
    requests_mock: Any,
) -> None:
    """Per-object write mode should take precedence over the API default."""
    api = API("https://example.com/v3/", default_write_mode="eager")

    @json_object
    class OverrideTodo(JSONObject):
        """Model used to verify object-level write-mode overrides."""

        class_url = "/todos/"

        id: int
        title: str

    OverrideTodo.register_api(api)

    requests_mock.get(
        "https://example.com/v3/todos/1/",
        json={"id": 1, "title": "old"},
    )
    patch = requests_mock.patch(
        "https://example.com/v3/todos/1/",
        json={"id": 1, "title": "new"},
    )

    todo = OverrideTodo(id=1, write_mode="lazy")
    todo.title = "new"

    assert patch.call_count == 0
    todo.save()
    assert patch.called
    assert patch.last_request.json() == {"title": "new"}


def test_invalid_write_mode_raises_value_error() -> None:
    """Unsupported write modes should be rejected."""
    with pytest.raises(ValueError, match="Unsupported write mode"):
        Todo(id=1, write_mode=cast(Any, "later"))


def test_switching_to_eager_flushes_pending_lazy_changes(
    requests_mock: Any,
) -> None:
    """Pending lazy changes should be included in the next eager write."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "new", "completed": True},
    )

    todo = Todo(id=1)
    todo.title = "new"
    todo.set_write_mode("eager")
    todo.completed = True

    assert patch.called
    assert patch.last_request.json() == {"title": "new", "completed": True}


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (False, 0),
        (1, True),
        ({"nested": [False]}, {"nested": [0]}),
    ],
)
def test_assignments_distinguish_booleans_from_numbers(
    requests_mock: Any, write_mode: WriteMode, old: Any, new: Any
) -> None:
    """Assignments must distinguish JSON booleans from numbers.

    Args:
        write_mode: Write mode of the setting.
        old: Value the setting starts with.
        new: Assigned value, equal to ``old`` in Python but of a different JSON
            type.
    """

    @json_object
    class Setting(ModelBase):
        """Model with an arbitrary JSON value."""

        class_url = "/settings/"

        id: int
        value: Any

    patch = requests_mock.patch(
        "https://example.com/v1/settings/1/", status_code=204
    )
    setting = Setting(json={"id": 1, "value": old}, write_mode=write_mode)

    setting.value = new
    setting.save()

    assert patch.call_count == 1
    # Inspect the serialized types as well as their values.
    assert type(patch.last_request.json()["value"]) is type(new)
    if isinstance(new, dict):
        assert type(patch.last_request.json()["value"]["nested"][0]) is int
    assert patch.last_request.json() == {"value": new}
    assert type(setting.value) is type(new)


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_equal_json_assignment_sends_no_patch(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """An equal nested JSON value still leaves the object unchanged.

    Args:
        write_mode: Write mode of the setting.
    """

    @json_object
    class Setting(ModelBase):
        """Model with a nested JSON value."""

        id: int
        value: dict[str, Any]

    value = {"nested": [False, 0, {"enabled": True}]}
    setting = Setting(json={"id": 1, "value": value}, write_mode=write_mode)
    setting.value = deepcopy(value)
    setting.save()

    assert requests_mock.call_count == 0


@json_object
class Profile(ModelBase):
    """Model with JSON values that are not converted."""

    class_url = "/profiles/"

    id: int
    settings: dict[str, Any]
    contacts: list[dict[str, Any]]


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_assigning_value_changed_in_place_sends_patch(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """A value read from a field, changed, and assigned back is saved.

    Args:
        write_mode: Write mode of the profile.
    """
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    profile = Profile(
        json={"id": 1, "settings": {"theme": "light"}, "contacts": [{"n": 1}]},
        write_mode=write_mode,
    )

    settings = profile.settings
    settings["theme"] = "dark"
    profile.settings = settings
    contacts = profile.contacts
    contacts[0]["n"] = 2
    profile.contacts = contacts
    profile.save()

    sent = [request.json() for request in patch.request_history]
    if write_mode == "eager":
        assert sent == [
            {"settings": {"theme": "dark"}},
            {"contacts": [{"n": 2}]},
        ]
    else:
        assert sent == [
            {"settings": {"theme": "dark"}, "contacts": [{"n": 2}]},
        ]
    assert profile.json["settings"] == {"theme": "dark"}


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_value_changed_in_place_is_saved(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """A value read and then changed in place is sent by ``save()``.

    Args:
        write_mode: Write mode of the profile.
    """
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    payload = {"id": 1, "settings": {"theme": "light"}, "contacts": [{"n": 1}]}
    profile = Profile(json=payload, write_mode=write_mode)

    profile.settings["theme"] = "dark"
    profile.contacts[0]["n"] = 2

    # The change is a copy until save() detects it.
    assert profile.json["settings"] == {"theme": "light"}
    assert patch.call_count == 0
    profile.save()
    profile.save()

    assert patch.call_count == 1
    assert patch.last_request.json() == {
        "settings": {"theme": "dark"},
        "contacts": [{"n": 2}],
    }
    assert profile.json["settings"] == {"theme": "dark"}
    assert payload == {
        "id": 1,
        "settings": {"theme": "light"},
        "contacts": [{"n": 1}],
    }


def test_in_place_change_compares_encodings(requests_mock: Any) -> None:
    """Another spelling of an unchanged value is not a change."""
    patch = requests_mock.patch(
        "https://example.com/v1/timelines/1/", status_code=204
    )
    timeline = Timeline(json={"id": 1, "starts_at": ["2026-01-02T03:04:05Z"]})
    later = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)

    assert len(timeline.starts_at) == 1
    timeline.save()
    assert patch.call_count == 0

    timeline.starts_at.append(later)
    timeline.save()

    assert patch.last_request.json() == {
        "starts_at": ["2026-01-02T03:04:05+00:00", later.isoformat()]
    }


def test_eager_assignment_sends_in_place_changes(
    requests_mock: Any,
) -> None:
    """An eager PATCH includes values changed in place."""
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    profile = Profile(
        json={"id": 1, "settings": {}, "contacts": []}, write_mode="eager"
    )

    profile.contacts.append({"n": 1})
    profile.settings = {"theme": "dark"}

    assert patch.last_request.json() == {
        "contacts": [{"n": 1}],
        "settings": {"theme": "dark"},
    }


def test_assignment_and_refresh_discard_in_place_changes(
    requests_mock: Any,
) -> None:
    """An assignment or a refresh wins over a change made in place."""
    requests_mock.get(
        "https://example.com/v1/profiles/1/",
        json={"id": 1, "settings": {}, "contacts": []},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    profile = Profile(json={"id": 1, "settings": {}, "contacts": []})

    profile.contacts.append({"n": 1})
    profile.contacts = []
    profile.settings["theme"] = "dark"
    profile.refresh()
    profile.save()

    assert patch.call_count == 0
    assert profile.contacts == []
    assert profile.settings == {}


def test_reload_keeps_in_place_changes(requests_mock: Any) -> None:
    """Loading the rest of a partial object keeps changes made in place."""
    requests_mock.get(
        "https://example.com/v1/profiles/1/",
        json={"id": 1, "settings": {}, "contacts": []},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    profile = Profile(id=1)
    profile.update_from_json({"contacts": []})

    profile.contacts.append({"n": 1})
    assert profile.settings == {}
    profile.save()

    assert profile.contacts == [{"n": 1}]
    assert patch.last_request.json() == {"contacts": [{"n": 1}]}


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_reassigning_value_changed_after_save_sends_patch(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """Saving an assigned value does not keep a reference to it.

    Args:
        write_mode: Write mode of the profile.
    """
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    profile = Profile(
        json={"id": 1, "settings": {}, "contacts": []}, write_mode=write_mode
    )

    settings = {"theme": "dark"}
    profile.settings = settings
    profile.save()
    settings["font"] = "mono"
    profile.settings = settings
    profile.save()

    assert [request.json() for request in patch.request_history] == [
        {"settings": {"theme": "dark"}},
        {"settings": {"theme": "dark", "font": "mono"}},
    ]


def test_update_from_json_copies_nested_payload_values() -> None:
    """A later change to the message does not reach the object."""
    settings = {"theme": "blue"}
    message = {"id": 1, "settings": settings}
    profile = Profile(json={"id": 1, "settings": {}, "contacts": []})

    profile.update_from_json(message)
    settings["theme"] = "red"

    assert profile.settings == {"theme": "blue"}


def test_update_from_json_merges_partial_payload_and_invalidates_cache(
    requests_mock: Any,
) -> None:
    """Incoming JSON should update cached values without a refetch."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )

    todo = Todo(id=1)
    original_user = todo.user

    todo.update_from_json({"title": "new", "userId": 3})

    assert todo.title == "new"
    assert todo.user.pk == 3
    assert todo.user is not original_user
    assert requests_mock.call_count == 1


def test_update_from_json_initializes_unloaded_object_from_partial_payload(
    requests_mock: Any,
) -> None:
    """Partial incoming JSON should bootstrap an object without GET."""
    todo = Todo(id=1)

    todo.update_from_json({"title": "new", "completed": True})

    assert todo.pk == 1
    assert todo.title == "new"
    assert todo.completed is True
    assert requests_mock.call_count == 0


def test_update_from_json_discards_pending_updates_for_incoming_fields(
    requests_mock: Any,
) -> None:
    """Incoming server state should win over queued local values."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "server", "completed": True},
    )

    todo = Todo(id=1)
    todo.title = "local"
    todo.completed = True

    todo.update_from_json({"title": "server"})
    todo.save()

    assert todo.title == "server"
    assert patch.called
    assert patch.last_request.json() == {"completed": True}


def test_update_from_json_rejects_mismatched_primary_key() -> None:
    """Incoming JSON must refer to the same object."""
    todo = Todo(id=1)

    with pytest.raises(ValueError, match="primary key"):
        todo.update_from_json({"id": 2, "title": "new"})


def test_reserved_field_name_is_rejected() -> None:
    """A field must not replace a ``JSONObject`` attribute such as ``url``."""
    with pytest.raises(ValueError, match="field_map"):

        @json_object
        class Hyperlinked(ModelBase):
            """Model with a field that would replace ``JSONObject.url``."""

            class_url = "/hyperlinked/"

            id: int
            url: str


@pytest.mark.parametrize("registered", [False, True])
def test_field_named_api_is_rejected_before_or_after_registration(
    registered: bool,
) -> None:
    """A field named ``api`` raises ``ValueError`` in either order.

    Args:
        registered: Whether an API is registered before the class is decorated.
    """

    class Base(JSONObject):
        """Base class of the model, with or without an API."""

    if registered:
        Base.register_api(API("https://api.example.com/"))

    with pytest.raises(ValueError, match="field_map"):

        @json_object
        class Service(Base):
            """Model with a field that would replace ``JSONObject.api``."""

            class_url = "/services/"

            id: int
            api: str  # type: ignore[assignment, misc]


def test_reserved_json_key_can_be_mapped_to_another_attribute() -> None:
    """A reserved JSON key is usable through ``field_map``."""

    @json_object(field_map={"resource_url": "url"})
    class Hyperlinked(ModelBase):
        """Model that maps the ``url`` JSON key to another attribute."""

        class_url = "/hyperlinked/"

        id: int
        resource_url: str

    obj = Hyperlinked(
        json={"id": 1, "url": "https://example.com/v1/hyperlinked/1/"}
    )

    assert obj.resource_url == "https://example.com/v1/hyperlinked/1/"
    assert obj.url == "/hyperlinked/1/"


def test_postponed_annotations_resolve_local_self_and_nested_types() -> None:
    """A local model resolves both its own name and class-local field types."""

    @json_object
    class Node(JSONObject):
        """Local model with postponed annotations."""

        class Kind(str, enum.Enum):  # noqa: UP042
            """Type declared in the model's own namespace."""

            ROOT = "root"

        id: int
        parent: Node | None
        kind: Kind

    node = Node(json={"id": 1, "parent": None, "kind": "root"})

    assert node.id == 1
    assert node.parent is None
    assert node.kind is Node.Kind.ROOT


def test_type_checking_only_annotations_leave_other_fields_readable() -> None:
    """Only a field's own annotation must resolve when the field is read."""

    @json_object(exclude={"excluded_ratio"})
    class Measurement(ModelBase):
        """Model with annotations that name a type-checking-only import."""

        class_url = "/measurements/"
        default_ratio: ClassVar[Fraction]

        id: int
        name: str
        excluded_ratio: Fraction
        ratio: Fraction

    measurement = Measurement(json={"id": 1, "name": "x", "ratio": "1/2"})

    assert measurement.id == 1
    assert measurement.name == "x"
    with pytest.raises(NameError, match="Fraction"):
        _ = measurement.ratio


@dataclasses.dataclass
class Shadowing:
    """Dataclass whose field is named like the builtin that types it."""

    int: int = 0


def test_a_dataclass_field_named_like_its_type_resolves_to_the_type() -> None:
    """A field's default does not hide the type that its annotation names."""

    @json_object
    class Holder(ModelBase):
        class_url = "/holders/"

        id: int
        shadowing: Shadowing

    holder = Holder(json={"id": 1, "shadowing": {"int": 3}})

    assert holder.shadowing == Shadowing(3)


def test_an_error_resolving_an_annotation_waits_for_the_fields_use() -> None:
    """A ``TypeError`` raised by an annotation's evaluation is not a refusal.

    Decoration refuses a type that eazyrest does not convert. An annotation
    that cannot be evaluated is not one, so the class is defined and the field
    raises what the evaluation raised when it is first read.
    """

    @json_object
    class Broken(ModelBase):
        class_url = "/broken/"

        id: int
        value: 1 + None  # type: ignore[valid-type]

    broken = Broken(json={"id": 1, "value": 2})

    with pytest.raises(TypeError, match="unsupported operand"):
        _ = broken.value
    assert broken.id == 1


def test_class_var_annotations_are_not_fields() -> None:
    """``ClassVar`` annotations declare class attributes, not fields."""

    @json_object
    class Widget(ModelBase):
        """Model with annotated class attributes."""

        class_url: ClassVar[str] = "/widgets/"
        prefetch_batch_size: typing.ClassVar[int] = 5
        kind: ClassVar[str] = "widget"

        id: int

    widget = Widget(json={"id": 1})

    assert Widget._json_fields == {"id"}
    assert widget.url == "/widgets/1/"
    assert Widget.prefetch_batch_size == 5
    assert widget.kind == "widget"


def test_pk_and_exclude_options() -> None:
    """``pk=`` names the key field and ``exclude=`` skips annotations."""

    @json_object(pk="uuid", exclude={"display"})
    class Thing(ModelBase):
        """Model with a non-default primary key and an excluded field."""

        class_url = "/things/"

        uuid: str
        display: str

    thing = Thing(uuid="abc")

    assert thing.pk == "abc"
    assert thing.url == "/things/abc/"
    with pytest.raises(AttributeError):
        thing.display = "label"


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"pk": "uid"}, "pk='uid'"),
        ({"pk": "name", "exclude": {"name"}}, "pk='name'"),
        ({"field_map": {"emial": "emailAddress"}}, "'emial'"),
        ({"field_map": {"label": "Label"}}, "'label'"),
        ({"exclude": {"dispaly"}}, "'dispaly'"),  # codespell:ignore dispaly
    ],
)
def test_json_object_rejects_options_that_name_no_field(
    options: dict[str, Any], match: str
) -> None:
    """A misspelled option is an error, not silently ignored.

    Args:
        options: Keyword arguments to ``json_object()``, one of which names no
            field.
        match: Pattern that the ``ValueError`` message should match.
    """

    class Thing(ModelBase):
        """Model whose decoration fails."""

        class_url = "/things/"
        label: ClassVar[str] = "Thing"

        id: int
        name: str

    with pytest.raises(ValueError, match=match):
        json_object(**options)(Thing)


def test_json_object_rejects_fields_that_share_a_json_key() -> None:
    """Two attributes backed by one JSON key are rejected."""

    class Alias(ModelBase):
        """Model mapping a second attribute to the key of the first."""

        class_url = "/aliases/"

        id: int
        name: str
        label: str

    class Renamed(Org):
        """Subclass mapping a new attribute to an inherited key."""

        title: str

    with pytest.raises(ValueError, match=r"Alias\.name and Alias\.label"):
        json_object(field_map={"label": "name"})(Alias)
    with pytest.raises(ValueError, match=r"Renamed\.name and Renamed\.title"):
        json_object(field_map={"title": "name"})(Renamed)


def test_json_object_accepts_inherited_primary_key() -> None:
    """A subclass may name the primary key that it inherits."""

    @json_object(pk="slug")
    class Department(Org):
        """Subclass that restates its inherited primary key."""

        size: int

    assert Department(slug="core").url == "/orgs/core/"


def test_subclass_keeps_inherited_primary_key(requests_mock: Any) -> None:
    """Without ``pk=``, a subclass keeps the primary key it inherits."""

    @json_object
    class Renamed(Org):
        """Subclass that redeclares its inherited primary key."""

        slug: str

    @json_object
    class Numbered(Org):
        """Subclass that declares a field named ``id``."""

        id: int

    assert Renamed(slug="core").url == "/orgs/core/"
    assert Numbered(slug="core").url == "/orgs/core/"
    assert Numbered._pk.field == "slug"
    assert requests_mock.call_count == 0


def test_subclass_with_another_key_reads_the_old_key_as_a_field(
    requests_mock: Any,
) -> None:
    """A field that is the base class's key is a plain field of the subclass.

    The subclass's change must not change the base class either.
    """

    @json_object
    class Thing(ModelBase):
        """Model keyed by ``id``."""

        class_url = "/things/"

        id: int

    @json_object(pk="slug")
    class Slugged(Thing):
        """Subclass keyed by ``slug``."""

        slug: str

    requests_mock.get(
        "https://example.com/v1/things/abc/", json={"id": 7, "slug": "abc"}
    )

    slugged = Slugged(slug="abc")

    assert slugged.pk == "abc"
    assert requests_mock.call_count == 0
    assert slugged.id == 7
    assert slugged.slug == "abc"
    assert requests_mock.call_count == 1
    assert Thing(id=3).id == 3
    assert Thing._pk is Thing.id
    assert requests_mock.call_count == 1


@json_object
class Dated(ModelBase):
    """Model with a creation time, used to test mixed inheritance."""

    class_url = "/dated/"

    id: int
    created: str


def test_mixed_model_inheritance_is_rejected() -> None:
    """A class cannot inherit fields from two separate models."""
    # The class is rejected when it is created, before any decorator runs.
    with pytest.raises(TypeError, match="both User and Dated"):
        type("Both", (User, Dated), {})


def test_undecorated_mixins_and_chains_are_accepted() -> None:
    """Bases without fields mix in, and decorated chains are allowed."""

    class Hooks(JSONObject):
        """Mixin that declares no fields."""

        prefetch_batch_size = 10

    @json_object
    class Member(User, Hooks):
        """Model with a mixin."""

        role: str

    @json_object
    class Admin(Member):
        """Model two decorated classes below ``User``."""

        level: int

    assert Admin._json_fields == {"id", "name", "role", "level"}


def test_subclass_accepts_inherited_and_own_fields(
    requests_mock: Any,
) -> None:
    """A decorated subclass keeps the fields of its decorated base."""
    patch = requests_mock.patch(
        "https://example.com/v1/users/1/",
        json={"id": 1, "name": "Grace", "role": "owner"},
    )

    by_pk = Admin(pk=1)
    by_id = Admin(id=1)
    admin = Admin(json={"id": 1, "name": "Ada", "role": "staff"})
    admin.name = "Grace"
    admin.role = "owner"
    admin.save()

    assert by_pk.pk == 1
    assert by_id.pk == 1
    assert patch.last_request.json() == {"name": "Grace", "role": "owner"}


def test_update_from_json_invalidates_inherited_fields() -> None:
    """Incoming JSON replaces cached values of inherited fields."""
    admin = Admin(json={"id": 1, "name": "old", "role": "staff"})
    assert admin.name == "old"

    admin.update_from_json({"name": "new"})

    assert admin.name == "new"


def test_create_accepts_string_primary_key_for_related_field(
    requests_mock: Any,
) -> None:
    """Related fields accept primary keys of any type, not only ints."""
    post = requests_mock.post(
        "https://example.com/v1/repos/",
        json={"id": 1, "org": "acme"},
    )

    Repo.create(org="acme")
    by_key = post.last_request.json()
    Repo.create(org=Org(slug="acme"))
    by_object = post.last_request.json()

    assert by_key == {"org": "acme"}
    assert by_object == {"org": "acme"}


def test_related_primary_key_uses_attribute_name(requests_mock: Any) -> None:
    """A related key works when the primary key has a JSON name mapping."""
    requests_mock.get(
        "https://example.com/v1/legacy-users/5/",
        json={"ID": 5, "name": "Ada"},
    )

    todo = LegacyTodo(json={"id": 1, "owner": 5})

    assert todo.owner.pk == 5
    assert todo.owner.name == "Ada"


def test_get_or_create_encodes_lookup_like_create(
    requests_mock: Any,
) -> None:
    """Lookup parameters use the same JSON names and values as creation."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])
    post = requests_mock.post(
        "https://example.com/v1/todos/",
        json={"id": 9, "userId": 2, "title": "t", "completed": False},
    )

    obj, created = Todo.get_or_create(
        user=User(id=2),
        title="t",
        defaults={"completed": False},
    )

    assert created
    assert obj.pk == 9
    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "userId": ["2"],
        "title": ["t"],
    }
    assert post.last_request.json() == {
        "userId": 2,
        "title": "t",
        "completed": False,
    }


@pytest.mark.parametrize(
    ("query", "url", "expected"),
    [
        (
            lambda: Todo.filter(user=User(id=2), ordering="-id"),
            "https://example.com/v1/todos/",
            {"userId": ["2"], "ordering": ["-id"]},
        ),
        (
            lambda: Event.filter(
                starts_at=datetime.datetime(
                    2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC
                )
            ),
            "https://example.com/v1/events/",
            {"starts_at": ["2026-01-02T03:04:05+00:00"]},
        ),
        (
            lambda: Issue.filter(status=IssueStatus.OPEN),
            "https://example.com/v1/issues/",
            {"status": ["open"]},
        ),
        (
            lambda: Team.filter(members=User(id=3)),
            "https://example.com/v1/teams/",
            {"members": ["3"]},
        ),
        (
            lambda: Timeline.filter(starts_at="2026-01-02"),
            "https://example.com/v1/timelines/",
            {"starts_at": ["2026-01-02"]},
        ),
        (
            lambda: Readings.filter(values=[1, 2]),
            "https://example.com/v1/readings/",
            {"values": ["1", "2"]},
        ),
        (
            lambda: Todo.filter(completed=False, archived=True),
            "https://example.com/v1/todos/",
            {"completed": ["false"], "archived": ["True"]},
        ),
    ],
)
def test_filter_encodes_parameters_like_create(
    requests_mock: Any,
    query: Any,
    url: str,
    expected: dict[str, list[str]],
) -> None:
    """Query parameters for fields use the JSON names and values.

    Args:
        query: Callable that runs the query under test.
        url: URL that the query requests.
        expected: Query parameters that the request should carry, as
            ``parse_qs`` returns them.
    """
    lookup = requests_mock.get(url, json=[])

    list(query())

    assert parse_qs(urlparse(lookup.last_request.url).query) == expected


def test_get_encodes_parameters_like_create(requests_mock: Any) -> None:
    """``get()`` encodes its query parameters as ``filter()`` does."""
    lookup = requests_mock.get(
        "https://example.com/v1/todos/",
        json=[{"id": 1, "userId": 2, "title": "t", "completed": False}],
    )

    Todo.get(user=User(id=2))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "userId": ["2"]
    }


@pytest.mark.parametrize(
    "query",
    [
        lambda: MaybeTodo.filter(user=None),
        lambda: MaybeTodo.get(user=None),
        lambda: MaybeTodo.get_or_create(user=None),
    ],
)
def test_query_rejects_none_for_json_backed_field(
    requests_mock: Any, query: Any
) -> None:
    """A ``None`` field value is not silently dropped from the query.

    Args:
        query: Callable that runs a query constraining a field to ``None``.
    """
    with pytest.raises(ValueError, match="parameter user for"):
        query()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("query", "match"),
    [
        (lambda: Todo.filter(userId=None), "parameter userId for"),
        (lambda: Todo.get_or_create(userId=None), "parameter userId for"),
        (lambda: Team.filter(members=[]), "parameter members for"),
        (lambda: SetTeam.get(members=set()), "empty collection"),
        (lambda: Team.get_or_create(members=()), "empty collection"),
    ],
)
def test_query_rejects_unsendable_field_constraints(
    requests_mock: Any, query: Any, match: str
) -> None:
    """A JSON-key alias or an empty collection is not silently dropped.

    Args:
        query: Callable that runs a query with a constraint that cannot be
            sent.
        match: Pattern that the ``ValueError`` message should match.
    """
    with pytest.raises(ValueError, match=match):
        query()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("query", "match"),
    [
        (lambda: Profile.filter(settings={"theme": "dark"}), "is a mapping"),
        (lambda: Profile.get(contacts={"name": "a"}), "is a mapping"),
        (
            lambda: Profile.filter(contacts=[{"name": "a"}]),
            "contains a collection",
        ),
        (lambda: Readings.filter(values=[None]), "contains None"),
        (lambda: Readings.get_or_create(values=[1, None]), "contains None"),
        (lambda: Readings.get(rows=[[1, 2]]), "contains a collection"),
    ],
)
def test_query_rejects_values_requests_would_change(
    requests_mock: Any, query: Any, match: str
) -> None:
    """A constraint that requests would drop or flatten is not sent.

    requests sends a mapping as its keys, omits a ``None`` item, and flattens a
    nested collection, so each of these queries would match other objects.

    Args:
        query: Callable that runs a query with a constraint that requests would
            change.
        match: Pattern that the ``ValueError`` message should match.
    """
    with pytest.raises(ValueError, match=match):
        query()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_collection_field_reads_and_writes_none(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """A collection field that is not optional still writes ``None``.

    Args:
        write_mode: Write mode of the object that is assigned.
    """
    patch = requests_mock.patch(
        "https://example.com/v1/timelines/1/",
        json={"id": 1, "starts_at": None},
    )
    assert Timeline(json={"id": 1, "starts_at": None}).starts_at is None
    timeline = Timeline(json={"id": 1, "starts_at": []})
    timeline.set_write_mode(write_mode)

    # The annotation rejects None statically, so assign it dynamically to
    # check the run-time behavior.
    setattr(timeline, "starts_at", None)  # noqa: B010
    timeline.save()

    assert patch.last_request.json() == {"starts_at": None}
    assert timeline.starts_at is None


@json_object
class Secretive(ModelBase):
    """Model whose fields decode with eazyrest's own codecs."""

    class_url = "/secretive/"

    id: int
    when: datetime.datetime
    day: datetime.date
    length: datetime.timedelta
    status: IssueStatus
    key: uuid.UUID
    at: datetime.time


SECRET = "TOP-SECRET-1234"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("when", SECRET, "not in ISO 8601 format"),
        ("when", 1e300, "out of range"),
        ("day", SECRET, "not in ISO 8601 format"),
        ("day", "2024-13-01", "Date has a year, month, or day out of range"),
        ("length", SECRET, "not in a supported format"),
        ("length", f"P{SECRET}", "not in ISO 8601 format"),
        ("status", SECRET, "not a valid IssueStatus"),
        ("key", "secretsecretsecretsecretsecretab", "UUID is not valid"),
        ("when", 253402300800, "Timestamp is out of range$"),
        ("day", "1987-02-30", "Date has a year, month, or day out of range$"),
        (
            "at",
            "07:61:00",
            "Time has an hour, minute, or second out of range$",
        ),
    ],
)
def test_field_conversion_error_does_not_quote_the_value(
    field: str, value: Any, reason: str
) -> None:
    """A message names the field and the reason but not the value.

    The value can be a secret that the server sent. The error that eazyrest
    replaced is still the cause.

    Args:
        field: Field whose JSON value is invalid.
        value: Invalid JSON value.
        reason: Text that the message gives as the reason.
    """
    obj = Secretive(json={"id": 1, field: value})

    with pytest.raises(FieldConversionError, match=reason) as info:
        getattr(obj, field)

    assert str(info.value).startswith(f"Secretive.{field}: ")
    assert SECRET not in str(info.value)
    assert repr(value) not in str(info.value)


def test_lookup_errors_name_parameters_without_values(
    requests_mock: Any,
) -> None:
    """``get()`` names the lookup parameters but not their values."""
    requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(DoesNotExist) as info:
        Todo.get(title="a", password=SECRET)

    assert str(info.value) == "No Todo matching title, password exists"


def test_query_error_names_parameter_without_value(
    requests_mock: Any,
) -> None:
    """A query value that cannot be sent is not quoted."""
    with pytest.raises(ValueError, match="parameter settings for") as info:
        Profile.filter(settings={"token": SECRET})

    assert SECRET not in str(info.value)
    assert requests_mock.call_count == 0


def test_named_type_aliases_convert_as_their_types() -> None:
    """A field typed by a named alias converts as the type it names.

    This holds for an alias with type parameters, and for one inside another.
    """
    when = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    timeline = AliasedTimeline(
        json={
            "id": 1,
            "starts_at": ["2024-01-01T00:00:00Z"],
            "ends_at": [1704067200],
            "paused_at": None,
        }
    )

    assert timeline.starts_at == [when]
    assert timeline.ends_at == [when]
    assert timeline.paused_at is None
    timeline.paused_at = [when]
    assert timeline.json["paused_at"] == ["2024-01-01T00:00:00+00:00"]


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="The type statement needs Python 3.12"
)
@pytest.mark.parametrize(
    ("statement", "match"),
    [
        ("type Alias = list[Alias] | int", "refers to itself"),
        ("type Alias[T] = list[T]", "needs one type argument"),
    ],
)
def test_unsupported_type_aliases_raise(statement: str, match: str) -> None:
    """A self-referring alias, or a generic one without arguments, raises.

    The class is refused when it is decorated.

    Args:
        statement: ``type`` statement that defines ``Alias``.
        match: Pattern that the error message should match.
    """
    namespace: dict[str, Any] = {}
    exec(statement, namespace)  # noqa: S102

    with pytest.raises(TypeError, match=match) as refused:
        json_object(
            type(
                "Aliased",
                (ModelBase,),
                {
                    "__annotations__": {
                        "id": int,
                        "value": namespace["Alias"],
                    },
                    "class_url": "/aliased/",
                },
            )
        )

    assert not isinstance(refused.value, FieldConversionError)
    assert str(refused.value).startswith("Aliased.value: ")


def test_quoted_reference_in_alias_resolves_in_its_module() -> None:
    """A string inside an alias's value names a type, not a string.

    Without resolution, ``list["datetime.datetime"]`` would read its items
    as unconverted strings.
    """

    @json_object
    class Quoted(ModelBase):
        class_url = "/quoted/"

        id: int
        dates: QuotedDates

    quoted = Quoted(json={"id": 1, "dates": ["2024-01-01T00:00:00Z"]})

    assert quoted.dates == [datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)]


def test_recursive_quoted_alias_is_refused_when_decorated() -> None:
    """A quoted self-reference in an alias raises when the class is decorated.

    The message names the field, and the reason says that the alias refers to
    itself.
    """
    with pytest.raises(TypeError, match="refers to itself") as refused:
        json_object(
            type(
                "Aliased",
                (ModelBase,),
                {
                    "__annotations__": {"id": int, "value": QuotedRecursive},
                    "class_url": "/aliased/",
                },
            )
        )

    assert not isinstance(refused.value, FieldConversionError)
    assert str(refused.value).startswith("Aliased.value: ")


def test_unknown_name_in_quoted_alias_raises_when_first_used() -> None:
    """A quoted name that nothing defines raises when the field is used."""
    model: Any = json_object(
        type(
            "Aliased",
            (ModelBase,),
            {
                "__annotations__": {"id": int, "value": QuotedMissing},
                "class_url": "/aliased/",
            },
        )
    )

    with pytest.raises(FieldConversionError, match="cannot be resolved"):
        _ = model(json={"id": 1, "value": ["bad"]}).value


def test_string_metadata_of_a_quoted_alias_is_not_a_reference() -> None:
    """A string in ``Annotated`` metadata is kept, not resolved as a name."""

    @json_object
    class Noted(ModelBase):
        class_url = "/noted/"

        id: int
        dates: NotedQuotedDates

    noted = Noted(json={"id": 1, "dates": ["2024-01-01T00:00:00Z"]})

    assert noted.dates == [datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)]


def test_collection_field_values_must_be_collections(
    requests_mock: Any,
) -> None:
    """A field value is not converted as one item of a collection."""
    profile = Profile(json={"id": 1, "settings": {}, "contacts": []})
    timeline = Timeline(json={"id": 1, "starts_at": []})
    when = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)

    # The annotations reject these values statically, so assign them
    # dynamically to check the run-time behavior.
    with pytest.raises(TypeError, match="must be collections, got dict"):
        setattr(profile, "contacts", {"n": 1})  # noqa: B010
    with pytest.raises(TypeError, match="must be collections, got datetime"):
        setattr(timeline, "starts_at", when)  # noqa: B010
    with pytest.raises(TypeError, match="must be collections, got User"):
        Team.create(members=User(id=2))

    assert profile.contacts == []
    assert profile._pending_updates == {}
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "query",
    [
        lambda: Todo.filter(ordering=None),
        lambda: Todo.get(title__iexact=None),
        lambda: Todo.get_or_create(title="a", owner__in=iter(())),
    ],
)
def test_query_rejects_unsendable_other_parameters(
    requests_mock: Any, query: Any
) -> None:
    """A parameter that names no field is checked as a field's is.

    requests would omit it, so ``get()`` could return an object that the
    constraint excludes.

    Args:
        query: Callable that runs a query with an unsendable parameter.
    """
    with pytest.raises(ValueError, match="cannot be sent"):
        query()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: iter(()), "empty collection"),
        (deque, "empty collection"),
        (lambda: range(0), "empty collection"),
        (lambda: {}.keys(), "empty collection"),
        (lambda: iter([None]), "contains None"),
        (lambda: deque([1, None]), "contains None"),
        (lambda: [iter([1])], "contains a collection"),
        (lambda: (deque([1]),), "contains a collection"),
    ],
)
@pytest.mark.parametrize("name", ["userId__in", "ordering"])
@pytest.mark.parametrize("method", ["filter", "get", "get_or_create"])
def test_query_rejects_unsendable_iterables(
    requests_mock: Any, make: Any, match: str, name: str, method: str
) -> None:
    """Any iterable is checked as a collection, whatever its type.

    requests iterates every value that is not a string or bytes, so an empty
    iterator or ``deque`` would be omitted and ``get()`` would return an
    unrelated object.

    Args:
        make: Callable that returns a fresh iterable query value.
        match: Pattern that the ``ValueError`` message should match.
        name: Parameter name, a lookup of several values under a JSON key or a
            name that is not a field.
        method: Name of the query method.
    """
    requests_mock.get("https://example.com/v1/todos/", json=[{"id": 99}])

    # filter() sends its request, and so checks its parameters, when called.
    with pytest.raises(ValueError, match=match):
        getattr(Todo, method)(**{name: make()})

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "make",
    [
        lambda: iter([1, 2]),
        lambda: deque([1, 2]),
        lambda: range(1, 3),
        lambda: iter(()),
        deque,
    ],
)
@pytest.mark.parametrize("name", ["user", "userId", "userId__exact"])
def test_query_refuses_an_iterable_for_the_json_name_of_a_relation(
    requests_mock: Any, make: Any, name: str
) -> None:
    """A relation compares one value, by its JSON name as by its own.

    requests would send the name repeated, and a server applies only one of the
    values.

    Args:
        make: Callable that returns a fresh iterable.
        name: Name of the relation, by its JSON name or its own.
    """
    requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(ValueError, match="compares one value"):
        list(Todo.filter(**{name: make()}))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "make", [lambda: iter([1, 2]), lambda: deque([1, 2]), lambda: range(1, 3)]
)
def test_query_sends_every_item_of_an_iterable(
    requests_mock: Any, make: Any
) -> None:
    """An iterable query value is read once and sent as its items.

    A name that is no field's repeats, and a lookup of several values joins
    them.

    Args:
        make: Callable that returns a fresh iterable of two items.
    """
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    list(Todo.filter(ordering=make(), id__in=make()))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "ordering": ["1", "2"],
        "id__in": ["1,2"],
    }


@pytest.mark.parametrize(
    "make",
    [
        lambda: (n for n in [1, 2]),
        lambda: deque([1, 2]),
        lambda: range(1, 3),
    ],
)
def test_query_refuses_an_iterable_for_a_field_that_compares_one_value(
    requests_mock: Any, make: Any
) -> None:
    """A parameter that compares one value takes no collection of them.

    Args:
        make: Callable that returns a fresh iterable of two items.
    """
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(ValueError, match="parameter id for Todo"):
        list(Todo.filter(id=make()))
    with pytest.raises(ValueError, match="parameter title for Todo"):
        list(Todo.filter(title=(str(n) for n in make())))

    assert lookup.call_count == 0


@pytest.mark.parametrize(
    "make",
    [
        lambda: {1, 2},
        lambda: frozenset({1, 2}),
        lambda: {1: "a", 2: "b"}.keys(),
        lambda: {"a": 1, "b": 2}.values(),
    ],
)
def test_query_repeats_a_set_or_view_for_a_name_that_is_no_field(
    requests_mock: Any, make: Any
) -> None:
    """A set or a dictionary view repeats a parameter that names no field.

    Args:
        make: Callable that returns a fresh set or view of two integers.
    """
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    list(Todo.filter(ordering=make()))

    sent = parse_qs(urlparse(lookup.last_request.url).query)
    assert sorted(sent["ordering"]) == ["1", "2"]


@pytest.mark.parametrize(
    "make",
    [
        lambda: {1, 2},
        lambda: frozenset({1, 2}),
        lambda: {1: "a", 2: "b"}.keys(),
        lambda: {"a": 1, "b": 2}.values(),
    ],
)
def test_query_refuses_a_set_or_view_for_a_field_that_compares_one_value(
    requests_mock: Any, make: Any
) -> None:
    """A set or a dictionary view is a collection, as a list is.

    Args:
        make: Callable that returns a fresh set or view of two integers.
    """
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(ValueError, match="parameter id for Todo"):
        list(Todo.filter(id=make()))

    assert lookup.call_count == 0


@pytest.mark.parametrize(
    ("make", "match", "collection"),
    [
        (lambda: iter(()), "empty collection", True),
        (deque, "empty collection", True),
        (lambda: range(0), "empty collection", True),
        (lambda: None, "is None", False),
        (dict, "is a mapping", False),
        (lambda: {"a": 1}, "is a mapping", False),
        (lambda: iter([None]), "contains None", True),
        (lambda: [[1]], "contains a collection", True),
        (lambda: ({"a": 1},), "contains a collection", True),
    ],
)
@pytest.mark.parametrize("name", ["id", "title", "ordering"])
@pytest.mark.parametrize("method", ["filter", "get", "get_or_create"])
def test_query_rejects_unsendable_values_for_a_field(
    requests_mock: Any,
    make: Any,
    match: str,
    collection: bool,
    name: str,
    method: str,
) -> None:
    """A field's parameter is refused as one that names no field is.

    A collection for a field that compares one value is refused as a
    collection, whatever its items are.

    Args:
        make: Callable that returns a fresh query value.
        match: Pattern that the ``ValueError`` message should match for a name
            that is no field.
        collection: Whether the value is a collection.
        name: Name of a field, or of a parameter that is no field.
        method: Name of the query method.
    """
    requests_mock.get("https://example.com/v1/todos/", json=[{"id": 99}])
    if collection and name != "ordering":
        match = "compares one value"

    with pytest.raises(ValueError, match=match):
        getattr(Todo, method)(**{name: make()})

    assert requests_mock.call_count == 0


class QueryCode:
    """Iterable class that a conversion encodes as one value."""

    def __iter__(self) -> Iterator[int]:
        """Return the iterator over two integers."""
        yield 9
        yield 10


@json_object
class Coded(ModelBase):
    """Model with a field of an iterable class that has a conversion."""

    class_url = "/coded/"
    conversions: ClassVar[dict[type, Conversion]] = {
        QueryCode: Conversion(
            decode=lambda value, cls: QueryCode(),
            encode=lambda value, cls: "code",
        )
    }

    id: int
    code: QueryCode


def test_query_sends_an_iterable_that_a_conversion_encodes_as_one_value(
    requests_mock: Any,
) -> None:
    """A registered iterable class is one operand, not its items."""
    lookup = requests_mock.get("https://example.com/v1/coded/", json=[])

    list(Coded.filter(code=QueryCode(), ordering=QueryCode()))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "code": ["code"],
        "ordering": ["9", "10"],
    }


def test_get_or_create_does_not_read_an_iterable_that_a_conversion_encodes(
    requests_mock: Any,
) -> None:
    """The lookup and the creation both use the value's conversion."""
    lookup = requests_mock.get("https://example.com/v1/coded/", json=[])
    post = requests_mock.post("https://example.com/v1/coded/", json={"id": 1})

    Coded.get_or_create(id=1, code=QueryCode())

    assert parse_qs(urlparse(lookup.last_request.url).query)["code"] == [
        "code"
    ]
    assert post.last_request.json() == {"id": 1, "code": "code"}


class QueryPoint(typing.NamedTuple):
    """A ``NamedTuple`` that a field holds as a whole value."""

    x: int
    y: int


class QueryPair:
    """Class whose registered conversion writes a list."""


@json_object
class Placed(ModelBase):
    """Model with fields whose conversions write a list."""

    class_url = "/placed/"
    conversions: ClassVar[dict[type, Conversion]] = {
        QueryPair: Conversion(
            decode=lambda value, cls: QueryPair(),
            encode=lambda value, cls: [1, 2],
        )
    }

    id: int
    point: QueryPoint
    fixed: tuple[int, int]
    pair: QueryPair


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("point", QueryPoint(1, 2)),
        ("fixed", (1, 2)),
        ("pair", QueryPair()),
    ],
)
@pytest.mark.parametrize("method", ["filter", "get", "get_or_create"])
def test_query_refuses_a_whole_value_that_its_conversion_writes_as_a_list(
    requests_mock: Any, name: str, value: Any, method: str
) -> None:
    """A value written as a list would repeat the parameter, so it is refused.

    Args:
        name: Name of a field whose conversion writes a list.
        value: Whole value for the field.
        method: Name of the query method.
    """
    requests_mock.get("https://example.com/v1/placed/", json=[])
    requests_mock.post("https://example.com/v1/placed/", json={"id": 1})

    with pytest.raises(
        ValueError, match="one value that the field's conversion writes"
    ):
        getattr(Placed, method)(**{name: value})

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("name", ["point", "fixed"])
@pytest.mark.parametrize("method", ["filter", "get", "get_or_create"])
def test_query_refuses_a_list_for_a_field_whose_conversion_writes_a_list(
    requests_mock: Any, name: str, method: str
) -> None:
    """No value goes under such a field's own name, not even a list.

    Args:
        name: Name of a ``NamedTuple`` or fixed-length tuple field.
        method: Name of the query method.
    """
    requests_mock.get("https://example.com/v1/placed/", json=[])
    requests_mock.post("https://example.com/v1/placed/", json={"id": 1})

    with pytest.raises(ValueError, match="cannot be sent"):
        getattr(Placed, method)(**{name: [1, 2]})

    assert requests_mock.call_count == 0


def test_query_sends_a_lookup_that_does_not_compare_values_as_given(
    requests_mock: Any,
) -> None:
    """A suffix that compares no value is sent as given, as its ``str()``."""
    lookup = requests_mock.get("https://example.com/v1/placed/", json=[])

    list(Placed.filter(point__iexact="1,2", fixed__icontains="3,4"))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "point__iexact": ["1,2"],
        "fixed__icontains": ["3,4"],
    }


def test_query_refuses_a_collection_of_models_for_a_single_relation(
    requests_mock: Any,
) -> None:
    """A relation to one object compares one key, so it takes no collection."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(ValueError, match="user"):
        list(Todo.filter(user=(User(id=pk) for pk in (2, 3))))

    assert lookup.call_count == 0


def test_get_or_create_sends_none_defaults(requests_mock: Any) -> None:
    """``None`` in ``defaults`` is sent only in the creation payload."""
    requests_mock.get("https://example.com/v1/maybe-todos/", json=[])
    post = requests_mock.post(
        "https://example.com/v1/maybe-todos/", json={"id": 5, "user": None}
    )

    todo, created = MaybeTodo.get_or_create(defaults={"user": None}, id=5)

    assert created
    assert todo.user is None
    assert post.last_request.json() == {"id": 5, "user": None}


def test_get_or_create_reads_a_generator_once(requests_mock: Any) -> None:
    """A generator's items are used for both the lookup and the creation."""
    lookup = requests_mock.get("https://example.com/v1/teams/", json=[])
    post = requests_mock.post(
        "https://example.com/v1/teams/", json={"id": 1, "members": [2, 3]}
    )

    team, created = Team.get_or_create(
        id=1, members=(User(id=pk) for pk in (2, 3))
    )

    assert created
    assert lookup.last_request.qs["members"] == ["2", "3"]
    assert post.last_request.json() == {"id": 1, "members": [2, 3]}
    assert [member.pk for member in team.members] == [2, 3]


def test_get_or_create_sends_lookups_only_in_query(requests_mock: Any) -> None:
    """A ``__`` lookup constrains the query but is not a field value."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])
    post = requests_mock.post(
        "https://example.com/v1/todos/",
        json={"id": 5, "userId": 2, "title": "Docs", "completed": False},
    )

    Todo.get_or_create(title__iexact="docs", title="Docs", user=User(id=2))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "title__iexact": ["docs"],
        "title": ["Docs"],
        "userId": ["2"],
    }
    assert post.last_request.json() == {"title": "Docs", "userId": 2}


def test_get_raises_does_not_exist(requests_mock: Any) -> None:
    """``get()`` should raise when no object matches."""
    requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(DoesNotExist, match=r"^No Todo matching title exists$"):
        Todo.get(title="missing")
    with pytest.raises(EazyrestError, match=r"^No Todo exists$"):
        Todo.get()


def test_delete_sends_delete_request(requests_mock: Any) -> None:
    """``delete()`` should send ``DELETE`` to the object URL."""
    delete = requests_mock.delete(
        "https://example.com/v1/todos/1/", status_code=204
    )

    Todo(id=1).delete()

    assert delete.called


def test_timedelta_field_decodes_and_encodes_iso_8601(
    requests_mock: Any,
) -> None:
    """Durations decode from JSON strings and encode as ISO 8601."""
    patch = requests_mock.patch(
        "https://example.com/v1/tasks/1/",
        json={"id": 1, "duration": "-PT1H"},
    )
    post = requests_mock.post(
        "https://example.com/v1/tasks/",
        json={"id": 2, "duration": "P1DT2H3M4S"},
    )

    task = Task(json={"id": 1, "duration": "1 02:03:04"}, write_mode="eager")
    decoded = task.duration
    task.duration = datetime.timedelta(hours=-1)
    created = Task.create(
        duration=datetime.timedelta(days=1, hours=2, minutes=3, seconds=4)
    )

    assert decoded == datetime.timedelta(days=1, hours=2, minutes=3, seconds=4)
    assert patch.last_request.json() == {"duration": "-PT1H"}
    assert task.duration == datetime.timedelta(hours=-1)
    assert post.last_request.json() == {"duration": "P1DT2H3M4S"}
    assert created.duration == datetime.timedelta(
        days=1, hours=2, minutes=3, seconds=4
    )


def test_update_from_json_partial_payload_loads_missing_fields(
    requests_mock: Any,
) -> None:
    """Reading a field missing from a partial payload loads the object."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "server", "completed": True},
    )

    todo = Todo(id=1)
    todo.update_from_json({"title": "new"})
    title = todo.title
    calls_before_load = requests_mock.call_count
    todo.title = "local"

    assert title == "new"
    assert calls_before_load == 0
    assert todo.completed is True
    assert requests_mock.call_count == 1
    # The unsaved lazy update survives loading the full object.
    assert todo.title == "local"


def test_json_payload_is_copied() -> None:
    """Field assignment does not modify the caller's JSON mapping."""
    payload = {"id": 1, "name": "Ada"}

    user = User(json=payload)
    user.name = "Grace"

    assert payload == {"id": 1, "name": "Ada"}
    assert user.name == "Grace"


def test_nested_json_payload_is_copied(requests_mock: Any) -> None:
    """A later change to a nested payload value does not reach the object."""
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    settings = {"theme": "light"}
    payload = {"id": 1, "settings": settings, "contacts": []}
    profile = Profile(json=payload)

    settings["theme"] = "dark"
    assert profile.settings == {"theme": "light"}
    profile.settings = settings
    profile.save()

    assert patch.last_request.json() == {"settings": {"theme": "dark"}}


def test_embedded_related_object_does_not_share_parent_json() -> None:
    """Unsaved writes to an embedded object do not change its parent."""
    todo = Todo(
        json={
            "id": 1,
            "userId": {"id": 2, "name": "Ada"},
            "title": "a",
            "completed": False,
        }
    )

    todo.user.name = "Grace"

    assert todo.json["userId"] == {"id": 2, "name": "Ada"}


def test_related_object_uses_its_own_registered_api(
    requests_mock: Any,
) -> None:
    """A related model registered on another API loads from that API."""
    requests_mock.get(
        "https://accounts.example.com/accounts/5/",
        json={"id": 5, "name": "Acme"},
    )

    invoice = Invoice(json={"id": 1, "account": 5})

    assert invoice.account.api is accounts_api
    assert invoice.account.name == "Acme"


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_related_assignment_follows_a_response_that_changes_it(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """A response that names another related object replaces the assigned one.

    Args:
        write_mode: Write mode of the object that is assigned.
    """
    requests_mock.patch(
        "https://example.com/v1/todos/1/", json={**TODO_JSON, "userId": 3}
    )
    todo = Todo(json=dict(TODO_JSON))
    todo.set_write_mode(write_mode)
    assigned = User(json={"id": 4, "name": "Ada"})

    todo.user = assigned
    todo.save()

    assert todo.json["userId"] == 3
    assert todo.user is not assigned
    assert todo.user.pk == 3


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_related_assignment_kept_when_the_response_agrees(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """The assigned related object stays the field's value after the write.

    Args:
        write_mode: Write mode of the object that is assigned.
    """
    requests_mock.patch(
        "https://example.com/v1/todos/1/", json={**TODO_JSON, "userId": 4}
    )
    todo = Todo(json=dict(TODO_JSON))
    todo.set_write_mode(write_mode)
    assigned = User(json={"id": 4, "name": "Ada"})

    todo.user = assigned
    todo.save()

    assert todo.user is assigned


@json_object
class Crew(ModelBase):
    """Model with embedded related objects and a field to load later."""

    class_url = "/crews/"

    id: int
    lead: User
    members: list[User]
    name: str


CREW_JSON = {
    "id": 1,
    "lead": {"id": 2, "name": "old"},
    "members": [{"id": 3, "name": "old"}],
}


@pytest.mark.parametrize("server_name", ["old", "new"])
def test_load_keeps_unchanged_related_objects(
    requests_mock: Any, server_name: str
) -> None:
    """Loading a missing field keeps the related objects it leaves unchanged.

    Their unsaved changes then stay reachable through the parent. Related
    objects that the server changed are read anew.

    Args:
        server_name: Name that the loaded JSON gives the related objects.
    """
    loaded = {
        "id": 1,
        "name": "loaded",
        "lead": {"id": 2, "name": server_name},
        "members": [{"id": 3, "name": server_name}],
    }
    requests_mock.get("https://example.com/v1/crews/1/", json=loaded)
    crew = Crew(id=1)
    crew.update_from_json({k: v for k, v in CREW_JSON.items() if k != "id"})
    lead = crew.lead
    member = crew.members[0]
    lead.name = "local"
    member.name = "local"

    assert crew.name == "loaded"

    unchanged = server_name == "old"
    assert (crew.lead is lead) == unchanged
    assert (crew.members[0] is member) == unchanged
    assert crew.lead.name == ("local" if unchanged else "new")
    assert lead._pending_updates == {"name": "local"}


def test_assigning_a_related_list_keeps_its_objects(
    requests_mock: Any,
) -> None:
    """``crew.members = crew.members`` keeps the members and their changes.

    The list is still checked for changes made in place.
    """
    patch = requests_mock.patch("https://example.com/v1/crews/1/", json={})
    crew = Crew(json=deepcopy(CREW_JSON))
    member = crew.members[0]
    member.name = "local"

    crew.members = crew.members

    assert crew.members[0] is member
    assert member._pending_updates == {"name": "local"}
    assert requests_mock.call_count == 0
    crew.members.append(User(json={"id": 4, "name": "new"}))
    crew.save()
    assert patch.last_request.json() == {"members": [3, 4]}


def test_related_object_inherits_instance_api_override(
    requests_mock: Any,
) -> None:
    """An instance API override also applies to its related objects."""
    override = API("https://override.example.com/")
    requests_mock.get(
        "https://override.example.com/todos/1/",
        json={"id": 1, "userId": 2, "title": "a", "completed": False},
    )

    todo = Todo(id=1, api=override)

    assert todo.user.api is override


TENANT_TODO = {"id": 1, "userId": 2, "title": "a", "completed": False}


def test_queries_use_the_api_passed_to_them(requests_mock: Any) -> None:
    """A query's ``api=`` sends it, and later requests, to that API."""
    tenant = API("https://tenant.example.com/v2/")
    requests_mock.get(
        "https://tenant.example.com/v2/todos/", json=[TENANT_TODO]
    )
    user = requests_mock.get(
        "https://tenant.example.com/v2/users/2/", json={"id": 2, "name": "Ada"}
    )

    todos = [
        *Todo.filter(api=tenant, title="a"),
        *Todo.all(api=tenant),
        Todo.get(api=tenant, title="a"),
    ]

    assert all(todo.api is tenant for todo in todos)
    assert todos[0].user.name == "Ada"
    assert user.call_count == 1
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example.com"
    }
    assert Todo.api is test_api


def test_creation_uses_the_api_passed_to_it(requests_mock: Any) -> None:
    """``create()`` and ``get_or_create()`` accept ``api=`` too."""
    tenant = API("https://tenant.example.com/v2/")
    requests_mock.get("https://tenant.example.com/v2/todos/", json=[])
    post = requests_mock.post(
        "https://tenant.example.com/v2/todos/", json=TENANT_TODO
    )

    created = Todo.create(api=tenant, title="a")
    todo, was_created = Todo.get_or_create(api=tenant, title="a")

    assert created.api is tenant
    assert todo.api is tenant
    assert was_created
    assert post.call_count == 2
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example.com"
    }


def test_default_hooks_accept_and_pass_on_keyword_arguments(
    requests_mock: Any,
) -> None:
    """Context that a later release passes reaches overrides through ``super``.

    The default hooks accept keyword arguments they do not know, and
    ``collection_items_from_response()`` passes them to
    ``collection_items()``.
    """
    seen: list[dict[str, Any]] = []

    @json_object
    class Probe(ModelBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            seen.append(kwargs)
            return super().collection_items(payload, api=api, **kwargs)

    requests_mock.get("https://example.com/v1/probes/", json=[{"id": 1}])
    resp = test_api.get(Probe.class_url)

    items = Probe.collection_items_from_response(resp, api=test_api, hint=1)
    page = Probe.collection_page(resp, api=test_api, hint=1)

    assert list(items) == [{"id": 1}]
    assert seen == [{"hint": 1}]
    assert page.items == [{"id": 1}]
    with pytest.raises(PrefetchNotSupported):
        Probe.bulk_get_by_pks({1}, api=test_api, hint=1)


@pytest.mark.parametrize(
    "hook",
    [
        "bulk_get_by_pks",
        "collection_page",
        "collection_items_from_response",
        "collection_items",
    ],
)
def test_decorating_refuses_hook_overrides_without_kwargs(hook: str) -> None:
    """An override without ``**kwargs`` raises when its class is decorated.

    The override is on an undecorated base class, as a shared hook is, so the
    decorated subclass inherits it. Without the check, the override would fail
    only when a query first called it.
    """

    def override(cls: type[JSONObject], value: Any, *, api: API) -> None:
        del cls, value, api

    class Base(ModelBase):
        pass

    setattr(Base, hook, classmethod(override))

    with pytest.raises(
        TypeError, match=rf"Base\.{hook}\(\) must accept api= and \*\*kwargs"
    ):

        @json_object
        class Old(Base):
            class_url = "/old/"

            id: int


def test_hook_overrides_whose_signature_is_hidden_are_accepted() -> None:
    """A hook whose signature cannot be read, such as a builtin, is valid."""

    class Wrapped(JSONObject):
        object_json = cast(Any, staticmethod(dict))

    assert Wrapped.object_json({"a": 1}) == {"a": 1}


def test_prefetch_passes_the_query_api_to_bulk_lookup(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bulk lookup receives the query's API, or the related model's own."""
    tenant = API("https://tenant.example.com/v2/")
    seen: list[API | None] = []

    def bulk_get_by_pks(
        cls: type[User],
        /,
        pks: set[Any],
        *,
        api: API | None = None,
        **kwargs: Any,
    ) -> dict[Any, User]:
        seen.append(api)
        return {pk: cls(json={"id": pk, "name": "Ada"}, api=api) for pk in pks}

    monkeypatch.setattr(User, "bulk_get_by_pks", classmethod(bulk_get_by_pks))
    requests_mock.get(
        "https://tenant.example.com/v2/todos/", json=[TENANT_TODO]
    )
    requests_mock.get("https://example.com/v1/todos/", json=[TENANT_TODO])

    (todo,) = Todo.filter(api=tenant, prefetch="user")
    (plain,) = Todo.filter(prefetch="user")

    assert seen == [tenant, User.api]
    assert todo.user.api is tenant
    # The hook created the user with the client it received, but prefetched
    # objects keep the query's own api=, here none.
    assert plain.user._api_override is None


def test_prefetch_loads_related_objects_from_the_query_api(
    requests_mock: Any,
) -> None:
    """A bulk lookup that passes its ``api`` on loads from the query's API."""
    tenant = API("https://tenant.example.com/v2/")
    requests_mock.get(
        "https://tenant.example.com/v2/todos/", json=[TENANT_TODO]
    )
    users = requests_mock.get(
        "https://tenant.example.com/v2/users/", json=[{"id": 2, "name": "Ada"}]
    )

    (todo,) = Todo.filter(api=tenant, prefetch="user")

    assert users.last_request.qs == {"id__in": ["2"]}
    assert todo.user.api is tenant
    assert todo.user.name == "Ada"
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example.com"
    }


def test_tuple_ellipsis_annotation_converts_items() -> None:
    """``tuple[T, ...]`` fields convert each item."""
    history = IssueHistory(json={"id": 1, "statuses": ["open", "closed"]})

    assert history.statuses == (IssueStatus.OPEN, IssueStatus.CLOSED)


def test_filter_rejects_unknown_prefetch_field_before_request(
    requests_mock: Any,
) -> None:
    """A misspelled prefetch field fails without sending a request."""
    with pytest.raises(ValueError, match="usr"):
        Todo.filter(prefetch="usr")

    assert requests_mock.call_count == 0


def test_url_percent_encodes_primary_key() -> None:
    """A primary key always forms a single URL path segment."""
    org = Org(slug="a/b?c")

    assert org.url == "/orgs/a%2Fb%3Fc/"


def test_url_omits_trailing_slash_when_api_does(requests_mock: Any) -> None:
    """An API without trailing slashes gets object URLs without one."""
    api = API("https://example.com/v1/")
    api.trailing_slash = False
    get = requests_mock.get(
        "https://example.com/v1/users/1", json={"id": 1, "name": "Ada"}
    )
    user = User(id=1, api=api)

    assert user.url == "/users/1"
    assert user.name == "Ada"
    assert get.call_count == 1


def test_url_without_api_keeps_trailing_slash() -> None:
    """A model with no registered API still has a URL."""

    @json_object
    class Unbound(JSONObject):
        """Model with no registered API."""

        class_url = "/unbound/"

        id: int

    assert Unbound(id=1).url == "/unbound/1/"


@pytest.mark.parametrize("slug", ["", ".", ".."])
def test_invalid_primary_key_has_no_url(requests_mock: Any, slug: str) -> None:
    """An invalid primary key cannot address another resource.

    Args:
        slug: Primary key that has no URL, such as ``".."``.
    """
    org = Org(slug=slug)

    with pytest.raises(ValueError, match="no URL"):
        org.delete()

    assert requests_mock.call_count == 0


def test_keyword_arguments_set_primary_key_first(requests_mock: Any) -> None:
    """Field values may precede the primary key in the constructor."""
    requests_mock.get(
        "https://example.com/v1/users/1/",
        json={"id": 1, "name": "Ada"},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/users/1/",
        json={"id": 1, "name": "Grace"},
    )

    user = User(name="Grace", id=1)
    user.save()

    assert patch.last_request.json() == {"name": "Grace"}


def test_save_accepts_empty_patch_response(requests_mock: Any) -> None:
    """A ``204 No Content`` response keeps the saved local values."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/", status_code=204
    )

    todo = Todo(id=1)
    todo.title = "new"
    todo.save()
    todo.save()

    assert patch.call_count == 1
    assert todo.title == "new"


TODO_JSON = {"id": 1, "userId": 2, "title": "old", "completed": False}


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
@pytest.mark.parametrize(
    "body",
    [
        {"title": "New"},
        {"id": 1, "title": "New"},
        {"status": "ok", "title": "New"},
    ],
)
def test_write_merges_partial_response_body(
    requests_mock: Any, write_mode: WriteMode, body: dict[str, Any]
) -> None:
    """A body without some fields leaves them as they were.

    Args:
        write_mode: Write mode of the todo.
        body: JSON body of the ``PATCH`` response, which lacks some of the
            todo's fields.
    """
    patch = requests_mock.patch("https://example.com/v1/todos/1/", json=body)
    todo = Todo(json=TODO_JSON, write_mode=write_mode)
    completed = todo.completed

    todo.title = "new"
    todo.save()

    assert patch.last_request.json() == {"title": "new"}
    assert todo.title == "New"
    assert todo.completed is completed
    assert todo.pk == 1
    assert todo.url == "/todos/1/"
    assert todo.json == {**TODO_JSON, **body}


def test_write_response_updates_fields_it_holds(requests_mock: Any) -> None:
    """A field that the body changes is read again from the new JSON."""
    requests_mock.patch(
        "https://example.com/v1/todos/1/",
        json={**TODO_JSON, "title": "new", "completed": True},
    )
    todo = Todo(json=TODO_JSON)
    grace = User(json={"id": 3, "name": "Grace"})
    assert todo.json["completed"] is False

    todo.title = "new"
    todo.user = grace
    todo.save()

    assert todo.completed is True
    assert todo.user is not grace
    assert todo.user.pk == 2


def test_save_keeps_assigned_related_object(requests_mock: Any) -> None:
    """A related object that the write did not change stays the value."""
    requests_mock.patch(
        "https://example.com/v1/todos/1/", json={**TODO_JSON, "userId": 3}
    )
    todo = Todo(json=TODO_JSON)
    grace = User(json={"id": 3, "name": "Grace"})

    todo.user = grace
    todo.save()

    assert todo.user is grace


def test_create_merges_sent_fields_and_loads_missing_ones(
    requests_mock: Any,
) -> None:
    """A creation body with only the key keeps what was sent."""
    post = requests_mock.post(
        "https://example.com/v1/todos/", status_code=201, json={"id": 9}
    )
    get = requests_mock.get(
        "https://example.com/v1/todos/9/",
        json={"id": 9, "userId": 2, "title": "t", "completed": False},
    )

    todo = Todo.create(title="t", user=User(id=2), password="secret")

    assert post.last_request.json() == {
        "title": "t",
        "userId": 2,
        "password": "secret",
    }
    assert todo.json == {"id": 9, "title": "t", "userId": 2}
    assert get.call_count == 0
    assert todo.completed is False
    assert get.call_count == 1
    assert "password" not in todo.json


def test_create_with_full_body_is_not_partial(requests_mock: Any) -> None:
    """A creation body with every field needs no further request."""
    requests_mock.post(
        "https://example.com/v1/todos/", status_code=201, json=TODO_JSON
    )

    todo = Todo.create(title="old", user=User(id=2))

    assert todo.completed is False
    assert requests_mock.call_count == 1


@pytest.mark.parametrize(
    "use",
    [
        lambda obj: obj.pk,
        lambda obj: obj.id,
        lambda obj: obj.url,
        lambda obj: obj == obj.__class__(id=1),
        hash,
    ],
    ids=["pk", "key-field", "url", "equality", "hash"],
)
def test_merge_response_cannot_use_the_key_during_create(
    requests_mock: Any, use: Callable[[Any], Any]
) -> None:
    """The object that reads a creation has no key, and says so.

    Args:
        use: What the hook does with the object.
    """

    @json_object
    class Probe(ModelBase):
        """Model whose hook reads the object during a creation."""

        class_url = "/probes/"

        id: int

        def merge_response(
            self,
            resp: Response,
            current: Mapping[str, Any],
            sent: Mapping[str, Any],
            **kwargs: Any,
        ) -> dict[str, Any]:
            """Use the object before the creation has named its key.

            Args:
                resp: Response to the write.
                current: JSON of the object before the write.
                sent: Fields that the write sent.
                **kwargs: Other arguments, which are ignored.

            Returns:
                The default merge.
            """
            use(self)
            return super().merge_response(resp, current, sent, **kwargs)

    requests_mock.post("https://example.com/v1/probes/", json={"id": 1})

    with pytest.raises(UsageError, match=r"merge_response\(\) reads during"):
        Probe.create()
    assert requests_mock.call_count == 1


def test_merge_response_can_be_overridden(requests_mock: Any) -> None:
    """An API with status bodies can ignore them."""

    class StatusModel(ModelBase):
        """Base model for an API whose writes answer with a status."""

        def merge_response(
            self,
            resp: Response,
            current: Mapping[str, Any],
            sent: Mapping[str, Any],
            **kwargs: Any,
        ) -> dict[str, Any]:
            """Ignore the status body.

            Args:
                resp: Response to the write, whose body is a status.
                current: JSON of the object before the write.
                sent: Fields that the write sent.
                **kwargs: Other arguments, which are ignored.

            Returns:
                ``current`` updated with ``sent``.
            """
            assert resp.json() == {"status": "ok"}
            return {**current, **sent}

    @json_object
    class Note(StatusModel):
        """Model whose writes answer with a status."""

        class_url = "/notes/"

        id: int
        text: str

    requests_mock.post("https://example.com/v1/notes/", json={"status": "ok"})
    requests_mock.patch(
        "https://example.com/v1/notes/1/", json={"status": "ok"}
    )

    note = Note.create(id=1, text="a")
    note.text = "b"
    note.save()

    assert note.json == {"id": 1, "text": "b"}


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_redirected_save_keeps_pending_updates(
    requests_mock: Any, status: int, write_mode: WriteMode
) -> None:
    """A PATCH that a redirect answers is not saved.

    The redirect is not followed, as for Django's ``APPEND_SLASH`` redirect of
    a URL without its trailing slash. A lazy update stays pending, and an eager
    assignment does not take effect.

    Args:
        status: Redirect status code that the server returns.
        write_mode: Write mode of the object.
    """
    requests_mock.patch(
        "https://example.com/v1/todos/1/",
        status_code=status,
        headers={"Location": "https://example.com/v1/todos/1/moved/"},
    )
    moved = requests_mock.register_uri(
        requests_mock_ANY,
        "https://example.com/v1/todos/1/moved/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    todo = Todo(
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
        write_mode=write_mode,
    )

    def update() -> None:
        todo.title = "new"
        todo.save()

    with pytest.raises(RedirectError, match=f"answered with status {status}"):
        update()

    assert not moved.called
    if write_mode == "lazy":
        assert todo.title == "new"
        assert todo._pending_updates == {"title": "new"}
    else:
        # A failed eager write leaves the field as the server has it.
        assert todo.title == "old"
        assert todo._pending_updates == {}


_NON_OBJECT_BODIES = ["null", '"ok"', "3", "[3]"]


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES)
def test_save_rejects_patch_response_without_object(
    requests_mock: Any, body: str
) -> None:
    """A body that holds no object leaves the object's state unchanged.

    Args:
        body: Text of the ``PATCH`` response, JSON that is not an object.
    """
    requests_mock.patch("https://example.com/v1/users/1/", text=body)
    user = User(json={"id": 1, "name": "Ada"})

    user.name = "Grace"
    with pytest.raises(TypeError, match="must return a JSON object"):
        user.save()

    assert user.json == {"id": 1, "name": "Grace"}
    assert user._pending_updates == {"name": "Grace"}
    assert user.url == "/users/1/"


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES)
def test_create_and_load_reject_response_without_object(
    requests_mock: Any, body: str
) -> None:
    """Creating or loading an object requires a JSON object.

    Args:
        body: Text of the ``POST`` and ``GET`` responses, JSON that is not an
            object.
    """
    requests_mock.post("https://example.com/v1/users/", text=body)
    requests_mock.get("https://example.com/v1/users/1/", text=body)

    with pytest.raises(TypeError, match="must return a JSON object"):
        User.create(name="Ada")
    with pytest.raises(TypeError, match="must return a JSON object"):
        _ = User(id=1).name


def test_eager_write_accepts_empty_patch_response(
    requests_mock: Any,
) -> None:
    """Eager writes also accept ``204 No Content`` responses."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    requests_mock.patch("https://example.com/v1/todos/1/", status_code=204)

    todo = Todo(id=1, write_mode="eager")
    todo.title = "new"

    assert todo.title == "new"
    assert todo.json["title"] == "new"


def test_failed_eager_write_keeps_previous_value(requests_mock: Any) -> None:
    """A rejected eager write leaves the local value unchanged."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    requests_mock.patch("https://example.com/v1/todos/1/", status_code=400)

    todo = Todo(id=1, write_mode="eager")
    with pytest.raises(requests.HTTPError):
        todo.title = "new"

    assert todo.title == "old"


@json_object
class Stamped(JSONObject):
    """Model whose field names are also the names of their types' modules."""

    class_url = "/stamped/"

    id: int
    datetime: datetime.datetime
    uuid: uuid.UUID


def test_string_annotation_naming_its_field_resolves_to_the_type() -> None:
    """A string annotation such as ``uuid.UUID`` does not name the field.

    This module uses ``from __future__ import annotations``, so the annotation
    of ``uuid: uuid.UUID`` is the string ``"uuid.UUID"``. The field's
    descriptor in the class namespace must not shadow the module.
    """
    obj = Stamped(
        json={
            "id": 1,
            "datetime": "2024-04-01T12:00:00Z",
            "uuid": "12345678-1234-5678-1234-567812345678",
        }
    )

    assert obj.datetime == datetime.datetime(
        2024, 4, 1, 12, tzinfo=datetime.UTC
    )
    assert obj.uuid == uuid.UUID("12345678-1234-5678-1234-567812345678")


_QUERY_SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(), st.text(max_size=3)
)
_QUERY_VALUES = st.recursive(
    _QUERY_SCALARS,
    lambda items: st.one_of(
        st.lists(items, max_size=3),
        st.lists(items, max_size=3).map(tuple),
        st.lists(items, max_size=3).map(iter),
        st.lists(items, max_size=3).map(deque),
        st.dictionaries(st.text(max_size=2), items, max_size=2),
    ),
    max_leaves=6,
)


@given(value=_QUERY_VALUES, name=st.sampled_from(["userId", "ordering"]))
def test_query_encoding_sends_constraint_or_raises(
    value: Any, name: str
) -> None:
    """Every query value is either sent intact or refused before sending.

    Args:
        value: Generated query value.
        name: Parameter name, a JSON key or a name that is not a field.
    """
    try:
        params = Todo._encode_query({name: value}, test_api)
    except ValueError:
        return

    url = requests.Request("GET", "https://example.com/", params=params)
    sent = parse_qs(
        urlparse(str(url.prepare().url)).query,
        keep_blank_values=True,
    )
    items = params[name] if isinstance(params[name], list) else [params[name]]
    assert len(sent[name]) == len(items)


@pytest.mark.parametrize(
    "response", [{"id": 1}, {}, {"id": 1, "settings": {"nested": {"x": 1}}}]
)
def test_create_does_not_keep_caller_containers(
    requests_mock: Any, response: dict[str, Any]
) -> None:
    """Changing a value passed to ``create()`` does not change saved state.

    When the response omits a field, the new object holds the value that was
    sent. It must be a copy, or a later change to the caller's value would
    change what the object believes the server holds, and assigning that
    value back would send nothing.

    Args:
        response: Body of the creation response.
    """
    requests_mock.post("https://example.com/v1/profiles/", json=response)
    patch = requests_mock.patch(
        "https://example.com/v1/profiles/1/", status_code=204
    )
    settings: dict[str, Any] = {"nested": {"x": 1}}

    profile = Profile.create(id=1, settings=settings)
    settings["nested"]["x"] = 2

    assert profile.settings == {"nested": {"x": 1}}
    profile.settings = settings
    profile.save()
    assert patch.last_request.json() == {"settings": {"nested": {"x": 2}}}


def test_params_sends_reserved_names(requests_mock: Any) -> None:
    """``params=`` sends a parameter that a query method reserves."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    list(
        Todo.filter(
            params={"api": "v2", "prefetch": "1", "defaults": "d", "url": "u"},
            title="a",
        )
    )

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "api": ["v2"],
        "prefetch": ["1"],
        "defaults": ["d"],
        "url": ["u"],
        "title": ["a"],
    }


_QUERY_OPTIONS = (
    "api",
    "prefetch",
    "defaults",
    "request_options",
    "params",
    "url",
)


@pytest.mark.parametrize(
    ("method", "name"),
    [
        (method, name)
        for method in ("filter", "get", "get_or_create", "get_page")
        for name in _QUERY_OPTIONS
        if name not in inspect.signature(getattr(Todo, method)).parameters
    ],
)
def test_query_methods_reserve_the_options_they_do_not_take(
    requests_mock: Any, method: str, name: str
) -> None:
    """Every query method refuses each option that it does not take.

    In 0.9.0, ``filter(url=...)`` queried another URL. Sending an option as
    a query parameter would also change what a call sends once a later
    release gives the option to that method.

    Args:
        method: Name of the query method.
        name: Option that the method does not take.
    """
    with pytest.raises(TypeError, match=rf"^{name}= is reserved\. .*params="):
        getattr(Todo, method)(**{name: "wire"})

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("name", ["object_json", "merge_response"])
def test_hooks_without_kwargs_are_refused(name: str) -> None:
    """An override of any hook must accept the keywords of later releases.

    The check runs when the class is defined, also for a mixin that is never
    decorated.

    Args:
        name: Hook to override.
    """

    def hook(self: Any, *args: Any) -> Any:
        return {}

    with pytest.raises(TypeError, match=rf"{name}\(\) must accept \*\*kwargs"):
        type("Mixin", (JSONObject,), {name: hook})


def test_params_are_encoded_as_keyword_arguments(requests_mock: Any) -> None:
    """A field named in ``params`` is renamed and converted as usual."""
    lookup = requests_mock.get(
        "https://example.com/v1/todos/", json=[{"id": 1, "userId": 2}]
    )

    todo = Todo.get(params={"user": User(id=2), "completed": True})

    assert todo.id == 1
    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "userId": ["2"],
        "completed": ["true"],
    }


@pytest.mark.parametrize(
    "call",
    [
        lambda: Todo.filter(params={"title": "a"}, title="b"),
        lambda: Todo.get(params={"title": "a"}, title="b"),
        lambda: Todo.get_or_create(params={"title": "a"}, title="b"),
        lambda: Todo.create(fields={"title": "a"}, title="b"),
    ],
)
def test_name_given_both_ways_raises(requests_mock: Any, call: Any) -> None:
    """A name given as a keyword argument and in the mapping is ambiguous.

    Args:
        call: Callable that gives one name both ways.
    """
    with pytest.raises(TypeError, match="both as a keyword argument"):
        call()

    assert requests_mock.call_count == 0


def test_query_request_options_reach_the_query_and_bulk_lookup(
    requests_mock: Any,
) -> None:
    """A query's options reach its own request and a same-origin bulk lookup.

    The objects that the query returns, and their related objects, do not
    keep the options.
    """
    todos = requests_mock.get(
        "https://example.com/v1/todos/", json=[TENANT_TODO]
    )
    users = requests_mock.get(
        "https://example.com/v1/users/", json=[{"id": 2, "name": "Ada"}]
    )
    options = {"headers": {"X-Trace": "t1"}, "timeout": 5}

    (todo,) = Todo.filter(prefetch="user", request_options=options)

    assert todos.last_request.headers["X-Trace"] == "t1"
    assert todos.last_request.timeout == 5
    assert users.last_request.headers["X-Trace"] == "t1"
    assert todo.user.name == "Ada"
    assert todo._api_override is None
    assert todo.user._api_override is None

    detail = requests_mock.get(
        "https://example.com/v1/todos/1/", json=TENANT_TODO
    )
    todo.refresh()
    assert todo.title == "a"
    assert "X-Trace" not in detail.last_request.headers


def test_query_request_options_stay_on_the_query_origin(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bulk lookup on another origin does not receive the query's options."""
    monkeypatch.setitem(_api_registry, User, API("https://users.example.net/"))
    requests_mock.get("https://example.com/v1/todos/", json=[TENANT_TODO])
    users = requests_mock.get(
        "https://users.example.net/users/", json=[{"id": 2, "name": "Ada"}]
    )

    list(
        Todo.filter(
            prefetch="user",
            request_options={"headers": {"Authorization": "Bearer t"}},
        )
    )

    assert users.called
    assert "Authorization" not in users.last_request.headers


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"json": {}}, "cannot set json"),
        ({"data": "x"}, "cannot set data"),
        ({"files": {}}, "cannot set files"),
        ({"params": {"a": 1}}, "cannot set params"),
        ({"stream": True}, "stream"),
        ({"method": "POST"}, "cannot set method"),
        ([("headers", {})], "must be a mapping"),
    ],
)
def test_query_rejects_invalid_request_options(
    requests_mock: Any, options: Any, match: str
) -> None:
    """Options that a query cannot send raise before any request.

    Args:
        options: Request options that a query rejects.
        match: Pattern that the ``TypeError`` message should match.
    """
    for call in (
        lambda: Todo.filter(request_options=options),
        lambda: Todo.all(request_options=options),
        lambda: Todo.get(request_options=options),
        lambda: Todo.get_or_create(title="a", request_options=options),
    ):
        with pytest.raises(TypeError, match=match):
            call()

    assert requests_mock.call_count == 0


def test_query_accepts_stream_false(requests_mock: Any) -> None:
    """``stream=False`` asks for what a query does anyway, so it is allowed."""
    requests_mock.get("https://example.com/v1/todos/", json=[TENANT_TODO])

    (todo,) = Todo.filter(request_options={"stream": False})

    assert todo.title == "a"


def test_query_with_unfollowed_redirect_raises(requests_mock: Any) -> None:
    """``allow_redirects=False`` with a redirect leaves no collection."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        status_code=302,
        headers={"Location": "/v1/elsewhere/"},
    )

    with pytest.raises(RedirectError, match="status 302"):
        Todo.filter(request_options={"allow_redirects": False})


@pytest.mark.parametrize("options", [None, {"timeout": 5}])
def test_hook_that_does_not_follow_a_redirect_gets_it_back(
    requests_mock: Any, options: dict[str, Any] | None
) -> None:
    """A hook's client returns a 3xx, in a query with options and without.

    Args:
        options: The query's ``request_options``, or ``None`` for none.
    """
    statuses: list[int] = []

    @json_object
    class Probe(ModelBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            statuses.append(
                api.get("/moved/", allow_redirects=False).status_code
            )
            return super().collection_items(payload, api=api, **kwargs)

    requests_mock.get("https://example.com/v1/probes/", json=[{"id": 1}])
    requests_mock.get(
        "https://example.com/v1/moved/",
        status_code=301,
        headers={"Location": "/v1/elsewhere/"},
    )

    list(Probe.filter(request_options=options))

    assert statuses == [301]


def test_get_or_create_sends_request_options_to_both_requests(
    requests_mock: Any,
) -> None:
    """``get_or_create()`` sends its options with the lookup and creation."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])
    post = requests_mock.post(
        "https://example.com/v1/todos/", json={"id": 3, "title": "a"}
    )

    todo, created = Todo.get_or_create(
        title="a", request_options={"headers": {"X-Trace": "t2"}}
    )

    assert created
    assert todo.id == 3
    assert lookup.last_request.headers["X-Trace"] == "t2"
    assert post.last_request.headers["X-Trace"] == "t2"


def test_params_sends_request_options_parameter(requests_mock: Any) -> None:
    """``params=`` sends a parameter named ``request_options``."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    list(Todo.filter(params={"request_options": "x"}))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "request_options": ["x"]
    }


def test_get_or_create_sends_reserved_names(requests_mock: Any) -> None:
    """``params=`` reaches both the lookup and the creation payload."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])
    post = requests_mock.post(
        "https://example.com/v1/todos/", json={"id": 3, "title": "a"}
    )

    todo, created = Todo.get_or_create(params={"api": "v2"}, title="a")

    assert created
    assert todo.id == 3
    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "api": ["v2"],
        "title": ["a"],
    }
    assert post.last_request.json() == {"api": "v2", "title": "a"}


def test_field_missing_from_full_object_raises_once(
    requests_mock: Any,
) -> None:
    """A field that the loaded object lacks raises without loading again."""
    detail = requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 1, "completed": False},
    )
    todo = Todo(id=1)

    with pytest.raises(AttributeError, match="no attribute 'title'"):
        _ = todo.title
    with pytest.raises(AttributeError, match="no attribute 'title'"):
        _ = todo.title

    assert detail.call_count == 1


def test_prefetch_of_field_that_is_not_related_raises() -> None:
    """``prefetch=`` names a related-object field, checked before a request."""
    with pytest.raises(ValueError, match="not a related-object field"):
        Todo.filter(prefetch="title")


def test_detail_load_keeps_key_from_partial_json(requests_mock: Any) -> None:
    """A detail response without the key keeps the key that the JSON held."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"userId": 1, "title": "a", "completed": True},
    )
    todo = Todo(id=1)
    todo.update_from_json({"id": 1, "title": "a"})

    assert todo.completed is True
    assert todo.id == 1
    assert todo.json["id"] == 1


def test_prefetch_skips_missing_null_and_malformed_relations(
    requests_mock: Any,
) -> None:
    """Items without a usable related key are left out of the bulk load."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "title": "no user", "completed": False},
            {"id": 2, "userId": None, "title": "null", "completed": False},
            {"id": 3, "userId": 5, "title": "user", "completed": False},
        ],
    )
    users = requests_mock.get(
        "https://example.com/v1/users/?id__in=5",
        json=[{"id": 5, "name": "Eve"}],
        complete_qs=True,
    )
    requests_mock.get(
        "https://example.com/v1/teams/",
        json=[{"id": 1, "members": "not a list"}, {"id": 2}],
    )

    todos = list(Todo.filter(prefetch="user"))
    teams = list(Team.filter(prefetch="members"))

    assert getattr(todos[1], "user") is None  # noqa: B009
    assert todos[2].user.name == "Eve"
    assert users.call_count == 1
    assert len(teams) == 2


def test_save_sends_request_options(requests_mock: Any) -> None:
    """``save(request_options=...)`` adds headers and a query to the PATCH."""
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/", json={"id": 1, "title": "new"}
    )
    todo = Todo(
        json={"id": 1, "userId": 2, "title": "old", "completed": False}
    )

    todo.title = "new"
    todo.save(
        request_options={"headers": {"If-Match": '"v1"'}, "params": {"x": "1"}}
    )

    assert patch.last_request.headers["If-Match"] == '"v1"'
    assert patch.last_request.qs == {"x": ["1"]}
    assert patch.last_request.json() == {"title": "new"}


def test_create_sends_request_options(requests_mock: Any) -> None:
    """``create(request_options=...)`` adds headers to the POST."""
    post = requests_mock.post(
        "https://example.com/v1/todos/", json={"id": 3, "title": "a"}
    )

    Todo.create(
        title="a", request_options={"headers": {"Idempotency-Key": "k"}}
    )

    assert post.last_request.headers["Idempotency-Key"] == "k"
    assert post.last_request.json() == {"title": "a"}


@pytest.mark.parametrize("name", ["json", "data", "files"])
def test_request_options_cannot_set_the_body(
    requests_mock: Any, name: str
) -> None:
    """The body belongs to the write, so options cannot replace it.

    Args:
        name: Request argument that would set the body.
    """
    todo = Todo(
        json={"id": 1, "userId": 2, "title": "old", "completed": False}
    )
    todo.title = "new"

    with pytest.raises(TypeError, match=f"cannot set {name}"):
        todo.save(request_options={name: {}})
    with pytest.raises(TypeError, match=f"cannot set {name}"):
        Todo.create(title="a", request_options={name: {}})

    assert requests_mock.call_count == 0
    assert todo._pending_updates == {"title": "new"}


def test_writes_refuse_request_options_that_are_not_a_mapping(
    requests_mock: Any,
) -> None:
    """Writes check ``request_options`` as queries do.

    A sequence of pairs is not a mapping, so it raises ``TypeError`` before
    anything is sent, where a failed attribute lookup would otherwise surface.
    """
    todo = Todo(
        json={"id": 1, "userId": 2, "title": "old", "completed": False}
    )
    todo.title = "new"
    options: Any = [("headers", {"If-Match": "x"})]

    with pytest.raises(TypeError, match="must be a mapping, got list"):
        todo.save(request_options=options)
    with pytest.raises(TypeError, match="must be a mapping, got list"):
        todo.delete(request_options=options)
    with pytest.raises(TypeError, match="must be a mapping, got list"):
        Todo.create(title="a", request_options=options)

    assert requests_mock.call_count == 0
    assert todo._pending_updates == {"title": "new"}


Labels = NewType("Labels", set[str])


@json_object
class TaggedTodo(ModelBase):
    """Model with set fields."""

    class_url = "/tagged-todos/"

    id: int
    tags: set[str]
    marks: frozenset[int] | None
    labels: Labels


@pytest.mark.parametrize(
    ("field", "server", "equal", "different"),
    [
        ("labels", ["b", "a", "c"], {"c", "a", "b"}, {"a", "b"}),
        ("tags", ["b", "a", "c"], {"c", "a", "b"}, {"a", "b"}),
        ("marks", [3, 1, 2], frozenset({1, 2, 3}), frozenset({1, 2})),
    ],
)
def test_assigning_an_equal_set_is_not_a_change(
    requests_mock: Any,
    field: str,
    server: list[Any],
    equal: Any,
    different: Any,
) -> None:
    """A set equal to the server's sends nothing, whatever the server's order.

    Args:
        field: Name of the set field.
        server: The server's items, in an order that is not the sorted one.
        equal: Set with the same items.
        different: Set with other items.
    """
    json = {"id": 1, "tags": ["a"], "marks": [1], "labels": ["a"]}
    json[field] = server
    requests_mock.get("https://example.com/v1/tagged-todos/1/", json=json)
    patch = requests_mock.patch(
        "https://example.com/v1/tagged-todos/1/", status_code=204
    )
    todo = TaggedTodo(id=1)

    setattr(todo, field, equal)
    todo.save()
    assert patch.call_count == 0

    setattr(todo, field, different)
    todo.save()
    assert patch.call_count == 1
    assert sorted(patch.last_request.json()[field]) == sorted(different)


def test_delete_sends_request_options(requests_mock: Any) -> None:
    """``delete()`` passes its request options to the request."""
    deleted = requests_mock.delete(
        "https://example.com/v1/todos/1/", status_code=204
    )

    Todo(id=1).delete(request_options={"headers": {"If-Match": '"v2"'}})

    assert deleted.last_request.headers["If-Match"] == '"v2"'


@pytest.mark.parametrize("name", ["json", "data", "files"])
def test_delete_rejects_body_options(requests_mock: Any, name: str) -> None:
    """``delete()`` rejects request options that set a body, as writes do.

    Args:
        name: Request option that sets the body.
    """
    with pytest.raises(TypeError, match=f"cannot set {name}"):
        Todo(id=1).delete(request_options={name: {}})

    assert requests_mock.call_count == 0


def test_delete_takes_request_options_by_keyword_only() -> None:
    """``delete()`` takes neither positional nor bare request arguments."""
    todo = Todo(id=1)
    with pytest.raises(TypeError):
        todo.delete({"headers": {}})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        todo.delete(headers={})  # type: ignore[call-arg]


@json_object(write_only={"password"})
class Login(ModelBase):
    """Model with a field that the API accepts but never returns."""

    class_url = "/logins/"

    id: int
    name: str
    password: str


LOGIN_JSON = {"id": 1, "name": "ada"}


def test_write_only_field_is_sent_by_create_but_not_kept(
    requests_mock: Any,
) -> None:
    """``create()`` sends a write-only field, and the object drops it."""
    post = requests_mock.post(
        "https://example.com/v1/logins/", json=LOGIN_JSON
    )

    login = Login.create(name="ada", password="hunter2")

    assert post.last_request.json() == {"name": "ada", "password": "hunter2"}
    assert login.json == LOGIN_JSON
    with pytest.raises(AttributeError, match="write-only"):
        _ = login.password


@pytest.mark.parametrize("loaded", [True, False])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_write_only_field_is_assigned_and_sent(
    requests_mock: Any, write_mode: WriteMode, loaded: bool
) -> None:
    """Assigning a write-only field sends it without keeping or loading it.

    Args:
        write_mode: Write mode of the login.
        loaded: Whether the login was loaded before the assignment.
    """
    get = requests_mock.get(
        "https://example.com/v1/logins/1/", json=LOGIN_JSON
    )
    patch = requests_mock.patch(
        "https://example.com/v1/logins/1/", json=LOGIN_JSON
    )
    if loaded:
        login = Login(json=LOGIN_JSON, write_mode=write_mode)
    else:
        login = Login(id=1, write_mode=write_mode)

    login.password = "hunter2"
    login.save()

    assert patch.last_request.json() == {"password": "hunter2"}
    assert not get.called
    assert "password" not in (login._json or {})
    assert login._pending_updates == {}
    assert login.name == "ada"
    assert "password" not in login.json


@pytest.mark.parametrize(
    "body", [{"password": "hunter2"}, {"detail": "ok"}, {"name": "ada"}]
)
def test_write_only_save_keeps_the_key_of_an_unloaded_object(
    requests_mock: Any, body: dict[str, Any]
) -> None:
    """A write-only save whose body lacks the key can still load the object.

    The body becomes the object's partial JSON, which keeps the key that the
    object was saved under, so a read of another field loads from its URL.

    Args:
        body: Response body of the write, without the primary key.
    """
    get = requests_mock.get(
        "https://example.com/v1/logins/1/", json=LOGIN_JSON
    )
    requests_mock.patch("https://example.com/v1/logins/1/", json=body)
    login = Login(id=1)

    login.password = "hunter2"
    login.save()

    assert login.id == 1
    assert login.name == "ada"
    assert get.call_count == (0 if "name" in body else 1)
    assert "password" not in login.json


def test_pending_write_only_field_is_not_kept_by_a_load(
    requests_mock: Any,
) -> None:
    """A load before ``save()`` does not put the pending value in the JSON."""
    requests_mock.get("https://example.com/v1/logins/1/", json=LOGIN_JSON)
    login = Login(id=1)

    login.password = "hunter2"

    assert login.name == "ada"
    assert login.json == LOGIN_JSON
    assert login._pending_updates == {"password": "hunter2"}


def test_json_shows_a_pending_value_over_the_servers() -> None:
    """``json`` and a field read show a pending value over the server's."""
    todo = Todo(json={"id": 1, "userId": 2, "title": "a", "completed": False})

    todo.title = "b"

    assert todo.json["title"] == "b"
    assert todo.title == "b"
    assert todo._json is not None
    assert todo._json["title"] == "a"
    assert todo._pending_updates == {"title": "b"}


def test_json_holds_no_pending_write_only_field(requests_mock: Any) -> None:
    """A write-only value that is pending is not in ``json``."""
    requests_mock.get("https://example.com/v1/logins/1/", json=LOGIN_JSON)
    login = Login(id=1)

    login.password = "hunter2"
    login.name = "grace"

    assert "password" not in login.json
    assert login.json == {**LOGIN_JSON, "name": "grace"}
    assert login._pending_updates == {"password": "hunter2", "name": "grace"}


def test_json_returns_a_new_mapping_on_each_call() -> None:
    """Two calls of ``json`` are equal and distinct, and a change is local."""
    todo = Todo(json={"id": 1, "userId": 2, "title": "a", "completed": False})
    todo.title = "b"

    first = todo.json
    second = todo.json
    assert first == second
    assert first is not second

    first["title"] = "changed"
    first["extra"] = 1

    assert todo.json == second
    assert todo.title == "b"
    assert todo._pending_updates == {"title": "b"}


def test_a_load_of_a_partial_object_keeps_a_pending_value(
    requests_mock: Any,
) -> None:
    """A pending value survives the load, in ``json`` and in a field read."""
    requests_mock.get(
        "https://example.com/v1/todos/1/",
        json={"id": 1, "userId": 2, "title": "server", "completed": False},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/todos/1/", status_code=204
    )
    todo = Todo(id=1)
    todo.update_from_json({"id": 1, "title": "partial"})
    todo.title = "local"

    assert todo.completed is False

    assert todo.title == "local"
    assert todo.json["title"] == "local"
    assert todo._json is not None
    assert todo._json["title"] == "server"
    todo.save()
    assert patch.last_request.json() == {"title": "local"}


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"write_only": {"nope"}}, "names \\['nope'\\]"),
        ({"write_only": {"id"}}, "primary key"),
    ],
)
def test_write_only_must_name_a_readable_field(
    options: dict[str, Any], match: str
) -> None:
    """``write_only`` must name fields of the class, and not its key.

    Args:
        options: Options for ``json_object()``.
        match: Pattern that the ``ValueError`` message should match.
    """
    with pytest.raises(ValueError, match=match):

        @json_object(**options)
        class Broken(ModelBase):
            id: int
            name: str


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"read_only": {"nope"}}, "names \\['nope'\\]"),
        (
            {"read_only": {"name"}, "write_only": {"name"}},
            "both read-only and write-only",
        ),
    ],
)
def test_read_only_must_name_a_field_that_is_not_write_only(
    options: dict[str, Any], match: str
) -> None:
    """``read_only`` must name fields of the class that are not write-only.

    Args:
        options: Options for ``json_object()``.
        match: Pattern that the ``ValueError`` message should match.
    """
    with pytest.raises(ValueError, match=match):

        @json_object(**options)
        class Broken(ModelBase):
            id: int
            name: str


def test_redeclared_field_inherits_read_only() -> None:
    """A redeclared field stays read-only unless ``read_only=`` says not."""

    @json_object(read_only={"created"})
    class Stamped(ModelBase):
        class_url = "/stamped/"

        id: int
        created: str

    @json_object
    class Inherits(Stamped):
        created: str

    @json_object(read_only=set())
    class Clears(Stamped):
        created: str

    with pytest.raises(AttributeError, match="read-only"):
        Inherits(json={"id": 1, "created": "a"}).created = "x"
    clears = Clears(json={"id": 1, "created": "a"}, write_mode="lazy")
    clears.created = "x"
    assert clears._pending_updates == {"created": "x"}


@json_object(write_map={"owner": "owner_id"})
class Chore(ModelBase):
    """Model whose relation is read nested and written as a primary key."""

    class_url = "/chores/"

    id: int
    title: str
    owner: User


CHORE_JSON = {"id": 1, "title": "t", "owner": {"id": 2, "name": "Ada"}}


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
@pytest.mark.parametrize("respond", [True, False])
def test_write_map_sends_a_field_under_its_write_key(
    requests_mock: Any, write_mode: WriteMode, respond: bool
) -> None:
    """A mapped field is read from its key and written under another.

    Args:
        write_mode: Write mode of the chore.
        respond: Whether the server answers with the object or no content.
    """
    if respond:
        patch = requests_mock.patch(
            "https://example.com/v1/chores/1/",
            json={**CHORE_JSON, "owner": {"id": 3, "name": "Bob"}},
        )
    else:
        patch = requests_mock.patch(
            "https://example.com/v1/chores/1/", status_code=204
        )
    chore = Chore(json=CHORE_JSON, write_mode=write_mode)
    assert chore.owner.name == "Ada"
    bob = User(id=3)

    chore.owner = bob
    chore.save()

    assert patch.last_request.json() == {"owner_id": 3}
    assert "owner_id" not in chore.json
    if respond:
        assert chore.json["owner"] == {"id": 3, "name": "Bob"}
        assert chore.owner.name == "Bob"
    else:
        assert chore.json["owner"] == 3
        assert chore.owner is bob
    assert requests_mock.call_count == 1


def test_write_map_applies_to_create_but_not_queries(
    requests_mock: Any,
) -> None:
    """``create()`` writes the mapped key, and a query uses the read key."""
    post = requests_mock.post(
        "https://example.com/v1/chores/", json=CHORE_JSON
    )
    lookup = requests_mock.get("https://example.com/v1/chores/", json=[])

    chore = Chore.create(title="t", owner=User(id=2))
    list(Chore.filter(owner=User(id=2)))

    assert post.last_request.json() == {"title": "t", "owner_id": 2}
    assert chore.owner.name == "Ada"
    assert lookup.last_request.qs == {"owner": ["2"]}


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"write_map": {"nope": "x"}}, "maps \\['nope'\\]"),
        ({"write_map": {"name": "title"}}, "both written under"),
    ],
)
def test_write_map_must_name_fields_and_distinct_keys(
    options: dict[str, Any], match: str
) -> None:
    """``write_map`` names fields, and no two fields share a write key.

    Args:
        options: Options for ``json_object()``.
        match: Pattern that the ``ValueError`` message should match.
    """
    with pytest.raises(ValueError, match=match):

        @json_object(**options)
        class Broken(ModelBase):
            id: int
            name: str
            title: str


def test_full_save_puts_every_field(requests_mock: Any) -> None:
    """``save(full=True)`` sends the whole object, encoded for writing."""
    put = requests_mock.put(
        "https://example.com/v1/chores/1/",
        json={**CHORE_JSON, "title": "new"},
    )
    chore = Chore(json=CHORE_JSON)

    chore.title = "new"
    chore.save(full=True)

    assert put.last_request.json() == {"id": 1, "title": "new", "owner_id": 2}
    assert chore.title == "new"
    assert chore._pending_updates == {}


def test_full_save_loads_and_sends_without_changes(requests_mock: Any) -> None:
    """A full save loads an unloaded object and sends even without changes."""
    requests_mock.get("https://example.com/v1/todos/1/", json=TODO_JSON)
    put = requests_mock.put("https://example.com/v1/todos/1/", status_code=204)

    Todo(id=1).save(full=True)

    assert put.last_request.json() == TODO_JSON


def test_full_save_sends_pending_write_only_fields(requests_mock: Any) -> None:
    """A full save includes a pending write-only field, and keeps none."""
    put = requests_mock.put(
        "https://example.com/v1/logins/1/", json=LOGIN_JSON
    )
    login = Login(json=LOGIN_JSON)

    login.password = "hunter2"
    login.save(full=True)

    assert put.last_request.json() == {**LOGIN_JSON, "password": "hunter2"}
    assert login.json == LOGIN_JSON


@json_object(write_map={"owner": "owner_id"})
class Photo(ModelBase):
    """Model with a file field that the API returns as a URL."""

    class_url = "/photos/"

    id: int
    title: str
    public: bool
    tags: list[str]
    meta: dict[str, int]
    owner: User
    image: str


PHOTO_JSON = {
    "id": 1,
    "title": "t",
    "public": False,
    "tags": [],
    "meta": {},
    "owner": {"id": 2, "name": "Ada"},
    "image": "https://cdn.example.com/1.png",
}


def _form(request: Any) -> dict[str, list[tuple[str | None, bytes]]]:
    """Return the parts of a multipart request, by name.

    Args:
        request: Request that ``requests_mock`` recorded.

    Returns:
        Each part's file name, or ``None``, and content, keyed by name.
    """
    message = email.message_from_bytes(
        b"Content-Type: "
        + request.headers["Content-Type"].encode()
        + b"\r\n\r\n"
        + request.body
    )
    parts: dict[str, list[tuple[str | None, bytes]]] = {}
    payload = message.get_payload()
    assert isinstance(payload, list)
    for part in payload:
        assert isinstance(part, email.message.Message)
        name = str(part.get_param("name", header="content-disposition"))
        content = part.get_payload(decode=True)
        assert isinstance(content, bytes)
        parts.setdefault(name, []).append((part.get_filename(), content))
    return parts


def test_create_uploads_files_with_form_fields(requests_mock: Any) -> None:
    """``create(files=...)`` sends multipart, with fields as form fields."""
    post = requests_mock.post(
        "https://example.com/v1/photos/", json=PHOTO_JSON
    )

    photo = Photo.create(
        title="t",
        public=True,
        tags=["a", "b"],
        owner=User(id=2),
        files={"image": ("a.png", b"PNG")},
    )

    assert post.last_request.headers["Content-Type"].startswith(
        "multipart/form-data"
    )
    assert _form(post.last_request) == {
        "title": [(None, b"t")],
        "public": [(None, b"true")],
        "tags": [(None, b"a"), (None, b"b")],
        "owner_id": [(None, b"2")],
        "image": [("a.png", b"PNG")],
    }
    assert photo.image == "https://cdn.example.com/1.png"


def test_save_uploads_files_without_other_changes(requests_mock: Any) -> None:
    """``save(files=...)`` uploads even when no field changed."""
    patch = requests_mock.patch(
        "https://example.com/v1/photos/1/",
        json={**PHOTO_JSON, "image": "https://cdn.example.com/2.png"},
    )
    photo = Photo(json=PHOTO_JSON)

    photo.save(files={"image": ("b.png", b"PNG2")})

    assert _form(patch.last_request) == {"image": [("b.png", b"PNG2")]}
    assert photo.image == "https://cdn.example.com/2.png"


@pytest.mark.parametrize(("field", "value"), [("meta", {"a": 1})])
def test_values_that_a_form_cannot_hold_raise(
    requests_mock: Any, field: str, value: Any
) -> None:
    """A mapping cannot be a form field, so the upload is not sent.

    Args:
        field: Field whose value a form field cannot express.
        value: That value.
    """
    photo = Photo(json=PHOTO_JSON)
    setattr(photo, field, value)

    with pytest.raises(TypeError, match=f"Field {field} cannot be sent"):
        photo.save(files={"image": ("a.png", b"PNG")})
    with pytest.raises(TypeError, match=f"Field {field} cannot be sent"):
        Photo.create(files={"image": ("a.png", b"PNG")}, **{field: value})

    assert requests_mock.call_count == 0
    assert photo._pending_updates == {field: value}


_RELATIONS = [
    (
        Todo,
        {"id": 1, "userId": 2, "title": "t", "completed": False},
        "user",
        "User",
    ),
    (TodoNote, {"id": 1, "todo": 2}, "todo", "Todo"),
    (TupleTeam, {"id": 1, "members": []}, "members", "User"),
    (SetTeam, {"id": 1, "members": []}, "members", "User"),
]


@pytest.mark.parametrize(
    ("model", "json", "field", "expected"),
    _RELATIONS,
    ids=["renamed", "related", "tuple", "set"],
)
def test_a_relation_refuses_an_object_of_another_model(
    requests_mock: Any,
    model: type[JSONObject],
    json: dict[str, Any],
    field: str,
    expected: str,
) -> None:
    """Assignment and ``create()`` refuse an object of another model.

    The key or URL of such an object would name a resource of the wrong
    collection. The other model here is ``Todo``, or ``User`` for a ``Todo``
    relation.

    Args:
        model: Model with the relation.
        json: JSON of an object of the model.
        field: Name of the relation field.
        expected: Name of the related model.
    """
    other: JSONObject = User(id=7) if expected == "Todo" else Todo(id=7)
    value: Any = [other] if field in ("members", "readers") else other
    message = (
        rf"{model.__name__}\.{field}: (\[0\]: )?A relation to {expected} "
        rf"cannot take an object of {type(other).__name__}"
    )
    holder = model(json=json)

    with pytest.raises(FieldConversionError, match=message):
        setattr(holder, field, value)
    with pytest.raises(FieldConversionError, match=message):
        model.create(**{field: value})
    if field == "user":
        with pytest.raises(FieldConversionError, match=message):
            model.filter(**{field: value})
        with pytest.raises(FieldConversionError, match=message):
            model.filter(params={"user": value})

    assert requests_mock.call_count == 0
    assert holder._pending_updates == {}


def test_encode_json_refuses_an_object_of_another_model() -> None:
    """``encode_json()`` refuses an object of another model."""
    with pytest.raises(TypeError, match="cannot take an object of Todo"):
        encode_json(Todo(id=7), User)
    assert encode_json(Admin(id=7), User) == 7


def test_a_relation_refuses_an_object_of_another_model_in_a_list(
    requests_mock: Any,
) -> None:
    """A list element of another model is refused, and a subclass is not."""
    team = TupleTeam(json={"id": 1, "members": []})

    with pytest.raises(FieldConversionError, match="cannot take an object of"):
        team.members = (User(id=1), Todo(id=7))  # type: ignore[assignment]
    assert team._pending_updates == {}

    team.members = (User(id=1), Admin(id=2))
    assert team._pending_updates == {"members": [1, 2]}
    assert requests_mock.call_count == 0


def test_a_relation_takes_an_object_of_a_subclass(requests_mock: Any) -> None:
    """An object of a subclass of the related model is written as one."""
    requests_mock.post(
        "https://example.com/v1/todos/",
        json={"id": 5, "userId": 3, "title": "t", "completed": False},
    )

    Todo.create(user=Admin(id=3), title="t", completed=False)

    assert requests_mock.last_request.json()["userId"] == 3


def test_a_key_that_is_not_a_relation_refuses_a_model_object() -> None:
    """A model object is not a value of a plain key, as in JSON."""
    with pytest.raises(FieldConversionError, match=r"Todo\.id: .*User"):
        Todo(id=User(id=3))
    todo = Todo(id=1)
    with pytest.raises(FieldConversionError, match=r"Todo\.id: .*User"):
        todo.pk = User(id=3)

    assert todo.pk == 1


class _UserPair(typing.NamedTuple):
    """A ``NamedTuple`` that holds a related model."""

    user: User
    size: int


class _UserRecord(TypedDict):
    """A ``TypedDict`` that holds a related model."""

    user: User


@dataclasses.dataclass
class _UserEntry:
    """A dataclass that holds a related model."""

    user: User


class _NestedRecord(TypedDict):
    """A ``TypedDict`` that holds a list of another one that holds a model."""

    records: list[_UserRecord]


@pytest.mark.parametrize(
    "key_type",
    [
        pytest.param(dict[str, User], id="mapping"),
        pytest.param(dict[str, list[User]], id="mapping-of-lists"),
        pytest.param(tuple[User, int], id="fixed-tuple"),
        pytest.param(tuple[int, User] | None, id="optional-fixed-tuple"),
        pytest.param(_UserPair, id="named-tuple"),
        pytest.param(_UserRecord, id="typed-dict"),
        pytest.param(_UserEntry, id="dataclass"),
        pytest.param(list[_UserRecord], id="list-of-typed-dicts"),
        pytest.param(_NestedRecord, id="typed-dict-in-typed-dict"),
    ],
)
def test_a_primary_key_cannot_hold_related_objects_in_any_container(
    key_type: Any,
) -> None:
    """Decorating a model whose key holds a model in a container is refused.

    Mappings, fixed-length tuples, ``TypedDict``, ``NamedTuple``, and dataclass
    keys are refused as a collection is, when the class is decorated, and the
    error is a plain ``TypeError`` that names the field.

    Args:
        key_type: Declared type of the primary key.
    """
    with pytest.raises(
        TypeError,
        match=r"Crate\.id is the primary key, whose type must be",
    ) as error:
        json_object(
            type(
                "Crate",
                (ModelBase,),
                {"__annotations__": {"id": key_type}, "class_url": "/crates/"},
            )
        )

    assert not isinstance(error.value, FieldConversionError)


_UserPairKey = NewType("_UserPairKey", _UserPair)
_UserRecordKey = NewType(  # type: ignore[valid-newtype]
    "_UserRecordKey", _UserRecord
)
_UserEntryKey = NewType("_UserEntryKey", _UserEntry)


@pytest.mark.parametrize(
    "newtype",
    [_UserPairKey, _UserRecordKey, _UserEntryKey],
    ids=["named-tuple", "typed-dict", "dataclass"],
)
@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda ty: ty, id="alone"),
        pytest.param(
            lambda ty: Optional[ty],  # noqa: UP045
            id="optional",
        ),
        pytest.param(
            lambda ty: dict[str, ty],  # type: ignore[valid-type]
            id="mapping",
        ),
        pytest.param(
            lambda ty: list[ty],  # type: ignore[valid-type]
            id="list",
        ),
        pytest.param(
            lambda ty: tuple[ty, int],  # type: ignore[valid-type]
            id="fixed-tuple",
        ),
    ],
)
def test_a_primary_key_cannot_hold_related_objects_through_a_new_type(
    newtype: Any, shape: Callable[[Any], Any]
) -> None:
    """A ``NewType`` of a form that holds a model is refused as the form is.

    Args:
        newtype: ``NewType`` of a ``NamedTuple``, ``TypedDict``, or dataclass
            that holds a model.
        shape: Wraps the ``NewType`` in the container that the key declares.
    """
    with pytest.raises(
        TypeError,
        match=r"Crate\.id is the primary key, whose type must be",
    ) as error:
        json_object(
            type(
                "Crate",
                (ModelBase,),
                {
                    "__annotations__": {"id": shape(newtype)},
                    "class_url": "/crates/",
                },
            )
        )

    assert not isinstance(error.value, FieldConversionError)


def test_a_primary_key_cannot_hold_related_objects_in_a_list_new_type() -> (
    None
):
    """A ``NewType`` of a list of a form that holds a model is refused."""
    with pytest.raises(TypeError, match=r"Crate\.id is the primary key"):
        json_object(
            type(
                "Crate",
                (ModelBase,),
                {
                    "__annotations__": {
                        "id": NewType("_Records", list[_UserRecord])
                    },
                    "class_url": "/crates/",
                },
            )
        )


def _decode_pair(value: Any, cls: Any) -> _UserPair:
    return _UserPair(User(id=value), 0)


def _decode_record(value: Any, cls: Any) -> _UserRecord:
    return {"user": User(id=value)}


def _decode_entry(value: Any, cls: Any) -> _UserEntry:
    return _UserEntry(User(id=value))


@pytest.mark.parametrize(
    ("form", "key_type", "decode", "encode", "expected"),
    [
        pytest.param(
            _UserPair,
            _UserPair,
            _decode_pair,
            lambda value, cls: value.user.pk,
            _UserPair(User(id=7), 0),
            id="named-tuple",
        ),
        pytest.param(
            _UserRecord,
            _UserRecord,
            _decode_record,
            lambda value, cls: value["user"].pk,
            {"user": User(id=7)},
            id="typed-dict",
        ),
        pytest.param(
            _UserEntry,
            _UserEntry,
            _decode_entry,
            lambda value, cls: value.user.pk,
            _UserEntry(User(id=7)),
            id="dataclass",
        ),
        pytest.param(
            _UserEntry,
            _UserEntryKey,
            _decode_entry,
            lambda value, cls: value.user.pk,
            _UserEntry(User(id=7)),
            id="new-type-of-dataclass",
        ),
        pytest.param(
            _UserEntryKey,
            _UserEntryKey,
            _decode_entry,
            lambda value, cls: value.user.pk,
            _UserEntry(User(id=7)),
            id="new-type-with-its-own-entry",
        ),
    ],
)
def test_a_key_that_a_conversion_converts_is_refused_whatever_it_reads(
    form: Any,
    key_type: Any,
    decode: Callable[[Any, Any], Any],
    encode: Callable[[Any, Any], Any],
    expected: Any,
) -> None:
    """A conversion replaces a key class's structure, but no key converts.

    The conversion reads and writes the related model's key, and the key is
    refused as any converted key is, when the class is decorated.

    Args:
        form: Type that the conversion table has an entry for.
        key_type: Declared type of the primary key.
        decode: The entry's ``decode``.
        encode: The entry's ``encode``.
        expected: What ``pk`` would read for the key 7.
    """
    with pytest.raises(
        TypeError, match=r"Crate\.id is the primary key, whose type must be"
    ):
        json_object(
            type(
                "Crate",
                (ModelBase,),
                {
                    "__annotations__": {"id": key_type, "label": str},
                    "class_url": "/crates/",
                    "conversions": {
                        form: Conversion(decode=decode, encode=encode)
                    },
                },
            )
        )


def test_a_key_in_a_container_that_holds_a_model_is_refused_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that names a class defined later is refused when it resolves.

    The refusal is a plain ``TypeError``, from ``url`` and from ``==``, for a
    mapping and for a ``NamedTuple`` whose field names the later class.

    Args:
        monkeypatch: Defines the classes as globals, after the models that name
            them are decorated.
    """

    @json_object
    class Shelf(ModelBase):
        class_url = "/shelves/"

        id: dict[str, Item]

    @json_object
    class Rack(ModelBase):
        class_url = "/racks/"

        id: Slot

    @json_object
    class Item(ModelBase):
        class_url = "/items/"

        id: int

    class Slot(typing.NamedTuple):
        item: Item

    monkeypatch.setitem(globals(), "Item", Item)
    monkeypatch.setitem(globals(), "Slot", Slot)

    with pytest.raises(TypeError, match=r"Shelf\.id is the primary key"):
        Shelf(json={"id": {"a": 1}}).url  # noqa: B018
    with pytest.raises(TypeError, match=r"Rack\.id is the primary key"):
        Rack(json={"id": [1]}).url  # noqa: B018
    with pytest.raises(TypeError, match=r"Rack\.id is the primary key"):
        Rack(json={"id": [1]}) == Rack(json={"id": [1]})  # noqa: B015


def test_an_object_whose_key_annotation_names_a_later_class_is_built_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key annotated by a class defined later is built once it is defined.

    Until the class is defined, building an object raises the ``NameError`` of
    the undefined name, and nothing is kept, so afterwards objects build with
    their keys. A field that is not the key still raises a plain ``NameError``
    when it is read.

    Args:
        monkeypatch: Defines the class as a global, after the objects fail.
    """

    @json_object
    class Slot(ModelBase):
        class_url = "/slots/"

        id: LaterSlotKey

    @json_object
    class Note(ModelBase):
        class_url = "/notes/"

        id: int
        body: LaterSlotKey

    monkeypatch.setitem(globals(), "Slot", Slot)

    builds: list[Callable[[], Any]] = [
        lambda: Slot(json={"id": 1}),
        lambda: Slot(id=1),
    ]
    for build in builds:
        with pytest.raises(NameError, match="LaterSlotKey") as undefined:
            build()
        assert not isinstance(undefined.value, TypeError)
    assert "analyzed_type" not in vars(Slot._pk)
    # A field that is not the key raises a NameError alone, which no handler of
    # a conversion's TypeError reads as a refusal.
    note = Note(json={"id": 1, "body": 1})
    with pytest.raises(NameError, match="LaterSlotKey") as undefined:
        note.body  # noqa: B018
    assert not isinstance(undefined.value, TypeError)

    LaterSlotKey = NewType("LaterSlotKey", int)

    monkeypatch.setitem(globals(), "LaterSlotKey", LaterSlotKey)

    built = Slot(json={"id": 1})
    assert built.url == "/slots/1/"
    assert built == Slot(id=1)
    assert hash(built) == hash(Slot(id=1))


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda model: model(json={"id": 1}), id="json"),
        pytest.param(lambda model: model(id=1), id="key"),
        pytest.param(lambda model: model(pk=1), id="pk"),
    ],
)
def test_a_key_naming_a_later_class_raises_the_name_error_and_no_other_type(
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    build: Callable[[Any], Any],
) -> None:
    """The error of an undefined key class is a ``NameError`` and nothing else.

    A ``TypeError`` or a ``ValueError`` would be taken for bad data or a failed
    conversion by the code that handles those. So building an object, from its
    JSON or from its key, raises the ``NameError`` of the undefined name. It
    sends nothing, and once the class is defined the object builds.

    Args:
        requests_mock: Fails the test if a request is sent.
        monkeypatch: Defines the class as a global, after the build fails.
        build: Builds an object of the model.
    """

    @json_object
    class Stall(ModelBase):
        class_url = "/stalls/"

        id: LaterStallKey

    monkeypatch.setitem(globals(), "Stall", Stall)

    with pytest.raises(NameError, match="LaterStallKey") as undefined:
        build(Stall)
    assert not isinstance(undefined.value, (TypeError, ValueError))
    assert requests_mock.call_count == 0
    assert "analyzed_type" not in vars(Stall._pk)

    LaterStallKey = NewType("LaterStallKey", int)

    monkeypatch.setitem(globals(), "LaterStallKey", LaterStallKey)

    assert build(Stall) == Stall(json={"id": 1})
    assert requests_mock.call_count == 0


def _form_keyed_by(shape: str, member: Any) -> tuple[Any, Any]:
    """Return a key form with one member, and the JSON of a value of it.

    Args:
        shape: ``"typed-dict"``, ``"named-tuple"``, or ``"dataclass"``.
        member: The annotation of the form's one field.

    Returns:
        The form, and the JSON of a value whose field holds ``1``.
    """
    match shape:
        case "typed-dict":
            return TypedDict("Rec", {"n": member}), {"n": 1}  # type: ignore[operator]
        case "named-tuple":
            return typing.NamedTuple("Pair", [("n", member)]), [1]
    return dataclasses.make_dataclass("Entry", [("n", member)]), {"n": 1}


def test_an_owner_of_a_relation_to_a_later_model_is_built_before_it(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the model's own key resolves at construction, not its relations.

    An owner whose relation names a model that is not defined yet, or a model
    whose own key names a class not defined yet, as a circular import leaves
    them, is built from its JSON. Reading the relation raises the undefined
    name, and once it is defined the relation reads.

    Args:
        requests_mock: Fails the test if a request is sent.
        monkeypatch: Defines the classes as globals, after the owners are
            built.
    """

    @json_object
    class Post(ModelBase):
        class_url = "/posts/"

        id: int
        owner: LaterOwner

    @json_object
    class Reply(ModelBase):
        class_url = "/replies/"

        id: int
        author: Author

    @json_object
    class Author(ModelBase):
        class_url = "/authors/"

        id: LaterAuthorKey

    monkeypatch.setitem(globals(), "Author", Author)
    post = Post(json={"id": 1, "owner": 7})
    reply = Reply(json={"id": 1, "author": 7})

    assert post.pk == 1
    assert reply.pk == 1
    with pytest.raises(NameError, match="LaterOwner"):
        post.owner  # noqa: B018
    with pytest.raises(NameError, match="LaterAuthorKey"):
        reply.author  # noqa: B018

    @json_object
    class LaterOwner(ModelBase):
        class_url = "/owners/"

        id: int

    LaterAuthorKey = NewType("LaterAuthorKey", int)

    monkeypatch.setitem(globals(), "LaterOwner", LaterOwner)
    monkeypatch.setitem(globals(), "LaterAuthorKey", LaterAuthorKey)

    assert post.owner == LaterOwner(id=7)
    assert reply.author == Author(id=7)
    assert requests_mock.call_count == 0


def _box_keyed_by(form: Any) -> Any:
    """Return a model whose key has a form.

    Args:
        form: The type of the model's key field.

    Returns:
        The model class.
    """
    return json_object(
        type(
            "Box",
            (ModelBase,),
            {"__annotations__": {"id": form}, "class_url": "/boxes/"},
        )
    )


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="The type statement is new in 3.12."
)
def test_a_key_alias_of_a_later_class_is_built_after() -> None:
    """An alias that names a class defined later is unresolved until then.

    The alias is a ``type`` statement's, which evaluates its value on first
    use. Until the class is defined, building an object raises the
    ``NameError`` of the undefined name, and nothing is kept, so afterwards
    objects build with their keys.
    """
    namespace: dict[str, Any] = {}
    exec("type PendingKey = LaterKey", namespace)  # noqa: S102
    box = _box_keyed_by(namespace["PendingKey"])

    with pytest.raises(NameError, match="LaterKey"):
        box(json={"id": 1})
    assert "analyzed_type" not in vars(box._pk)

    namespace["LaterKey"] = int

    built = box(json={"id": 1})
    assert built.url == "/boxes/1/"
    assert built == box(id=1)
    assert len({built, box(id=1)}) == 1


def test_an_object_keyed_by_a_module_attribute_is_built_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key named as an attribute of a module is unresolved until it is set.

    Reading an attribute that the module does not have yet raises an
    ``AttributeError``. Until the attribute is set, building an object raises
    it, as for a class that is not defined yet, and nothing is kept, so
    afterwards objects build with their keys.

    Args:
        monkeypatch: Defines the module as a global, and sets the attribute
            after the build fails.
    """
    later = types.ModuleType("later_attribute_module")
    monkeypatch.setitem(globals(), "later_attribute_module", later)
    box = _box_keyed_by("later_attribute_module.LaterAttributeKey")

    with pytest.raises(AttributeError, match="LaterAttributeKey"):
        box(json={"id": 1})
    assert "analyzed_type" not in vars(box._pk)

    later.LaterAttributeKey = int  # type: ignore[attr-defined]

    built = box(json={"id": 1})
    assert built.url == "/boxes/1/"
    assert built == box(id=1)


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="The type statement is new in 3.12."
)
@pytest.mark.parametrize("field", ["alias", "nested", "items", "mapping"])
def test_a_field_that_is_not_the_key_raises_the_name_error_of_a_later_alias(
    field: str,
) -> None:
    """A type alias that names a later class raises its own error in a field.

    The alias is nested at any depth in a field that is not the key. Reading
    the field raises the ``NameError`` of the undefined name, not a
    ``FieldConversionError``, which says that a value failed to convert, and
    not a ``TypeError`` that a handler of bad data would take. Once the class
    is defined, the field reads.

    Args:
        field: Whether the field is the alias itself, a structure that holds
            it, a list of the structure, or a mapping of it.
    """
    namespace: dict[str, Any] = {}
    exec("type PendingNested = LaterNested", namespace)  # noqa: S102
    inner: Any = _form_keyed_by("typed-dict", namespace["PendingNested"])[0]
    outer: Any = _form_keyed_by("typed-dict", inner)[0]
    annotations: dict[str, Any] = {
        "alias": namespace["PendingNested"],
        "nested": outer,
        "items": list[inner],
        "mapping": dict[str, inner],
    }
    values = {
        "alias": 1,
        "nested": {"n": {"n": 1}},
        "items": [{"n": 1}],
        "mapping": {"a": {"n": 1}},
    }
    note: Any = json_object(
        type(
            "Note",
            (ModelBase,),
            {
                "__annotations__": {"id": int, field: annotations[field]},
                "class_url": "/notes/",
            },
        )
    )
    obj = note(json={"id": 1, field: values[field]})

    with pytest.raises(NameError, match="LaterNested") as undefined:
        getattr(obj, field)
    assert not isinstance(undefined.value, TypeError)

    namespace["LaterNested"] = int
    assert getattr(obj, field) == values[field]


def test_building_raises_a_key_declaration_refused_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declaration that is refused once it resolves refuses every object.

    Building an object resolves its key, so it raises the refusal, whether the
    object is built from its JSON or from its key.

    Args:
        monkeypatch: Defines the class as a global, after the model that names
            it is decorated.
    """

    @json_object(pk="later")
    class Anchor(ModelBase):
        class_url = "/anchors/"

        later: Later
        x: str

    @json_object
    class Later(ModelBase):
        class_url = "/laters/"

        id: int

    monkeypatch.setitem(globals(), "Later", Later)
    message = r"Anchor\.later is the primary key and a relation to Later"

    with pytest.raises(TypeError, match=message) as error:
        Anchor(json={"later": 1, "x": "p"})
    assert not isinstance(error.value, FieldConversionError)
    with pytest.raises(TypeError, match=message):
        Anchor(later=1)


def test_a_primary_key_cannot_be_a_relation_to_its_own_model() -> None:
    """Decorating a model keyed by itself is refused, naming the model."""
    with pytest.raises(TypeError, match=r"Node\.parent is the primary key"):

        @json_object(pk="parent")
        class Node(ModelBase):
            class_url = "/nodes/"

            parent: Node


def test_a_key_cycle_through_a_class_defined_later_is_refused_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relation key that decoration cannot see is refused when it resolves.

    Neither model's key names a class that is defined when the model is
    decorated, so decoration refuses nothing. Each key is a relation, which a
    key cannot be.

    Args:
        monkeypatch: Defines the classes as globals, after they are decorated.
    """

    @json_object(pk="later")
    class Earlier(ModelBase):
        class_url = "/earlier/"

        later: Later

    @json_object(pk="earlier")
    class Later(ModelBase):
        class_url = "/later/"

        earlier: Earlier

    monkeypatch.setitem(globals(), "Earlier", Earlier)
    monkeypatch.setitem(globals(), "Later", Later)

    with pytest.raises(TypeError, match=r"Earlier\.later is the primary key"):
        Earlier(json={"later": 1}).url  # noqa: B018


class _PlainPair(typing.NamedTuple):
    """A ``NamedTuple`` of plain values."""

    left: int
    right: int


class _PlainRecord(TypedDict):
    """A ``TypedDict`` of plain values."""

    left: int


@dataclasses.dataclass
class _PlainEntry:
    """A dataclass of plain values."""

    left: int


class _Token:
    """Key type that a conversion of the model's table converts."""

    def __init__(self, text: str) -> None:
        """Keep the text."""
        self.text = text


_PlainPairKey = NewType("_PlainPairKey", _PlainPair)

_ConvertedCount = NewType("_ConvertedCount", int)
"""A ``NewType`` of ``int`` that ``_CRATE_CONVERSIONS`` converts."""


@pytest.mark.parametrize(
    "key_type",
    [
        pytest.param(list[int], id="list"),
        pytest.param(set[int], id="set"),
        pytest.param(tuple[int, ...], id="variable-tuple"),
        pytest.param(tuple[int, int], id="fixed-tuple"),
        pytest.param(dict[str, int], id="mapping"),
        pytest.param(list, id="bare-list"),
        pytest.param(dict, id="bare-mapping"),
        pytest.param(typing.Any, id="any"),
        pytest.param(_PlainRecord, id="typed-dict"),
        pytest.param(_PlainPair, id="named-tuple"),
        pytest.param(_PlainEntry, id="dataclass"),
        pytest.param(_PlainPairKey, id="new-type-of-named-tuple"),
        pytest.param(int | str, id="union"),
        pytest.param(Optional[list[int]], id="optional-list"),  # noqa: UP045
        pytest.param(list[User], id="list-of-relations"),
        pytest.param(datetime.datetime, id="datetime"),
        pytest.param(datetime.date, id="date"),
        pytest.param(datetime.time, id="time"),
        pytest.param(datetime.timedelta, id="timedelta"),
        pytest.param(decimal.Decimal, id="decimal"),
        pytest.param(IssueStatus, id="enum"),
        pytest.param(typing.Literal["a", "b"], id="literal"),
        pytest.param(Optional[decimal.Decimal], id="optional-decimal"),  # noqa: UP045
        pytest.param(NewType("_Stamp", datetime.datetime), id="new-type"),
        pytest.param(_Token, id="class-with-a-conversion"),
        pytest.param(_PlainPairKey, id="new-type-with-a-conversion"),
        pytest.param(_ConvertedCount, id="new-type-of-int-with-a-conversion"),
        pytest.param(bool, id="bool"),
        pytest.param(float, id="float"),
    ],
)
def test_a_primary_key_that_is_not_a_key_type_is_refused(
    key_type: Any,
) -> None:
    """Decorating a model whose key is not a str, an int, or a UUID is refused.

    A type that a conversion converts is refused too, even a ``NewType`` of
    ``int``, since a conversion may map two of the service's keys to one value.
    The error is a plain ``TypeError`` that names the field and the types that
    a key may be.

    Args:
        key_type: Declared type of the primary key.
    """
    with pytest.raises(
        TypeError,
        match=(
            r"Crate\.id is the primary key, whose type must be the one that "
            r"the service sends for it: a str, an int, or a UUID"
        ),
    ) as error:
        json_object(
            type(
                "Crate",
                (ModelBase,),
                {
                    "__annotations__": {"id": key_type},
                    "class_url": "/crates/",
                    "conversions": _CRATE_CONVERSIONS,
                },
            )
        )

    assert not isinstance(error.value, FieldConversionError)


def test_two_objects_keyed_by_a_mapping_cannot_be_declared() -> None:
    """A mapping key has no one spelling, so its model is refused.

    Two mappings with the same items in another order would be equal, and would
    format as different URLs.
    """
    with pytest.raises(TypeError, match=r"Crate\.id is the primary key"):

        @json_object
        class Crate(ModelBase):
            class_url = "/crates/"

            id: dict[str, int]


@pytest.mark.parametrize(
    "key_type",
    [
        pytest.param(User, id="relation"),
        pytest.param(Optional[User], id="optional-relation"),  # noqa: UP045
        pytest.param(NewType("_UserRef", User), id="new-type-of-relation"),
    ],
)
def test_a_primary_key_that_is_a_relation_is_refused(key_type: Any) -> None:
    """A relation as the key is refused, naming the related key's type.

    Args:
        key_type: Declared type of the primary key.
    """
    with pytest.raises(
        TypeError,
        match=(
            r"Settings\.profile is the primary key and a relation to User, "
            r"and a primary key cannot be a relation\. Declare it as int"
        ),
    ):
        json_object(pk="profile")(
            type(
                "Settings",
                (ModelBase,),
                {
                    "__annotations__": {"profile": key_type},
                    "class_url": "/settings/",
                },
            )
        )


def _decode_token(value: str, cls: type[_Token]) -> _Token:
    return cls(value)


_CRATE_CONVERSIONS: dict[Any, Conversion] = {
    _Token: Conversion(
        decode=_decode_token,
        encode=lambda token, cls: token.text,
    ),
    _PlainPairKey: Conversion(
        decode=lambda text, cls: _PlainPair(len(text), 0),
        encode=lambda pair, cls: "a" * pair.left,
    ),
    _ConvertedCount: Conversion(
        decode=lambda value, cls: _ConvertedCount(value),
        encode=lambda value, cls: int(value),
    ),
}
"""Conversions of types that a key cannot be, which a model may declare."""


@pytest.mark.parametrize(
    ("key_type", "value"),
    [
        pytest.param(int, 1, id="int"),
        pytest.param(str, "a", id="str"),
        pytest.param(
            uuid.UUID, "12345678-1234-5678-1234-567812345678", id="uuid"
        ),
        pytest.param(Annotated[int, "a note"], 1, id="annotated"),
        pytest.param(Optional[int], 1, id="optional"),  # noqa: UP045
        pytest.param(NewType("_Id", int), 1, id="new-type"),
        pytest.param(NewType("_Slug", str), "a", id="new-type-of-str"),
        pytest.param(
            Annotated[NewType("_Ref", uuid.UUID) | None, "a note"],
            "12345678-1234-5678-1234-567812345678",
            id="wrapped-uuid",
        ),
    ],
)
def test_a_primary_key_of_a_key_type_is_accepted(
    key_type: Any, value: Any
) -> None:
    """A str, an int, or a UUID, in the wrappers that a key may have, works.

    A conversion that the model declares for another type leaves the key alone.

    Args:
        key_type: Declared type of the primary key.
        value: JSON of a key of that type.
    """
    crate_type: Any = json_object(
        type(
            "Crate",
            (ModelBase,),
            {
                "__annotations__": {"id": key_type},
                "class_url": "/crates/",
                "conversions": _CRATE_CONVERSIONS,
            },
        )
    )
    crate = crate_type(json={"id": value})
    twin = crate_type(json={"id": value})

    assert crate == twin
    assert hash(crate) == hash(twin)
    assert crate.url.startswith("/crates/")


def test_a_key_of_a_later_class_that_is_not_one_value_is_refused_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that names a class defined later is refused when it resolves.

    Until the class is defined, the undefined name is raised as it is, and
    afterwards the refusal is a plain ``TypeError``.

    Args:
        monkeypatch: Defines the classes as globals, after the models that name
            them are decorated.
    """

    @json_object
    class Roster(ModelBase):
        class_url = "/rosters/"

        id: LaterRosterKey

    with pytest.raises(NameError, match="LaterRosterKey"):
        Roster(json={"id": [1]}).url  # noqa: B018

    LaterRosterKey = list[int]
    monkeypatch.setitem(globals(), "LaterRosterKey", LaterRosterKey)

    with pytest.raises(TypeError, match=r"Roster\.id is the primary key") as e:
        Roster(json={"id": [1]}).url  # noqa: B018
    assert not isinstance(e.value, FieldConversionError)


def _model_with_field(annotation: Any) -> Any:
    """Decorate a model whose ``links`` field has the given annotation.

    Args:
        annotation: Type of the field.

    Returns:
        The decorated model class.
    """
    namespace: dict[str, Any] = {
        "class_url": "/linked-teams/",
        "__annotations__": {"id": int, "links": annotation},
    }
    cls = types.new_class(
        "LinkedTeam", (ModelBase,), exec_body=lambda ns: ns.update(namespace)
    )
    return json_object(cls)


@pytest.mark.parametrize(
    ("annotation", "links"),
    [
        (list[User], [2]),
        (tuple[User, ...], [2]),
        (Optional[list[User]], [2]),  # noqa: UP045
        (set[User], [2]),
        (frozenset[User], [2]),
    ],
)
def test_other_collections_of_relations_are_accepted(
    annotation: Any, links: list[Any], requests_mock: Any
) -> None:
    """A list, a tuple, and a set of relations are fine.

    Args:
        annotation: A collection type of relations.
        links: JSON value of the field, with one related object.
    """
    team = _model_with_field(annotation)(json={"id": 1, "links": links})

    assert len(list(team.links)) == 1
    assert requests_mock.call_count == 0


def test_objects_of_one_model_with_equal_known_keys_are_equal(
    requests_mock: Any,
) -> None:
    """Objects of one model are equal if they have the same known key.

    The key is known if the object holds it or its loaded JSON has it, and the
    objects need not agree in anything else. Comparing sends no request.
    """
    held = User(id=2)
    loaded = User(json={"id": 2, "name": "Ada"})
    other_api = User(id=2, api=API("https://other.example.com/"))

    assert held == loaded
    assert loaded == held
    assert held == other_api
    assert held == held  # noqa: PLR0124
    assert held != User(id=3)
    assert loaded != User(json={"id": 3, "name": "Ada"})
    assert hash(held) == hash(loaded) == hash(other_api)
    assert requests_mock.call_count == 0


def test_objects_of_different_models_are_not_equal_even_with_equal_keys(
    requests_mock: Any,
) -> None:
    """A subclass does not equal its base, nor does a model with the key."""
    user = User(id=1)
    admin = Admin(id=1)

    assert user != admin
    assert admin != user
    assert user != Todo(id=1)
    assert admin == Admin(id=1)
    assert requests_mock.call_count == 0


@json_object
class TokenOwner(ModelBase):
    """Model whose key is a ``UUID``, which JSON can spell wrongly."""

    class_url = "/token-owners/"

    id: uuid.UUID


@json_object
class NamedTokenOwner(TokenOwner):
    """Model whose key is a ``UUID``, which JSON can spell several ways."""

    name: str


def test_json_with_a_key_that_is_no_uuid_is_refused_by_the_constructor() -> (
    None
):
    """The key's field is named, and no object is made."""
    with pytest.raises(FieldConversionError, match=r"TokenOwner\.id"):
        TokenOwner(json={"id": "not-a-uuid"})


@pytest.mark.parametrize(
    ("make", "payload"),
    [
        (lambda: User(json={"id": 5, "name": "a"}), {"id": None}),
        (lambda: User(json={"id": 5, "name": "a"}), {"id": "5"}),
        (
            lambda: TokenOwner(
                json={"id": "12345678-1234-5678-1234-567812345678"}
            ),
            {"id": "not-a-uuid"},
        ),
    ],
    ids=["null", "another-json-type", "unreadable"],
)
def test_update_from_json_refuses_a_payload_whose_key_cannot_be_read(
    make: Callable[[], JSONObject],
    payload: dict[str, Any],
    requests_mock: Any,
) -> None:
    """A payload whose key is ``null`` or not of the key's type is refused.

    Such a payload names no resource, so it does not name this object, and the
    object keeps its JSON.

    Args:
        make: Builds the object.
        payload: The payload, which has the key field.
    """
    obj = make()
    before = copy.deepcopy(obj._json)

    with pytest.raises(ValueError, match="primary key does not match"):
        obj.update_from_json(payload)

    assert obj._json == before
    assert requests_mock.call_count == 0


_CountKey = NewType("_CountKey", int)


@json_object
class TextKeyed(ModelBase):
    """Model whose key is a ``str``."""

    class_url = "/text-keyed/"

    id: str
    name: str


@json_object
class WrappedKeyed(ModelBase):
    """Model whose key is an ``int`` in the wrappers that a key may have."""

    class_url = "/wrapped-keyed/"

    id: Annotated[_CountKey | None, "the key"]
    name: str


@pytest.mark.parametrize(
    ("model", "given"),
    [
        (User, "1"),
        (User, 1.0),
        (User, True),
        (TextKeyed, 1),
        (TextKeyed, True),
        (WrappedKeyed, "1"),
    ],
)
def test_a_key_of_another_json_type_is_refused_where_it_is_given(
    requests_mock: Any, model: type[JSONObject], given: Any
) -> None:
    """A key that is a leaf type has one JSON type, which a given key must be.

    ``bool`` is not an ``int`` key, because the two encode differently. The
    constructor and an assignment refuse the value, whether the key is a plain
    type or wrapped as an optional, a ``NewType``, or ``Annotated``, and an
    object keeps its key.

    Args:
        model: Model with a key of a leaf type.
        given: A key of another JSON type than the field's.
    """
    with pytest.raises(FieldConversionError, match=rf"{model.__name__}\.id"):
        model(id=given)
    key = "a" if model is TextKeyed else 1
    obj = model(id=key)
    with pytest.raises(FieldConversionError, match=rf"{model.__name__}\.id"):
        obj.pk = given
    with pytest.raises(FieldConversionError, match=rf"{model.__name__}\.id"):
        setattr(obj, "id", given)  # noqa: B010

    assert obj.pk == key
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("model", "given"),
    [
        (User, 1),
        (TextKeyed, "a"),
        (WrappedKeyed, 1),
    ],
)
def test_a_key_of_its_own_json_type_is_taken(
    model: type[JSONObject], given: Any
) -> None:
    """The constructor takes the key of the field's type.

    Args:
        model: Model with a key of a leaf type.
        given: A key of the field's JSON type.
    """
    obj = model(id=given)

    assert obj.pk == given
    assert obj == model(id=given)
    assert hash(obj) == hash(model(id=given))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lead", "2"),
        ("lead", 2.0),
        ("lead", True),
        ("members", ["3", 4]),
    ],
)
def test_a_related_key_of_another_json_type_from_the_server_is_refused(
    requests_mock: Any, field: str, value: Any
) -> None:
    """A related object read from the server's JSON has its key's JSON type.

    The key is not given by the program, but a wrong one would name an object
    that never equals the one with the key's own type, and would fail only at
    its first load. The read refuses it, names the relation field and not the
    related model's key, and caches nothing.

    Args:
        field: Name of the related field.
        value: The field's JSON, with a key of another type.
    """
    crew = Crew(json={**CREW_JSON, field: value})

    for _ in range(2):
        with pytest.raises(FieldConversionError, match=rf"Crew\.{field}"):
            getattr(crew, field)

    assert field not in crew._field_cache
    assert requests_mock.call_count == 0


def _keyed_user(write_mode: WriteMode = "lazy") -> User:
    """Return the user 1 with its JSON, in a hash set that it must stay in.

    Args:
        write_mode: Write mode of the user.

    Returns:
        The user.
    """
    return User(json={"id": 1, "name": "Ada"}, write_mode=write_mode)


def _assert_user_kept(user: User) -> None:
    """Check that an object kept its key, hash, URL, and JSON.

    Args:
        user: The user 1, after a refused server state.
    """
    assert user.pk == 1
    assert user.url == "/users/1/"
    assert user == User(id=1)
    assert hash(user) == hash(User(id=1))
    assert user.json["id"] == 1


@pytest.mark.parametrize("named", [2, None, "1"])
def test_a_load_that_names_another_key_is_refused(
    requests_mock: Any, named: Any
) -> None:
    """The hash of an object never changes once its key is known.

    Server state that names another key, no key, or the key in another JSON
    type is refused, and the object, its JSON included, is as it was.

    Args:
        named: What the server's JSON holds for the key.
    """
    requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": named, "name": "Grace"}
    )
    user = _keyed_user()
    members = {user}
    user.refresh()

    def load() -> None:
        user.name  # noqa: B018

    with pytest.raises(ResponseShapeError, match="primary key does not match"):
        load()

    assert user in members
    assert user.pk == 1
    assert user.url == "/users/1/"
    assert hash(user) == hash(User(id=1))
    assert user._json is None


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_a_write_response_that_names_another_key_is_refused(
    requests_mock: Any, write_mode: WriteMode
) -> None:
    """The object keeps its key and its pending updates, and the write is sent.

    Args:
        write_mode: Write mode of the user.
    """
    patch = requests_mock.patch(
        "https://example.com/v1/users/1/", json={"id": 2, "name": "Grace"}
    )
    user = _keyed_user(write_mode)
    members = {user}

    def write() -> None:
        user.name = "Grace"
        if write_mode == "lazy":
            user.save()

    with pytest.raises(ResponseShapeError, match="primary key does not match"):
        write()

    assert patch.call_count == 1
    assert user in members
    _assert_user_kept(user)
    if write_mode == "lazy":
        assert user._pending_updates == {"name": "Grace"}
    else:
        assert user._pending_updates == {}
        assert user.json["name"] == "Ada"


@pytest.mark.parametrize("named", [2, None])
def test_update_from_json_refuses_a_payload_that_names_another_key(
    requests_mock: Any, named: Any
) -> None:
    """The refusal is a ``ResponseShapeError``, which is a ``ValueError``.

    Args:
        named: What the payload holds for the key.
    """
    user = _keyed_user()
    members = {user}

    with pytest.raises(ResponseShapeError) as caught:
        user.update_from_json({"id": named, "name": "Grace"})

    assert isinstance(caught.value, ValueError)
    assert user in members
    _assert_user_kept(user)
    assert user.name == "Ada"
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("path", ["load", "write", "update_from_json"])
def test_server_state_that_spells_the_same_key_another_way_is_taken(
    requests_mock: Any, path: str
) -> None:
    """Two spellings of one UUID are one key, so the hash and the URL stay.

    Args:
        path: How the object takes the server's state.
    """
    url = "https://example.com/v1/token-owners/12345678-1234-5678-1234-567812345678/"
    spelled = {"id": "12345678-1234-5678-1234-567812345678", "name": "b"}
    requests_mock.get(url, json=spelled)
    requests_mock.patch(url, json=spelled)
    owner = NamedTokenOwner(
        json={"id": "12345678123456781234567812345678", "name": "a"}
    )
    members = {owner}
    before = (owner.url, hash(owner))

    if path == "load":
        owner.refresh()
        assert owner.name == "b"
    elif path == "write":
        owner.name = "b"
        owner.save()
    else:
        owner.update_from_json(spelled)

    assert owner.name == "b"
    assert (owner.url, hash(owner)) == before
    assert owner in members
    assert owner.json["id"] == "12345678-1234-5678-1234-567812345678"


@json_object(pk="slug")
class BulkLabel(ModelBase):
    """Model keyed by a ``str``, which loads in bulk."""

    class_url = "/bulk-labels/"

    slug: str
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, BulkLabel]:
        """Return an object named ``bulk`` for each key, without a request.

        Args:
            pks: Keys as the parents' JSON spells them.
            api: Ignored.
            **kwargs: Ignored.

        Returns:
            The objects, keyed as the parents spell the keys.
        """
        return {pk: cls(json={"slug": pk, "name": "bulk"}) for pk in pks}


@json_object
class LabelHolder(ModelBase):
    """Model with a relation to ``BulkLabel``."""

    class_url = "/label-holders/"

    id: int
    label: BulkLabel


def test_prefetch_keeps_str_keys_of_another_case_apart(
    requests_mock: Any,
) -> None:
    """Parents that spell a ``str`` key in two cases get two objects."""
    requests_mock.get(
        "https://example.com/v1/label-holders/",
        json=[{"id": 1, "label": "a"}, {"id": 2, "label": "A"}],
    )

    first, second = LabelHolder.filter(prefetch="label")

    assert first.label is not second.label
    assert first.label.url == "/bulk-labels/a/"
    assert second.label.url == "/bulk-labels/A/"
    assert first.label.name == second.label.name == "bulk"
    assert requests_mock.call_count == 1


def test_update_from_json_accepts_a_payload_that_names_the_key_it_has() -> (
    None
):
    """A payload that names the object's own key, in any spelling, is taken."""
    user = User(json={"id": 1, "name": "a"})
    owner = NamedTokenOwner(
        json={"id": "12345678-1234-5678-1234-567812345678", "name": "a"}
    )
    members = {user, owner}

    user.update_from_json({"id": 1, "name": "z"})
    owner.update_from_json(
        {"id": "12345678-1234-5678-1234-567812345678", "name": "z"}
    )

    assert user.name == "z"
    assert owner.name == "z"
    assert user in members
    assert owner in members
    with pytest.raises(ValueError, match="does not match"):
        user.update_from_json({"id": 2, "name": "z"})


def test_a_refresh_followed_by_a_load_keeps_the_hash(
    requests_mock: Any,
) -> None:
    """Refreshing and loading the same key leaves the object where it was."""
    requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": 1, "name": "b"}
    )
    user = User(json={"id": 1, "name": "a"})
    before = hash(user)

    user.refresh()
    between = hash(user)
    assert user.name == "b"

    assert before == between == hash(user)
    assert user == User(id=1)


def test_a_payload_without_the_key_is_accepted() -> None:
    """A payload that names no key cannot name another object."""
    user = User(json={"id": 1, "name": "a"})
    members = {user}

    user.update_from_json({"name": "z"})

    assert user.name == "z"
    assert user in members


def test_a_load_that_spells_a_uuid_key_another_way_keeps_the_url(
    requests_mock: Any,
) -> None:
    """The URL names the key, whichever way a response spells it."""
    requests_mock.get(
        "https://example.com/v1/token-owners/12345678-1234-5678-1234-567812345678/",
        json={"id": "12345678-1234-5678-1234-567812345678", "name": "a"},
    )
    owner = NamedTokenOwner(
        json={"id": "{12345678-1234-5678-1234-567812345678}"}
    )
    url = owner.url
    owner.refresh()

    assert owner.name == "a"

    assert (
        owner.url
        == url
        == "/token-owners/12345678-1234-5678-1234-567812345678/"
    )


def test_objects_of_a_model_without_a_key_equal_only_themselves(
    requests_mock: Any,
) -> None:
    """A keyless model compares and hashes by identity, and never fails."""
    address = Address(json={"street": "1 Main St"})
    twin = Address(json={"street": "1 Main St"})

    assert address == address  # noqa: PLR0124
    assert address != twin
    assert hash(address) == hash(address)
    assert len({address, twin}) == 2
    assert requests_mock.call_count == 0


def test_a_model_object_is_not_equal_to_a_value_that_is_not_a_model() -> None:
    """Comparing with a value that is not a model defers to the other side."""
    user = User(id=1)

    assert user.__eq__(1) is NotImplemented
    assert user.__eq__({"id": 1}) is NotImplemented
    assert user != 1
    assert user != {"id": 1, "name": "Ada"}


def test_objects_with_equal_keys_are_one_member_of_a_set_or_dict(
    requests_mock: Any,
) -> None:
    """A set or dict keeps one object for each key and finds it by key."""
    first = User(id=1)
    members = {first, User(json={"id": 1, "name": "Ada"}), User(id=2)}
    by_user = {first: "first", Todo(id=1): "todo"}

    assert len(members) == 2
    assert User(id=2) in members
    assert User(id=3) not in members
    assert by_user[User(id=1)] == "first"
    assert by_user[Todo(id=1)] == "todo"
    assert requests_mock.call_count == 0


def test_objects_of_one_key_assigned_to_an_eager_set_are_written_once(
    requests_mock: Any,
) -> None:
    """An eager set field sends the set it keeps, and only once."""
    patch = requests_mock.patch(
        "https://example.com/v1/named-set-teams/1/", status_code=204
    )
    team = NamedSetTeam(
        json={"id": 1, "members": [], "name": "n"}, write_mode="eager"
    )

    team.members = cast(Any, [User(id=1), User(id=1)])
    team.name = "m"

    assert team.members == {User(id=1)}
    assert [call.json() for call in patch.request_history] == [
        {"members": [1]},
        {"name": "m"},
    ]


@given(st.integers(), st.integers())
def test_equality_of_objects_by_key_is_reflexive_symmetric_and_hashable(
    left: int, right: int
) -> None:
    """Equal objects hash alike, and equality is that of the keys."""
    a = User(id=left)
    b = User(json={"id": right, "name": "Ada"})

    assert a == a  # noqa: PLR0124
    assert (a == b) == (b == a) == (left == right)
    if a == b:
        assert hash(a) == hash(b)


@json_object(write_only={"password"})
class Operator(ModelBase):
    """Model with a write-only field."""

    class_url = "/operators/"

    id: int
    name: str
    password: str


OPERATOR_URL = "https://example.com/v1/operators/4/"


def test_write_only_write_to_an_unloaded_object_leaves_nothing_pending(
    requests_mock: Any,
) -> None:
    """A write of only write-only fields answered by an empty body is spent.

    The object never keeps a write-only value, so a later ``save()`` must not
    send the password again.
    """
    patch = requests_mock.patch(OPERATOR_URL, json={})
    operator = Operator(id=4)
    operator.password = "secret"

    operator.save()

    assert patch.call_count == 1
    assert operator._pending_updates == {}
    operator.save()
    assert patch.call_count == 1


def test_a_load_that_omits_the_key_keeps_the_key_the_object_was_saved_under(
    requests_mock: Any,
) -> None:
    """The object keeps its identity when its detail response lacks the key."""
    requests_mock.get(OPERATOR_URL, json={"name": "Ada"})
    operator = Operator(id=4)

    assert operator.name == "Ada"

    assert operator.pk == 4
    assert operator.url == "/operators/4/"
    assert repr(operator) == "<Operator id=4>"


@pytest.mark.parametrize("status", [300, 304])
def test_filter_refuses_a_collection_redirect(
    requests_mock: Any, status: int
) -> None:
    """A collection answered with an unfollowed redirect has no items.

    Args:
        status: Redirect status that requests does not follow.
    """
    requests_mock.get(
        "https://example.com/v1/todos/", status_code=status, json=[{"id": 1}]
    )

    with pytest.raises(RedirectError, match=f"status {status}"):
        Todo.filter()


TODO_URL = "https://example.com/v1/todos/1/"


def test_write_options_reach_eager_assignments(requests_mock: Any) -> None:
    """Each eager ``PATCH`` in the block sends the block's options."""
    patch = requests_mock.patch(TODO_URL, status_code=204)
    todo = Todo(json=TENANT_TODO, write_mode="eager")

    with todo.write_options(headers={"If-Match": '"v1"'}) as same:
        assert same is todo
        todo.title = "b"

    assert patch.last_request.headers["If-Match"] == '"v1"'

    todo.title = "c"

    assert "If-Match" not in patch.last_request.headers


def test_write_options_reach_save_and_delete_but_not_loads(
    requests_mock: Any,
) -> None:
    """A lazy object sends the block's options with ``save()``, ``delete()``.

    Loading the object inside the block does not send them.
    """
    get = requests_mock.get(TODO_URL, json=TENANT_TODO)
    patch = requests_mock.patch(TODO_URL, status_code=204)
    delete = requests_mock.delete(TODO_URL, status_code=204)
    todo = Todo(id=1)

    with todo.write_options(headers={"If-Match": '"v1"'}):
        todo.title = "b"
        assert not patch.called
        todo.save()
        todo.delete()

    assert "If-Match" not in get.last_request.headers
    assert patch.last_request.headers["If-Match"] == '"v1"'
    assert delete.last_request.headers["If-Match"] == '"v1"'


def test_write_options_nest_and_yield_to_request_options(
    requests_mock: Any,
) -> None:
    """Inner blocks and a call's ``request_options`` merge over outer ones."""
    patch = requests_mock.patch(TODO_URL, status_code=204)
    todo = Todo(json=TENANT_TODO)

    with todo.write_options(headers={"X-Outer": "o", "X-Shared": "outer"}):
        with todo.write_options(headers={"X-Shared": "inner"}, timeout=7):
            todo.title = "b"
            todo.save(request_options={"headers": {"X-Call": "c"}})
            inner = patch.last_request
        todo.title = "c"
        todo.save()
        outer = patch.last_request

    assert inner.headers["X-Outer"] == "o"
    assert inner.headers["X-Shared"] == "inner"
    assert inner.headers["X-Call"] == "c"
    assert inner.timeout == 7
    assert outer.headers["X-Shared"] == "outer"
    assert outer.timeout != 7
    assert dict(todo._write_options) == {}


def test_write_options_are_restored_when_the_block_raises() -> None:
    """The block's options end with the block, even when it raises."""
    todo = Todo(json=TENANT_TODO)

    with pytest.raises(RuntimeError), todo.write_options(timeout=1):
        raise RuntimeError

    assert dict(todo._write_options) == {}


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"json": {}}, "cannot set json"),
        ({"stream": True}, "stream"),
        ({"method": "PUT"}, "cannot set method"),
    ],
)
def test_write_options_reject_invalid_options(
    options: dict[str, Any], match: str
) -> None:
    """Options that a write cannot send raise at the ``with`` statement.

    Args:
        options: Options that a write rejects.
        match: Pattern that the ``TypeError`` message should match.
    """
    todo = Todo(json=TENANT_TODO)

    with pytest.raises(TypeError, match=match), todo.write_options(**options):
        pass

    assert dict(todo._write_options) == {}


def test_save_rejects_unknown_request_option(requests_mock: Any) -> None:
    """A name that is not a request option fails before anything is sent."""
    todo = Todo(json=TENANT_TODO)
    todo.title = "b"

    with pytest.raises(TypeError, match="cannot set method"):
        todo.save(request_options={"method": "PUT"})

    assert requests_mock.call_count == 0
    assert todo._pending_updates == {"title": "b"}


class Shade(enum.Enum):
    """Enum whose JSON values are strings."""

    LIGHT = "light"
    DARK = "dark"


@json_object(
    field_map={"kind": "type"},
    write_map={"owner": "owner_id"},
    write_only={"secret"},
    read_only={"created"},
)
class Specimen(ModelBase):
    """Model whose fields cover each kind of JSON schema."""

    class_url = "/specimens/"

    id: int
    name: str
    score: float
    flag: bool
    when: datetime.datetime
    day: datetime.date
    token: uuid.UUID
    length: datetime.timedelta
    shade: Shade
    kind: typing.Literal["a", "b"]
    tags: list[str]
    labels: set[str]
    note: str | None
    extra: dict[str, Any]
    owner: User
    secret: str
    created: datetime.datetime


def test_json_schema_describes_what_a_model_reads() -> None:
    """The read schema uses read keys and omits write-only fields."""
    owner = {
        "anyOf": [
            {"type": "integer"},
            {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
            },
        ]
    }

    assert Specimen.json_schema() == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Specimen",
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "score": {"type": "number"},
            "flag": {"type": "boolean"},
            "when": {
                "anyOf": [
                    {"type": "string", "format": "date-time"},
                    {"type": "number"},
                ]
            },
            "day": {"type": "string", "format": "date"},
            "token": {"type": "string", "format": "uuid"},
            "length": {
                "anyOf": [
                    {"type": "string", "format": "duration"},
                    {"type": "number"},
                ]
            },
            "shade": {"enum": ["light", "dark"]},
            "type": {"enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "extra": {"type": "object", "additionalProperties": {}},
            "owner": owner,
            "created": {
                "anyOf": [
                    {"type": "string", "format": "date-time"},
                    {"type": "number"},
                ]
            },
        },
        "required": ["id"],
        "additionalProperties": True,
    }


def test_json_schema_describes_what_a_model_writes() -> None:
    """The write schema uses write keys and keeps only writable fields."""
    properties = Specimen.json_schema("write")["properties"]

    assert "created" not in properties
    assert properties["secret"] == {"type": "string"}
    assert properties["owner_id"] == {"type": "integer"}
    assert "owner" not in properties
    assert list(properties)[:3] == ["id", "name", "score"]


def test_json_schema_of_a_self_relation_has_the_models_key_schema() -> None:
    """A relation to the model itself is written as the model's own key."""

    @json_object
    class Employee(ModelBase):
        class_url = "/employees/"

        id: int
        manager: Employee | None
        reports: list[Employee]

    assert Employee.json_schema("write")["properties"]["manager"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }
    assert Employee.json_schema("write")["properties"]["reports"] == {
        "type": "array",
        "items": {"type": "integer"},
    }
    assert Employee.json_schema()["properties"]["reports"]["items"] == {
        "anyOf": [
            {"type": "integer"},
            {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
            },
        ]
    }


def test_json_schema_follows_declaration_order() -> None:
    """Properties follow declaration order, with or without defaults.

    A field with a default stays in the class's namespace where the class
    declares it, while ``json_object()`` adds a field without one at the end.
    """

    @json_object
    class Base(ModelBase):
        class_url = "/ordered/"

        id: int
        a: str
        b: int = 0

    @json_object
    class Ordered(Base):
        c: str = ""
        d: str
        a: str

    assert list(Ordered.json_schema()["properties"]) == [
        "id",
        "a",
        "b",
        "c",
        "d",
    ]


@json_object
class Odd(ModelBase):
    """Model with a field that keeps any JSON."""

    class_url = "/odd/"

    id: int
    address: Any


def test_json_schema_allows_any_value_of_an_any_field() -> None:
    """A field annotated ``Any`` accepts any value, which its schema says."""
    properties = Odd.json_schema("write")["properties"]

    assert properties["address"] == {}


def test_json_schema_rejects_an_unknown_mode() -> None:
    """Only the read and write modes exist."""
    with pytest.raises(ValueError, match="mode must be"):
        Specimen.json_schema("both")  # type: ignore[arg-type]


def test_json_schema_reports_an_unresolvable_annotation() -> None:
    """A field whose annotation does not resolve fails as a read does."""

    @json_object
    class Broken(ModelBase):
        class_url = "/broken/"

        id: int
        missing: NoSuchType  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(NameError, match="NoSuchType"):
        Broken.json_schema()


def test_alpha_conversion_overrides_are_refused() -> None:
    """An override of ``from_json()`` or ``to_json()`` with an old signature.

    eazyrest no longer calls them, so the override would be skipped without a
    word. The alphas' signature has a ``conversion`` parameter, and 0.9.0's has
    a ``ty`` parameter. A method of either name with neither parameter is the
    class's own.
    """
    with pytest.raises(TypeError, match=r"from_json\(\) is no longer called"):

        class Reading(JSONObject):
            def from_json(
                self, value: Any, conversion: Any, **kwargs: Any
            ) -> Any:
                return value

    with pytest.raises(TypeError, match=r"to_json\(\) is no longer called"):

        class Writing(JSONObject):
            @classmethod
            def to_json(cls, value: Any, conversion: Any) -> Any:
                return value

    with pytest.raises(TypeError, match=r"from_json\(\) is no longer called"):

        class OldReading(JSONObject):
            def from_json(self, value: Any, ty: Any) -> Any:
                return value

    with pytest.raises(TypeError, match=r"to_json\(\) is no longer called"):

        class OldWriting(JSONObject):
            def to_json(self, value: Any, ty: Any) -> Any:
                return value

    class Helper(JSONObject):
        def to_json(self) -> dict[str, Any]:
            return {}

    assert Helper().to_json() == {}

    class ConversionMixin:
        def from_json(self, value: Any, conversion: Any) -> Any:
            return value

    with pytest.raises(
        TypeError, match=r"ConversionMixin\.from_json\(\) is no longer called"
    ):

        class Mixed(ConversionMixin, JSONObject):
            pass


def test_an_assigned_api_is_refused(requests_mock: Any) -> None:
    """An API assigned to a class's ``api``, rather than registered, raises.

    The assignment would replace what resolves each object's own API, so
    every ``api=`` would be ignored.
    """
    with pytest.raises(TypeError, match=r"register_api"):

        class Defining(JSONObject):
            api = API("https://service.example.com")

    class Base(JSONObject):
        pass

    @json_object
    class Assigned(Base):
        class_url = "/assigned/"

        id: int

    Base.api = API("https://service.example.com")

    with pytest.raises(UsageError, match=r"Base\.register_api\(api\)"):
        Assigned(id=1, api=API("https://alice.example.com"))
    # Nothing is sent, so a create() is not repeated after the correction.
    for call in (
        lambda: Assigned.create(id=1),
        lambda: list(Assigned.filter()),
        lambda: Assigned.get(id=1),
        lambda: Assigned.get_page(),
    ):
        with pytest.raises(UsageError, match=r"Base\.register_api\(api\)"):
            call()
    assert requests_mock.call_count == 0


def test_an_api_assigned_on_a_mixin_names_the_models_register_api(
    requests_mock: Any,
) -> None:
    """The advice for a mixin's ``api`` is the model's ``register_api()``.

    A plain mixin has no ``register_api()``, so the message names the model
    that is being used.
    """

    class Mixin:
        pass

    @json_object
    class Base(Mixin, JSONObject):
        class_url = "/bases/"

        id: int

    Base.register_api(API("https://good.example.com"))
    Mixin.api = API("https://other.example.com")  # type: ignore[attr-defined]

    with pytest.raises(UsageError) as info:
        Base(json={"id": 1})
    assert "Mixin.api was assigned" in str(info.value)
    assert "Base.register_api(api)" in str(info.value)
    assert "Mixin.register_api" not in str(info.value)
    assert requests_mock.call_count == 0


def test_a_mixin_defining_api_is_refused_naming_the_model_register_api() -> (
    None
):
    """A mixin whose body defines ``api`` is refused when the model is defined.

    The message names the model's ``register_api()``, not the mixin's.
    """

    class Mixin:
        api = API("https://service.example.com")

    with pytest.raises(TypeError) as info:

        class Base(Mixin, JSONObject):
            pass

    assert "Mixin.api was assigned" in str(info.value)
    assert "Base.register_api(api)" in str(info.value)
    assert "Mixin.register_api" not in str(info.value)


def test_a_mixin_after_the_base_class_may_define_an_unrelated_api() -> None:
    """An ``api`` that a model never reads does not shadow the model's.

    ``JSONObject`` precedes the mixin in the MRO, so ``User.api`` is eazyrest's
    attribute and the mixin's is never read.
    """

    class Helper:
        api = "unrelated"

    class User(JSONObject, Helper):  # type: ignore[misc]
        pass

    api = API("https://good.example.com")
    User.register_api(api)
    assert User.api is api


def test_an_api_assigned_none_is_refused(requests_mock: Any) -> None:
    """``None`` assigned to a class's ``api`` is an assignment like any API."""

    class Base(JSONObject):
        pass

    @json_object
    class Nulled(Base):
        class_url = "/nulled/"

        id: int
        name: str

    Base.register_api(API("https://alice.example.com"))
    for method in ("GET", "PATCH"):
        requests_mock.register_uri(
            method, re.compile(".*"), json={"id": 1, "name": "n"}
        )
    existing = Nulled(json={"id": 1, "name": "a"})
    existing.name = "b"
    Base.api = None  # type: ignore[assignment]
    try:
        for call in (
            lambda: Nulled(id=1),
            lambda: list(Nulled.filter()),
            existing.save,
        ):
            with pytest.raises(UsageError, match=r"Base\.register_api\(api\)"):
                call()
    finally:
        del Base.api

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "request_kind",
    [
        "save",
        "full save",
        "delete",
        "lazy load",
        "eager assign",
        "eager write-only",
    ],
)
def test_an_api_assigned_after_an_object_is_built_is_refused(
    requests_mock: Any, request_kind: str
) -> None:
    """An existing object's requests do not follow an assigned ``api``.

    The assignment would send them through that API and ignore the API that the
    object was built with.

    Args:
        request_kind: The request that the object sends.
    """

    class Base(JSONObject):
        pass

    @json_object(write_only={"secret"})
    class Later(Base):
        class_url = "/later/"

        id: int
        name: str
        secret: str

    for method in ("GET", "PATCH", "DELETE"):
        requests_mock.register_uri(
            method, re.compile(".*"), json={"id": 1, "name": "n"}
        )
    alice = API("https://alice.example.com")
    if request_kind == "lazy load":
        obj = Later(id=1, api=alice)
    elif request_kind.startswith("eager"):
        obj = Later(json={"id": 1, "name": "a"}, api=alice, write_mode="eager")
    else:
        obj = Later(json={"id": 1, "name": "a"}, api=alice)

    def send() -> None:
        if request_kind == "save":
            obj.save()
        elif request_kind == "full save":
            obj.save(full=True)
        elif request_kind == "eager assign":
            obj.name = "b"
        elif request_kind == "eager write-only":
            obj.secret = "s"
        elif request_kind == "delete":
            obj.delete()
        else:
            _ = obj.name

    if request_kind == "save":
        obj.name = "b"
    Base.api = API("https://mallory.example.com")
    try:
        with pytest.raises(UsageError, match=r"Base\.register_api\(api\)"):
            send()
    finally:
        del Base.api

    assert requests_mock.call_count == 0


class WriterBase(JSONObject):
    """Base of a related model, whose ``api`` the tests assign."""


class ArticleBase(JSONObject):
    """Base of a model that relates to a ``Writer``."""


WriterBase.register_api(API("https://alice.example.com"))
ArticleBase.register_api(API("https://alice.example.com"))


@json_object
class Writer(WriterBase):
    """Related model that implements bulk lookup."""

    class_url = "/writers/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: Any, *, api: Any, **kwargs: Any
    ) -> dict[Any, Any]:
        """Send a bulk request through ``api``."""
        api.get("/writers/bulk/")
        return {}


@json_object
class Article(ArticleBase):
    """Model with a relation to a ``Writer``."""

    class_url = "/articles/"

    id: int
    author: Writer


@pytest.mark.parametrize("query", ["filter", "get_page"])
def test_a_prefetch_of_a_model_with_an_assigned_api_is_refused(
    requests_mock: Any, query: str
) -> None:
    """A prefetch raises before any request when the related ``api`` is set.

    The bulk request would go through the assigned API.

    Args:
        query: The query that prefetches.
    """
    requests_mock.register_uri(
        "GET", re.compile(".*"), json=[{"id": 1, "author": 1}]
    )
    WriterBase.api = API("https://mallory.example.com")

    def send() -> None:
        if query == "filter":
            list(Article.filter(prefetch="author"))
        else:
            Article.get_page(prefetch="author")

    try:
        with pytest.raises(UsageError, match=r"Base\.register_api\(api\)"):
            send()
    finally:
        del WriterBase.api

    assert requests_mock.call_count == 0


_READ_ONLY_VALUE = (
    "Address has no primary-key field, so it is a read-only value. Build it "
    "with json="
)


@json_object
class Address(ModelBase):
    """Model without a key, as a nested serializer without an id sends."""

    street: str
    city: str = ""


@json_object
class AddressWithUrl(ModelBase):
    """Model without a key that has a collection URL to create from."""

    class_url = "/addresses/"

    street: str
    city: str = ""


@json_object
class AddressWithTags(ModelBase):
    """Model without a key that has a list field."""

    street: str
    city: str = ""
    tags: list[str]


AddressAlias = NewType("AddressAlias", Address)


@pytest.mark.parametrize(
    "annotation",
    [
        Address,
        Optional[Address],  # noqa: UP045
        Address | None,
        list[Address],
        Iterable[Address],
        tuple[Address, ...],
        set[Address],
        dict[str, Address],
        list[Address | None],
        AddressAlias,
        Annotated[Address, "note"],
        "Address",
        "list[Address]",
    ],
    ids=repr,
)
def test_a_model_without_a_key_is_refused_as_the_type_of_a_field(
    annotation: Any,
) -> None:
    """A field of such a type is refused when its class is defined."""
    with pytest.raises(
        TypeError,
        match=re.escape(
            "Address has no primary-key field, so it cannot be the type of a "
            "field. Declare nested JSON as a TypedDict or a dataclass, "
            "or declare the key field of Address."
        ),
    ):
        json_object(
            type(
                "Holder",
                (ModelBase,),
                {
                    "__module__": __name__,
                    "__annotations__": {"id": int, "address": annotation},
                },
            )
        )


class Undecorated(ModelBase):
    """Model that declares a key but lacks ``@json_object``."""

    class_url = "/undecorated/"

    id: int


def test_an_undecorated_model_is_refused_as_the_type_of_a_field() -> None:
    """A model that lacks ``@json_object`` is told to use it.

    The class declares a key, so a message about a missing key field would
    mislead.
    """
    with pytest.raises(
        TypeError,
        match=re.escape(
            "Undecorated must be decorated with @json_object, which declares "
            "its fields, before it can be the type of a field."
        ),
    ):

        @json_object
        class Holder(ModelBase):
            class_url = "/holders/"

            id: int
            member: Undecorated


class UndecoratedAddress(Address):
    """Subclass of a decorated model without a key, which declares one."""

    id: int


class UndecoratedAlias(Address):
    """Subclass of a decorated model without a key, which declares nothing."""


def test_an_undecorated_subclass_without_a_key_is_told_what_it_needs() -> None:
    """One message names the decorator, the key, and the alternative.

    Decorating the class would otherwise only change the message to one about
    the missing key, and a class meant as nested JSON needs a value type.
    """
    with pytest.raises(
        TypeError,
        match=re.escape(
            "UndecoratedAlias must be decorated with @json_object, which "
            "declares its fields, before it can be the type of a field. Its "
            "decorator also needs a primary-key field, which the class "
            "declares or json_object(pk=...) names. Declare nested JSON "
            "without a key as a TypedDict or a dataclass instead."
        ),
    ):

        @json_object
        class Holder(ModelBase):
            class_url = "/holders/"

            id: int
            member: UndecoratedAlias


def test_an_undecorated_subclass_is_refused_as_the_type_of_a_field() -> None:
    """A subclass that lacks ``@json_object`` is told to use it.

    Its decorated base has no key, but the subclass declares one, so a message
    that only called it keyless would mislead.
    """
    with pytest.raises(
        TypeError,
        match=re.escape(
            "UndecoratedAddress must be decorated with @json_object, which "
            "declares its fields, before it can be the type of a field."
        ),
    ):

        @json_object
        class Holder(ModelBase):
            class_url = "/holders/"

            id: int
            member: UndecoratedAddress


def test_a_model_without_a_key_is_refused_when_a_later_class_is_defined(
    requests_mock: Any,
) -> None:
    """A type defined after its holder is refused when it is first used.

    The holder cannot resolve the name when it is defined.
    """
    holder = EarlyHolder(json={"id": 1, "late": {"name": "x"}, "n": 2})
    message = "LateKeyless has no primary-key field, so it cannot be the type"

    assert holder.n == 2
    with pytest.raises(TypeError, match=message):
        _ = holder.late
    with pytest.raises(TypeError, match=message):
        holder.late = LateKeyless(json={"name": "y"})
    with pytest.raises(TypeError, match=message):
        EarlyHolder.json_schema()
    assert requests_mock.call_count == 0


@json_object
class EarlyHolder(ModelBase):
    """Model whose field names a model without a key defined after it."""

    class_url = "/early-holders/"

    id: int
    late: LateKeyless
    n: int


@json_object
class LateKeyless(ModelBase):
    """Model without a key, defined after the model that names it."""

    name: str


@json_object
class Chapter(ModelBase):
    """Model that refers to itself and to a model defined after it."""

    class_url = "/chapters/"

    id: int
    parent: Chapter | None
    children: list[Chapter]
    book: Book


@json_object
class Book(ModelBase):
    """Model that refers back to the model that refers to it."""

    class_url = "/books/"

    id: int
    chapters: list[Chapter]


def test_models_with_keys_may_refer_to_themselves_and_each_other() -> None:
    """A key that exists is found while a class is being defined."""
    chapter = Chapter(
        json={
            "id": 2,
            "parent": {"id": 1, "parent": None, "children": [], "book": 5},
            "children": [3],
            "book": {"id": 5, "chapters": [1, 2]},
        }
    )

    assert chapter.parent is not None
    assert chapter.parent.id == 1
    assert [child.pk for child in chapter.children] == [3]
    assert [item.pk for item in chapter.book.chapters] == [1, 2]

    @json_object
    class Node(ModelBase):
        class_url = "/nodes/"

        id: int
        next: Node | None

    assert (
        Node(json={"id": 1, "next": {"id": 2, "next": None}}).next is not None
    )


def test_encoding_a_model_without_a_key_as_an_annotation_is_refused() -> None:
    """``encode_json()`` reads an annotation as a field does."""
    with pytest.raises(TypeError, match="Address has no primary-key field"):
        encode_json([], list[Address])


def test_an_object_without_a_key_merges_json_from_a_push(
    requests_mock: Any,
) -> None:
    """It has no key to compare, so the payload merges as it is."""
    address = Address(json={"street": "1 Main St"})

    address.update_from_json({"city": "Z"})

    assert address.city == "Z"
    assert address.street == "1 Main St"
    assert address.json == {"street": "1 Main St", "city": "Z"}
    assert requests_mock.call_count == 0


def test_fields_given_to_an_object_without_a_key_are_refused(
    requests_mock: Any,
) -> None:
    """Such an object is a read-only value, built with ``json=``."""
    with pytest.raises(UsageError, match=_READ_ONLY_VALUE):
        Address(street="1 Main St")
    with pytest.raises(UsageError, match=_READ_ONLY_VALUE):
        Address(json={"street": "1 Main St"}, city="Z")
    with pytest.raises(UsageError, match=_READ_ONLY_VALUE):
        Address(pk=1)
    assert Address().pk is None
    assert Address(api=API("https://example.com/v1/")).pk is None
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
@pytest.mark.parametrize("field", ["street", "city"])
def test_assigning_a_field_of_an_object_without_a_key_is_refused(
    requests_mock: Any, write_mode: WriteMode, field: str
) -> None:
    """A field in the object's JSON and one it lacks are both refused.

    Args:
        write_mode: Write mode of the object.
        field: Name of the assigned field.
    """
    address = Address(
        json={"street": "1 Main St"},
        api=API("https://example.com/v1/", default_write_mode=write_mode),
    )

    with pytest.raises(UsageError, match=_READ_ONLY_VALUE):
        setattr(address, field, "x")
    assert address.json == {"street": "1 Main St"}
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("verb", ["save", "refresh", "delete"])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_an_object_without_a_key_has_nothing_to_save_refresh_or_delete(
    requests_mock: Any, write_mode: WriteMode, verb: str
) -> None:
    """These raise always, also ``save()`` with nothing pending.

    Args:
        write_mode: Write mode of the object.
        verb: Name of the method.
    """
    address = Address(
        json={"street": "1 Main St"},
        api=API("https://example.com/v1/", default_write_mode=write_mode),
    )

    with pytest.raises(
        UsageError,
        match=f"Address has no primary-key field, so it has nothing to {verb}",
    ):
        getattr(address, verb)()
    assert address.json == {"street": "1 Main St"}
    assert requests_mock.call_count == 0


def test_saving_an_object_without_a_key_in_full_is_refused(
    requests_mock: Any,
) -> None:
    """``save(full=True)`` has no URL to send to."""
    address = Address(json={"street": "1 Main St"})

    with pytest.raises(UsageError, match="nothing to save"):
        address.save(full=True)
    assert requests_mock.call_count == 0


def test_creating_an_object_without_a_key_is_refused_before_a_request(
    requests_mock: Any,
) -> None:
    """There is nothing to create, so no request is sent."""
    requests_mock.post(
        "https://example.com/v1/addresses/", json={"street": "1 Main St"}
    )

    with pytest.raises(UsageError, match="nothing to create"):
        AddressWithUrl.create(
            api=API("https://example.com/v1/"), street="1 Main St"
        )
    assert requests_mock.request_history == []


def test_getting_or_creating_an_object_without_a_key_sends_no_request(
    requests_mock: Any,
) -> None:
    """The refusal comes before the lookup, not after it finds nothing."""
    requests_mock.get(
        "https://example.com/v1/addresses/", json={"results": []}
    )

    with pytest.raises(UsageError, match="nothing to create"):
        AddressWithUrl.get_or_create(
            api=API("https://example.com/v1/"), street="1 Main St"
        )
    assert requests_mock.request_history == []


def test_a_partial_object_without_a_key_never_loads_or_queues_changes(
    requests_mock: Any,
) -> None:
    """A missing field raises ``AttributeError`` and nothing is queued."""
    address = AddressWithTags(json={"street": "1 Main St", "tags": ["a"]})
    address.update_from_json({"street": "2 Main St"})
    address._json_is_partial = True
    address.tags.append("b")

    with pytest.raises(AttributeError, match="missing"):
        _ = address.city

    assert requests_mock.request_history == []
    assert address.json == {"street": "2 Main St", "tags": ["a"]}
    assert address._pending_updates == {}


def test_an_object_without_a_key_has_no_key_and_no_url(
    requests_mock: Any,
) -> None:
    """It reads its JSON, has the key ``None``, and cannot be addressed."""
    address = Address(json={"street": "1 Main St", "city": "X"})

    assert address.street == "1 Main St"
    assert address.pk is None
    with pytest.raises(ValueError, match="has no primary-key field"):
        _ = address.url
    with pytest.raises(AttributeError, match="has no primary-key field"):
        address.pk = 1
    assert requests_mock.call_count == 0


class _KeyUser:
    """A class for a key annotation that names a class as a type."""


@pytest.mark.parametrize(
    "form",
    [typing.Final[int], type[_KeyUser], Callable[[int], int]],
    ids=["final", "type-of-class", "callable"],
)
def test_a_key_type_that_cannot_convert_is_refused_when_decorated(
    form: Any,
) -> None:
    """A key whose annotation resolves and cannot convert names the field.

    The refusal is the key's declaration, a plain ``TypeError``, raised when
    the class is decorated, not a ``url`` that cannot be built or a key that
    cannot be hashed once the object exists.

    Args:
        form: The type of the model's key field.
    """
    with pytest.raises(TypeError, match=r"Box\.id is the primary key") as e:
        _box_keyed_by(form)
    assert not isinstance(e.value, FieldConversionError)
    assert isinstance(e.value.__cause__, TypeError)


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="The type statement is new in 3.12."
)
def test_a_key_alias_of_a_later_class_decorates_and_is_checked_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key alias that names a class defined later is not refused early.

    The class is not defined when the model is decorated, so the model
    decorates. Once the class is defined and is not one value, first use
    refuses the key.

    Args:
        monkeypatch: Defines the class as a global, after the model is
            decorated.
    """
    namespace: dict[str, Any] = {}
    exec("type PendingLaterKey = LaterAliasedKey", namespace)  # noqa: S102
    box = _box_keyed_by(namespace["PendingLaterKey"])
    monkeypatch.setitem(namespace, "LaterAliasedKey", list[int])

    with pytest.raises(TypeError, match=r"Box\.id is the primary key"):
        box(json={"id": [1]}).url  # noqa: B018


def test_a_key_alias_of_a_string_naming_nothing_yet_is_checked_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key alias whose string names nothing yet decorates, then resolves.

    Until the name is defined, building an object raises the ``TypeError`` that
    says the alias cannot be resolved, which is no conversion error.

    Args:
        monkeypatch: Defines the name as a global, after the model is
            decorated.
    """
    PendingStringKey = TypeAliasType(
        "PendingStringKey",
        "LaterStringKey",  # type: ignore[name-defined]  # noqa: F821
    )
    box = _box_keyed_by(PendingStringKey)

    with pytest.raises(TypeError, match="cannot be resolved") as error:
        box(json={"id": 1})
    assert not isinstance(error.value, FieldConversionError)
    assert "analyzed_type" not in vars(box._pk)

    monkeypatch.setitem(globals(), "LaterStringKey", int)

    assert box(json={"id": 1}).url == "/boxes/1/"


def test_decorating_a_model_resolves_each_field_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decoration resolves each declared annotation once, the key included.

    Args:
        monkeypatch: Counts the calls that resolve an annotation.
    """
    resolve = typing.get_type_hints
    calls: list[Any] = []

    def counting(obj: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(obj)
        return resolve(obj, *args, **kwargs)

    monkeypatch.setattr(typing, "get_type_hints", counting)

    @json_object
    class Ledger(ModelBase):
        class_url = "/ledgers/"

        id: int
        title: str
        opened: datetime.date

    assert len(calls) == 3
    assert Ledger(json={"id": 1, "title": "a", "opened": "2024-04-01"}).title


@dataclasses.dataclass
class _ClassVarForCheckers:
    """A dataclass whose class variable names a type unbound at run time."""

    count: int
    ratio: ClassVar[Fraction]


@dataclasses.dataclass
class _FieldForCheckers:
    """A dataclass whose field names a type unbound at run time."""

    count: int
    ratio: Fraction


def test_a_dataclass_with_a_checker_only_class_variable_type_converts() -> (
    None
):
    """Only a field's annotation must resolve, not a class variable's."""

    @json_object
    class Tally(ModelBase):
        class_url = "/tallies/"

        id: int
        entry: _ClassVarForCheckers

    tally = Tally(json={"id": 1, "entry": {"count": 2}})

    assert tally.entry == _ClassVarForCheckers(count=2)


def test_a_dataclass_whose_field_names_a_checker_only_type_still_raises() -> (
    None
):
    """A field's annotation that cannot resolve is an error on first use."""

    @json_object
    class Tally(ModelBase):
        class_url = "/tallies/"

        id: int
        entry: _FieldForCheckers

    tally = Tally(json={"id": 1, "entry": {"count": 2, "ratio": "1"}})

    with pytest.raises(
        FieldConversionError, match="annotations of _FieldForCheckers"
    ):
        tally.entry  # noqa: B018


_ShadowedDate = datetime.date


@dataclasses.dataclass
class _ShadowedDateEntry:
    """A dataclass whose class variable has the name of a module's type."""

    when: _ShadowedDate
    _ShadowedDate: ClassVar[type] = datetime.datetime


def test_a_method_does_not_shadow_a_module_type_in_a_model_field() -> None:
    """A string annotation names the module's type, not a method of the class.

    The method has the name of a type that the module imports, and the field's
    annotation is a string that uses that name.
    """

    @json_object
    class Visit(ModelBase):
        class_url = "/visits/"

        id: int
        created: _ShadowedDate

        def _ShadowedDate(self) -> str:
            return "not a type"

    visit = Visit(json={"id": 1, "created": "2026-11-17"})

    assert visit.created == datetime.date(2026, 11, 17)


def test_a_class_variable_does_not_shadow_a_module_type_in_a_dataclass() -> (
    None
):
    """A dataclass member's string annotation names the module's type."""

    @json_object
    class Visit(ModelBase):
        class_url = "/visits/"

        id: int
        entry: _ShadowedDateEntry

    visit = Visit(json={"id": 1, "entry": {"when": "2026-11-17"}})

    assert type(visit.entry.when) is datetime.date


def test_unknown_name_in_quoted_alias_reports_the_alias_and_module() -> None:
    """The error names the alias and the module where its string was read."""
    model: Any = json_object(
        type(
            "Aliased",
            (ModelBase,),
            {
                "__annotations__": {"id": int, "value": QuotedMissing},
                "class_url": "/aliased/",
            },
        )
    )

    with pytest.raises(FieldConversionError) as refused:
        _ = model(json={"id": 1, "value": ["bad"]}).value

    assert (
        "Type alias QuotedMissing names a type by a string that cannot be "
        f"resolved in module {__name__}"
    ) in str(refused.value)
