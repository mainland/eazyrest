"""Tests for annotations evaluated by the running Python version.

This module does not use ``from __future__ import annotations``, so
field annotations follow the semantics of the Python version running the
tests.
"""

import sys

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
