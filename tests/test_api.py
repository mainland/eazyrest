"""Tests for the HTTP API client wrapper."""

from __future__ import annotations

from typing import Any, cast

import pytest

from eazyrest import API


def test_api_preserves_base_path_prefix_in_relative_requests(
    requests_mock: Any,
) -> None:
    """Relative resource paths keep the base URL path prefix."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/users/1", json={"id": 1})

    response = api.get("/users/1")

    assert response.json() == {"id": 1}


def test_api_allows_absolute_request_urls(requests_mock: Any) -> None:
    """Absolute URLs bypass base URL resolution."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://other.example.com/users/1", json={"id": 1})

    response = api.get("https://other.example.com/users/1")

    assert response.json() == {"id": 1}


def test_api_closes_session_as_context_manager() -> None:
    """The API client should close its session on context exit."""
    api = API("https://example.com/")
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    api.session.close = close  # type: ignore[method-assign]

    with api as entered:
        assert entered is api

    assert closed


def test_api_rejects_invalid_default_write_mode() -> None:
    """API defaults should validate supported write modes."""
    with pytest.raises(ValueError):
        API(
            "https://example.com/",
            default_write_mode=cast(Any, "later"),
        )
