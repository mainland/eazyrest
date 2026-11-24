"""An error's message holds the reason phrase of the response, cut short.

The server chooses the phrase, and a status line may be 65,536 bytes long, so
the message shows 40 characters of it, as it shows 40 of each name of the
body's members.
"""

from __future__ import annotations

import pytest
import requests_mock

from eazyrest import API, APIError, NotFound

api = API("https://reason.example/")


def failure(status: int, reason: str) -> APIError:
    """Return the error that a response with a reason phrase raises."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://reason.example/x/",
            status_code=status,
            reason=reason,
            json={},
        )
        with pytest.raises(APIError) as caught:
            api.get("/x/")
    return caught.value


def test_a_reason_phrase_that_fits_is_shown_whole() -> None:
    """The message holds the status and the phrase."""
    error = failure(400, "Bad Request")
    assert str(error).startswith("400 Client Error: Bad Request for GET ")


def test_a_phrase_of_exactly_40_characters_is_shown_whole() -> None:
    """The limit is on the length, and 40 characters fit."""
    reason = "R" * 40
    error = failure(500, reason)
    assert f"Server Error: {reason} for" in str(error)


@pytest.mark.parametrize("length", [41, 60_000])
def test_a_longer_phrase_is_cut_to_40_characters(length: int) -> None:
    """The message ends the phrase with an ellipsis, and stays short."""
    error = failure(400, "A" * length)
    message = str(error)
    assert f"Client Error: {'A' * 40}... for" in message
    assert "A" * 41 not in message
    assert len(message) < 200


def test_a_phrase_that_is_not_printable_is_escaped_inside_the_bound() -> None:
    """Escapes are made after the cut, so the cut never splits one."""
    error = failure(400, "\x85" * 100)
    message = str(error)
    assert "\\x85" * 40 + "..." in message
    assert "\\x85" * 41 not in message


def test_a_not_found_has_the_same_message() -> None:
    """``NotFound`` is built as ``APIError`` is."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://reason.example/x/",
            status_code=404,
            reason="N" * 100,
            json={},
        )
        with pytest.raises(NotFound) as caught:
            api.get("/x/")
    assert f"Client Error: {'N' * 40}... for" in str(caught.value)
