"""An error of eazyrest that a conversion raises passes through unchanged.

A conversion reports a value that it cannot convert with ``TypeError`` or
``ValueError``, which reaches the caller as a ``FieldConversionError``. A
``ResponseShapeError`` or a ``PaginationError`` is a ``ValueError`` too, but it
says something else than that the value cannot be converted, so it is not
wrapped.
"""

from __future__ import annotations

from typing import Any

import pytest

from eazyrest import (
    Conversion,
    FieldConversionError,
    JSONObject,
    PaginationError,
    ResponseShapeError,
    UsageError,
    json_object,
)


class Price:
    """A value whose conversion raises what a test says."""


RAISES: dict[str, Exception] = {
    "ResponseShapeError": ResponseShapeError("the answer has the wrong shape"),
    "PaginationError": PaginationError("the page is a loop"),
    "UsageError": UsageError("not supported"),
}


def holder(error: Exception) -> Any:
    """Return a model whose conversion of ``Price`` raises ``error``."""

    def decode(value: Any, cls: type[Any]) -> Any:
        raise error

    @json_object
    class Holder(JSONObject):
        """A model with a ``Price`` field."""

        conversions = {  # noqa: RUF012
            Price: Conversion(decode=decode, encode=lambda value, cls: 1)
        }
        class_url = "/holders/"
        id: int
        price: Price

    return Holder


@pytest.mark.parametrize("name", sorted(RAISES))
def test_an_error_of_eazyrest_is_not_wrapped(name: str) -> None:
    """The caller gets the error that the conversion raised."""
    error = RAISES[name]
    obj = holder(error)(json={"id": 1, "price": 5})
    with pytest.raises(type(error)) as caught:
        _ = obj.price
    assert caught.value is error
    assert not isinstance(caught.value, FieldConversionError)


@pytest.mark.parametrize("raised", [ValueError, TypeError])
def test_a_plain_error_is_wrapped_naming_the_field(
    raised: type[Exception],
) -> None:
    """The value error and the type error are the conversion's report."""
    obj = holder(raised("bad price"))(json={"id": 1, "price": 5})
    with pytest.raises(FieldConversionError, match=r"Holder\.price") as caught:
        _ = obj.price
    assert isinstance(caught.value.__cause__, raised)
