"""An object of a model without a key, built without JSON, has none.

Such a model describes a value that a server embeds, as a nested serializer
without an ``id`` does. Built without ``json=``, its object holds an empty
object, so it has no field to read.
"""

from __future__ import annotations

import pytest

from eazyrest import API, JSONObject, json_object

api = API("https://keyless.example/")


@json_object
class Address(JSONObject):
    """A model without a key."""

    street: str


Address.register_api(api)


def test_the_json_of_a_keyless_object_built_without_json_is_empty() -> None:
    """The object holds an empty object."""
    assert Address().json == {}


def test_a_field_of_such_an_object_is_missing() -> None:
    """Reading a field raises ``AttributeError`` and sends no request."""
    address = Address()
    with pytest.raises(AttributeError, match="street"):
        address.street  # noqa: B018
    assert not hasattr(address, "street")


def test_a_keyless_object_built_with_json_reads_it() -> None:
    """The JSON that is given is the object's own."""
    assert Address(json={"street": "Main"}).street == "Main"
