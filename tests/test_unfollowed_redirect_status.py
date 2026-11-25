"""One rule says which final status is a redirect that has no result.

A final response with a 3xx status that requests did not follow has none of the
body or the effect that the request asked for. A query, a model write, and a
request with a body refuse it alike, and no other status.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, APIError, JSONObject, RedirectError, json_object
from eazyrest._errors import _is_unfollowed_redirect

BASE = "https://example.com/v1/"
URL = BASE + "notes/"
NO_FOLLOW = {"allow_redirects": False}


@json_object
class Note(JSONObject):
    """Model whose requests the tests send."""

    class_url = "/notes/"

    id: int
    text: str


Note.register_api(API(BASE))


def _note() -> Note:
    """Return a note that can be written."""
    note = Note(json={"id": 1, "text": "a"}, write_mode="lazy")
    note.text = "b"
    return note


QUERIES: dict[str, tuple[str, str, Callable[[], object]]] = {
    "filter": (
        "GET",
        URL,
        lambda: list(Note.filter(request_options=NO_FOLLOW)),
    ),
    "all": ("GET", URL, lambda: list(Note.all(request_options=NO_FOLLOW))),
    "get_page": (
        "GET",
        URL,
        lambda: Note.get_page(request_options=NO_FOLLOW),
    ),
}

WRITES: dict[str, tuple[str, str, Callable[[], object]]] = {
    "save": ("PATCH", URL + "1/", lambda: _note().save()),
    "save(full=True)": ("PUT", URL + "1/", lambda: _note().save(full=True)),
    "delete": ("DELETE", URL + "1/", lambda: _note().delete()),
    "create": ("POST", URL, lambda: Note.create(id=2, text="c")),
    "post": ("POST", URL, lambda: API(BASE).post("notes/", json={})),
    "put": ("PUT", URL, lambda: API(BASE).put("notes/", json={})),
}

ENTRY_POINTS = {**QUERIES, **WRITES}
REDIRECTS = [300, 301, 302, 303, 304, 305, 307, 308, 399]


@pytest.mark.parametrize(
    ("method", "url", "send"), ENTRY_POINTS.values(), ids=ENTRY_POINTS
)
@pytest.mark.parametrize("status", REDIRECTS)
def test_a_final_3xx_status_is_a_redirect_that_has_no_result(
    requests_mock: Any,
    method: str,
    url: str,
    send: Callable[[], object],
    status: int,
) -> None:
    """Every kind of request refuses it, and the error holds the response."""
    requests_mock.register_uri(
        method, url, status_code=status, headers={"Location": BASE + "other/"}
    )

    with pytest.raises(RedirectError) as raised:
        send()

    assert raised.value.response is not None
    assert raised.value.response.status_code == status
    assert requests_mock.call_count == 1


@pytest.mark.parametrize(
    ("method", "url", "send"), ENTRY_POINTS.values(), ids=ENTRY_POINTS
)
@pytest.mark.parametrize("status", [299, 400])
def test_a_status_next_to_the_redirects_is_not_one(
    requests_mock: Any,
    method: str,
    url: str,
    send: Callable[[], object],
    status: int,
) -> None:
    """A 299 is a success and a 400 is an error, for every kind."""
    requests_mock.register_uri(
        method, url, status_code=status, json={"id": 1, "text": "b"}
    )
    raised: Exception | None = None

    try:
        send()
    except Exception as err:  # noqa: BLE001
        raised = err

    assert not isinstance(raised, RedirectError)
    assert isinstance(raised, APIError) == (status == 400)


@pytest.mark.parametrize("status", REDIRECTS)
def test_the_rule_is_the_300_to_399_range(status: int) -> None:
    """The statuses on and next to the edges."""
    assert _is_unfollowed_redirect(status)


@pytest.mark.parametrize("status", [0, 100, 200, 299, 400, 404, 500, 599])
def test_other_statuses_are_not_redirects(status: int) -> None:
    """A status below 300 or from 400 is no redirect."""
    assert not _is_unfollowed_redirect(status)
