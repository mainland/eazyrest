"""A field whose type contains itself reads to the depth that the stack takes.

``API.max_json_depth`` is the only limit on how deeply a read value may nest.
When the stack gives out first, reading a field of a type that contains itself
raises ``FieldConversionError`` naming the field, since the value is what
nests. A field whose type does not contain itself raises the stack's own
``RecursionError``, whatever it holds.
"""

from __future__ import annotations

import contextlib
import dataclasses
import re
import sys
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

BASE = "https://self-holding.example/"
api = API(BASE)
api.max_json_depth = None


class Chain(TypedDict, total=False):
    """A ``TypedDict`` that holds itself by a key."""

    next: Chain | None


@dataclasses.dataclass
class Link:
    """A dataclass that holds itself by a field."""

    next: Link | None = None


class Node(NamedTuple):
    """A ``NamedTuple`` that holds itself by a field."""

    next: Node | None = None


class Tree(TypedDict, total=False):
    """A ``TypedDict`` that holds itself through a list."""

    children: list[Tree]


class Flat(TypedDict, total=False):
    """A ``TypedDict`` that does not hold itself."""

    name: str
    tags: list[str]


@json_object
class Holder(JSONObject):
    """A model with a field of each type that holds itself, and one not."""

    class_url = "/holders/"
    id: int
    chain: Chain
    link: Link
    node: Node
    tree: Tree
    flat: Flat


Holder.register_api(api)


def runaway(value: Any, cls: type[Any]) -> Any:
    """Fail as a conversion that recurses without end does."""
    raise RecursionError("maximum recursion depth exceeded")


@json_object
class Converted(JSONObject):
    """A model whose self-holding type has a conversion that fails."""

    conversions = {  # noqa: RUF012
        Link: Conversion(decode=lambda value, cls: Link(), encode=runaway),
    }
    class_url = "/converted/"
    id: int
    link: Link


def chain_of(depth: int) -> Any:
    """Return a ``Chain``, or a ``Link``, that nests ``depth`` levels."""
    value: Any = {}
    for _ in range(depth - 1):
        value = {"next": value}
    return value


def node_of(depth: int) -> Any:
    """Return a ``Node`` that nests ``depth`` levels."""
    value: Any = [None]
    for _ in range(depth - 1):
        value = [value]
    return value


def tree_of(depth: int) -> Any:
    """Return a ``Tree`` that nests about ``depth`` levels of JSON."""
    value: Any = {}
    for _ in range((depth - 1) // 2):
        value = {"children": [value]}
    return value


MAKERS: dict[str, Callable[[int], Any]] = {
    "chain": chain_of,
    "link": chain_of,
    "node": node_of,
    "tree": tree_of,
}


def failure(field: str, depth: int) -> FieldConversionError | None:
    """Read a field of an object that holds a value of ``depth`` levels.

    Args:
        field: A key of ``MAKERS``.
        depth: The levels of the value.

    Returns:
        The refusal that the read raised, or ``None`` if the field read.
    """
    obj = Holder(json={"id": 1, field: MAKERS[field](depth)})
    try:
        getattr(obj, field)
    except FieldConversionError as err:
        return err
    return None


def first_refused(field: str) -> int:
    """Return the first depth, in steps of 25 from 129, that is refused."""
    depth = 129
    while failure(field, depth) is None:
        depth += 25
        assert depth < 20 * sys.getrecursionlimit(), "the stack never gave out"
    return depth


@pytest.mark.parametrize("field", MAKERS)
def test_a_field_that_holds_itself_reads_129_levels(field: str) -> None:
    """``max_json_depth`` is 128 by default, and a read may exceed it."""
    assert failure(field, 129) is None


@pytest.mark.parametrize("field", MAKERS)
def test_a_field_that_holds_itself_refuses_a_value_too_deep_for_the_stack(
    field: str,
) -> None:
    """The refusal names the field, where the stack gave a bare error."""
    first = first_refused(field)
    # Every depth before the first refusal reads or is refused, so no read runs
    # out of stack in a step that the refusal does not cover.
    for depth in range(first - 25, first):
        failure(field, depth)
    err = failure(field, first)
    assert err is not None
    assert err.model is Holder
    assert err.field == field
    assert f"Holder.{field}" in str(err)
    assert not isinstance(err.__cause__, RecursionError)


@pytest.mark.parametrize("field", MAKERS)
def test_a_deep_value_is_refused_by_a_full_payload_too(field: str) -> None:
    """``save(full=True)`` reads every field, as a field read does."""
    depth = first_refused(field)
    obj = Holder(json={"id": 1, field: MAKERS[field](depth)})
    with requests_mock.Mocker() as mocker:
        mocker.put(BASE + "holders/1/", json={})
        with pytest.raises(FieldConversionError, match=f"Holder.{field}"):
            obj.save(full=True)
        assert mocker.call_count == 0


def test_a_field_that_does_not_hold_itself_raises_the_stack_error() -> None:
    """A stack that gives out is the program's, whatever the field holds."""

    def read() -> Any:
        obj = Holder(json={"id": 1, "flat": {"name": "a", "tags": ["b"]}})
        return obj.flat

    def deep(frames: int) -> Any:
        return deep(frames - 1) if frames else read()

    read()
    # A FieldConversionError would escape this loop and fail the test, where
    # the program's own RecursionError ends the walk down the stack.
    for frames in range(sys.getrecursionlimit()):
        try:
            deep(frames)
        except RecursionError:
            break
    else:
        pytest.fail("The stack never gave out")
    for more in range(frames, frames + 20):
        with contextlib.suppress(RecursionError):
            deep(more)


def test_a_conversion_that_runs_out_of_stack_keeps_its_error() -> None:
    """A conversion's own error is no value too deep, whatever the type."""
    obj = Converted(json={"id": 1, "link": {}})
    with pytest.raises(RecursionError) as caught:
        _ = obj.link
    assert not isinstance(caught.value, FieldConversionError)


@pytest.mark.parametrize("field", MAKERS)
def test_a_nearly_full_stack_gives_no_bare_error_before_the_refusal(
    field: str,
) -> None:
    """Whichever step of a read meets the end of the stack, it is refused.

    The outcomes by depth of stack are a read, then refusals, then the bare
    error where not even the refusal fits.
    """
    obj = Holder(json={"id": 1, field: MAKERS[field](20)})

    def deep(frames: int) -> Any:
        return deep(frames - 1) if frames else getattr(obj, field)

    outcomes = ""
    for frames in range(
        sys.getrecursionlimit() - 150, sys.getrecursionlimit()
    ):
        obj._field_cache.clear()
        try:
            deep(frames)
        except FieldConversionError:
            outcomes += "F"
        except RecursionError:
            outcomes += "R"
        else:
            outcomes += "."
    assert re.fullmatch(r"\.*F*R*", outcomes), outcomes
