"""A budget with no request name names a response as errors elsewhere do."""

from __future__ import annotations

import io

import pytest
import requests
from urllib3.response import HTTPResponse

from eazyrest import LimitExceeded
from eazyrest._errors import _response_name
from eazyrest.api import API, _Budget


def _response(url: str, asked: str | None) -> requests.Response:
    """Return a response to a GET of ``url``, asked for as ``asked``."""
    resp = requests.Response()
    resp.url = url
    resp.raw = HTTPResponse(body=io.BytesIO(b""), preload_content=False)
    resp.request = requests.Request("GET", url).prepare()
    if asked is not None:
        resp._asked_url = asked  # type: ignore[attr-defined]
        resp._asked_page = None  # type: ignore[attr-defined]
    return resp


CASES = {
    "asked for": ("https://host.example/redirected", "/things/1/"),
    "not asked for": ("https://host.example/things/1/", None),
}


@pytest.mark.parametrize(("url", "asked"), CASES.values(), ids=CASES)
@pytest.mark.parametrize("over", ["time", "size"])
def test_a_budget_without_a_name_names_the_response_as_an_error_does(
    url: str, asked: str | None, over: str
) -> None:
    """The error holds the name that ``_response_name()`` gives."""
    API("https://host.example/")
    resp = _response(url, asked)
    budget = _Budget(1, 0.0)

    refuse = (
        budget.check_time
        if over == "time"
        else lambda resp: budget._too_large(resp, False)
    )
    with pytest.raises(LimitExceeded) as caught:
        refuse(resp)

    assert _response_name(resp) in str(caught.value)


def test_a_budget_with_a_name_names_the_request_by_it() -> None:
    """The name of the request wins over the response's."""
    resp = _response("https://host.example/other", "/other")
    budget = _Budget(1, 0.0, False, "GET https://host.example/named")

    with pytest.raises(
        LimitExceeded, match=r"GET https://host\.example/named"
    ):
        budget.check_time(resp)
