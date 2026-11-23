"""``params=``, ``fields=``, and ``defaults=`` are mappings of string names.

A call that gives one of them another form raises ``TypeError`` that names the
argument, before any request, as ``request_options=`` does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://argument-types.example/")


@json_object
class Item(JSONObject):
    """A model that is queried and created."""

    class_url = "/items/"
    id: int
    name: str


Item.register_api(api)

BAD_NAMES = {7: 1}
NOT_MAPPINGS = [[("name", "x")], ("name", "x"), "name", 7]

QUERIES: dict[str, Callable[..., Any]] = {
    "filter": lambda **kw: list(Item.filter(**kw)),
    "get": lambda **kw: Item.get(**kw),
    "get_page": lambda **kw: Item.get_page(**kw),
    "get_or_create": lambda **kw: Item.get_or_create(id=1, **kw),
}


@pytest.mark.parametrize("query", sorted(QUERIES))
def test_a_parameter_name_that_is_no_string_raises_type_error(
    query: str,
) -> None:
    """The error names ``params``, and nothing is sent."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(TypeError, match="params="):
            QUERIES[query](params=BAD_NAMES)
        assert mocker.call_count == 0


@pytest.mark.parametrize("query", sorted(QUERIES))
@pytest.mark.parametrize("params", NOT_MAPPINGS)
def test_parameters_that_are_no_mapping_raise_type_error(
    query: str, params: Any
) -> None:
    """The error names ``params``, and nothing is sent."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(TypeError, match="params="):
            QUERIES[query](params=params)
        assert mocker.call_count == 0


@pytest.mark.parametrize("defaults", [*NOT_MAPPINGS, BAD_NAMES])
def test_defaults_that_are_no_mapping_of_names_raise_type_error(
    defaults: Any,
) -> None:
    """The error names ``defaults``, and nothing is sent."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(TypeError, match="defaults="):
            Item.get_or_create(id=1, defaults=defaults)
        assert mocker.call_count == 0


def test_a_field_name_that_is_no_string_raises_type_error() -> None:
    """``create()`` refuses it as it did."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(TypeError, match="fields="):
            Item.create(fields=BAD_NAMES)  # type: ignore[arg-type]
        assert mocker.call_count == 0
