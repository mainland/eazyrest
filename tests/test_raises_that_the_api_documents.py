"""The exceptions that ``API`` documents are the ones it raises."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest
import requests
from requests.exceptions import InvalidURL, MissingSchema

from eazyrest import API

BASE = "https://documented-raises.example/"

BASES = {
    "no scheme": ("example.com/v1/", MissingSchema),
    "bad": ("http://[x/", InvalidURL),
}


@pytest.mark.parametrize(("url", "error"), BASES.values(), ids=BASES)
def test_a_base_url_that_requests_cannot_prepare_raises_its_class(
    url: str, error: type[Exception]
) -> None:
    """The constructor raises the class that ``reset_session()`` raises."""
    with pytest.raises(error):
        API(url)
    api = API(BASE)
    api.base_url = url
    with pytest.raises(error):
        api.reset_session()


def test_the_constructor_documents_both_classes() -> None:
    """``Raises:`` names ``MissingSchema`` and ``InvalidURL``."""
    doc = inspect.getdoc(API.__init__) or ""
    assert "MissingSchema" in doc
    assert "InvalidURL" in doc


METHODS: dict[str, Callable[[API, str], Any]] = {
    "get": lambda api, uri: api.get(uri),
    "post": lambda api, uri: api.post(uri),
    "patch": lambda api, uri: api.patch(uri),
    "put": lambda api, uri: api.put(uri),
    "delete": lambda api, uri: api.delete(uri),
}


@pytest.mark.parametrize("method", METHODS.values(), ids=METHODS)
def test_a_redirect_origin_that_is_no_url_raises_value_error_before_sending(
    method: Callable[[API, str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request method raises it, and documents it."""
    api = API(BASE)
    api.redirect_origins = ("not a url",)
    sent: list[requests.PreparedRequest] = []
    monkeypatch.setattr(
        api.session, "send", lambda request, **_: sent.append(request)
    )

    with pytest.raises(ValueError, match="redirect_origins"):
        method(api, "things/")

    assert not sent


@pytest.mark.parametrize("name", ["get", "post", "patch", "put", "delete"])
def test_each_request_method_documents_value_error(name: str) -> None:
    """``Raises:`` names ``ValueError``."""
    assert "ValueError" in (inspect.getdoc(getattr(API, name)) or "")
