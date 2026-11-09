"""Tests for JSON-backed REST objects."""

from __future__ import annotations

import datetime
import decimal
import email
import email.message
import enum
import inspect
import sys
import types
import typing
import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from ipaddress import IPv4Address
from typing import Annotated, Any, ClassVar, TypeVar, cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st
from requests_mock import ANY as requests_mock_ANY
from typing_extensions import TypeAliasType

from eazyrest import (
    API,
    Client,
    DoesNotExist,
    EazyrestError,
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    RedirectError,
    Response,
    ResponseShapeError,
    WriteMode,
    encode_json,
    json_object,
)

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
        users = list(cls.filter(id=sorted(pks), api=api))
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


def test_object_with_overridden_url_loads_without_primary_key(
    requests_mock: Any,
) -> None:
    """A singleton resource that needs no key loads on first read."""

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

    me = CurrentUser()

    assert me.name == "Ada"
    assert me.pk == 7
    assert requests_mock.call_count == 1


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
    members: Iterable[User]


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


class IssueStatus(str, enum.Enum):
    """Status values for enum conversion tests."""

    OPEN = "open"
    CLOSED = "closed"


class Priority(str, enum.Enum):
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


def test_object_json_raises_for_empty_list_response(
    requests_mock: Any,
) -> None:
    """Empty list object responses should raise ``DoesNotExist``."""
    requests_mock.get("https://example.com/v1/todos/1/", json=[])

    todo = Todo(id=1)

    try:
        _ = todo.json
    except DoesNotExist:
        pass
    else:
        raise AssertionError("Expected DoesNotExist for empty object response")


def test_object_json_rejects_list_of_several_objects(
    requests_mock: Any,
) -> None:
    """Only a singleton list holds one object."""
    requests_mock.get(
        "https://example.com/v1/users/1/",
        json=[{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}],
    )

    with pytest.raises(MultipleObjectsReturned):
        _ = User(id=1).name


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
        tzinfo=datetime.timezone.utc,
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
    """Iterable relations should not fetch nested objects until iterated."""
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
    assert isinstance(members, Iterable)

    loaded = list(members)

    assert [member.name for member in loaded] == ["Ada", "Grace"]
    assert requests_mock.call_count == 3


def test_lazy_related_collection_is_single_pass() -> None:
    """A second traversal raises instead of creating new members."""
    team = Team(json={"id": 1, "members": [2, 3]})

    members = iter(team.members)
    first = next(members)
    rest = list(members)

    assert first.pk == 2
    assert [member.pk for member in rest] == [3]
    with pytest.raises(RuntimeError, match=r"Team\.members is a lazy related"):
        iter(team.members)


def test_encoding_lazy_related_collection_keeps_its_traversal(
    requests_mock: Any,
) -> None:
    """Assigning or creating from the field does not use it up."""
    post = requests_mock.post(
        "https://example.com/v1/teams/", json={"id": 2, "members": [2, 3]}
    )
    team = Team(json={"id": 1, "members": [2, 3]})

    team.members = team.members
    Team.create(members=team.members)

    assert post.last_request.json() == {"members": [2, 3]}
    assert team._pending_updates == {}
    assert [member.pk for member in team.members] == [2, 3]


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
    assert first == datetime.datetime(
        2024, 4, 1, 12, 0, tzinfo=datetime.timezone.utc
    )


def test_datetime_list_field_decodes_nested_items() -> None:
    """Collection items should use nested datetime conversion."""
    timeline = Timeline(
        json={"id": 1, "starts_at": [1711972800.0, 1711976400.0]}
    )

    assert timeline.starts_at == [
        datetime.datetime(2024, 4, 1, 12, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2024, 4, 1, 13, 0, tzinfo=datetime.timezone.utc),
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
    start = datetime.datetime(2024, 4, 1, 12, tzinfo=datetime.timezone.utc)

    assert isinstance(timeline.owner, User)
    assert timeline.owner.id == 2
    assert timeline.starts_at == [start]
    assert typing.get_args(vars(AnnotatedTimeline)["starts_at"].ty)[1:] == (
        "all",
    )

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
            datetime.datetime(2024, 4, 1, 12, tzinfo=datetime.timezone.utc),
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
            datetime.datetime(2024, 4, 1, 12, tzinfo=datetime.timezone.utc),
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
        2024, 4, 1, 12, tzinfo=datetime.timezone.utc
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
        ("09:30Z", datetime.time(9, 30, tzinfo=datetime.timezone.utc)),
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
    """A time reads from ISO 8601 alike on every supported Python.

    Python 3.10's ``time.fromisoformat()`` rejects ``Z`` and fractions of
    other than three or six digits.

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
    """A number is not read as seconds since midnight, as Pydantic reads it."""
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
        return {user.pk: user for user in cls.filter(key=sorted(pks))}


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
    when = datetime.datetime(2026, 1, 3, tzinfo=datetime.timezone.utc)

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
            2026, 1, 3, 10, 30, tzinfo=datetime.timezone.utc
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
            tzinfo=datetime.timezone.utc,
        ),
        datetime.datetime(
            2024,
            4,
            1,
            13,
            0,
            tzinfo=datetime.timezone.utc,
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
    assert users.last_request.qs == {"id": ["2", "3"]}


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
    monkeypatch.setattr(Todo, "api", parent_api)
    monkeypatch.setattr(User, "api", related_api)
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

    assert isinstance(members, Iterable)
    assert requests_mock.call_count == 2
    assert [member.name for member in members] == ["Ada", "Grace"]
    assert users.last_request.qs == {"id": ["2", "3"]}


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
    assert users.last_request.qs == {"id": ["3"]}


@pytest.mark.parametrize(
    ("members", "expected_names", "expected_ids"),
    [
        ([None], [None], []),
        (
            [2, None, {"id": 3, "name": "Grace"}, 2],
            ["Ada", None, "Grace", "Ada"],
            ["2"],
        ),
    ],
)
def test_prefetch_skips_null_collection_members(
    requests_mock: Any,
    members: list[Any],
    expected_names: list[str | None],
    expected_ids: list[str],
) -> None:
    """Null and embedded members need no primary-key lookup.

    Args:
        members: JSON value of the team's ``members`` collection.
        expected_names: Names of the loaded members, with ``None`` for a null
            member.
        expected_ids: Primary keys that the prefetch request should ask for, as
            query-string values.
    """

    @json_object
    class MaybeTeam(ModelBase):
        """Model with nullable members in a related collection."""

        class_url = "/maybe-teams/"

        id: int
        members: list[User | None]

    requests_mock.get(
        "https://example.com/v1/maybe-teams/",
        json=[{"id": 1, "members": members}],
    )
    users = requests_mock.get(
        "https://example.com/v1/users/", json=[{"id": 2, "name": "Ada"}]
    )

    team = next(MaybeTeam.filter(prefetch="members"))

    assert [
        member.name if member is not None else None for member in team.members
    ] == expected_names
    assert users.call_count == bool(expected_ids)
    if expected_ids:
        assert users.last_request.qs == {"id": expected_ids}
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
    assert repr(User()) == "<User id=None>"
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


def test_object_without_primary_key_reports_missing_url(
    requests_mock: Any,
) -> None:
    """An object created without a key cannot load, and says why."""
    user = User()

    assert user.pk is None
    with pytest.raises(ValueError, match="primary key None, so it has no URL"):
        _ = user.name
    with pytest.raises(ValueError, match="no URL"):
        User(name="Ada")
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "body", [{"status_code": 204}, {"json": {}}, {"json": {"title": "a"}}]
)
def test_create_without_returned_key_reports_missing_url(
    requests_mock: Any, body: dict[str, Any]
) -> None:
    """A creation whose response lacks the new key leaves the object keyless.

    Its JSON is partial, and loading the rest would need the key, so reading
    a field that the JSON lacks says that the object has no URL.

    Args:
        body: Keyword arguments of the mocked ``POST`` response.
    """
    requests_mock.post("https://example.com/v1/todos/", **body)

    todo = Todo.create(title="a")

    assert todo.pk is None
    assert todo.title == "a"
    with pytest.raises(ValueError, match="primary key None, so it has no URL"):
        _ = todo.completed
    with pytest.raises(ValueError, match="no URL"):
        todo.save(full=True)
    assert requests_mock.call_count == 1


def test_keyless_push_reports_missing_url(requests_mock: Any) -> None:
    """A push without a key into a keyless object cannot load the rest."""
    todo = Todo()

    todo.update_from_json({"title": "a"})

    assert todo.pk is None
    assert todo.title == "a"
    with pytest.raises(ValueError, match="no URL"):
        _ = todo.completed
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


@pytest.mark.parametrize("state", ["unloaded", "loaded", "refreshed"])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_primary_key_assignment_preserves_saved_identity(
    requests_mock: Any, state: str, write_mode: WriteMode
) -> None:
    """Key changes address the saved object in every loading state.

    Args:
        state: Loading state of the user before its key changes, one of
            ``"unloaded"``, ``"loaded"``, or ``"refreshed"``.
        write_mode: Write mode of the user.
    """
    requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": 1, "name": "Ada"}
    )
    patch = requests_mock.patch(
        "https://example.com/v1/users/1/", status_code=204
    )
    if state == "unloaded":
        user = User(id=1, write_mode=write_mode)
    else:
        user = User(json={"id": 1, "name": "Ada"}, write_mode=write_mode)
        if state == "refreshed":
            user.refresh()

    assert requests_mock.call_count == 0
    user.pk = 2

    assert user.pk == 2
    assert patch.call_count == (1 if write_mode == "eager" else 0)
    if write_mode == "lazy":
        assert user.url == "/users/1/"
    user.save()

    assert patch.call_count == 1
    assert patch.last_request.json() == {"id": 2}
    assert user.url == "/users/2/"


def test_failed_unloaded_primary_key_write_keeps_saved_identity(
    requests_mock: Any,
) -> None:
    """A rejected eager key change preserves the original resource."""
    requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": 1, "name": "Ada"}
    )
    requests_mock.patch("https://example.com/v1/users/1/", status_code=400)
    user = User(id=1, write_mode="eager")

    with pytest.raises(requests.HTTPError):
        user.id = 2

    assert user.pk == 1
    assert user.url == "/users/1/"


def test_lazy_primary_key_change_saves_to_saved_url(
    requests_mock: Any,
) -> None:
    """``save()`` sends a new primary key to the object's saved URL."""
    old = requests_mock.patch(
        "https://example.com/v1/users/1/",
        json={"id": 2, "name": "Ada"},
    )

    user = User(json={"id": 1, "name": "Ada"})
    user.id = 2

    assert user.pk == 2
    assert user.url == "/users/1/"

    user.save()

    assert old.last_request.json() == {"id": 2}
    assert user.url == "/users/2/"


@pytest.mark.parametrize("loaded", [False, True])
def test_delete_with_pending_primary_key_change_uses_saved_url(
    requests_mock: Any, loaded: bool
) -> None:
    """``delete()`` removes the saved object, not one at the new key.

    Args:
        loaded: Whether the user starts from its JSON or from only its primary
            key.
    """
    requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": 1, "name": "Ada"}
    )
    delete = requests_mock.delete("https://example.com/v1/users/1/")

    user = User(json={"id": 1, "name": "Ada"}) if loaded else User(id=1)
    user.id = 2
    user.delete()

    assert delete.called


def test_eager_write_after_lazy_primary_key_change_uses_saved_url(
    requests_mock: Any,
) -> None:
    """An eager write sends the pending key change to the saved URL."""
    patch = requests_mock.patch(
        "https://example.com/v1/users/1/",
        json={"id": 2, "name": "Grace"},
    )

    user = User(json={"id": 1, "name": "Ada"})
    user.id = 2
    user.set_write_mode("eager")
    user.name = "Grace"

    assert patch.last_request.json() == {"id": 2, "name": "Grace"}
    assert user.url == "/users/2/"


def test_update_from_json_matches_saved_primary_key() -> None:
    """Server state for the saved object replaces a pending key change."""
    user = User(json={"id": 1, "name": "Ada"})
    user.id = 2

    user.update_from_json({"id": 1, "name": "Grace"})

    assert user.pk == 1
    assert user.name == "Grace"
    assert user.url == "/users/1/"


@pytest.mark.parametrize("loaded", [False, True])
def test_refresh_discards_pending_primary_key_change(
    requests_mock: Any, loaded: bool
) -> None:
    """``refresh()`` reloads the object under its saved primary key.

    Args:
        loaded: Whether the user starts from its JSON or from only its primary
            key.
    """
    requests_mock.get(
        "https://example.com/v1/users/1/", json={"id": 1, "name": "Ada"}
    )

    user = User(json={"id": 1, "name": "Ada"}) if loaded else User(id=1)
    user.id = 2
    user.refresh()

    assert user.name == "Ada"
    assert user.pk == 1


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
    later = datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc)

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

        class Kind(str, enum.Enum):
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
                    2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc
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
        (lambda: MaybeTodo.get(user=User(pk=None)), "is None"),
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
    when = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
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

    Args:
        statement: ``type`` statement that defines ``Alias``.
        match: Pattern that the error message should match.
    """
    namespace: dict[str, Any] = {}
    exec(statement, namespace)  # noqa: S102
    model: Any = json_object(
        type(
            "Aliased",
            (ModelBase,),
            {
                "__annotations__": {"id": int, "value": namespace["Alias"]},
                "class_url": "/aliased/",
            },
        )
    )

    with pytest.raises(FieldConversionError, match=match):
        _ = model(json={"id": 1, "value": [1]}).value


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

    assert quoted.dates == [
        datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    ]


@pytest.mark.parametrize(
    ("alias", "match"),
    [
        (QuotedRecursive, "refers to itself"),
        (QuotedMissing, "cannot be resolved"),
    ],
)
def test_unresolvable_quoted_alias_raises(alias: Any, match: str) -> None:
    """A quoted self-reference or unknown name raises rather than passing.

    Args:
        alias: Named alias whose value holds the quoted reference.
        match: Pattern that the error message should match.
    """
    model: Any = json_object(
        type(
            "Aliased",
            (ModelBase,),
            {
                "__annotations__": {"id": int, "value": alias},
                "class_url": "/aliased/",
            },
        )
    )

    with pytest.raises(FieldConversionError, match=match):
        _ = model(json={"id": 1, "value": ["bad"]}).value


def test_collection_field_values_must_be_collections(
    requests_mock: Any,
) -> None:
    """A field value is not converted as one item of a collection."""
    profile = Profile(json={"id": 1, "settings": {}, "contacts": []})
    timeline = Timeline(json={"id": 1, "starts_at": []})
    when = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)

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
@pytest.mark.parametrize("name", ["userId", "ordering"])
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
        name: Parameter name, a JSON key or a name that is not a field.
        method: Name of the query method.
    """
    requests_mock.get("https://example.com/v1/todos/", json=[{"id": 99}])

    # filter() sends its request, and so checks its parameters, when called.
    with pytest.raises(ValueError, match=match):
        getattr(Todo, method)(**{name: make()})

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "make", [lambda: iter([1, 2]), lambda: deque([1, 2]), lambda: range(1, 3)]
)
def test_query_sends_every_item_of_an_iterable(
    requests_mock: Any, make: Any
) -> None:
    """An iterable query value is read once and sent as repeated parameters.

    Args:
        make: Callable that returns a fresh iterable of two items.
    """
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    list(Todo.filter(userId=make(), id__in=make()))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {
        "userId": ["1", "2"],
        "id__in": ["1", "2"],
    }


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
    with pytest.raises(PrefetchNotSupported):
        Probe.bulk_get_by_urls({"https://example.com/"}, api=test_api, hint=1)


@pytest.mark.parametrize(
    "hook",
    [
        "bulk_get_by_pks",
        "bulk_get_by_urls",
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

    assert users.last_request.qs == {"id": ["2"]}
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


@pytest.mark.parametrize("initialization", ["raw", "typed", "json"])
def test_enum_primary_key_urls_use_serialized_values(
    requests_mock: Any, initialization: str
) -> None:
    """Loading and saving enum keys must keep their wire values in URLs.

    Args:
        initialization: How the workflow is constructed: ``"json"`` from its
            JSON, ``"typed"`` from an enum key, or ``"raw"`` from the key's
            wire value.
    """

    @json_object(pk="status", field_map={"status": "state"})
    class Workflow(ModelBase):
        """Resource addressed by an enum key."""

        class_url = "/workflows/"

        status: IssueStatus
        label: str

    requests_mock.get(
        "https://example.com/v1/workflows/open/",
        json={"state": "open", "label": "Active"},
    )
    patch = requests_mock.patch(
        "https://example.com/v1/workflows/open/", status_code=204
    )
    delete = requests_mock.delete(
        "https://example.com/v1/workflows/closed/", status_code=204
    )
    if initialization == "json":
        workflow = Workflow(json={"state": "open", "label": "Active"})
    elif initialization == "typed":
        workflow = Workflow(status=IssueStatus.OPEN)
    else:
        workflow = Workflow(status="open")

    assert workflow.url == "/workflows/open/"
    assert requests_mock.call_count == 0
    assert workflow.label == "Active"
    assert workflow.pk is IssueStatus.OPEN
    assert workflow.url == "/workflows/open/"
    workflow.status = IssueStatus.CLOSED
    assert workflow.url == "/workflows/open/"
    workflow.save()

    assert patch.last_request.json() == {"state": "closed"}
    assert workflow.url == "/workflows/closed/"
    workflow.delete()
    assert delete.called


@pytest.mark.parametrize("value", [None, "", ".", ".."])
def test_invalid_serialized_enum_key_has_no_url(
    requests_mock: Any, value: str | None
) -> None:
    """Validate the serialized key before it becomes a URL segment.

    Args:
        value: Wire value of an enum key that has no URL.
    """

    @json_object
    class InvalidKey(ModelBase):
        """Model whose enum values cannot address resources."""

        class Kind(enum.Enum):
            """Keys whose wire values have no resource URL."""

            MISSING = None
            EMPTY = ""
            DOT = "."
            PARENT = ".."

        class_url = "/invalid-keys/"

        id: Kind

    obj = InvalidKey(id=InvalidKey.Kind(value))
    with pytest.raises(ValueError, match="no URL"):
        obj.delete()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("slug", [None, "", ".", ".."])
def test_invalid_primary_key_has_no_url(
    requests_mock: Any, slug: str | None
) -> None:
    """An invalid primary key cannot address another resource.

    Args:
        slug: Primary key that has no URL, such as ``None`` or ``".."``.
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


@pytest.mark.parametrize("status", [301, 302, 303])
@pytest.mark.parametrize("write_mode", ["lazy", "eager"])
def test_redirected_save_keeps_pending_updates(
    requests_mock: Any, status: int, write_mode: WriteMode
) -> None:
    """A PATCH that a redirect would resend without its body is not saved.

    A 301 keeps the method but drops the body, as Django's ``APPEND_SLASH``
    does to a URL without its trailing slash. The redirect is not followed. A
    lazy update stays pending, and an eager assignment does not take effect.

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

    with pytest.raises(
        requests.HTTPError,
        match="status 303" if status == 303 else "not be performed",
    ):
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
    with pytest.raises(TypeError, match="must hold a JSON object"):
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

    with pytest.raises(TypeError, match="must hold a JSON object"):
        User.create(name="Ada")
    with pytest.raises(TypeError, match="must hold a JSON object"):
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
        2024, 4, 1, 12, tzinfo=datetime.timezone.utc
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
        params = Todo._encode_query({name: value})
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
    monkeypatch.setattr(User, "api", API("https://users.example.net/"))
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
        "https://example.com/v1/users/?id=5",
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


@json_object
class TaggedTodo(ModelBase):
    """Model with set fields."""

    class_url = "/tagged-todos/"

    id: int
    tags: set[str]
    marks: frozenset[int] | None


@pytest.mark.parametrize(
    ("field", "server", "equal", "different"),
    [
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
    json = {"id": 1, "tags": ["a"], "marks": [1]}
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


@json_object
class Note(ModelBase):
    """Model whose relations are hyperlinks, as DRF's hyperlinked fields."""

    class_url = "/notes/"

    id: int
    author: Annotated[User, Hyperlinked()]
    readers: list[Annotated[User, Hyperlinked()]]
    editor: Annotated[User | None, Hyperlinked()]


NOTE_JSON = {
    "id": 1,
    "author": "https://example.com/v1/users/2/",
    "readers": ["https://example.com/v1/users/3/", "/v1/users/4/"],
    "editor": None,
}


def test_hyperlinked_relation_loads_from_its_url(requests_mock: Any) -> None:
    """A hyperlinked field reads as an object that loads from the URL."""
    get = requests_mock.get(
        "https://example.com/v1/users/2/", json={"id": 2, "name": "Ada"}
    )
    note = Note(json=NOTE_JSON)

    author = note.author

    assert not get.called
    assert author.url == "https://example.com/v1/users/2/"
    assert author.name == "Ada"
    assert author.pk == 2
    assert [reader.url for reader in note.readers] == [
        "https://example.com/v1/users/3/",
        "https://example.com/v1/users/4/",
    ]
    assert note.editor is None


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example.net/users/2/",
        "http://example.com/v1/users/2/",
        "https://example.com:8443/v1/users/2/",
    ],
)
def test_hyperlink_to_another_origin_is_refused(
    requests_mock: Any, url: str
) -> None:
    """A URL on another origin raises rather than receive credentials.

    Args:
        url: Hyperlink on an origin that the API does not allow.
    """
    note = Note(json={**NOTE_JSON, "author": url})

    with pytest.raises(FieldConversionError, match="origin") as info:
        _ = note.author

    assert url not in str(info.value)
    assert requests_mock.call_count == 0


def test_malformed_hyperlink_is_not_quoted(requests_mock: Any) -> None:
    """A hyperlink that urllib cannot join raises without quoting it.

    urllib's own message for a host such as ``[secret]`` quotes the host,
    which the server chose.
    """
    note = Note(json={**NOTE_JSON, "author": "https://[secret]/users/2/"})

    with pytest.raises(FieldConversionError, match="not a valid URL") as info:
        _ = note.author

    assert "secret" not in str(info.value)
    assert requests_mock.call_count == 0


def test_hyperlinked_relation_writes_urls_and_queries_keys(
    requests_mock: Any,
) -> None:
    """A related object writes its URL and uses its key in a query."""
    patch = requests_mock.patch(
        "https://example.com/v1/notes/1/", status_code=204
    )
    lookup = requests_mock.get("https://example.com/v1/notes/", json=[])
    note = Note(json=NOTE_JSON)

    note.author = User(id=5)
    note.readers = [note.author, User(id=6)]
    note.save()
    list(Note.filter(author=User(id=5)))

    assert patch.last_request.json() == {
        "author": "https://example.com/v1/users/5/",
        "readers": [
            "https://example.com/v1/users/5/",
            "https://example.com/v1/users/6/",
        ],
    }
    assert lookup.last_request.qs == {"author": ["5"]}


def test_hyperlinked_prefetch_needs_bulk_get_by_urls(
    requests_mock: Any,
) -> None:
    """A hyperlinked field prefetches through ``bulk_get_by_urls()``.

    ``User`` implements only ``bulk_get_by_pks()``, so prefetching fails
    before any request.
    """
    with pytest.raises(PrefetchNotSupported, match="bulk_get_by_urls"):
        Note.filter(prefetch="author")

    assert requests_mock.call_count == 0


def _user_from_url(url: str, api: API | None) -> User:
    """Return the user that a hyperlink names, as a bulk lookup would.

    Args:
        url: Absolute URL of the user.
        api: API to create the user with.

    Returns:
        A loaded user whose key is the URL's last path segment.
    """
    pk = int(url.rstrip("/").rsplit("/", 1)[1])
    return User(json={"id": pk, "name": f"user{pk}"}, api=api)


def test_hyperlinked_prefetch_loads_each_url_once(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hyperlinks load in one bulk call per field and read without a request.

    Relative links and spellings that differ in host case or default port
    name the same object. A link to another origin is not loaded, and
    reading it still raises.
    """
    calls: list[tuple[set[str], API | None]] = []

    def bulk_get_by_urls(
        cls: type[User],
        /,
        urls: set[str],
        *,
        api: API | None = None,
        **kwargs: Any,
    ) -> dict[str, User]:
        calls.append((urls, api))
        # Return each URL in another spelling, to check matching.
        return {
            url.replace("https://example.com/", "HTTPS://EXAMPLE.COM:443/"): (
                _user_from_url(url, api)
            )
            for url in urls
        }

    monkeypatch.setattr(
        User, "bulk_get_by_urls", classmethod(bulk_get_by_urls)
    )
    requests_mock.get(
        "https://example.com/v1/notes/",
        json=[
            NOTE_JSON,
            {
                **NOTE_JSON,
                "id": 2,
                "author": "/v1/users/2/",
                "readers": ["https://other.example/users/5/"],
            },
        ],
    )

    first, second = Note.filter(prefetch=["author", "readers"])

    assert [sorted(urls) for urls, _ in calls] == [
        ["https://example.com/v1/users/2/"],
        ["https://example.com/v1/users/3/", "https://example.com/v1/users/4/"],
    ]
    assert all(api is User.api for _, api in calls)
    assert first.author is second.author
    assert first.author.name == "user2"
    assert first.author.url == "https://example.com/v1/users/2/"
    assert first.author._api_override is None
    assert [reader.name for reader in first.readers] == ["user3", "user4"]
    with pytest.raises(FieldConversionError, match="origin"):
        _ = second.readers
    assert requests_mock.call_count == 1


def test_hyperlinked_prefetch_skips_values_it_cannot_load(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An embedded object, a key, and refused URLs send no bulk lookup.

    The embedded author is read as the object it is, and the refused reader
    still raises when it is read.
    """
    calls: list[set[str]] = []

    def bulk_get_by_urls(
        cls: type[User],
        /,
        urls: set[str],
        *,
        api: API | None = None,
        **kwargs: Any,
    ) -> dict[str, User]:
        calls.append(urls)
        return {}

    monkeypatch.setattr(
        User, "bulk_get_by_urls", classmethod(bulk_get_by_urls)
    )
    requests_mock.get(
        "https://example.com/v1/notes/",
        json=[
            {
                **NOTE_JSON,
                "author": {"id": 2, "name": "Ada"},
                "readers": ["https://other.example/users/5/", "mailto:x", 7],
            }
        ],
    )

    (note,) = Note.filter(prefetch=["author", "readers"])

    assert calls == []
    assert note.author.name == "Ada"
    with pytest.raises(FieldConversionError):
        _ = note.readers
    assert requests_mock.call_count == 1


def test_hyperlinked_prefetch_ignores_urls_it_did_not_request(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup's extra objects are dropped, and an object keeps its own URL.

    An object that a lookup returns with a URL of its own, such as one it
    read from a hyperlink, keeps that URL rather than the requested one.
    """

    def bulk_get_by_urls(
        cls: type[User],
        /,
        urls: set[str],
        *,
        api: API | None = None,
        **kwargs: Any,
    ) -> dict[str, User]:
        found = {url: _user_from_url(url, api) for url in urls}
        for user in found.values():
            user._url_override = f"https://example.com/v1/people/{user.id}/"
        found["https://example.com/v1/users/99/"] = _user_from_url(
            "https://example.com/v1/users/99/", api
        )
        return found

    monkeypatch.setattr(
        User, "bulk_get_by_urls", classmethod(bulk_get_by_urls)
    )
    requests_mock.get("https://example.com/v1/notes/", json=[NOTE_JSON])

    (note,) = Note.filter(prefetch="author")

    assert note.author.name == "user2"
    assert note.author.url == "https://example.com/v1/people/2/"
    assert requests_mock.call_count == 1


def test_hyperlinked_applies_only_to_related_types() -> None:
    """``Hyperlinked()`` on a type without a related model raises."""

    @json_object
    class Wrong(ModelBase):
        id: int
        name: Annotated[str, Hyperlinked()]

    with pytest.raises(FieldConversionError, match="related model"):
        _ = Wrong(json={"id": 1, "name": "x"}).name


def test_hyperlinked_field_takes_urls_and_objects_only(
    requests_mock: Any,
) -> None:
    """A hyperlink is a URL or an object, never a bare key."""
    patch = requests_mock.patch(
        "https://example.com/v1/notes/1/", status_code=204
    )
    note = Note(json={**NOTE_JSON, "author": 2})

    with pytest.raises(FieldConversionError, match="must be URLs"):
        _ = note.author
    with pytest.raises(FieldConversionError, match="must be objects or URLs"):
        note.author = 5  # type: ignore[assignment]

    note.author = "https://example.com/v1/users/7/"  # type: ignore[assignment]
    note.save()

    assert patch.last_request.json() == {
        "author": "https://example.com/v1/users/7/"
    }


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


@json_object
class Review(ModelBase):
    """Model with an optional and a lazy hyperlinked relation."""

    class_url = "/reviews/"

    id: int
    editor: Annotated[User | None, Hyperlinked()]
    watchers: Iterable[Annotated[User, Hyperlinked()]]


def test_hyperlinked_prefetch_covers_optional_and_lazy_fields(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional and lazy hyperlinked fields read prefetched objects too."""

    def bulk_get_by_urls(
        cls: type[User],
        /,
        urls: set[str],
        *,
        api: API | None = None,
        **kwargs: Any,
    ) -> dict[str, User]:
        return {url: _user_from_url(url, api) for url in urls}

    monkeypatch.setattr(
        User, "bulk_get_by_urls", classmethod(bulk_get_by_urls)
    )
    requests_mock.get(
        "https://example.com/v1/reviews/",
        json=[
            {
                "id": 1,
                "editor": "/v1/users/7/",
                "watchers": ["/v1/users/8/", "/v1/users/9/"],
            }
        ],
    )

    (review,) = Review.filter(prefetch=["editor", "watchers"])

    assert review.editor is not None
    assert review.editor.name == "user7"
    assert [user.name for user in review.watchers] == ["user8", "user9"]
    assert requests_mock.call_count == 1


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
    link: Annotated[User, Hyperlinked()]
    secret: str
    created: datetime.datetime


def test_json_schema_describes_what_a_model_reads() -> None:
    """The read schema uses read keys and omits write-only fields."""
    owner = {"anyOf": [{"type": "integer"}, {"type": "object"}]}
    link = {"anyOf": [{"type": "string", "format": "uri"}, {"type": "object"}]}

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
            "link": link,
            "created": {
                "anyOf": [
                    {"type": "string", "format": "date-time"},
                    {"type": "number"},
                ]
            },
        },
        "additionalProperties": True,
    }


def test_json_schema_describes_what_a_model_writes() -> None:
    """The write schema uses write keys and keeps only writable fields."""
    properties = Specimen.json_schema("write")["properties"]

    assert "created" not in properties
    assert properties["secret"] == {"type": "string"}
    assert properties["owner_id"] == {"type": "integer"}
    assert "owner" not in properties
    assert properties["link"] == {"type": "string", "format": "uri"}
    assert list(properties)[:3] == ["id", "name", "score"]


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
class Keyless(ModelBase):
    """Model without a primary key, such as an embedded value."""

    name: str


@json_object(pk="partner")
class Left(ModelBase):
    """Model whose primary key is a related object, which names it back."""

    partner: Right


@json_object(pk="partner")
class Right(ModelBase):
    """Model whose primary key is a ``Left``."""

    partner: Left


@json_object
class Odd(ModelBase):
    """Model with relations whose keys have no schema, and an unknown type."""

    class_url = "/odd/"

    id: int
    keyless: Keyless
    left: Left
    address: IPv4Address


def test_json_schema_allows_any_key_it_cannot_describe() -> None:
    """A relation without a key, or in a cycle of keys, accepts any key.

    So does a type that eazyrest does not convert, whose JSON form it does
    not know.
    """
    properties = Odd.json_schema("write")["properties"]

    assert properties["keyless"] == {}
    assert properties["left"] == {}
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
