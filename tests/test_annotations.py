"""Tests for annotations evaluated by the running Python version.

This module does not use ``from __future__ import annotations``, so field
annotations follow the semantics of the Python version running the tests.
"""

import datetime
import sys
from typing import ClassVar

import pytest

from eazyrest import JSONObject, json_object


@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="Python 3.14 is the first version to defer annotations",
)
def test_field_annotation_can_refer_to_decorated_class() -> None:
    """A field type may name the class that ``@json_object`` decorates."""

    @json_object
    class Node(JSONObject):
        """Model that refers to itself without quotes."""

        class_url = "/nodes/"

        id: int
        # Python 3.14 defers evaluation of this annotation, but ruff checks
        # with target-version py311, where Node is not defined yet.
        parent: Node | None  # noqa: F821

    node = Node(json={"id": 2, "parent": {"id": 1, "parent": None}})

    assert node.parent is not None
    assert node.parent.id == 1


def test_evaluated_class_var_annotation_is_not_a_field() -> None:
    """An evaluated ``ClassVar`` annotation declares a class attribute."""

    @json_object
    class Widget(JSONObject):
        """Model with an annotated class attribute."""

        class_url: ClassVar[str] = "/widgets/"

        id: int

    assert Widget._json_fields == {"id"}
    assert Widget(json={"id": 1}).url == "/widgets/1/"


def test_quoted_annotation_can_refer_to_local_class() -> None:
    """Quoted self-references resolve without a module-level class name."""

    @json_object
    class Node(JSONObject):
        """Local model with a quoted reference to itself."""

        id: int
        parent: "Node | None"

    node = Node(json={"id": 2, "parent": {"id": 1, "parent": None}})

    assert node.id == 2
    assert node.parent is not None
    assert node.parent.id == 1
    assert node.parent.parent is None


def _not_a_type() -> None:
    """Function that a field annotation names by mistake."""


def test_a_function_as_a_field_annotation_is_refused_when_decorated() -> None:
    """A field annotated as a function raises, naming the field."""
    with pytest.raises(TypeError, match=r"^Holder\.value: .*not a type"):

        @json_object
        class Holder(JSONObject):
            class_url = "/holders/"

            id: int
            value: _not_a_type  # type: ignore[valid-type]


def test_a_method_that_shadows_a_field_type_is_refused_when_decorated() -> (
    None
):
    """A method named like the field's type hides the type from its class.

    The annotation then evaluates to the method, which is no type, so the class
    is refused where it would otherwise read ``created`` unconverted.
    """
    with pytest.raises(TypeError, match=r"^Event\.created: .*not a type"):

        @json_object
        class Event(JSONObject):
            class_url = "/events/"

            id: int

            def date(self) -> str:
                return "today"

            created: date  # type: ignore[valid-type]


def test_a_field_that_shadows_its_own_type_with_none_is_refused() -> None:
    """A field named like its type, with a default of ``None``, is refused.

    The default replaces the type in the class body, so the annotation
    evaluates to ``None``, and the field would otherwise read its JSON
    unconverted. The message names the shadowing.
    """
    date = datetime.date

    with pytest.raises(TypeError, match=r"^Booking\.date: .*shadows"):

        @json_object
        class Booking(JSONObject):
            class_url = "/bookings/"

            id: int
            date: date = None  # type: ignore[valid-type]


@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="Python 3.14 is the first version to defer annotations",
)
def test_a_later_method_that_shadows_a_field_type_is_refused() -> None:
    """A method defined after the field hides its type once annotations defer.

    Before Python 3.14, the annotation is evaluated at the field and names
    ``datetime.date``. Since then, it is evaluated in the class scope when
    first needed, where ``date`` is the method, so the class is refused. The
    source is compiled without the future import that this test would otherwise
    inherit.
    """
    source = (
        "@json_object\n"
        "class Event(JSONObject):\n"
        "    class_url = '/events/'\n"
        "    id: int\n"
        "    created: date\n"
        "    def date(self):\n"
        "        return 'today'\n"
    )
    code = compile(source, "<event>", "exec", dont_inherit=True)
    namespace = {
        "JSONObject": JSONObject,
        "json_object": json_object,
        "date": datetime.date,
    }

    with pytest.raises(TypeError, match=r"^Event\.created: .*not a type"):
        exec(code, namespace)  # noqa: S102
