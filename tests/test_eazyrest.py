"""Tests for JSON-backed REST objects."""

from __future__ import annotations

import datetime
import enum
from collections.abc import Iterable
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from eazyrest import (
    API,
    DoesNotExist,
    JSONObject,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    json_object,
)


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
        """Load users in one request for explicit prefetch support."""
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


@json_object
class SearchResult(ModelBase):
    """Model that extracts results from envelope responses."""

    class_url = "/search/"

    id: int
    name: str

    @classmethod
    def collection_items(cls, payload: Any) -> Any:
        """Extract wrapped collection items for this test model."""
        return payload["results"]

    @classmethod
    def object_json(cls, payload: Any) -> Any:
        """Extract a wrapped single object for this test model."""
        return payload["result"]


@json_object
class PaginatedSearchResult(ModelBase):
    """Model that follows paginated collection responses."""

    class_url = "/paginated-search/"

    id: int
    name: str

    @classmethod
    def collection_items(cls, payload: Any) -> Any:
        """Follow ``next`` links and yield items across all pages."""
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
class Timeline(ModelBase):
    """Model with a list of datetimes."""

    class_url = "/timelines/"

    id: int
    starts_at: list[datetime.datetime]


class IssueStatus(str, enum.Enum):
    """Status values for enum conversion tests."""

    OPEN = "open"
    CLOSED = "closed"


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
        json={"id": 1, "starts_at": 1711972800.0},
    )

    event = Event.create(starts_at=starts_at)

    assert event.id == 1
    assert post.last_request.json() == {"starts_at": 1711972800.0}


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
    """Malformed related collections should fail fast."""
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

    assert snapshot.statuses == (IssueStatus.OPEN, IssueStatus.CLOSED)


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
        json={"id": 1, "starts_at": [1711972800.0, 1711976400.0]},
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
        "starts_at": [1711972800.0, 1711976400.0]
    }
    assert snapshot.statuses == (
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


def test_prefetch_requires_explicit_bulk_support(
    requests_mock: Any,
) -> None:
    """Requested prefetch should fail for models without bulk support."""
    requests_mock.get(
        "https://example.com/v1/nobulk-todos/",
        json=[{"id": 1, "userId": 2, "title": "a"}],
    )

    with pytest.raises(PrefetchNotSupported):
        list(NoBulkTodo.filter(prefetch=["user"]))


def test_get_raises_multiple_objects_returned(requests_mock: Any) -> None:
    """``get()`` should raise when more than one object matches."""
    requests_mock.get(
        "https://example.com/v1/todos/",
        json=[
            {"id": 1, "userId": 2, "title": "a", "completed": False},
            {"id": 2, "userId": 2, "title": "b", "completed": False},
        ],
    )

    with pytest.raises(MultipleObjectsReturned):
        Todo.get(userId=2)


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

    assert patch.called is False
    assert todo.title == "new"
    assert todo.user.pk == 3

    todo.save()

    assert patch.called
    assert patch.last_request.json() == {"title": "new", "userId": 3}


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

    assert patch.called is False
    todo.save()
    assert patch.called
    assert patch.last_request.json() == {"title": "new"}


def test_invalid_write_mode_raises_value_error() -> None:
    """Unsupported write modes should be rejected."""
    with pytest.raises(ValueError):
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

    with pytest.raises(ValueError):
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


def test_get_raises_does_not_exist(requests_mock: Any) -> None:
    """``get()`` should raise when no object matches."""
    requests_mock.get("https://example.com/v1/todos/", json=[])

    with pytest.raises(DoesNotExist):
        Todo.get(title="missing")


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
