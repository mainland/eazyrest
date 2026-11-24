"""Tests of what a message shows of a URL, at every site that shows one.

Each case sends a sentinel credential, query, and fragment, with an escape
character and a right-to-left override in the path, through one site that shows
a URL, from the source the URL came from: the caller, the configuration, or a
server. The text of every error, its ``repr()``, its ``args``, and its
formatted traceback, with its chain, must hold no part that the source's rule
hides.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
import requests
from requests_mock import ANY

from eazyrest import (
    API,
    APIError,
    NotFound,
    follow_link_headers,
    follow_next_links,
    link_headers_page,
    next_links_page,
)
from eazyrest.api import _check_final_response

PASSWORD = "PWSENT"
USER = "USERMARK"
QUERY = "QUERYSENT"
FRAGMENT = "FRAGSENT"
HOST = "HOSTMARK"
ESCAPE = "\x1b"
OVERRIDE = chr(0x202E)
# U+2100 normalizes to "a/c", which moves the end of a host.
NORMALIZED = chr(0x2100)


def _url(
    host: str, path: str, *, query: bool = True, header: bool = False
) -> str:
    """Return a URL that carries every sentinel.

    Args:
        host: Host, with a port if the case needs one.
        path: Path marker, which has an escape and a right-to-left override
            appended.
        query: Whether the URL has a query and a fragment.
        header: Whether the URL is the value of a header, which cannot hold the
            override.

    Returns:
        The URL, with a user and password.
    """
    override = "" if header else OVERRIDE
    url = f"https://{USER}:{PASSWORD}@{host}/{path}{ESCAPE}[31m{override}"
    if query:
        url += f"?token={QUERY}#{FRAGMENT}"
    return url


def _caught(thunk: Callable[[], object]) -> BaseException:
    """Return the exception that a call raises.

    Args:
        thunk: Call that must raise.

    Returns:
        The exception.
    """
    try:
        thunk()
    except Exception as err:  # noqa: BLE001
        return err
    raise AssertionError("the call raised nothing")


def _surfaces(thing: object) -> list[str]:
    """Return every text by which a caller or a log can read ``thing``.

    Args:
        thing: An exception, or another object, such as a page.

    Returns:
        ``str()`` and ``repr()`` of the object, and of each argument and each
        exception reachable by ``__cause__`` or ``__context__``, and the line
        that a traceback prints for each. The lines of source code that a
        traceback prints are the test's own, so they are left out.
    """
    if not isinstance(thing, BaseException):
        return [str(thing), repr(thing)]
    texts: list[str] = []
    pending: list[BaseException | None] = [thing]
    seen: set[int] = set()
    while pending:
        err = pending.pop()
        if err is None or id(err) in seen:
            continue
        seen.add(id(err))
        texts += [
            str(err),
            repr(err),
            "".join(traceback.format_exception_only(err)),
        ]
        for arg in err.args:
            texts += [str(arg), repr(arg)]
        pending += [err.__cause__, err.__context__]
    return texts


@dataclass(frozen=True)
class Case:
    """One site, reached from one source."""

    run: Callable[[Any], object]
    """Drive the site, given the ``requests_mock`` fixture, and return what it
    produced: the exception it raised, or a page."""
    shows: tuple[str, ...] = ()
    """Text that the message must hold."""
    hides: tuple[str, ...] = ()
    """Text that the message must not hold, besides the sentinels."""
    unreadable: bool = False
    """Whether requests cannot prepare the URL, so that nothing is quoted and
    no exception is chained."""
    mocked: bool = True
    """Whether the case needs ``requests_mock``, which replaces the session's
    choice of an adapter, so that no scheme lacks one."""


def _api(base: str = "https://h.test/", **attributes: Any) -> API:
    """Return an API with the attributes set.

    Args:
        base: Base URL.
        **attributes: Attributes to set on the API.

    Returns:
        The API.
    """
    api = API(base)
    for name, value in attributes.items():
        setattr(api, name, value)
    return api


def _page(api: API, requests_mock: Any, link: str, **headers: Any) -> Any:
    """Return the first page of a collection that links to ``link``.

    Args:
        api: Client that reads the pages.
        requests_mock: The ``requests_mock`` fixture.
        link: The next-page link.
        **headers: Headers for the first page's response.

    Returns:
        The page.
    """
    requests_mock.get(
        "https://h.test/items",
        json={"results": [1], "next": link},
        headers=headers,
    )
    return next_links_page(
        api,
        api.get("https://h.test/items"),
        allow_other_origins=False,
    )


def _walk(api: API, link: str, **kwargs: Any) -> Any:
    """Follow a collection whose first page links to ``link``.

    Args:
        api: Client that reads the pages.
        link: The next-page link.
        **kwargs: Further arguments of ``follow_next_links()``.

    Returns:
        The items, which the call has read.
    """
    first = {"results": [1], "next": link}
    return list(
        follow_next_links(api, first, url="https://h.test/items", **kwargs)
    )


# --- URLs that the caller gave -------------------------------------------


def _caller_status(status: int) -> Callable[[Any], object]:
    """Return a case for a failed request that the caller named.

    Args:
        status: Status of the response.

    Returns:
        The case's driver.
    """

    def run(mock: Any) -> object:
        """Send a request that fails with the status.

        Args:
            mock: The ``requests_mock`` fixture.

        Returns:
            The error.
        """
        mock.get(ANY, status_code=status, reason="Boom")
        return _caught(lambda: _api().get(_url("h.test", "CALLERPATH")))

    return run


def _caller_not_json(mock: Any) -> object:
    """Decode a body that is no JSON.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, text="nope")
    api = _api()
    resp = api.get(_url("h.test", "CALLERPATH"))
    return _caught(lambda: api.json(resp))


def _caller_too_deep(mock: Any) -> object:
    """Decode a body that nests too deeply.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, json=[[[1]]])
    api = _api(max_json_depth=1)
    resp = api.get(_url("h.test", "CALLERPATH"))
    return _caught(lambda: api.json(resp))


def _caller_too_large(mock: Any) -> object:
    """Read a body larger than the limit.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, text="abcdefgh")
    api = _api(max_response_bytes=2)
    return _caught(lambda: api.get(_url("h.test", "CALLERPATH")))


def _caller_out_of_time(mock: Any) -> object:
    """Send a request that outlasts the total timeout.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, text="x")
    api = _api(total_timeout=1e-9)
    return _caught(lambda: api.get(_url("h.test", "CALLERPATH")))


def _caller_redirected_away(mock: Any) -> object:
    """Send a request that a server redirects to another origin.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(
        ANY,
        status_code=302,
        headers={"Location": _url("other.test", "SERVERPATH", header=True)},
    )
    return _caught(lambda: _api().get(_url("h.test", "CALLERPATH")))


def _caller_redirect_unparsable(mock: Any) -> object:
    """Send a request that a server redirects to a port that is no number.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(
        ANY,
        status_code=302,
        headers={
            "Location": _url("other.test:99999", "SERVERPATH", header=True)
        },
    )
    return _caught(lambda: _api().get(_url("h.test", "CALLERPATH")))


def _caller_write_redirected(mock: Any) -> object:
    """Send a write that a server answers with a redirect.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.post(
        ANY,
        status_code=302,
        headers={"Location": _url("h.test", "SERVERPATH", header=True)},
    )
    return _caught(lambda: _api().post(_url("h.test", "CALLERPATH"), json={}))


def _caller_final_redirect(mock: Any) -> object:
    """Accept a final response that is a redirect.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(
        ANY,
        status_code=302,
        headers={"Location": _url("h.test", "SERVERPATH", header=True)},
    )
    resp = _api().get(_url("h.test", "CALLERPATH"), allow_redirects=False)
    return _caught(lambda: _check_final_response(resp, "nothing was read"))


def _caller_failure_after_redirect(mock: Any) -> object:
    """Send a request that a server redirects, and the target fails.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    target = _url("h.test", "SERVERPATH", header=True)
    mock.get(
        ANY,
        [
            {"status_code": 302, "headers": {"Location": target}},
            {"status_code": 500, "reason": "Boom"},
        ],
    )
    return _caught(lambda: _api().get(_url("h.test", "CALLERPATH")))


def _caller_unpreparable(url: str) -> Callable[[Any], object]:
    """Return a case for a URL that requests cannot send.

    Args:
        url: The URL that the caller gives.

    Returns:
        The case's driver.
    """

    def run(mock: Any) -> object:
        """Send a request to the URL.

        Args:
            mock: The ``requests_mock`` fixture.

        Returns:
            The error.
        """
        del mock
        return _caught(lambda: _api().get(url))

    return run


def _caller_page_repr(mock: Any) -> object:
    """Show a page that the caller's URL named.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The page.
    """
    mock.get(ANY, json={"results": [1]})
    api = _api()
    return next_links_page(api, api.get(_url("h.test", "CALLERPATH")))


def _caller_resolution(mock: Any) -> object:
    """Resolve a URL whose host normalizes to another host.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    del mock
    url = f"https://{USER}:{PASSWORD}@{HOST}{NORMALIZED}/x"
    return _caught(lambda: _api().resolve_url(url))


CALLER_CASES = {
    "caller-server-error": Case(
        _caller_status(500), shows=("CALLERPATH", "?..."), hides=(HOST,)
    ),
    "caller-not-found": Case(
        _caller_status(404), shows=("CALLERPATH", "?...")
    ),
    "caller-body-is-no-json": Case(_caller_not_json, shows=("CALLERPATH",)),
    "caller-json-too-deep": Case(_caller_too_deep, shows=("CALLERPATH",)),
    "caller-body-too-large": Case(
        _caller_too_large, shows=("CALLERPATH", "?...")
    ),
    "caller-out-of-time": Case(
        _caller_out_of_time, shows=("CALLERPATH", "?...")
    ),
    "caller-redirected-away": Case(
        _caller_redirected_away,
        shows=("CALLERPATH", "?..."),
        hides=("SERVERPATH", "other.test"),
    ),
    "caller-redirect-unparsable": Case(
        _caller_redirect_unparsable,
        shows=("CALLERPATH",),
        hides=("SERVERPATH", "other.test"),
    ),
    "caller-write-redirected": Case(
        _caller_write_redirected,
        shows=("CALLERPATH",),
        hides=("SERVERPATH",),
    ),
    "caller-final-redirect": Case(
        _caller_final_redirect,
        shows=("CALLERPATH",),
        hides=("SERVERPATH",),
    ),
    "caller-failure-after-redirect": Case(
        _caller_failure_after_redirect,
        shows=("CALLERPATH",),
        hides=("SERVERPATH",),
    ),
    "caller-scheme-without-adapter": Case(
        _caller_unpreparable(
            f"ftp://{USER}:{PASSWORD}@{HOST}/x?token={QUERY}#{FRAGMENT}"
        ),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "caller-port-out-of-range": Case(
        _caller_unpreparable(_url(f"{HOST}:99999", "x")),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "caller-url-without-scheme": Case(
        _caller_unpreparable(f"{USER}:{PASSWORD}@{HOST}/x"),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "caller-page-repr": Case(_caller_page_repr, shows=("CALLERPATH", "?...")),
    "caller-host-that-normalizes": Case(
        _caller_resolution, hides=(HOST,), unreadable=True, mocked=False
    ),
}


# --- URLs that a server chose --------------------------------------------


def _server_failed_page(mock: Any) -> object:
    """Request a next-page link that answers with a failure.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, status_code=500, reason="Boom")
    return _caught(lambda: _walk(_api(), _url("h.test", "SERVERPATH")))


def _server_redirected_page(mock: Any) -> object:
    """Request a next-page link that redirects to another origin.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(
        ANY,
        status_code=302,
        headers={"Location": _url("evil.test", "TARGETPATH", header=True)},
    )
    return _caught(lambda: _walk(_api(), _url("h.test", "SERVERPATH")))


def _server_other_origin(mock: Any) -> object:
    """Read a next-page link to another origin.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    del mock
    return _caught(lambda: _walk(_api(), _url("other.test", "SERVERPATH")))


def _server_backslash_host(mock: Any) -> object:
    """Read a link whose backslash moves the end of its user.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    del mock
    link = f"https://other.test\\@h.test/SERVERPATH?token={QUERY}"
    return _caught(lambda: _walk(_api(), link))


def _server_page_read_twice(mock: Any) -> object:
    """Follow a link to a page that was read.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    link = _url("h.test", "SERVERPATH")
    mock.get(ANY, json={"results": [2], "next": link})
    return _caught(lambda: _walk(_api(), link, allow_other_origins=True))


def _server_page_is_no_json(mock: Any) -> object:
    """Request a next-page link whose body is no JSON.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, text="nope")
    return _caught(lambda: _walk(_api(), _url("h.test", "SERVERPATH")))


def _server_unpreparable_link(
    link: str, *, allow: bool
) -> Callable[[Any], object]:
    """Return a case for a next-page link that requests cannot prepare.

    Args:
        link: The link.
        allow: Whether the walk accepts any origin.

    Returns:
        The case's driver.
    """

    def run(mock: Any) -> object:
        """Follow the link.

        Args:
            mock: The ``requests_mock`` fixture.

        Returns:
            The error.
        """
        del mock
        return _caught(lambda: _walk(_api(), link, allow_other_origins=allow))

    return run


def _server_link_that_normalizes(mock: Any) -> object:
    """Read a link whose host normalizes to another host.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, json={"results": [1], "next": None})
    api = _api()
    link = f"https://{USER}:{PASSWORD}@{HOST}{NORMALIZED}/x"
    payload = {"results": [1], "next": link}
    mock.get("https://h.test/items", json=payload)
    resp = api.get("https://h.test/items")
    return _caught(lambda: next_links_page(api, resp))


def _server_next_page_fails(mock: Any) -> object:
    """Request the page that a page links to, which fails.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    api = _api()
    page = _page(api, mock, _url("h.test", "SERVERPATH"))
    mock.get(ANY, status_code=500, reason="Boom")
    return _caught(page.next_page)


def _server_next_page_other_origin(mock: Any) -> object:
    """Request the page that a page links to, at another origin.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    page = _page(_api(), mock, _url("other.test", "SERVERPATH"))
    return _caught(page.next_page)


def _server_next_page_read_twice(mock: Any) -> object:
    """Walk to a page whose link names a page that was read.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    link = _url("h.test", "SERVERPATH")
    api = _api()
    page = _page(api, mock, link)
    mock.get(
        ANY,
        json={"results": [2], "next": link},
    )
    second = page.next_page()
    assert second is not None
    return _caught(second.next_page)


def _server_page_repr(mock: Any) -> object:
    """Show a page that a link named.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The page.
    """
    api = _api()
    page = _page(api, mock, _url("h.test", "SERVERPATH"))
    mock.get(ANY, json={"results": [2]})
    second = page.next_page()
    assert second is not None
    return second


def _server_link_header_fails(mock: Any) -> object:
    """Request a link of a ``Link`` header, which fails.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    api = _api()
    link = _url("h.test", "SERVERPATH", header=True)
    mock.get(
        ANY,
        [
            {"json": [1], "headers": {"Link": f'<{link}>; rel="next"'}},
            {"status_code": 500, "reason": "Boom"},
        ],
    )
    first = api.get("https://h.test/items")
    return _caught(lambda: list(follow_link_headers(api, first)))


def _server_link_header_that_normalizes(mock: Any) -> object:
    """Read a ``Link`` header whose host normalizes to another host.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    link = f"https://{USER}:{PASSWORD}@{HOST}{NORMALIZED}/x"
    mock.get(
        "https://h.test/items",
        json=[1],
        headers={"Link": f'<{link}>; rel="next"'},
    )
    api = _api()
    resp = api.get("https://h.test/items")
    return _caught(lambda: link_headers_page(api, resp))


SERVER_CASES = {
    "server-link-fails": Case(
        _server_failed_page,
        shows=("page 2",),
        hides=("SERVERPATH", HOST),
    ),
    "server-link-redirects-away": Case(
        _server_redirected_page,
        shows=("page 2",),
        hides=("SERVERPATH", "TARGETPATH", "evil.test"),
    ),
    "server-link-to-other-origin": Case(
        _server_other_origin,
        shows=("other.test",),
        hides=("SERVERPATH",),
    ),
    "server-link-with-backslash-in-authority": Case(
        _server_backslash_host,
        shows=("other.test",),
        hides=("SERVERPATH",),
    ),
    "server-link-to-page-read": Case(
        _server_page_read_twice,
        shows=("page 3", "'https://h.test'"),
        hides=("SERVERPATH",),
    ),
    "server-link-answers-no-json": Case(
        _server_page_is_no_json,
        shows=("GET page 2 of https://h.test",),
        hides=("SERVERPATH",),
    ),
    "server-link-with-scheme-without-adapter": Case(
        _server_unpreparable_link(
            f"ftp://{USER}:{PASSWORD}@{HOST}/x?token={QUERY}", allow=False
        ),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "server-link-with-scheme-without-adapter-any-origin": Case(
        _server_unpreparable_link(
            f"ftp://{USER}:{PASSWORD}@{HOST}/x?token={QUERY}", allow=True
        ),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "server-link-with-port-out-of-range": Case(
        _server_unpreparable_link(_url(f"{HOST}:99999", "x"), allow=True),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "server-link-with-host-that-normalizes": Case(
        _server_link_that_normalizes, hides=(HOST,), unreadable=True
    ),
    "server-next-page-fails": Case(
        _server_next_page_fails,
        shows=("GET the next page of https://h.test",),
        hides=("SERVERPATH",),
    ),
    "server-next-page-at-other-origin": Case(
        _server_next_page_other_origin,
        shows=("other.test",),
        hides=("SERVERPATH",),
    ),
    "server-next-page-read-twice": Case(
        _server_next_page_read_twice,
        shows=("the next page of 'https://h.test'",),
        hides=("SERVERPATH",),
    ),
    "server-page-repr": Case(
        _server_page_repr,
        shows=("url='https://h.test'",),
        hides=("SERVERPATH",),
    ),
    "server-link-header-fails": Case(
        _server_link_header_fails,
        shows=("GET page 2 of https://h.test",),
        hides=("SERVERPATH",),
    ),
    "server-link-header-host-that-normalizes": Case(
        _server_link_header_that_normalizes,
        hides=(HOST,),
        unreadable=True,
    ),
}


# --- URLs that the configuration holds -----------------------------------

CONFIG_BASE = _url("h.test", "CONFIGPATH")


def _config_page_refused(mock: Any) -> object:
    """Read a next-page link to another origin, which names the base URL.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    del mock
    return _caught(lambda: _walk(_api(CONFIG_BASE), "https://other.test/next"))


def _config_page_redirected(mock: Any) -> object:
    """Read a page that redirects to another origin, naming the base URL.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(
        ANY,
        status_code=302,
        headers={"Location": "https://other.test/x"},
    )
    return _caught(
        lambda: _walk(_api(CONFIG_BASE), _url("h.test", "SERVERPATH"))
    )


def _config_failure(mock: Any) -> object:
    """Fail a request that is relative to the base URL.

    Args:
        mock: The ``requests_mock`` fixture.

    Returns:
        The error.
    """
    mock.get(ANY, status_code=500, reason="Boom")
    base = _url("h.test", "CONFIGPATH", query=False)
    return _caught(lambda: _api(base).get("item"))


def _config_unpreparable(
    base: str, **attributes: Any
) -> Callable[[Any], object]:
    """Return a case for a configured URL that requests cannot send.

    Args:
        base: Base URL.
        **attributes: Attributes to set on the API before the request.

    Returns:
        The case's driver.
    """

    def run(mock: Any) -> object:
        """Send a request relative to the base URL.

        Args:
            mock: The ``requests_mock`` fixture.

        Returns:
            The error.
        """
        del mock
        return _caught(lambda: _api(base, **attributes).get("x"))

    return run


CONFIG_CASES = {
    "config-page-refused": Case(
        _config_page_refused,
        shows=("CONFIGPATH",),
        hides=("other.test/next",),
    ),
    "config-page-redirected": Case(
        _config_page_redirected,
        shows=("CONFIGPATH",),
        hides=("SERVERPATH",),
    ),
    "config-failure": Case(_config_failure, shows=("CONFIGPATH",)),
    "config-base-without-scheme": Case(
        _config_unpreparable(f"{USER}:{PASSWORD}@{HOST}/"),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "config-base-port-out-of-range": Case(
        _config_unpreparable(_url(f"{HOST}:99999", "x")),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "config-base-host-that-normalizes": Case(
        _config_unpreparable(f"https://{USER}:{PASSWORD}@{HOST}{NORMALIZED}/"),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "config-redirect-origin-without-scheme": Case(
        _config_unpreparable(
            "https://h.test/",
            redirect_origins=[f"{USER}:{PASSWORD}@{HOST}/"],
        ),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
    "config-redirect-origin-port-out-of-range": Case(
        _config_unpreparable(
            "https://h.test/",
            redirect_origins=[_url(f"{HOST}:99999", "x")],
        ),
        hides=(HOST,),
        unreadable=True,
        mocked=False,
    ),
}

CASES: dict[str, tuple[str, Case]] = {
    **{name: ("caller", case) for name, case in CALLER_CASES.items()},
    **{name: ("server", case) for name, case in SERVER_CASES.items()},
    **{name: ("config", case) for name, case in CONFIG_CASES.items()},
}

# Cases that show more than the rule for their source allows, with the behavior
# that each one gets wrong. Each is a strict expected failure until the code
# that parses a URL does so as requests prepares it.
_RAW_ERROR = "an error of requests for a URL that it cannot send quotes it"
KNOWN_FAILURES: dict[str, str] = {
    "caller-scheme-without-adapter": _RAW_ERROR,
    "caller-port-out-of-range": _RAW_ERROR,
    "caller-url-without-scheme": _RAW_ERROR,
    "caller-host-that-normalizes": _RAW_ERROR,
    "config-base-without-scheme": _RAW_ERROR,
    "config-base-port-out-of-range": _RAW_ERROR,
    "config-base-host-that-normalizes": _RAW_ERROR,
    "config-redirect-origin-without-scheme": _RAW_ERROR,
    "config-redirect-origin-port-out-of-range": _RAW_ERROR,
}


def _param(name: str) -> Any:
    """Return the pytest parameter for a case.

    Args:
        name: Case name.

    Returns:
        The parameter, marked as an expected failure if the case is known to
        fail.
    """
    marks = []
    if name in KNOWN_FAILURES:
        marks.append(
            pytest.mark.xfail(strict=True, reason=KNOWN_FAILURES[name])
        )
    return pytest.param(name, id=name, marks=marks)


@pytest.mark.parametrize("name", [_param(name) for name in CASES])
def test_message_shows_only_what_its_source_allows(
    name: str, request: pytest.FixtureRequest
) -> None:
    """A URL is shown by its source's rule at every site that shows one."""
    source, case = CASES[name]
    mock = request.getfixturevalue("requests_mock") if case.mocked else None
    result = case.run(mock)
    texts = _surfaces(result)

    forbidden = [
        PASSWORD,
        USER,
        QUERY,
        FRAGMENT,
        ESCAPE,
        OVERRIDE,
        *case.hides,
    ]
    if source == "server":
        forbidden.append("SERVERPATH")
    for text in texts:
        for part in forbidden:
            assert part not in text, (name, part, text)

    joined = "\n".join(texts)
    for part in case.shows:
        assert part in joined, (name, part, joined)
    if case.unreadable:
        assert isinstance(result, BaseException)
        assert result.__cause__ is None, result.__cause__
        assert result.__context__ is None, result.__context__


def test_error_that_requests_raises_while_sending_passes_unchanged(
    requests_mock: Any,
) -> None:
    """A failure of the connection keeps its class, arguments, and chain."""
    cause = ConnectionResetError("reset")
    failure = requests.ConnectionError("boom")
    failure.__cause__ = cause
    requests_mock.get(ANY, exc=failure)
    error = _caught(lambda: _api().get("https://h.test/x"))
    assert error is failure
    assert type(error) is requests.ConnectionError
    assert error.args == ("boom",)
    assert error.__cause__ is cause


def test_not_found_and_api_error_keep_their_classes(
    requests_mock: Any,
) -> None:
    """A message that hides a URL's parts changes no exception class."""
    requests_mock.get("https://h.test/a", status_code=404)
    requests_mock.get("https://h.test/b", status_code=500)
    assert type(_caught(lambda: _api().get("a"))) is NotFound
    assert type(_caught(lambda: _api().get("b"))) is APIError
