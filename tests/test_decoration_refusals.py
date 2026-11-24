"""A class that cannot hold fields, or declares a default, is refused.

``@json_object`` gives a ``JSONObject`` subclass its fields. A class that is
not one would build and then fail with a ``TypeError`` about arguments and with
bare ``AttributeError`` for each field. A default in the class body is never
applied, so the field would raise ``AttributeError`` when the JSON lacks it,
and 1.0 has no defaults.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from eazyrest import JSONObject, json_object


class Plain:
    """A class that forgot to derive from ``JSONObject``."""

    id: int
    name: str


def test_a_class_that_is_no_jsonobject_is_refused() -> None:
    """The error names the class and ``JSONObject``."""
    with pytest.raises(TypeError, match=r"Plain .*JSONObject") as caught:
        json_object(Plain)  # type: ignore[type-var]
    assert not isinstance(caught.value, AttributeError)


def test_a_class_that_is_no_jsonobject_is_refused_with_options() -> None:
    """The decorator with arguments refuses it too."""
    with pytest.raises(TypeError, match=r"Plain .*JSONObject"):
        json_object(pk="id")(Plain)  # type: ignore[type-var]


def test_an_object_that_is_no_class_is_refused() -> None:
    """A value that is no class is named by its representation."""
    with pytest.raises(TypeError, match="JSONObject"):
        json_object(7)  # type: ignore[call-overload]


DEFAULTS: dict[str, Any] = {
    "a string": "anonymous",
    "None": None,
    "an empty list": [],
    "a number": 0,
}


@pytest.mark.parametrize("default", DEFAULTS.values(), ids=DEFAULTS.keys())
def test_a_default_in_the_class_body_is_refused(default: Any) -> None:
    """The error names the class and the field."""
    namespace = {
        "class_url": "/things/",
        "__annotations__": {"id": int, "name": Any},
        "name": default,
    }
    with pytest.raises(TypeError, match=r"Thing\.name .*default") as caught:
        json_object(type("Thing", (JSONObject,), namespace))
    assert not isinstance(caught.value, AttributeError)


def test_a_default_in_a_subclass_is_refused() -> None:
    """A subclass that declares a field with a default is refused too."""

    @json_object
    class Base(JSONObject):
        """A model without defaults."""

        class_url = "/bases/"
        id: int

    namespace = {"__annotations__": {"name": Any}, "name": "x"}
    with pytest.raises(TypeError, match=r"Child\.name .*default"):
        json_object(type("Child", (Base,), namespace))


def test_the_other_class_attributes_are_taken() -> None:
    """A ``ClassVar``, a method, and an excluded annotation hold no field."""

    @json_object(exclude={"skipped"})
    class Thing(JSONObject):
        """A model with class attributes that are not fields."""

        class_url = "/things/"
        id: int
        label: ClassVar[str] = "thing"
        skipped: int = 3

        def shout(self) -> str:
            """Return the label."""
            return self.label.upper()

    assert Thing(id=1).shout() == "THING"


def test_decorating_a_class_again_is_taken() -> None:
    """A class whose fields are descriptors already holds no default."""

    @json_object
    class Thing(JSONObject):
        """A decorated model."""

        class_url = "/things/"
        id: int

    assert json_object(Thing) is Thing
