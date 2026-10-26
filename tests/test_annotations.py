"""Tests for annotations evaluated by the running Python version.

This module does not use ``from __future__ import annotations``, so field
annotations follow the semantics of the Python version running the tests.
"""

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
        # with target-version py310, where Node is not defined yet.
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
