"""A string in ``redirect_origins`` that is no absolute URL is refused.

An origin needs a scheme and a host. A string without either raises
``ValueError`` naming the attribute and the check that failed, and not quoting
the entry, before any request, instead of the error that requests would raise
for it, which names neither.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, link_headers_page

BASE = "https://api.example.com/"

REFUSED = [
    "files.example.com",
    "files",
    "//files.example.com",
    "",
    "https://",
    "https:///path",
    "http://:8080",
    "mailto:a@example.com",
    "file:///etc/hosts",
    "ftp://files.example.com",
    "htps://files.example.com",
    "localhost:8080",
]
ACCEPTED = {
    "https://files.example.com": "https://files.example.com/f",
    "https://files.example.com:8443": "https://files.example.com:8443/f",
    "https://files.example.com/ignored/path": "https://files.example.com/f",
    "http://[::1]:8080": "http://[::1]:8080/f",
    "HTTPS://files.example.com": "https://files.example.com/f",
    "Http://FILES.example.com": "http://files.example.com/f",
}


def first_page(api: API) -> Any:
    """Request the first page of a collection."""
    return link_headers_page(api, api.get("/items/"))


REQUESTS: dict[str, Callable[[API], Any]] = {
    "get": lambda api: api.get("/items/"),
    "post": lambda api: api.post("/items/", json={}),
    "first page": first_page,
}


@pytest.mark.parametrize("request_", REQUESTS.values(), ids=REQUESTS)
@pytest.mark.parametrize("entry", REFUSED)
def test_an_entry_that_is_no_absolute_url_is_refused_before_a_request(
    request_: Callable[[API], Any], entry: str
) -> None:
    """The error is exactly a ``ValueError`` that names the position."""
    api = API(BASE)
    api.redirect_origins = ["https://cdn.example.com", entry]
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match="API\\.redirect_origins") as info:
            request_(api)
        assert mocker.call_count == 0
    assert type(info.value) is ValueError
    assert "redirect_origins[1]" in str(info.value)
    assert "https://files.example.com" in str(info.value)


@pytest.mark.parametrize("kind", [tuple, frozenset])
def test_an_entry_of_any_collection_kind_is_refused(kind: type) -> None:
    """A tuple and a frozenset are read as a list is."""
    api = API(BASE)
    api.redirect_origins = kind(["files.example.com"])
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match="API\\.redirect_origins"):
            api.get("/items/")
        assert mocker.call_count == 0


def test_a_next_page_refuses_an_entry_that_is_no_absolute_url() -> None:
    """A page that has a link to follow reads the attribute as well."""
    api = API(BASE)
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(
            requests_mock.ANY,
            requests_mock.ANY,
            json=[],
            headers={"Link": f'<{BASE}items/?p=2>; rel="next"'},
        )
        page = first_page(api)
        api.redirect_origins = ["files.example.com"]
        before = mocker.call_count
        with pytest.raises(ValueError, match="API\\.redirect_origins"):
            page.next_page()
        assert mocker.call_count == before


SECRET = "s3cret"
CREDENTIALS = [
    f"https://user:{SECRET}@",
    f"https://user:{SECRET}@/path",
    f"files.example.com/?token={SECRET}#f",
    f"files.example.com/#{SECRET}",
    f"ftp://user:{SECRET}@files.example.com/?k={SECRET}#{SECRET}",
    f"https://exa mple.com/?token={SECRET}",
    f"https://user:{SECRET}@exa mple.com",
    f"user:{SECRET}@files.example.com",
    f"https:user:{SECRET}@files.example.com",
    f"files.example.com/p?a=b@{SECRET}",
    f"http://[::1?{SECRET}",
    f"https://user:{SECRET}@files.example.com:99999/?t={SECRET}",
]


def messages(error: BaseException | None) -> list[str]:
    """Return the messages of an exception and of its causes and contexts."""
    found: list[str] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None:
            continue
        found.append(str(current))
        pending += [current.__cause__, current.__context__]
    return found


@pytest.mark.parametrize("entry", CREDENTIALS)
def test_a_credential_in_a_refused_entry_is_not_quoted(entry: str) -> None:
    """The userinfo, query, and fragment appear in no message of the chain."""
    api = API(BASE)
    api.redirect_origins = [entry]
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=r"API\.redirect_origins") as info:
            api.get("/items/")
        assert mocker.call_count == 0
    assert all(SECRET not in text for text in messages(info.value))


REASONS = {
    "it has no scheme": [
        "files.example.com",
        "//files.example.com",
        "",
    ],
    "its scheme is not http or https": [
        "ftp://files.example.com",
        "mailto:a@example.com",
        "file:///etc/hosts",
        f"user:{SECRET}@files.example.com",
        f"ftp://user:{SECRET}@files.example.com/?k={SECRET}#{SECRET}",
    ],
    "it has no host": [
        "https://",
        "https:///x",
        "http://:8080",
        f"https://user:{SECRET}@",
    ],
    "requests cannot parse it": [
        "https://files.example.com:99999",
        "https://\u2100.example.com",
        "http://[::1",
        f"https://user:{SECRET}@exa mple.com",
    ],
}


@pytest.mark.parametrize(
    ("entry", "reason"),
    [(entry, reason) for reason, group in REASONS.items() for entry in group],
)
def test_a_refused_entry_is_followed_by_the_check_that_failed(
    entry: str, reason: str
) -> None:
    """The message ends with one reason and quotes no part of the entry."""
    api = API(BASE)
    api.redirect_origins = [entry]
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=r"API\.redirect_origins") as info:
            api.get("/items/")
        assert mocker.call_count == 0
    assert str(info.value) == (
        "API.redirect_origins[0] must be an absolute http or https URL with a "
        'host, such as "https://files.example.com", but ' + reason
    )


def test_an_entry_that_cannot_be_parsed_is_named_by_position() -> None:
    """The entry is not quoted, and its position in the attribute is."""
    api = API(BASE)
    api.redirect_origins = ["https://a.example.com", "http://b", "http://[::1"]
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=r"API\.redirect_origins") as info:
            api.get("/items/")
        assert mocker.call_count == 0
    assert "redirect_origins[2]" in str(info.value)
    assert str(info.value).endswith("but requests cannot parse it")
    assert "[::1" not in str(info.value)


@pytest.mark.parametrize("kind", [tuple, list])
def test_an_entry_of_a_tuple_or_a_list_is_named_by_position(
    kind: type,
) -> None:
    """The position finds the entry."""
    api = API(BASE)
    api.redirect_origins = kind(["https://a.example.com", "files"])
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=r"API\.redirect_origins") as info:
            api.get("/items/")
    assert "API.redirect_origins[1] must" in str(info.value)


def test_an_entry_of_a_frozenset_is_named_by_no_position() -> None:
    """The iteration order of a frozenset is no position."""
    api = API(BASE)
    api.redirect_origins = frozenset(["https://a.example.com", "files"])
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match=r"API\.redirect_origins") as info:
            api.get("/items/")
    assert str(info.value).startswith("An entry of API.redirect_origins must")
    assert "redirect_origins[" not in str(info.value)


@pytest.mark.parametrize(("entry", "target"), ACCEPTED.items())
def test_an_absolute_url_is_followed_to_its_origin(
    entry: str, target: str
) -> None:
    """A port, a path that is ignored, and an IPv6 host are accepted."""
    api = API(BASE)
    api.redirect_origins = [entry]
    with requests_mock.Mocker() as mocker:
        mocker.get(
            f"{BASE}items/", status_code=302, headers={"Location": target}
        )
        mocker.get(target, json={"ok": True})
        assert api.get("/items/").json() == {"ok": True}
        assert mocker.call_count == 2
