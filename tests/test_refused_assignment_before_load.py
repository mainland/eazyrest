"""A refused assignment to an unloaded object sends no request."""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://refused.example/")


@json_object
class Person(JSONObject):
    """A related model."""

    class_url = "/people/"
    id: int


@json_object
class Page(JSONObject):
    """A model with a relation and a collection of them."""

    class_url = "/pages/"
    id: int
    author: Person
    editors: list[Person]


Person.register_api(api)
Page.register_api(api)

REFUSED = [
    ("author", "8"),
    ("author", True),
    ("author", {"id": 8}),
    ("editors", [None]),
    ("editors", ["8"]),
    ("editors", Person(id=8)),
]


@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize(("field", "value"), REFUSED)
def test_a_refused_value_is_refused_before_the_load(
    field: str, value: Any, status: int
) -> None:
    """The conversion error comes first, whatever the server would answer."""
    page = Page(id=1)
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, status_code=status, json={})
        with pytest.raises(FieldConversionError, match=f"Page.{field}"):
            setattr(page, field, value)
        assert mocker.call_count == 0
