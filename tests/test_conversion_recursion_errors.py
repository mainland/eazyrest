"""A ``RecursionError`` that a conversion raises is the conversion's bug.

Only the walk of a value reports one as a value that holds itself or nests too
deeply. A conversion's own code, or a dataclass's ``__post_init__``, that runs
out of stack propagates unchanged, as any other exception that is no
``TypeError`` or ``ValueError`` does.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sys
from typing import Any

import pytest

from eazyrest import (
    Conversion,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)


class Price:
    """A value whose conversion runs out of stack."""


def runaway(value: Any, cls: type[Any]) -> Any:
    """Fail as a conversion that recurses without end does."""
    raise RecursionError("maximum recursion depth exceeded")


@dataclasses.dataclass
class Careful:
    """A dataclass whose ``__post_init__`` runs out of stack."""

    text: str

    def __post_init__(self) -> None:
        """Fail as a ``__post_init__`` that recurses without end does."""
        raise RecursionError("maximum recursion depth exceeded")


@json_object
class Holder(JSONObject):
    """A model with fields whose conversions run out of stack."""

    conversions = {  # noqa: RUF012
        Price: Conversion(decode=runaway, encode=runaway),
    }
    class_url = "/holders/"
    id: int
    price: Price
    careful: Careful
    title: str


def test_a_decode_that_runs_out_of_stack_is_not_a_value_too_deep() -> None:
    """The caller reads the conversion's error, not a conversion error."""
    obj = Holder(json={"id": 1, "price": 5, "careful": {"text": "a"}})
    with pytest.raises(RecursionError) as caught:
        _ = obj.price
    assert not isinstance(caught.value, FieldConversionError)


def test_a_post_init_that_runs_out_of_stack_is_not_a_value_too_deep() -> None:
    """A dataclass that cannot be built raises its own error."""
    obj = Holder(json={"id": 1, "price": 5, "careful": {"text": "a"}})
    with pytest.raises(RecursionError) as caught:
        _ = obj.careful
    assert not isinstance(caught.value, FieldConversionError)


def test_an_encode_that_runs_out_of_stack_is_not_a_value_too_deep() -> None:
    """An assignment and ``encode_json()`` raise the conversion's error."""
    obj = Holder(json={"id": 1, "price": 5, "careful": {"text": "a"}})
    with pytest.raises(RecursionError) as caught:
        obj.price = Price()
    assert not isinstance(caught.value, FieldConversionError)
    assert obj._pending_updates == {}
    with pytest.raises(RecursionError) as caught:
        encode_json(Price(), Price, model=Holder)
    assert not isinstance(caught.value, ValueError)


@pytest.mark.parametrize("read_by", ["a field", "a full payload"])
def test_a_flat_field_read_from_a_full_stack_is_no_value_too_deep(
    read_by: str,
) -> None:
    """A stack that gives out is the program's, whatever the field holds."""

    @json_object
    class Plain(JSONObject):
        """A model with a flat list field."""

        class_url = "/plain/"
        id: int
        tags: list[str]

    def read() -> Any:
        obj = Plain(json={"id": 1, "tags": ["a"]})
        if read_by == "a field":
            return obj.tags
        return obj._full_payload()

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
