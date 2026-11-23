"""A response that names related objects replaces a cached empty relation.

A relation read as ``null`` and a collection read as empty cache that value
without building any key. When a write or a load then brings JSON that names
related objects, the field cannot be compared by key while the related model's
key type is not defined, so it has changed and its cached value goes. Reading
it then raises the error of the undefined name, as a fresh object does, and
reads the server's objects once the name is defined.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://cache-after-response.example/"
_LATER = types.ModuleType("_cache_after_response_later")

NAMES = {
    "undefined_name": "_CacheAfterResponseKey",
    "dotted_attribute": "_cache_after_response_later.Key",
}

NAMED = {"id": 1, "title": "x", "owner": 7, "owners": [7, 8]}
"""What the server says once the relations name related objects."""


class Scenario:
    """Models whose related key names something not defined yet.

    Attributes:
        thing: The model that holds a relation and a collection.
        owner: The related model, whose key cannot be built yet.
    """

    def __init__(
        self, name: str, monkeypatch: pytest.MonkeyPatch, tag: str
    ) -> None:
        """Decorate the models before the name of the key is defined.

        Args:
            name: Key of ``NAMES``.
            monkeypatch: Defines the name later and undoes it.
            tag: Distinguishes the models of one test from another's.
        """
        self._monkeypatch = monkeypatch
        self._annotation = NAMES[name]
        self._global = self._annotation.split(".")[0]
        monkeypatch.delitem(globals(), self._global, raising=False)
        monkeypatch.delattr(_LATER, "Key", raising=False)
        if "." in self._annotation:
            monkeypatch.setitem(globals(), self._global, _LATER)
        api = API(BASE)
        self.owner: Any = json_object(
            type(
                f"Owner_{tag}",
                (JSONObject,),
                {
                    "__module__": __name__,
                    "class_url": "/owners/",
                    "__annotations__": {"id": self._annotation},
                },
            )
        )
        self.thing: Any = json_object(
            type(
                f"Thing_{tag}",
                (JSONObject,),
                {
                    "__module__": __name__,
                    "class_url": "/things/",
                    "__annotations__": {
                        "id": int,
                        "title": str,
                        "owner": self.owner | None,
                        "owners": types.GenericAlias(list, (self.owner,)),
                    },
                },
            )
        )
        self.owner.register_api(api)
        self.thing.register_api(api)

    def define(self) -> None:
        """Define the name that the related model's key refers to."""
        if "." in self._annotation:
            self._monkeypatch.setattr(_LATER, "Key", int, raising=False)
        else:
            self._monkeypatch.setitem(globals(), self._global, int)

    def empty(self, **extra: Any) -> Any:
        """Return an object that reads a null relation and an empty list.

        Args:
            **extra: Keyword arguments of the constructor.

        Returns:
            The object, with both values already read.
        """
        obj = self.thing(
            json={"id": 1, "title": "a", "owner": None, "owners": []}, **extra
        )
        assert obj.owner is None
        assert obj.owners == []
        return obj

    def check_stale_value_is_gone(self, obj: Any) -> None:
        """Check that the fields raise while undefined and read once defined.

        Args:
            obj: Object whose JSON names related objects.
        """
        for field in ("owner", "owners"):
            with pytest.raises((NameError, AttributeError)):
                getattr(obj, field)
        self.define()
        assert obj.owner == self.owner(id=7)
        assert obj.owners == [self.owner(id=7), self.owner(id=8)]


@pytest.fixture(params=sorted(NAMES))
def scenario(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Scenario:
    """Return the models for each way a key's type can be undefined."""
    return Scenario(request.param, monkeypatch, request.param)


def save_patch(obj: Any) -> None:
    """Change the title and save it."""
    obj.title = "x"
    obj.save()


def save_put(obj: Any) -> None:
    """Save the whole object."""
    obj.save(full=True)


def assign_eagerly(obj: Any) -> None:
    """Change the title, which an eager object sends at once."""
    obj.title = "x"


def load_missing(obj: Any) -> None:
    """Read a field that the JSON lacks, which loads the object."""
    assert obj.title == "x"


WRITES: dict[str, tuple[Callable[[Any], None], str, dict[str, Any]]] = {
    "save": (save_patch, "PATCH", {}),
    "full_save": (save_put, "PUT", {}),
    "eager_assignment": (assign_eagerly, "PATCH", {"write_mode": "eager"}),
}


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_write_response_that_names_related_objects_replaces_empty_values(
    scenario: Scenario, write: str
) -> None:
    """A response that names owners drops the null and empty cached values."""
    act, method, extra = WRITES[write]
    obj = scenario.empty(**extra)
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(method, BASE + "things/1/", json=NAMED)
        act(obj)
    scenario.check_stale_value_is_gone(obj)


def test_a_load_that_names_related_objects_replaces_empty_values(
    scenario: Scenario,
) -> None:
    """A load of missing fields drops the null and empty cached values."""
    obj = scenario.thing(id=1)
    obj.update_from_json({"owner": None, "owners": []})
    assert obj.owner is None
    assert obj.owners == []
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "things/1/", json=NAMED)
        load_missing(obj)
    scenario.check_stale_value_is_gone(obj)


def test_a_merge_that_names_related_objects_replaces_empty_values(
    scenario: Scenario,
) -> None:
    """A merged payload that names owners drops the cached values."""
    obj = scenario.empty()
    obj.update_from_json({"owner": 7, "owners": [7, 8]})
    scenario.check_stale_value_is_gone(obj)


def test_an_appended_item_is_sent_with_the_servers_items(
    scenario: Scenario,
) -> None:
    """The list read after a response holds the server's items to append to."""
    obj = scenario.empty()
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "things/1/", json=NAMED)
        save_patch(obj)
    scenario.define()
    obj.owners.append(scenario.owner(id=9))
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "things/1/", json={})
        obj.save()
        assert mocker.request_history[0].json() == {"owners": [7, 8, 9]}
