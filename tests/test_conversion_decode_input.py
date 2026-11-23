"""A conversion's ``decode`` gets a copy of the JSON that it is given."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from typing_extensions import TypedDict

from eazyrest import Conversion, JSONObject, json_object


class Box:
    """A class of the program, whose conversion changes its input."""

    def __init__(self, data: Any) -> None:
        """Hold the data."""
        self.data = data


class Window(TypedDict):
    """A ``TypedDict`` whose entry replaces its structure."""

    start: int


def decode_box(value: Any, cls: type[Any]) -> Box:
    """Decode a box, and then change the JSON that was given."""
    box = cls(dict(value))
    value["changed"] = True
    return box  # type: ignore[no-any-return]


def decode_window(value: Any, cls: type[Any]) -> Window:
    """Decode a window, and then change the JSON that was given."""
    window = Window(start=value["start"])
    value["changed"] = True
    return window


@json_object
class Screen(JSONObject):
    """A model whose conversions change what they are given."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Box: Conversion(decode=decode_box, encode=lambda value, cls: {}),
        Window: Conversion(
            decode=decode_window, encode=lambda value, cls: dict(value)
        ),
    }

    class_url = "/screens/"
    id: int
    box: Box
    window: Window


def test_a_conversion_of_a_leaf_cannot_change_the_objects_json() -> None:
    """The object's JSON is what the server sent, whatever decode does."""
    screen = Screen(json={"id": 1, "box": {"a": 1}, "window": {"start": 2}})
    assert screen.box.data == {"a": 1}
    assert screen.json["box"] == {"a": 1}


def test_a_conversion_of_a_class_cannot_change_the_objects_json() -> None:
    """The same holds for a class that an entry converts as a whole."""
    screen = Screen(json={"id": 1, "box": {"a": 1}, "window": {"start": 2}})
    assert screen.window == {"start": 2}
    assert screen.json["window"] == {"start": 2}
