"""A key whose annotation cannot be read is refused before a query's request.

The declaration is at fault, not the response, so a query raises the key's
``FieldConversionError`` as building an object does, instead of sending its
request and blaming the response for items that name no key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://unreadable-key.example/")


@json_object
class Broken(JSONObject):
    """A model whose key annotation is a string that is no expression."""

    class_url = "/broken/"
    id: "list[int"  # type: ignore[valid-type]  # noqa: F722


Broken.register_api(api)

QUERIES: dict[str, Callable[[], Any]] = {
    "build": lambda: Broken(id=1),
    "filter": lambda: list(Broken.filter()),
    "all": lambda: list(Broken.all()),
    "get": lambda: Broken.get(id=1),
    "get_page": lambda: Broken.get_page(),
}


@pytest.mark.parametrize("call", sorted(QUERIES))
def test_the_keys_error_is_raised_before_any_request(call: str) -> None:
    """The error names the model and the key, and nothing is sent."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[{"id": 1}])
        with pytest.raises(FieldConversionError, match=r"Broken\.id"):
            QUERIES[call]()
        assert mocker.call_count == 0
