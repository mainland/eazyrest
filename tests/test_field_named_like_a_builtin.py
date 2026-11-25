"""A structure's field may be named like a builtin that an annotation names.

With ``from __future__ import annotations`` the annotation ``list[int]`` of a
field named ``list`` is a string, and reading it must resolve ``list`` to the
builtin, not to the field's default or to a name that the class defines.
"""

from __future__ import annotations

import dataclasses
from typing import NamedTuple

from typing_extensions import TypedDict

from eazyrest import JSONObject, json_object


class Page(NamedTuple):
    """A named tuple with a field named like the type of its annotation."""

    list: list[int]
    total: int = 0


@dataclasses.dataclass
class Box:
    """A dataclass with fields named like the builtins that it uses."""

    list: list[int] = dataclasses.field(default_factory=list)
    type: str = ""


class Shelf(TypedDict):
    """A typed dict with a key named like the type of its annotation."""

    list: list[int]
    dict: dict[str, int]


@json_object
class Doc(JSONObject):
    """A model with a field of each structure."""

    class_url = "/docs/"
    id: int
    page: Page
    box: Box
    shelf: Shelf


def test_a_named_tuple_with_a_field_named_like_a_builtin_is_read() -> None:
    """The field reads as a list of integers."""
    doc = Doc(json={"id": 1, "page": [[1, 2], 2], "box": {}, "shelf": {}})
    assert doc.page == Page(list=[1, 2], total=2)


def test_a_dataclass_with_fields_named_like_builtins_is_read() -> None:
    """The fields read as a list and a string."""
    doc = Doc(
        json={
            "id": 1,
            "page": [[1], 0],
            "box": {"list": [1], "type": "a"},
            "shelf": {},
        }
    )
    assert doc.box == Box(list=[1], type="a")


def test_a_typed_dict_with_keys_named_like_builtins_is_read() -> None:
    """The keys read as a list and a mapping."""
    doc = Doc(
        json={
            "id": 1,
            "page": [[1], 0],
            "box": {},
            "shelf": {"list": [1, 2], "dict": {"a": 1}},
        }
    )
    assert doc.shelf == {"list": [1, 2], "dict": {"a": 1}}


def test_a_named_tuple_with_a_field_named_like_a_builtin_is_written() -> None:
    """The field is written back as a list of integers."""
    doc = Doc(
        json={"id": 1, "page": [[1], 0], "box": {}, "shelf": {}},
        write_mode="lazy",
    )
    doc.page = Page(list=[3, 4], total=1)
    assert doc.json["page"] == [[3, 4], 1]
