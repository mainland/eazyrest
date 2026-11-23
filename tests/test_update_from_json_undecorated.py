"""``update_from_json()`` on an object of an undecorated class is refused."""

from __future__ import annotations

import pytest

from eazyrest import JSONObject, json_object


class Plain(JSONObject):
    """A model class that nothing decorated."""


@json_object
class Decorated(JSONObject):
    """A decorated model without a key."""

    name: str


class Derived(Decorated):
    """An undecorated subclass, which keeps its parent's fields."""


def test_an_undecorated_class_names_itself_and_says_to_decorate_it() -> None:
    """The error is a ``TypeError`` and nothing is merged."""
    plain = Plain(json={"a": 1})
    with pytest.raises(
        TypeError, match=r"Plain is not decorated.*@json_object"
    ):
        plain.update_from_json({"a": 2})
    assert plain.json == {"a": 1}


def test_an_undecorated_subclass_of_a_decorated_model_still_merges() -> None:
    """The subclass has its parent's fields."""
    derived = Derived(json={"name": "a"})
    derived.update_from_json({"name": "b"})
    assert derived.name == "b"
