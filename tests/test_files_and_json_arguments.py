"""``files=`` and ``json=`` are mappings of string names, or the call raises.

The other mapping arguments, such as ``fields=`` and ``params=``, raise
``TypeError`` naming the argument for a value that is no mapping of string
names. ``files=`` and ``json=`` do the same, before anything else, instead of
raising ``AttributeError`` from a method of the wrong type, or the
``ValueError`` of ``dict()``.
"""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://mapping-arguments.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class Doc(Base):
    """A model that uploads files."""

    class_url = "/docs/"
    id: int
    name: str


def loaded() -> Doc:
    """Return a document that has read its fields."""
    return Doc(json={"id": 1, "name": "a"})


FILES: dict[str, Callable[[Any], Any]] = {
    "create": lambda value: Doc.create(name="a", files=value),
    "save": lambda value: change(loaded(), value, full=False),
    "save(full=True)": lambda value: change(loaded(), value, full=True),
}
JSON: dict[str, Callable[[Any], Any]] = {
    "constructor": lambda value: Doc(json=value),
    "update_from_json": lambda value: loaded().update_from_json(value),
}
NO_MAPPING: dict[str, Any] = {
    "a list of pairs": [("image", b"x")],
    "a tuple of pairs": (("image", b"x"),),
    "a list of one object": [{"id": 1}],
    "a string": "abc",
    "bytes": b"abc",
    "an int": 7,
    "an empty list": [],
    "zero": 0,
}
BAD_NAMES: dict[str, Any] = {
    "an int name": {7: b"x"},
    "a tuple name": {("a", "b"): b"x"},
}


def change(obj: Doc, files: Any, *, full: bool) -> None:
    """Change a field and save it with files."""
    obj.name = "b"
    obj.save(full=full, files=files)


@pytest.mark.parametrize("value", [*NO_MAPPING.values(), *BAD_NAMES.values()])
@pytest.mark.parametrize("call", FILES.values(), ids=FILES.keys())
def test_files_that_is_no_mapping_of_names_is_refused(
    call: Callable[[Any], Any], value: Any
) -> None:
    """The error names ``files=`` and nothing is sent."""
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(TypeError, match=r"files="):
            call(value)
        assert mocker.call_count == 0


@pytest.mark.parametrize("value", [*NO_MAPPING.values(), *BAD_NAMES.values()])
@pytest.mark.parametrize("call", JSON.values(), ids=JSON.keys())
def test_json_that_is_no_mapping_of_names_is_refused(
    call: Callable[[Any], Any], value: Any
) -> None:
    """The error names ``json=``."""
    with pytest.raises(TypeError, match=r"json="):
        call(value)


@pytest.mark.parametrize("files", [None, {}, {"doc": b"x"}])
def test_none_and_mappings_of_names_are_taken(files: Any) -> None:
    """A mapping of string names, empty or not, and ``None``, are files."""
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(
            requests_mock.ANY, requests_mock.ANY, json={"id": 1}
        )
        Doc.create(name="a", files=files)
        assert mocker.call_count == 1


def test_a_json_mapping_of_any_kind_is_taken() -> None:
    """A mapping that is no ``dict`` is a mapping."""
    assert Doc(json=MappingProxyType({"id": 1, "name": "a"})).name == "a"
