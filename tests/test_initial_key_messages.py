"""An object built from JSON without a usable key says which it lacks."""

from __future__ import annotations

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object


@json_object
class User(JSONObject):
    """A model with an integer key."""

    class_url = "/users/"
    id: int
    name: str


def test_a_null_key_is_called_a_null_key() -> None:
    """The JSON holds the key, as ``null``."""
    with pytest.raises(FieldConversionError) as caught:
        User(json={"id": None, "name": "a"})
    assert "holds null key" in str(caught.value.__cause__)


def test_a_missing_key_is_called_no_key() -> None:
    """The JSON holds nothing under the key's name."""
    with pytest.raises(FieldConversionError) as caught:
        User(json={"name": "a"})
    assert "holds no key" in str(caught.value.__cause__)
