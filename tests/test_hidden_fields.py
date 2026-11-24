"""A class attribute that hides an inherited field is refused by name.

A subclass can define a property, a method, a plain value, or ``None`` under
the name of a field that it inherits. The attribute hides the field's
descriptor, which the library reads, so every use would fail with an
``AttributeError`` that names neither the class nor the field, and
``hasattr()`` would report any field missing. The class raises ``TypeError``
naming both, when it is decorated, and, for a subclass that is not decorated,
when it is first used, before any request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://hidden-fields.example/"
api = API(BASE)


@json_object
class Person(JSONObject):
    """A model with fields that a subclass may hide."""

    class_url = "/people/"
    id: int
    name: str
    email: str


Person.register_api(api)


def a_method(self: Any) -> str:
    """Stand in for a method that a subclass defines."""
    return "method"


HIDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "None": lambda: {"name": None},
    "a property": lambda: {"name": property(lambda self: "p")},
    "a method": lambda: {"name": a_method},
    "a plain value": lambda: {"name": "x"},
    "a ClassVar": lambda: {
        "name": "x",
        "__annotations__": {"name": ClassVar[str]},
    },
}


def subclass(attributes: dict[str, Any]) -> type[Person]:
    """Return an undecorated subclass of ``Person`` with attributes."""
    return type("Staff", (Person,), attributes)


@pytest.mark.parametrize("attributes", HIDERS.values(), ids=HIDERS.keys())
def test_a_decorated_subclass_that_hides_a_field_is_refused(
    attributes: Callable[[], dict[str, Any]],
) -> None:
    """Decorating raises, naming the class and the field."""
    with pytest.raises(TypeError, match=r"Staff\.name .*hides") as caught:
        json_object(subclass(attributes()))
    assert not isinstance(caught.value, AttributeError)


UNDECORATED: dict[str, Callable[[type[Person]], Any]] = {
    "construction": lambda cls: cls(id=1),
    "construction from json": lambda cls: cls(json={"id": 1, "name": "a"}),
    "create": lambda cls: cls.create(email="e"),
    "filter": lambda cls: list(cls.filter()),
    "all": lambda cls: list(cls.all()),
    "get": lambda cls: cls.get(id=1),
    "get_page": lambda cls: cls.get_page(),
    "get_or_create": lambda cls: cls.get_or_create(id=1),
    "json_schema": lambda cls: cls.json_schema(),
}


@pytest.mark.parametrize("attributes", HIDERS.values(), ids=HIDERS.keys())
@pytest.mark.parametrize("use", UNDECORATED.values(), ids=UNDECORATED.keys())
def test_an_undecorated_subclass_that_hides_a_field_is_refused_at_first_use(
    use: Callable[[type[Person]], Any],
    attributes: Callable[[], dict[str, Any]],
) -> None:
    """The first use raises, naming the class and the field."""
    cls = subclass(attributes())
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(TypeError, match=r"Staff\.name .*hides") as caught:
            use(cls)
        assert mocker.call_count == 0
    assert not isinstance(caught.value, AttributeError)


def test_hasattr_does_not_swallow_the_refusal() -> None:
    """Reading another field of such a class raises, not answers False."""
    cls = subclass({"name": None})
    with pytest.raises(TypeError):
        hasattr(cls(id=1), "email")


def test_a_hidden_field_that_the_subclass_declares_again_is_taken() -> None:
    """An annotation replaces the inherited field, so nothing is hidden."""

    @json_object
    class Staff(Person):
        """A subclass that declares the field again."""

        name: int  # type: ignore[assignment]

    assert Staff(json={"id": 1, "name": 3}).name == 3


class Mixin:
    """A class that is no model and defines a name."""

    email = "mixed in"


def test_a_mixin_that_hides_a_field_is_named() -> None:
    """The message names the class that defines the attribute."""
    with pytest.raises(TypeError, match=r"class attribute of Mixin"):
        json_object(type("Staff", (Mixin, Person), {}))


def test_attributes_that_hide_no_field_are_taken() -> None:
    """A subclass may define other attributes, a property among them."""

    @json_object
    class Staff(Person):
        """A subclass with its own members."""

        badge: int

        @property
        def shout(self) -> str:
            """Return the name in capitals."""
            return self.name.upper()

    staff = Staff(json={"id": 1, "name": "a", "email": "e", "badge": 2})
    assert staff.shout == "A"
    assert Person(id=1).pk == 1
