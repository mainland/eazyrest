"""A written value is no deeper than 128 levels, and says so when it is.

What a field or a request writes is copied, compared, and sent by steps that
recurse, so a value that is deep or holds itself is refused where it is
written, by one count that every later step can take, instead of raising a bare
``RecursionError`` from one of them. 128 levels is the default
``max_json_depth``, so what is written can be read back.
"""

from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://write-depth.example/"
api = API(BASE)


@json_object
class Holder(JSONObject):
    """A model with a field that keeps its JSON as it is."""

    class_url = "/holders/"
    id: int
    value: Any


Holder.register_api(api)


class Chain(TypedDict, total=False):
    """A recursive ``TypedDict``."""

    next: Chain | None


@dataclasses.dataclass
class Link:
    """A recursive dataclass."""

    next: Link | None = None


class Node(NamedTuple):
    """A recursive ``NamedTuple``."""

    next: Node | None = None


def nested(depth: int) -> Any:
    """Return lists nested to a given depth, a flat list having depth 1."""
    value: Any = []
    for _ in range(depth - 1):
        value = [value]
    return value


def cyclic(kind: type[Any]) -> Any:
    """Return a value of a recursive structure that holds itself."""
    if kind is Chain:
        chain: Any = {}
        chain["next"] = chain
        return chain
    link = Link()
    link.next = link
    return link


def holder() -> Holder:
    """Return a holder that has read its value."""
    return Holder(json={"id": 1, "value": 0})


def test_a_value_of_128_levels_is_written() -> None:
    """The depth of the default ``max_json_depth`` is allowed."""
    value = nested(128)
    assert encode_json(value, Any) == value
    obj = holder()
    obj.value = value
    assert obj.value == value
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "holders/1/", json={})
        obj.save()
    assert mocker.call_count == 1


@pytest.mark.parametrize("depth", [129, 300, 500, 990, 5000])
def test_a_deeper_value_is_refused_by_encode_json(depth: int) -> None:
    """The error is a ``ValueError``, whatever the recursion limit is."""
    with pytest.raises(ValueError, match="nests too deeply") as caught:
        encode_json(nested(depth), Any)
    assert not isinstance(caught.value, RecursionError)


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("depth", [129, 500, 990])
def test_a_deeper_value_is_refused_where_it_is_assigned(
    depth: int, mode: Any
) -> None:
    """The field raises ``FieldConversionError``, and nothing is sent."""
    obj = Holder(json={"id": 1, "value": 0}, write_mode=mode)
    with requests_mock.Mocker() as mocker:
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            obj.value = nested(depth)
        assert mocker.call_count == 0
    assert obj.value == 0
    assert obj._pending_updates == {}


def test_a_value_changed_in_place_to_be_too_deep_is_refused() -> None:
    """Every ``save()`` says so, and sends nothing."""
    obj = Holder(json={"id": 1, "value": []})
    value = obj.value
    for _ in range(500):
        value.append([])
        value = value[0]
    with requests_mock.Mocker() as mocker:
        for _ in range(2):
            with pytest.raises(FieldConversionError, match=r"Holder\.value"):
                obj.save()
        assert mocker.call_count == 0


@pytest.mark.parametrize("depth", [129, 500, 990])
def test_create_refuses_a_deep_value_before_its_request(depth: int) -> None:
    """No resource is made that the caller then cannot reach."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            Holder.create(value=nested(depth))
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            Holder.get_or_create(id=1, defaults={"value": nested(depth)})
        assert mocker.call_count == 0


def test_create_copies_its_payload_before_the_request() -> None:
    """A value that cannot be copied is refused while nothing exists."""

    class Uncopyable(dict[str, Any]):
        """A JSON object that refuses to be copied."""

        def __deepcopy__(self, memo: Any) -> Any:
            raise RuntimeError("cannot copy")

    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "holders/", json={"id": 1, "value": {}})
        with pytest.raises(RuntimeError, match="cannot copy"):
            Holder.create(value=Uncopyable())
        assert mocker.call_count == 0


@pytest.mark.parametrize("kind", [Chain, Link])
def test_a_value_that_holds_itself_is_refused_as_too_deep(kind: Any) -> None:
    """The error does not blame the annotations, from any depth of stack."""
    value = cyclic(kind)

    def deeper(frames: int) -> None:
        if frames:
            deeper(frames - 1)
        else:
            encode_json(value, kind)

    for frames in (0, 60):
        with pytest.raises(ValueError, match="nests too deeply") as caught:
            deeper(frames)
        assert "cannot be resolved" not in str(caught.value)


@pytest.mark.parametrize("kind", [Chain, Link, Node])
@pytest.mark.parametrize("depth", [129, 200, 400])
def test_a_deep_chain_of_each_recursive_structure_is_refused(
    kind: Any, depth: int
) -> None:
    """A chain with no cycle is refused by its depth."""
    value: Any = None
    for _ in range(depth):
        value = {"next": value} if kind is Chain else kind(next=value)
    with pytest.raises(ValueError, match="nests too deeply"):
        encode_json(value, kind | None)


@pytest.mark.parametrize("kind", [Chain, Link])
def test_a_field_refuses_a_value_that_holds_itself(kind: Any) -> None:
    """The field raises ``FieldConversionError`` from any depth of stack."""
    model: Any = json_object(
        type(
            "Cycle",
            (JSONObject,),
            {
                "__annotations__": {"id": int, "value": kind | None},
                "class_url": "/cycles/",
            },
        )
    )
    obj = model(json={"id": 1, "value": None})
    value = cyclic(kind)

    def deeper(frames: int) -> None:
        if frames:
            deeper(frames - 1)
        else:
            obj.value = value

    for frames in (0, 60):
        with pytest.raises(FieldConversionError) as caught:
            deeper(frames)
        assert "cannot be resolved" not in str(caught.value)
        assert "nests too deeply" in str(caught.value)
