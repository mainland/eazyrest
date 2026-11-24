"""A ``class_url`` that is no string is refused as a missing one is."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object

api = API("https://class-url.example/")


@json_object
class Base(JSONObject):
    """A base class that leaves ``class_url`` to its subclasses."""

    class_url: ClassVar[Any] = None
    id: int
    name: str


@json_object
class Child(Base):
    """A subclass that sets it."""

    class_url = "/children/"


Base.register_api(api)

CALLS: dict[str, Callable[[], Any]] = {
    "url": lambda: Base(id=1).url,
    "absolute_url": lambda: Base(id=1).absolute_url,
    "read": lambda: Base(id=1).name,
    "refresh": lambda: Base(id=1).refresh(),
    "delete": lambda: Base(id=1).delete(),
    "create": lambda: Base.create(name="x"),
    "filter": lambda: Base.filter(),
    "get": lambda: Base.get(id=1),
    "get_page": lambda: Base.get_page(),
    "get_or_create": lambda: Base.get_or_create(id=1),
}


@pytest.mark.parametrize("call", sorted(CALLS))
def test_a_class_url_of_none_is_a_usage_error_before_any_request(
    call: str,
) -> None:
    """The error names the class and ``class_url``, and nothing is sent."""
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        with pytest.raises(UsageError, match=r"Base\.class_url is a NoneType"):
            CALLS[call]()
        assert mocker.call_count == 0


def test_hasattr_does_not_swallow_the_error() -> None:
    """An unloaded object's field read raises, as a missing one does."""
    with pytest.raises(UsageError):
        hasattr(Base(id=1), "name")


def test_a_subclass_that_sets_a_string_has_its_url() -> None:
    """The placeholder in the base does not reach the subclass."""
    assert Child(id=1).url == "/children/1/"
