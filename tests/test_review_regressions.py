"""Regressions for the release reviews of 784537f."""

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


class ReviewBase(JSONObject):
    """Models served by the review's mocked API."""


ReviewBase.register_api(API("https://review.example/"))


@json_object
class ReviewUser(ReviewBase):
    """Related resource with explicit bulk lookup support."""

    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: API | None = None
    ) -> dict[Any, ReviewUser]:
        """Return loaded users for the requested keys.

        Args:
            pks: Keys to load.
            api: Query API override.

        Returns:
            Users keyed by their primary keys.
        """
        return {pk: cls(json={"id": pk, "name": "Ada"}, api=api) for pk in pks}


@json_object(write_only={"password"}, write_map={"owner": "owner_id"})
class ReviewDoc(ReviewBase):
    """Resource combining the new write and relation features."""

    class_url = "/docs/"

    id: int
    title: str
    body: str
    tags: list[str]
    owner: ReviewUser
    linked: ReviewUser
    password: str


DOC_URL = "https://review.example/docs/1/"
DOC_JSON = {
    "id": 1,
    "title": "old",
    "body": "keep",
    "tags": ["a"],
    "owner": 2,
    "linked": "https://review.example/users/2/",
}


@pytest.mark.parametrize("source", ["push", "create", "write-only"])
def test_full_save_completes_partial_state(
    requests_mock: Any, source: str
) -> None:
    """Replacement writes load fields unknown to a partial object.

    Args:
        source: Operation that established the partial state.
    """
    detail = requests_mock.get(
        DOC_URL,
        json={**DOC_JSON, "title": "new" if source == "create" else "old"},
    )
    put = requests_mock.put(DOC_URL, status_code=204)
    if source == "create":
        requests_mock.post("https://review.example/docs/", json={"id": 1})
        doc = ReviewDoc.create(title="new")
    else:
        doc = ReviewDoc(id=1)
        if source == "push":
            doc.update_from_json({"title": "old"})
            doc.title = "new"
        else:
            requests_mock.patch(DOC_URL, json={"id": 1})
            doc.password = "dummy"
            doc.save()

    doc.save(full=True)

    assert detail.call_count == 1
    assert put.last_request.json() == {
        "id": 1,
        "title": "old" if source == "write-only" else "new",
        "body": "keep",
        "tags": ["a"],
        "owner_id": 2,
        "linked": "https://review.example/users/2/",
    }
    assert doc._pending_updates == {}


@pytest.mark.parametrize("operation", ["create", "save", "full", "delete"])
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
    url = "https://review.example/docs/" if operation == "create" else DOC_URL
    requests_mock.register_uri(
        method, url, status_code=status, headers={"Location": "/elsewhere/"}
    )
    doc = ReviewDoc(json=DOC_JSON)
    doc.title = "new"
    options = {"allow_redirects": False}

    def write() -> None:
        """Perform the requested operation."""
        if operation == "create":
            ReviewDoc.create(title="new", request_options=options)
        elif operation == "delete":
            doc.delete(request_options=options)
        else:
            doc.save(full=operation == "full", request_options=options)

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
    doc = ReviewDoc(json=DOC_JSON)
    doc.title = "new"

    def write() -> None:
        """Perform the requested operation."""
        if operation == "create":
            ReviewDoc.create(title="new", request_options={"stream": True})
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
        ReviewDoc.create(owner=ReviewUser(id=2), owner_id=3)

    assert not requests_mock.called


def test_query_rejects_colliding_read_names(requests_mock: Any) -> None:
    """A raw alias must not replace a converted query constraint."""

    @json_object(field_map={"owner": "owner_id"})
    class Mapped(ReviewDoc):
        owner: ReviewUser

    with pytest.raises(TypeError, match="owner_id"):
        Mapped.filter(owner=ReviewUser(id=2), owner_id=3)

    assert not requests_mock.called


def test_non_field_queries_encode_model_keys(requests_mock: Any) -> None:
    """Lookup syntax must not send a model's display representation."""
    query = requests_mock.get("https://review.example/docs/", json=[])

    list(ReviewDoc.filter(owner__in=[ReviewUser(id=2), ReviewUser(id=3)]))

    assert query.last_request.qs == {"owner__in": ["2", "3"]}


def test_redeclared_fields_keep_their_options() -> None:
    """Narrowing or repeating an annotation preserves its wire policy."""

    @json_object(
        field_map={"owner": "user"},
        write_map={"owner": "user_id"},
        write_only={"password"},
    )
    class Parent(ReviewBase):
        id: int
        owner: ReviewUser
        password: str

    @json_object
    class Child(Parent):
        owner: ReviewUser
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
    class Child(ReviewDoc):
        password: str
        owner: ReviewUser

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
        doc = ReviewDoc(json=payload)
    elif source == "create":
        requests_mock.post("https://review.example/docs/", json=payload)
        doc = ReviewDoc.create(title="old", password="dummy")
    elif source == "load":
        requests_mock.get(DOC_URL, json=payload)
        doc = ReviewDoc(id=1)
    else:
        doc = ReviewDoc(json=DOC_JSON)
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
        _ = ReviewDoc(id=1).tags

    requests_mock.get(DOC_URL, status_code=400, text='{"error":' + body + "}")
    with pytest.raises(APIError) as info:
        ReviewDoc.api.get(DOC_URL)
    assert info.value.data is None


def test_error_body_depth_limit_is_not_silently_lost(
    requests_mock: Any,
) -> None:
    """HTTP error parsing preserves an explicit depth-limit violation."""
    api = API("https://review.example/")
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
    class Dated(ReviewBase):
        id: int
        date: datetime.date

    dated = Dated(json={"id": 1, "date": value})

    with pytest.raises(FieldConversionError):
        _ = dated.date


def test_mapped_write_only_field_stays_out_of_full_save_state(
    requests_mock: Any,
) -> None:
    """Full and eager writes keep a write-only value only until it is sent."""

    @json_object(write_only={"password"}, write_map={"password": "secret"})
    class Account(ReviewBase):
        class_url = "/accounts/"

        id: int
        name: str
        password: str

    url = "https://review.example/accounts/1/"
    put = requests_mock.put(url, json={"id": 1, "password": "echo"})
    account = Account(json={"id": 1, "name": "Ada"})
    account.password = "dummy"

    account.save(full=True)

    assert put.last_request.json() == {
        "id": 1,
        "name": "Ada",
        "secret": "dummy",
    }
    assert "password" not in account.json
    assert account._pending_updates == {}

    patch = requests_mock.patch(url, status_code=204)
    account.set_write_mode("eager")
    account.password = "next"

    assert patch.last_request.json() == {"secret": "next"}
    assert "password" not in account.json


def test_public_json_helper_keeps_limits_in_response_hooks(
    requests_mock: Any,
) -> None:
    """Custom response decoders can use the same policy as model reads."""
    api = API("https://review.example/")
    api.max_json_depth = 1
    requests_mock.get(DOC_URL, json={"field": ["nested"]})
    response = api.get(DOC_URL)

    with pytest.raises(LimitExceeded, match="max_json_depth"):
        api.json(response)
