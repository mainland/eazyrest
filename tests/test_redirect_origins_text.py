"""A string given as ``redirect_origins`` is refused, not read by character."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API

ORIGIN = "https://cdn.example.com"

REQUESTS: dict[str, Callable[[API], Any]] = {
    "get": lambda api: api.get("/items/"),
    "post": lambda api: api.post("/items/", json={}),
    "patch": lambda api: api.patch("/items/1/", json={}),
    "put": lambda api: api.put("/items/1/", json={}),
    "delete": lambda api: api.delete("/items/1/"),
}


class Subclassed(API):
    """An API whose subclass sets the attribute to a string."""

    redirect_origins = ORIGIN


@pytest.mark.parametrize("request_", REQUESTS.values(), ids=REQUESTS.keys())
def test_a_string_assigned_to_an_instance_is_refused(
    request_: Callable[[API], Any],
) -> None:
    """The error names the attribute and says to give a collection."""
    api = API("https://api.example.com/")
    api.redirect_origins = ORIGIN
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(TypeError, match="redirect_origins") as caught:
            request_(api)
        assert mocker.call_count == 0
    assert "collection of origins" in str(caught.value)


def test_a_string_set_in_a_subclass_is_refused() -> None:
    """A class attribute is refused as an instance's is."""
    api = Subclassed("https://api.example.com/")
    with requests_mock.Mocker() as mocker:
        mocker.get("https://api.example.com/items/", json={})
        with pytest.raises(TypeError, match="collection of origins"):
            api.get("/items/")
        assert mocker.call_count == 0


def test_a_list_of_one_origin_is_accepted() -> None:
    """A collection of origins works as before."""
    api = API("https://api.example.com/")
    api.redirect_origins = [ORIGIN]
    with requests_mock.Mocker() as mocker:
        mocker.get("https://api.example.com/items/", json={"id": 1})
        assert api.json(api.get("/items/")) == {"id": 1}
