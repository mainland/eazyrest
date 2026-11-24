"""Tests of the reading of a URL and of what a message shows of it."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from eazyrest._urls import (
    UNREADABLE,
    Source,
    _read_url,
    _shown,
    _unreadable_error,
)

PRIVATE = "https://user:SECRET@Api.Example.com:8443/a%20b/c?t=TOKEN#FRAG"


@pytest.mark.parametrize(
    ("source", "shown"),
    [
        (Source.SERVER, "'https://api.example.com:8443'"),
        (Source.CONFIG, "'https://api.example.com:8443/a%20b/c'"),
        (Source.CALLER, "'https://api.example.com:8443/a%20b/c?...'"),
    ],
)
def test_source_decides_how_much_of_a_url_is_shown(
    source: Source, shown: str
) -> None:
    """A server's URL shows its origin, and a caller's its path and ?...."""
    assert _shown(_read_url(PRIVATE), source) == shown


def test_name_after_a_method_is_not_quoted() -> None:
    """A URL that follows a method is shown without quotes."""
    url = _read_url("https://h.test/items?page=2")

    assert (
        _shown(url, Source.CALLER, quoted=False) == "https://h.test/items?..."
    )


def test_url_without_a_query_shows_no_marker_for_one() -> None:
    """Requests drops an empty query, so the name shows no ?...."""
    assert _shown(_read_url("https://h.test/items?"), Source.CALLER) == (
        "'https://h.test/items'"
    )


def test_default_port_is_not_shown() -> None:
    """The port of a scheme's default is left out, and another is kept."""
    assert _shown(_read_url("https://h.test:443/"), Source.SERVER) == (
        "'https://h.test'"
    )
    assert _shown(_read_url("http://h.test:8080/"), Source.SERVER) == (
        "'http://h.test:8080'"
    )


def test_ipv6_host_is_bracketed() -> None:
    """An IPv6 address is shown in brackets."""
    assert _shown(_read_url("http://[::1]:81/x"), Source.CONFIG) == (
        "'http://[::1]:81/x'"
    )


@pytest.mark.parametrize(
    "text",
    [
        "ftp://user:SECRET@h.test/x",
        "https://h.test:99999/",
        "https://user:SECRET@h.test:port/",
        "user:SECRET@h.test/x",
        "//user:SECRET@h.test/x",
        "",
    ],
)
@pytest.mark.parametrize("source", list(Source))
def test_url_that_is_no_http_url_shows_only_a_fixed_phrase(
    text: str, source: Source
) -> None:
    """A URL that requests cannot send, or that is not HTTP, is not quoted."""
    shown = _shown(_read_url(text, requests.Session()), source)

    assert shown == UNREADABLE


def test_text_that_is_not_printable_ascii_is_not_quoted() -> None:
    """A path or host that is not plain printable ASCII is not shown."""
    url = _read_url("https://h.test/x")
    assert url is not None
    odd = url._replace(url="https://h.test/x\x1b[31m")

    assert _shown(odd, Source.CALLER) == UNREADABLE
    assert _shown(url._replace(url="https://h.test/a b"), Source.CALLER) == (
        UNREADABLE
    )


def test_path_with_control_characters_is_percent_encoded() -> None:
    """Requests encodes an escape and a right-to-left override in a path."""
    override = chr(0x202E)
    url = _read_url(f"https://h.test/a\x1b[31m{override}")
    shown = _shown(url, Source.CALLER)

    assert "\x1b" not in shown
    assert override not in shown
    assert "%1B" in shown


def test_session_without_an_adapter_makes_an_http_url_unreadable() -> None:
    """A URL that begins with a control character has no adapter."""
    session = requests.Session()

    assert _read_url("\x01https://h.test/", session) is None


def test_other_scheme_with_an_adapter_is_sendable() -> None:
    """A scheme other than HTTP is readable, and sendable if mounted."""
    session = requests.Session()
    bare = _read_url("ftp://h.test/x", session)
    session.mount("ftp://", requests.adapters.HTTPAdapter())
    mounted = _read_url("ftp://h.test/x", session)

    assert bare is not None
    assert not bare.sendable
    assert mounted is not None
    assert mounted.sendable


def test_reading_without_a_session_skips_the_adapter_check() -> None:
    """A reading with no session checks only what requests prepares."""
    assert _read_url("ftp://h.test/x") is not None


def test_bytes_are_read_as_requests_reads_them() -> None:
    """A URL given as bytes is decoded as UTF-8."""
    url = _read_url(b"https://h.test/x")

    assert url is not None
    assert url.url == "https://h.test/x"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("user:SECRET@h.test/x", requests.exceptions.InvalidSchema),
        ("ftp://user:SECRET@h.test/x", requests.exceptions.InvalidSchema),
        ("//user:SECRET@h.test/x", requests.exceptions.MissingSchema),
        ("https://user:SECRET@h.test:99999/", requests.exceptions.InvalidURL),
        (
            "https://user:SECRET@h.test" + chr(0x2100) + "/",
            requests.exceptions.InvalidURL,
        ),
        ("https:///x", requests.exceptions.InvalidURL),
    ],
)
def test_unreadable_url_raises_the_class_that_requests_raises(
    text: str, error: type[Exception]
) -> None:
    """The error has requests' class, quotes no URL, and has no chain."""
    err = _unreadable_error(text, requests.Session())

    assert type(err) is error
    assert "SECRET" not in str(err)
    assert "h.test" not in str(err)
    assert err.__cause__ is None
    assert err.__context__ is None


def test_unreadable_errors_match_the_classes_of_a_real_request() -> None:
    """For each text, requests raises the class that the error has."""
    session = requests.Session()
    for text in ["user:x@h.test/x", "//h.test/x", "https://h.test:99999/"]:
        with pytest.raises(requests.RequestException) as real:
            session.get(text)
        assert type(_unreadable_error(text, session)) is type(real.value)


@pytest.mark.parametrize("source", list(Source))
def test_host_is_the_one_that_requests_connects_to(source: Source) -> None:
    """A backslash ends the authority, as requests reads it."""
    url: Any = _read_url("https://a.test\\@b.test/x")

    assert url.host == "a.test"
    assert _shown(url, source).startswith("'https://a.test")
