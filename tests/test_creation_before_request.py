"""A creation resolves its model's key type before it sends a request."""

from __future__ import annotations

import enum
import sys
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests_mock

SOURCE = """
from eazyrest import API, JSONObject, json_object

class Base(JSONObject):
    pass

Base.register_api(API("https://early.example/"))

@json_object
class Late(Base):
    class_url = "/late/"
    id: "Key"
    name: str
"""


@pytest.fixture
def module() -> Iterator[types.ModuleType]:
    """Return a module whose model names a key class that it lacks."""
    mod = types.ModuleType("early_key_module")
    sys.modules[mod.__name__] = mod
    code = compile(SOURCE, mod.__name__, "exec", dont_inherit=True)
    exec(code, mod.__dict__)  # noqa: S102
    yield mod
    del sys.modules[mod.__name__]


CALLS: dict[str, Callable[[Any], Any]] = {
    "create": lambda model: model.create(name="a"),
    "create_with_key": lambda model: model.create(id=7, name="a"),
    "get_or_create": lambda model: model.get_or_create(name="a"),
    "get_or_create_by_params": lambda model: model.get_or_create(
        params={"name": "a"}
    ),
}


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_a_key_class_that_is_not_defined_raises_before_any_request(
    module: types.ModuleType, call: Callable[[Any], Any]
) -> None:
    """The NameError comes with no request sent, not after the POST."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"id": 1, "name": "a"})
        with pytest.raises(NameError, match="Key"):
            call(module.Late)
        assert mocker.call_count == 0


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_a_key_type_that_is_refused_raises_before_any_request(
    module: types.ModuleType, call: Callable[[Any], Any]
) -> None:
    """The TypeError of a refused key type comes with no request sent."""

    class Key(enum.Enum):
        A = "a"

    module.__dict__["Key"] = Key
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"id": 1, "name": "a"})
        with pytest.raises(TypeError, match="primary key"):
            call(module.Late)
        assert mocker.call_count == 0
