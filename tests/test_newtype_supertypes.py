"""A ``NewType`` converts as its supertype, whatever wraps the supertype."""

from __future__ import annotations

import datetime
import enum
import sys
from collections.abc import Callable
from typing import Annotated, Any, Literal, NewType, Optional

import pytest
import requests_mock
from typing_extensions import TypeAliasType

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://newtype.example/")

DateAlias = TypeAliasType("DateAlias", datetime.date)
AnnotatedAlias = TypeAliasType(
    "AnnotatedAlias", Annotated[datetime.date, "doc"]
)

SUPERTYPES: dict[str, Any] = {
    "alias": DateAlias,
    "annotated": Annotated[datetime.date, "doc"],
    "annotated_alias": Annotated[DateAlias, "doc"],
    "alias_of_annotated": AnnotatedAlias,
}
if sys.version_info >= (3, 12):
    _namespace: dict[str, Any] = {"datetime": datetime}
    exec("type StatementDate = datetime.date", _namespace)  # noqa: S102
    SUPERTYPES["type_statement"] = _namespace["StatementDate"]

WRAPPERS: dict[str, Callable[[Any], Any]] = {
    "plain": lambda ty: ty,
    "optional": lambda ty: Optional[ty],  # noqa: UP045
    "list": lambda ty: list[ty],  # type: ignore[valid-type]
    "annotated": lambda ty: Annotated[ty, "doc"],
}


def declare(annotation: Any) -> Any:
    """Return a model with a key and a field of the given annotation."""
    model: Any = type(
        "Booking",
        (JSONObject,),
        {
            "class_url": "/bookings/",
            "__annotations__": {"id": int, "day": annotation},
        },
    )
    model = json_object(model)
    model.register_api(api)
    return model


def composition(supertype: str, wrapper: str) -> Any:
    """Return the annotation of one composition, around a ``NewType``."""
    return WRAPPERS[wrapper](NewType("Day", SUPERTYPES[supertype]))


def wrapped(wrapper: str, value: Any) -> Any:
    """Return the JSON or the value that a wrapper holds for a date."""
    return [value] if wrapper == "list" else value


COMPOSITIONS = [
    pytest.param(supertype, wrapper, id=f"{supertype}-{wrapper}")
    for supertype in SUPERTYPES
    for wrapper in WRAPPERS
]


@pytest.mark.parametrize(("supertype", "wrapper"), COMPOSITIONS)
def test_a_newtype_over_a_wrapped_date_reads_a_date(
    supertype: str, wrapper: str
) -> None:
    """The field reads the date, and refuses a string that is none."""
    model = declare(composition(supertype, wrapper))
    good = model(json={"id": 1, "day": wrapped(wrapper, "2024-01-02")})
    assert good.day == wrapped(wrapper, datetime.date(2024, 1, 2))
    bad = model(json={"id": 1, "day": wrapped(wrapper, "not a date")})
    with pytest.raises(FieldConversionError):
        bad.day  # noqa: B018


@pytest.mark.parametrize(("supertype", "wrapper"), COMPOSITIONS)
def test_a_newtype_over_a_wrapped_date_writes_a_date(
    supertype: str, wrapper: str
) -> None:
    """A date is accepted and queued as its text."""
    model = declare(composition(supertype, wrapper))
    obj = model(json={"id": 1, "day": wrapped(wrapper, "2024-01-02")})
    obj.day = wrapped(wrapper, datetime.date(2024, 5, 6))
    assert obj._pending_updates == {"day": wrapped(wrapper, "2024-05-06")}


@pytest.mark.parametrize(
    ("supertype", "wrapper"),
    [p for p in COMPOSITIONS if "list" not in p.id],  # type: ignore[operator]
)
def test_a_newtype_over_a_wrapped_date_is_sent_by_filter(
    supertype: str, wrapper: str
) -> None:
    """A date in a lookup is sent as its text."""
    model = declare(composition(supertype, wrapper))
    with requests_mock.Mocker() as mocker:
        mocker.get("https://newtype.example/bookings/", json=[])
        list(model.filter(day=datetime.date(2024, 5, 6)))
        assert mocker.last_request.qs == {"day": ["2024-05-06"]}


@pytest.mark.parametrize(("supertype", "wrapper"), COMPOSITIONS)
@pytest.mark.parametrize("mode", ["read", "write"])
def test_a_newtype_over_a_wrapped_date_has_the_schema_of_a_date(
    supertype: str, wrapper: str, mode: Any
) -> None:
    """The field's schema is that of a date, in the wrapper that it has."""
    model = declare(composition(supertype, wrapper))
    schema = model.json_schema(mode)["properties"]["day"]
    date = {"type": "string", "format": "date"}
    expected = {
        "plain": date,
        "annotated": date,
        "optional": {"anyOf": [date, {"type": "null"}]},
        "list": {"type": "array", "items": date},
    }[wrapper]
    assert schema == expected


class Color(enum.Enum):
    """A member of a ``Literal`` and an enum of the fields below."""

    RED = "red"
    BLUE = "blue"


Warm = NewType("Warm", Literal[Color.RED])  # type: ignore[valid-newtype]
Word = NewType("Word", Literal["a", "b"])  # type: ignore[valid-newtype]


def test_a_newtype_of_a_literal_reads_and_describes_its_values() -> None:
    """A ``NewType`` of a ``Literal`` is the ``Literal`` for the field."""
    model = declare(Warm)
    assert model(json={"id": 1, "day": "red"}).day is Color.RED
    for mode in ("read", "write"):
        schema = model.json_schema(mode)
        assert schema["properties"]["day"] == {"enum": ["red"]}
    assert declare(Word).json_schema()["properties"]["day"] == {
        "enum": ["a", "b"]
    }
    obj = model(json={"id": 1, "day": "red"})
    obj.day = Color.RED
    assert obj._pending_updates == {}
