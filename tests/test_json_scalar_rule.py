"""One rule says which scalar JSON can carry, at every entry point.

A scalar that JSON carries is a string, a finite number that Python can write
as text, a boolean, or ``None``. The declaration of a ``Literal`` or an enum
and the write of a value agree on which values those are.
"""

import enum
import math
from typing import Any, Literal

import pytest

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://json-scalars.example/")

BIG = 10**5000


class Flavor(str, enum.Enum):  # noqa: UP042
    """An enum that mixes in ``str``."""

    SWEET = "sweet"


CARRIED: list[Any] = [
    "a",
    "",
    0,
    -1,
    1.5,
    0.0,
    True,
    False,
    None,
    10**20,
    Flavor.SWEET,
]
NOT_CARRIED: list[Any] = [
    b"x",
    object(),
    math.nan,
    math.inf,
    -math.inf,
    BIG,
    -BIG,
]


def _id(value: Any) -> str:
    try:
        return repr(value)[:30]
    except ValueError:
        return "integer of too many digits"


def declared_literal(value: Any) -> type[Any]:
    """Return a model whose field is a ``Literal`` of ``value``."""

    @json_object
    class Holder(JSONObject):
        """A model with a ``Literal`` field."""

        class_url = "/holders/"
        id: int
        member: Literal[value]  # type: ignore[valid-type]

    Holder.json_schema()
    return Holder


def declared_enum(value: Any) -> type[Any]:
    """Return a model whose field is an enum with ``value`` as a member."""
    Choice = enum.Enum("Choice", {"ONE": value})

    @json_object
    class Holder(JSONObject):
        """A model with an enum field."""

        class_url = "/holders/"
        id: int
        member: Choice

    Holder.json_schema()
    return Holder


def written(value: Any) -> Any:
    """Assign ``value`` to a field that keeps its JSON as it is."""

    @json_object
    class Holder(JSONObject):
        """A model with a field of any JSON."""

        class_url = "/holders/"
        id: int
        value: Any

    Holder.register_api(api)
    holder = Holder(json={"id": 1, "value": 0}, write_mode="lazy")
    holder.value = value
    return holder.value


DECLARATIONS = [declared_literal, declared_enum]
ENTRY_POINTS = [*DECLARATIONS, written]


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    "value", [value for value in CARRIED if value is not None], ids=_id
)
def test_a_scalar_that_json_carries_is_accepted(
    entry: Any, value: Any
) -> None:
    """Every entry point accepts a string, number, or boolean."""
    entry(value)


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("value", NOT_CARRIED, ids=_id)
def test_a_value_that_json_cannot_carry_is_refused(
    entry: Any, value: Any
) -> None:
    """Every entry point refuses a value that is no scalar JSON carries."""
    with pytest.raises((TypeError, ValueError, FieldConversionError)):
        entry(value)


@pytest.mark.parametrize("entry", DECLARATIONS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("value", [[1], {"a": 1}], ids=_id)
def test_a_container_is_no_scalar_in_a_declaration(
    entry: Any, value: Any
) -> None:
    """A list or object is JSON but is no scalar that a type may hold."""
    with pytest.raises(TypeError):
        entry(value)


@pytest.mark.parametrize(
    "entry", [declared_enum, written], ids=lambda f: f.__name__
)
def test_a_tuple_is_no_scalar(entry: Any) -> None:
    """A tuple is no value that JSON carries, as a member or as a write."""
    with pytest.raises((TypeError, FieldConversionError)):
        entry((1, 2))


@pytest.mark.parametrize("entry", DECLARATIONS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    ("value", "words"),
    [
        (math.nan, "float that is not finite"),
        (math.inf, "float that is not finite"),
        (BIG, "integer of more than"),
        (b"x", "value of type bytes"),
    ],
    ids=["nan", "inf", "big-integer", "bytes"],
)
def test_a_declaration_says_why_a_value_is_refused(
    entry: Any, value: Any, words: str
) -> None:
    """The message names what is wrong with the value."""
    with pytest.raises(TypeError, match=words):
        entry(value)
