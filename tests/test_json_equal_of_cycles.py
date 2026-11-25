"""Comparing two JSON values ends when a value holds itself."""

from __future__ import annotations

from typing import Any

import pytest

from eazyrest._models import _json_equal


def _dict(name: int) -> dict[str, Any]:
    """Return a dict that holds itself."""
    value: dict[str, Any] = {"k": name}
    value["self"] = value
    return value


def _list(name: int) -> list[Any]:
    """Return a list that holds itself."""
    value: list[Any] = [name]
    value.append(value)
    return value


@pytest.mark.parametrize("make", [_dict, _list], ids=["dict", "list"])
def test_two_equal_values_that_hold_themselves_are_equal(
    make: Any,
) -> None:
    """The comparison ends and says that they are equal."""
    assert _json_equal(make(1), make(1))


@pytest.mark.parametrize("make", [_dict, _list], ids=["dict", "list"])
def test_two_values_that_hold_themselves_and_differ_are_unequal(
    make: Any,
) -> None:
    """The comparison ends and finds the member that differs."""
    assert not _json_equal(make(1), make(2))


def test_a_value_that_holds_itself_equals_itself() -> None:
    """One value compared with itself ends too."""
    value = _dict(1)
    assert _json_equal(value, value)
