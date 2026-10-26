"""Tests for JSON-backed REST objects."""

from __future__ import annotations

import datetime
import enum
import typing
import uuid
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from eazyrest import (
    API,
    DoesNotExist,
    EazyrestError,
    FieldConversionError,
    JSONObject,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    WriteMode,
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
        api: API | None = None,
    ) -> dict[Any, User]:
        """Load users in one request for explicit prefetch support.

        Args:
            pks: Primary keys of the users to load.
            api: Unused. The users load through the model's own API.

        Returns:
            The loaded users, keyed by primary key.
        """
        del api
        users = list(cls.filter(id=sorted(pks)))
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
    def collection_items(cls, payload: Any) -> Any:
        """Extract wrapped collection items for this test model.

        Args:
            payload: Decoded JSON body of a collection response.

        Returns:
            The items under the body's ``results`` key.
        """
        return payload["results"]

    @classmethod
    def object_json(cls, payload: Any) -> Any:
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
    def collection_items(cls, payload: Any) -> Any:
        """Follow ``next`` links and yield items across all pages.

        Args:
            payload: Decoded JSON body of the first page.

        Yields:
            Each item in the ``results`` list of each page, fetching the page
            that ``next`` names until ``next`` is ``None``.
        """
        while True:
            yield from payload["results"]
            next_url = payload["next"]
            if next_url is None:
                return
            payload = cls.api.get(next_url).json()


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
    members: tuple[User]


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
    statuses: tuple[IssueStatus]


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


def test_default_collection_items_rejects_non_list_payload(
    requests_mock: Any,
) -> None:
    """Default collection extraction should reject envelope payloads."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json={"results": [{"id": 1, "userId": 2, "title": "a"}]},
    )

    with pytest.raises(TypeError):
        list(Todo.filter())


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
        cls, pks: set[Any], *, api: API | None = None
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
    with pytest.raises(ValueError, match="badly formed hexadecimal UUID"):
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

    match = "More than one Todo userId=2 exists"
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
    with pytest.raises(ValueError, match="user=None"):
        query()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("query", "match"),
    [
        (lambda: Todo.filter(userId=None), "userId=None"),
        (lambda: Todo.get_or_create(userId=None), "userId=None"),
        (lambda: MaybeTodo.get(user=User(pk=None)), "is None"),
        (lambda: Team.filter(members=[]), "members=\\[\\]"),
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


def test_query_omits_none_for_other_parameters(requests_mock: Any) -> None:
    """A ``None`` value for another parameter is still left to requests."""
    lookup = requests_mock.get("https://example.com/v1/todos/", json=[])

    list(Todo.filter(ordering=None))

    assert parse_qs(urlparse(lookup.last_request.url).query) == {}


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

    with pytest.raises(DoesNotExist, match="No Todo title='missing' exists"):
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


def test_prefetch_passes_the_query_api_to_bulk_lookup(
    requests_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bulk lookup receives the query's API only when it passed one."""
    tenant = API("https://tenant.example.com/v2/")
    seen: list[API | None] = []

    def bulk_get_by_pks(
        cls: type[User], pks: set[Any], *, api: API | None = None
    ) -> dict[Any, User]:
        seen.append(api)
        return {pk: cls(json={"id": pk, "name": "Ada"}, api=api) for pk in pks}

    monkeypatch.setattr(User, "bulk_get_by_pks", classmethod(bulk_get_by_pks))
    requests_mock.get(
        "https://tenant.example.com/v2/todos/", json=[TENANT_TODO]
    )
    requests_mock.get("https://example.com/v1/todos/", json=[TENANT_TODO])

    (todo,) = Todo.filter(api=tenant, prefetch="user")
    list(Todo.filter(prefetch="user"))

    assert seen == [tenant, None]
    assert todo.user.api is tenant


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
            resp: requests.Response,
            current: Mapping[str, Any],
            sent: Mapping[str, Any],
        ) -> dict[str, Any]:
            """Ignore the status body.

            Args:
                resp: Response to the write, whose body is a status.
                current: JSON of the object before the write.
                sent: Fields that the write sent.

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


def test_redirected_save_keeps_pending_updates(requests_mock: Any) -> None:
    """A PATCH that a redirect turns into a GET is not reported as saved."""
    requests_mock.patch(
        "https://example.com/v1/todos/1/",
        status_code=302,
        headers={"Location": "https://example.com/v2/todos/1/"},
    )
    requests_mock.get(
        "https://example.com/v2/todos/1/",
        json={"id": 1, "userId": 2, "title": "old", "completed": False},
    )
    todo = Todo(
        json={"id": 1, "userId": 2, "title": "old", "completed": False}
    )

    todo.title = "new"
    with pytest.raises(requests.HTTPError, match="was not performed"):
        todo.save()

    assert todo.title == "new"
    assert todo._pending_updates == {"title": "new"}


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
