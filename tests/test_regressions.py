"""Regression tests for fixed defects."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from eazyrest import (
    API,
    APIError,
    FieldConversionError,
    JSONObject,
    LimitExceeded,
    RedirectError,
    ResponseShapeError,
    json_object,
)


class MockedBase(JSONObject):
    """Models served by the mocked API."""


MockedBase.register_api(API("https://mocked.example/"))


@json_object
class MockedUser(MockedBase):
    """Related resource with explicit bulk lookup support."""

    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: API | None = None
    ) -> dict[Any, MockedUser]:
        """Return loaded users for the requested keys.

        Args:
            pks: Keys to load.
            api: Query API override.

        Returns:
            Users keyed by their primary keys.
        """
        return {pk: cls(json={"id": pk, "name": "Ada"}, api=api) for pk in pks}


@json_object(write_only={"password"}, write_map={"owner": "owner_id"})
class MockedDoc(MockedBase):
    """Resource combining the new write and relation features."""

    class_url = "/docs/"

    id: int
    title: str
    body: str
    tags: list[str]
    owner: MockedUser
    linked: MockedUser
    password: str


DOC_URL = "https://mocked.example/docs/1/"
DOC_JSON = {
    "id": 1,
    "title": "old",
    "body": "keep",
    "tags": ["a"],
    "owner": 2,
    "linked": "https://mocked.example/users/2/",
}


@pytest.mark.parametrize("operation", ["create", "save", "delete"])
@pytest.mark.parametrize("status", [300, 307, 308])
def test_model_writes_reject_unconfirmed_redirects(
    requests_mock: Any, operation: str, status: int
) -> None:
    """An unfollowed response cannot acknowledge a model write.

    Args:
        operation: Model write to perform.
        status: Non-success final status.
    """
    method = {
        "create": "POST",
        "save": "PATCH",
        "full": "PUT",
        "delete": "DELETE",
    }[operation]
    url = "https://mocked.example/docs/" if operation == "create" else DOC_URL
    requests_mock.register_uri(
        method, url, status_code=status, headers={"Location": "/elsewhere/"}
    )
    doc = MockedDoc(json=DOC_JSON)
    doc.title = "new"
    options = {"allow_redirects": False}

    def write() -> None:
        """Perform the requested operation."""
        if operation == "create":
            MockedDoc.create(title="new", request_options=options)
        elif operation == "delete":
            doc.delete(request_options=options)
        else:
            doc.save(request_options=options)

    with pytest.raises(RedirectError):
        write()

    assert doc._pending_updates == {"title": "new"}
    assert requests_mock.call_count == 1


@pytest.mark.parametrize("operation", ["create", "save", "delete"])
def test_model_writes_refuse_streaming(
    requests_mock: Any, operation: str
) -> None:
    """Model operations consume responses themselves and must keep limits.

    Args:
        operation: Model write to perform.
    """
    doc = MockedDoc(json=DOC_JSON)
    doc.title = "new"

    def write() -> None:
        """Perform the requested operation."""
        if operation == "create":
            MockedDoc.create(title="new", request_options={"stream": True})
        elif operation == "delete":
            doc.delete(request_options={"stream": True})
        else:
            doc.save(request_options={"stream": True})

    with pytest.raises(TypeError, match="stream"):
        write()

    assert not requests_mock.called
    assert doc._pending_updates == {"title": "new"}


def test_create_rejects_colliding_write_names(requests_mock: Any) -> None:
    """Mapped fields and raw wire names must not overwrite one another."""
    with pytest.raises(TypeError, match="owner_id"):
        MockedDoc.create(owner=MockedUser(id=2), owner_id=3)

    assert not requests_mock.called


def test_query_rejects_colliding_read_names(requests_mock: Any) -> None:
    """A raw alias must not replace a converted query constraint."""

    @json_object(field_map={"owner": "owner_id"})
    class Mapped(MockedDoc):
        owner: MockedUser

    with pytest.raises(TypeError, match="owner_id"):
        Mapped.filter(owner=MockedUser(id=2), owner_id=3)

    assert not requests_mock.called


def test_non_field_queries_encode_model_keys(requests_mock: Any) -> None:
    """Lookup syntax must not send a model's display representation."""
    query = requests_mock.get("https://mocked.example/docs/", json=[])

    list(MockedDoc.filter(owner__in=[MockedUser(id=2), MockedUser(id=3)]))

    assert query.last_request.qs == {"owner__in": ["2", "3"]}


def test_redeclared_fields_keep_their_options() -> None:
    """Narrowing or repeating an annotation preserves its wire policy."""

    @json_object(
        field_map={"owner": "user"},
        write_map={"owner": "user_id"},
        write_only={"password"},
    )
    class Parent(MockedBase):
        id: int
        owner: MockedUser
        password: str

    @json_object
    class Child(Parent):
        owner: MockedUser
        password: str

    child = Child(json={"id": 1, "user": 2, "password": "dummy"})

    assert child.owner.pk == 2
    assert Child.owner.json_field == "user"  # type: ignore[attr-defined]
    assert Child.owner.write_field == "user_id"  # type: ignore[attr-defined]
    assert "password" not in child.json
    with pytest.raises(AttributeError, match="write-only"):
        _ = child.password


def test_redeclared_field_can_explicitly_replace_its_policy() -> None:
    """An explicit set or mapping overrides a redeclared field's policy."""

    @json_object(write_only=set(), write_map={"owner": "owner"})
    class Child(MockedDoc):
        password: str
        owner: MockedUser

    child = Child(json={**DOC_JSON, "password": "dummy"})

    assert child.password == "dummy"
    assert Child.owner.write_field == "owner"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "source", ["constructor", "push", "load", "create", "save"]
)
def test_write_only_values_never_enter_json(
    requests_mock: Any, source: str
) -> None:
    """Both response and caller JSON exclude declared write-only values.

    Args:
        source: Source of JSON containing a write-only member.
    """
    payload = {**DOC_JSON, "password": "dummy"}
    if source == "constructor":
        doc = MockedDoc(json=payload)
    elif source == "create":
        requests_mock.post("https://mocked.example/docs/", json=payload)
        doc = MockedDoc.create(title="old", password="dummy")
    elif source == "load":
        requests_mock.get(DOC_URL, json=payload)
        doc = MockedDoc(id=1)
    else:
        doc = MockedDoc(json=DOC_JSON)
        if source == "push":
            doc.update_from_json(payload)
        else:
            requests_mock.patch(DOC_URL, json=payload)
            doc.title = "new"
            doc.save()

    assert "password" not in doc.json
    assert "password" in payload


@pytest.mark.parametrize(
    "body", ["NaN", "Infinity", "-Infinity", "1e999", "9" * 5000]
)
def test_unrepresentable_json_keeps_the_error_boundary(
    requests_mock: Any, body: str
) -> None:
    """Invalid numeric responses use the public response/status errors.

    Args:
        body: Number that cannot be decoded as supported JSON.
    """
    requests_mock.get(DOC_URL, text='{"id":1,"tags":[' + body + "]}")
    with pytest.raises(ResponseShapeError):
        _ = MockedDoc(id=1).tags

    requests_mock.get(DOC_URL, status_code=400, text='{"error":' + body + "}")
    with pytest.raises(APIError) as info:
        MockedDoc.api.get(DOC_URL)
    assert info.value.data is None


def test_error_body_depth_limit_is_not_silently_lost(
    requests_mock: Any,
) -> None:
    """HTTP error parsing preserves an explicit depth-limit violation."""
    api = API("https://mocked.example/")
    api.max_json_depth = 1
    requests_mock.get(DOC_URL, status_code=400, json={"field": ["bad"]})

    with pytest.raises(LimitExceeded, match="max_json_depth"):
        api.get(DOC_URL)


@pytest.mark.parametrize(
    "value", ["20260401", "2026-W14-3", "2026-04-01T00:00:00"]
)
def test_date_strings_use_the_same_format_on_every_python(value: str) -> None:
    """Basic and week dates cannot depend on the interpreter's parser."""

    @json_object
    class Dated(MockedBase):
        id: int
        date: datetime.date

    dated = Dated(json={"id": 1, "date": value})

    with pytest.raises(FieldConversionError):
        _ = dated.date


def test_public_json_helper_keeps_limits_in_response_hooks(
    requests_mock: Any,
) -> None:
    """Custom response decoders can use the same policy as model reads."""
    api = API("https://mocked.example/")
    api.max_json_depth = 1
    requests_mock.get(DOC_URL, json={"field": ["nested"]})
    response = api.get(DOC_URL)

    with pytest.raises(LimitExceeded, match="max_json_depth"):
        api.json(response)
