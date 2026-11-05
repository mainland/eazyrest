"""Tests for the exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from eazyrest import (
    API,
    EazyrestError,
    JSONObject,
    PaginationError,
    RedirectError,
    ResponseShapeError,
    UsageError,
    follow_next_links,
    json_object,
)


@pytest.mark.parametrize(
    ("error", "bases"),
    [
        (RedirectError, (requests.HTTPError,)),
        (PaginationError, (ValueError,)),
        (ResponseShapeError, (TypeError, ValueError)),
        (UsageError, (RuntimeError,)),
    ],
)
def test_policy_errors_keep_the_types_they_replace(
    error: type[Exception], bases: tuple[type[Exception], ...]
) -> None:
    """Code that caught the replaced types still catches each error.

    Args:
        error: eazyrest exception class.
        bases: Exception types that ``error`` replaces.
    """
    assert issubclass(error, EazyrestError)
    for base in bases:
        assert issubclass(error, base)


class ErrorBase(JSONObject):
    """Base class for the models of these tests."""


ErrorBase.register_api(API("https://example.com/v1/"))


@json_object
class Note(ErrorBase):
    """Model whose responses these tests break."""

    class_url = "/notes/"

    id: int
    text: str


def test_refused_redirect_is_a_redirect_error(requests_mock: Any) -> None:
    """A refused redirect raises ``RedirectError`` with the redirect."""
    requests_mock.patch(
        "https://example.com/v1/notes/1/",
        status_code=301,
        headers={"Location": "https://example.com/v1/notes/1/x/"},
    )
    note = Note(json={"id": 1, "text": "a"})
    note.text = "b"

    with pytest.raises(RedirectError) as info:
        note.save()

    assert info.value.response is not None
    assert info.value.response.status_code == 301


def test_non_json_write_response_keeps_pending_updates(
    requests_mock: Any,
) -> None:
    """A write response that is not JSON raises without quoting the body."""
    requests_mock.patch(
        "https://example.com/v1/notes/1/", text="<html>secret page</html>"
    )
    note = Note(json={"id": 1, "text": "a"})
    note.text = "b"

    with pytest.raises(ResponseShapeError, match="does not hold JSON") as info:
        note.save()

    assert "secret" not in str(info.value)
    assert note._pending_updates == {"text": "b"}


def test_page_cycle_is_a_pagination_error(requests_mock: Any) -> None:
    """A next link to a page already read raises ``PaginationError``."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/notes/?page=2",
        json={"next": "/v1/notes/?page=2", "results": []},
    )

    with pytest.raises(PaginationError, match="already read"):
        list(
            follow_next_links(
                api, {"next": "/v1/notes/?page=2", "results": []}
            )
        )


def test_model_without_api_is_a_usage_error() -> None:
    """Using a model whose class has no registered API raises."""

    @json_object
    class Orphan(JSONObject):
        class_url = "/orphans/"

        id: int

    with pytest.raises(UsageError, match="No API registered"):
        _ = Orphan.api
