"""A value that holds itself, or nests too deeply, is refused as JSON."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import FieldConversionError, JSONObject, encode_json, json_object


@json_object
class Holder(JSONObject):
    """A model with fields that keep their JSON as it is."""

    class_url = "/holders/"
    id: int
    anything: Any
    mapping: dict[str, Any]


def cyclic_list() -> Any:
    """Return a list that holds itself."""
    value: list[Any] = []
    value.append(value)
    return value


def cyclic_dict() -> Any:
    """Return a dict that holds itself."""
    value: dict[str, Any] = {}
    value["self"] = value
    return value


def nested_list() -> Any:
    """Return a list nested past the recursion limit."""
    value: list[Any] = []
    for _ in range(sys.getrecursionlimit() * 2):
        value = [value]
    return value


VALUES: dict[str, Callable[[], Any]] = {
    "cyclic_list": cyclic_list,
    "cyclic_dict": cyclic_dict,
    "nested_list": nested_list,
}


@pytest.mark.parametrize("make", VALUES.values(), ids=VALUES.keys())
def test_assigning_a_value_that_is_not_finite_json_is_refused(
    make: Callable[[], Any],
) -> None:
    """The field raises ``FieldConversionError`` and queues nothing."""
    holder = Holder(json={"id": 1, "anything": 1, "mapping": {}})
    with pytest.raises(
        FieldConversionError, match=r"Holder\.anything"
    ) as caught:
        holder.anything = make()
    assert "nests too deeply" in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)
    assert holder._pending_updates == {}
    assert holder.anything == 1


@pytest.mark.parametrize("make", VALUES.values(), ids=VALUES.keys())
def test_a_value_inside_a_mapping_field_is_refused(
    make: Callable[[], Any],
) -> None:
    """The same holds for a value inside a mapping."""
    holder = Holder(json={"id": 1, "anything": 1, "mapping": {}})
    with pytest.raises(FieldConversionError, match=r"Holder\.mapping"):
        holder.mapping = {"key": make()}
    assert holder._pending_updates == {}


@pytest.mark.parametrize("make", VALUES.values(), ids=VALUES.keys())
def test_encode_json_raises_value_error_for_such_a_value(
    make: Callable[[], Any],
) -> None:
    """``encode_json()`` raises ``ValueError``, as ``json.dumps()`` does."""
    with pytest.raises(ValueError, match="nests too deeply") as caught:
        encode_json(make(), Any)
    assert not isinstance(caught.value, RecursionError)
