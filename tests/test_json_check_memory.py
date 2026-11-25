"""Checking a value for JSON types takes memory in proportion to its depth.

The check names where a bad value is, as ``[key][0][3]``, but only when it
refuses one. A check that passes builds no path, so a value nested a hundred
thousand levels deep, which the write rule then refuses as too deep, does not
exhaust memory on the way.
"""

from __future__ import annotations

import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)
from eazyrest import _models as models

BASE = "https://json-check-memory.example/"
api = API(BASE)


@json_object
class Holder(JSONObject):
    """A model with a field that keeps its JSON as it is."""

    class_url = "/holders/"
    id: int
    value: Any


Holder.register_api(api)

DEPTH = 4000
"""Deep enough that a path per level holds megabytes, and cheap to build."""

BYTES_PER_LEVEL = 1500
"""The most that a check may hold per level.

A check that keeps a path for each level holds about 3 bytes for each
level above it, which is more than 6000 bytes per level at ``DEPTH``. The
check holds about 650 bytes per level.
"""


def nested(depth: int, leaf: Any = None) -> Any:
    """Return lists nested to a given depth around a leaf."""
    value: Any = [] if leaf is None else [leaf]
    for _ in range(depth - 1):
        value = [value]
    return value


def encode(value: Any) -> Any:
    """Encode a value as ``Any``."""
    return encode_json(value, Any)


def assign(value: Any) -> Any:
    """Assign a value to an ``Any`` field."""
    obj = Holder(json={"id": 1, "value": 0})
    obj.value = value
    return obj


def construct(value: Any) -> Any:
    """Give a value as a constructor keyword."""
    return Holder(id=1, value=value)


def extra(value: Any) -> Any:
    """Give a value as an extra of a request body."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "holders/", json={"id": 1})
        return Holder.create(extra=value)


ENTRY_POINTS: dict[str, Callable[[Any], Any]] = {
    "encode_json": encode,
    "assignment": assign,
    "constructor keyword": construct,
    "extra of a request": extra,
}


def peak_of(call: Callable[[Any], Any], value: Any) -> int:
    """Return the most memory that a call holds at once, in bytes."""
    tracemalloc.start()
    try:
        with pytest.raises((ValueError, FieldConversionError)):
            call(value)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize(
    "call", ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
def test_a_deep_value_is_checked_in_memory_linear_in_its_depth(
    call: Callable[[Any], Any],
) -> None:
    """The peak does not grow with the square of the depth."""
    value = nested(DEPTH)
    assert peak_of(call, value) < BYTES_PER_LEVEL * DEPTH


@pytest.mark.parametrize(
    "call", ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
def test_a_deep_value_is_refused_as_too_deep(
    call: Callable[[Any], Any],
) -> None:
    """The refusal is the depth error, not a failure of the check."""
    with pytest.raises((ValueError, FieldConversionError)) as caught:
        call(nested(DEPTH))
    assert "nests too deeply" in str(caught.value)


@pytest.mark.parametrize("depth", [127, 128])
def test_the_depth_bound_is_unchanged_by_the_check(depth: int) -> None:
    """127 levels are accepted and 128 are refused."""
    value = nested(depth)
    if depth == 127:
        assert encode(value) == value
        return
    with pytest.raises(ValueError, match="nests too deeply"):
        encode(value)


def test_a_mistyped_leaf_is_named_by_its_path() -> None:
    """The message gives each index and masks each mapping key."""
    value = [[0, {"secret": [object()]}]]
    with pytest.raises(TypeError) as caught:
        encode(value)
    assert str(caught.value) == (
        "The value at [0][1][key][0] has the type object, which does not "
        "encode to JSON. Annotate it with a type that eazyrest converts."
    )


def test_a_mistyped_leaf_deep_in_a_value_is_named_by_its_whole_path() -> None:
    """The message names every level, though no path was kept for a pass."""
    value = nested(DEPTH, object())
    with pytest.raises(TypeError) as caught:
        encode(value)
    assert str(caught.value) == (
        f"The value at {'[0]' * DEPTH} has the type object, which does not "
        "encode to JSON. Annotate it with a type that eazyrest converts."
    )


def test_a_float_that_is_not_finite_is_named_by_its_path() -> None:
    """The message gives the place of the float."""
    with pytest.raises(ValueError, match="not finite") as caught:
        encode([{"a": [float("nan")]}])
    assert str(caught.value) == (
        "The value at [0][key][0] is a float that is not finite, which "
        "JSON cannot write"
    )


def test_a_scalar_that_is_not_json_is_named_without_a_path() -> None:
    """A value that is no container has no place."""
    with pytest.raises(TypeError) as caught:
        encode(object())
    assert str(caught.value) == (
        "The value has the type object, which does not encode to JSON. "
        "Annotate it with a type that eazyrest converts."
    )


def test_a_key_that_is_not_a_string_is_named_by_its_container() -> None:
    """The message gives the place of the mapping, not of the key."""
    with pytest.raises(TypeError) as caught:
        encode([{"a": {1: 2}}])
    assert str(caught.value) == (
        "The value at [0][key] has a key that is not a string, which JSON "
        "objects cannot have"
    )


def test_a_key_that_is_not_a_string_deep_in_a_value_is_named() -> None:
    """The message names every level to the mapping."""
    with pytest.raises(TypeError) as caught:
        encode(nested(DEPTH, {1: 2}))
    assert str(caught.value) == (
        f"The value at {'[0]' * DEPTH} has a key that is not a string, "
        "which JSON objects cannot have"
    )


def test_a_value_that_holds_itself_is_named_where_it_starts() -> None:
    """The message names the start of the value, not the repeat."""
    value: list[Any] = [0]
    value.append([value])
    with pytest.raises(ValueError, match="holds itself") as caught:
        encode(value)
    assert str(caught.value).startswith("The value holds itself")


def test_a_check_that_passes_writes_no_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a refusal gives a message, so only a refusal builds a path."""
    named: list[str] = []
    real = models._at

    def at(path: str) -> str:
        named.append(path)
        return real(path)

    monkeypatch.setattr(models, "_at", at)
    value: Any = [0, "a", 1.5, None, True]
    for _ in range(200):
        value = [0, {"key": 1}, value]
    models._check_json(value, "[key]")
    assert named == []
