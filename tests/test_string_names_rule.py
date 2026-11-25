"""One rule says whether a type names a type by a string.

A string or a ``ForwardRef`` at any depth of a type names a type, except in a
``Literal``, which holds values, and in the metadata of an ``Annotated``. A
type alias and a ``NewType`` read the types that they name by strings alike.
"""

import datetime
from typing import Annotated, Any, ForwardRef, Literal, NewType

import pytest
from typing_extensions import TypeAliasType

from eazyrest import API, JSONObject, json_object
from eazyrest._aliases import _names_by_string

api = API("https://string-names.example/")

DATE = datetime.date(2024, 1, 2)

# Each type, whether it names a type by a string, the JSON of a value, and what
# reading that JSON gives.
TYPES: list[tuple[str, Any, bool, Any, Any]] = [
    ("plain", datetime.date, False, "2024-01-02", DATE),
    ("string", "datetime.date", True, "2024-01-02", DATE),
    (
        "forward-reference",
        ForwardRef("datetime.date"),
        True,
        "2024-01-02",
        DATE,
    ),
    ("in-list", list["datetime.date"], True, ["2024-01-02"], [DATE]),
    (
        "literal",
        Literal["datetime.date"],
        False,
        "datetime.date",
        "datetime.date",
    ),
    (
        "annotated-note",
        Annotated[datetime.date, "a note"],
        False,
        "2024-01-02",
        DATE,
    ),
    (
        "annotated-string",
        Annotated["datetime.date", "a note"],
        True,
        "2024-01-02",
        DATE,
    ),
    (
        "list-of-annotated-note",
        list[Annotated[datetime.date, "a note"]],
        False,
        ["2024-01-02"],
        [DATE],
    ),
    (
        "list-of-annotated-string",
        list[Annotated["datetime.date", "a note"]],
        True,
        ["2024-01-02"],
        [DATE],
    ),
]


def _id(case: tuple[str, Any, bool, Any, Any]) -> str:
    return case[0]


# A bare string is no value of an alias and no supertype of a NewType.
TYPES_OF_A_FIELD = [
    case for case in TYPES if not isinstance(case[1], str | ForwardRef)
]


@pytest.mark.parametrize("case", TYPES, ids=_id)
def test_a_type_names_a_type_by_a_string_as_the_rule_says(
    case: tuple[str, Any, bool, Any, Any],
) -> None:
    """A string names a type outside a ``Literal`` and a note."""
    _, ty, names, _, _ = case
    assert _names_by_string(ty) is names


def holder_of(ty: Any, new_type: bool) -> type[JSONObject]:
    """Return a model with a field of an alias or a ``NewType`` of ``ty``."""
    Alias = TypeAliasType("Alias", ty)
    Made = NewType("Made", ty)  # type: ignore[valid-newtype]

    if new_type:

        @json_object
        class WithNewType(JSONObject):
            """A model with a field of a ``NewType`` of ``ty``."""

            class_url = "/holders/"
            id: int
            value: Made

        return WithNewType

    @json_object
    class WithAlias(JSONObject):
        """A model with a field of an alias of ``ty``."""

        class_url = "/holders/"
        id: int
        value: Alias

    return WithAlias


@pytest.mark.parametrize("case", TYPES_OF_A_FIELD, ids=_id)
def test_a_type_alias_reads_the_type_that_it_names(
    case: tuple[str, Any, bool, Any, Any],
) -> None:
    """A field of an alias reads as the type, with a string in it or not."""
    _, ty, _, wire, read = case
    holder = holder_of(ty, new_type=False)(json={"id": 1, "value": wire})
    assert holder.value == read  # type: ignore[attr-defined]


@pytest.mark.parametrize("case", TYPES_OF_A_FIELD, ids=_id)
def test_a_newtype_refuses_a_string_and_reads_otherwise(
    case: tuple[str, Any, bool, Any, Any],
) -> None:
    """A ``NewType`` is refused for a type that names a type by a string."""
    _, ty, names, wire, read = case
    if names:
        with pytest.raises(TypeError, match="has a string in its supertype"):
            holder_of(ty, new_type=True)(json={"id": 1, "value": wire}).value  # type: ignore[attr-defined]  # noqa: B018
    else:
        holder = holder_of(ty, new_type=True)(json={"id": 1, "value": wire})
        assert holder.value == read  # type: ignore[attr-defined]
