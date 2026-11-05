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


@json_object
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
