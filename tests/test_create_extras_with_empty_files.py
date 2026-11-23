"""``create()`` checks its extras whenever the body is JSON.

An empty ``files`` mapping sends no upload, so the body is JSON as it is
without one, and an extra that JSON would change is refused before any request
in both.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://empty-files.example/"
api = API(BASE)


@json_object
class Item(JSONObject):
    """A model that is created with extras."""

    class_url = "/items/"
    id: int


Item.register_api(api)

EXTRAS: dict[str, tuple[Any, type[Exception]]] = {
    "tuple": ((1, 2), TypeError),
    "mapping with a key that is no string": ({1: "a"}, TypeError),
    "float that is not finite": (float("nan"), ValueError),
    "object": (object(), TypeError),
}


@pytest.mark.parametrize("files", [None, {}])
@pytest.mark.parametrize("extra", sorted(EXTRAS))
def test_an_extra_that_json_would_change_is_refused_before_any_request(
    extra: str, files: Any
) -> None:
    """An empty ``files`` is refused as no ``files`` is."""
    value, error = EXTRAS[extra]
    with requests_mock.Mocker() as mocker:
        with pytest.raises(error):
            Item.create(id=7, x=value, files=files)
        assert mocker.call_count == 0


def test_an_empty_files_sends_a_json_body() -> None:
    """The extras that JSON keeps are sent as JSON."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "items/", json={"id": 7})
        Item.create(id=7, x=[1, "a", None], files={})
    assert mocker.last_request.json() == {"id": 7, "x": [1, "a", None]}
