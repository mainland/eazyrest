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


@json_object
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
