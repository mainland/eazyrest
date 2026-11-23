"""A decimal field reads the same under every decimal context."""

from __future__ import annotations

import decimal
from collections.abc import Iterator
from decimal import Decimal

import pytest

from eazyrest import FieldConversionError, JSONObject, json_object


@json_object
class Price(JSONObject):
    """A model with a decimal field."""

    class_url = "/prices/"
    id: int
    amount: Decimal


@pytest.fixture(params=["default", "extended", "basic"])
def context(request: pytest.FixtureRequest) -> Iterator[decimal.Context]:
    """Run the test under a context, two of which do not trap errors."""
    base = {
        "default": decimal.DefaultContext,
        "extended": decimal.ExtendedContext,
        "basic": decimal.BasicContext,
    }[request.param]
    with decimal.localcontext(base) as active:
        yield active


@pytest.mark.parametrize(
    "text", ["1E+99999999999999999999", "1E-99999999999999999999"]
)
def test_an_exponent_beyond_decimals_limits_is_refused(
    context: decimal.Context, text: str
) -> None:
    """The text is refused, whether or not the context traps the error."""
    price = Price(json={"id": 1, "amount": text})
    with pytest.raises(FieldConversionError) as caught:
        _ = price.amount
    assert text not in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)


def test_a_decimal_is_read_under_every_context(
    context: decimal.Context,
) -> None:
    """A decimal within the limits reads as the decimal it names."""
    assert Price(json={"id": 1, "amount": "1.10"}).amount == Decimal("1.10")
