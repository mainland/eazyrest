"""A related object that a read builds takes its parent's write mode."""

from __future__ import annotations

import pytest

from eazyrest import API, JSONObject, json_object

api = API("https://related-mode.example/")


@json_object
class Owner(JSONObject):
    """A related model."""

    class_url = "/owners/"
    id: int
    name: str


@json_object
class Item(JSONObject):
    """A model with a single relation and a collection of them."""

    class_url = "/items/"
    id: int
    owner: Owner
    owners: list[Owner]


Owner.register_api(api)
Item.register_api(api)


@pytest.mark.parametrize("mode", ["eager", "lazy"])
@pytest.mark.parametrize("embedded", [True, False])
def test_related_objects_inherit_the_write_mode(
    mode: str, embedded: bool
) -> None:
    """An embedded object and one built from a key take the parent's mode."""
    owner = {"id": 7, "name": "a"} if embedded else 7
    item = Item(
        json={"id": 1, "owner": owner, "owners": [owner]},
        write_mode=mode,  # type: ignore[arg-type]
    )
    assert item.owner.write_mode == mode
    assert item.owners[0].write_mode == mode
