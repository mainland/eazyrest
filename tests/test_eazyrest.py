"""Tests for JSON-backed REST objects."""

from __future__ import annotations

from typing import Any

import pytest

from eazyrest import (
    API,
    DoesNotExist,
    JSONObject,
    MultipleObjectsReturned,
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


@json_object(field_map={"user": "userId"})
class Todo(ModelBase):
    """Todo model used in tests."""

    class_url = "/todos/"

    id: int
    user: User
    title: str
    completed: bool


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


def test_invalid_write_mode_raises_value_error() -> None:
    """Unsupported write modes should be rejected."""
    with pytest.raises(ValueError):
        Todo(id=1, write_mode="later")


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
