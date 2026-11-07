"""Tests of ``TypedDict`` and ``dict[str, T]`` fields, which eazyrest converts.

None of these needs Pydantic. The tests of a ``TypedDict`` under a Pydantic
config, or inside a type that Pydantic converts, are in ``test_pydantic.py``.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping
from typing import Any, Generic, NamedTuple, TypeVar, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typing_extensions import NotRequired, ReadOnly, Required, TypedDict

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)
from eazyrest._models import (
    BaseType,
    CollectionType,
    MappingType,
    OptionalType,
    TypedDictType,
    analyze_type,
)

URL = "https://typed.example.com/jobs/1/"

KeyT = TypeVar("KeyT")


class Window(TypedDict):
    """Time window with a key that may be missing and one that is read-only."""

    start: datetime.datetime
    length: datetime.timedelta
    note: NotRequired[str]
    token: ReadOnly[uuid.UUID]
    day: Required[datetime.date]


class Tree(TypedDict):
    """Tree that refers to itself."""

    name: str
    children: list[Tree]


class Holder(TypedDict):
    """Mapping that holds a model, which eazyrest does not convert inside."""

    owner: Owner


class Unresolved(TypedDict):
    """Mapping whose annotation names nothing."""

    missing: Nowhere  # type: ignore[name-defined]  # noqa: F821


class GenericWindow(TypedDict, Generic[KeyT]):
    """Generic mapping, which eazyrest converts only unparameterized."""

    value: KeyT


class Closed(TypedDict, closed=True):
    """Closed mapping (PEP 728)."""

    name: str


class Extra(TypedDict, extra_items=int):  # type: ignore[call-arg]
    """Mapping with extra items (PEP 728)."""

    name: str


class Point(NamedTuple):
    """Named tuple, whose JSON form is a list."""

    x: int
    y: int


@dataclasses.dataclass
class Pair:
    """Standard library dataclass, whose JSON form is an object."""

    first: int
    second: int


class TypedBase(JSONObject):
    """Base model of these tests."""


TypedBase.register_api(API("https://typed.example.com/"))


@json_object
class Owner(TypedBase):
    """Related model."""

    class_url = "/owners/"

    id: int


@json_object
class Job(TypedBase):
    """Model with ``TypedDict`` and mapping fields."""

    class_url = "/jobs/"

    id: int
    window: Window
    windows: list[Window]
    tree: Tree
    days: dict[str, datetime.date]
    labels: Mapping[str, str] | None


WINDOW_JSON: dict[str, Any] = {
    "start": "2026-11-07T09:00:00Z",
    "length": "PT1H",
    "token": "6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40",
    "day": "2026-11-07",
    "server": {"kept": True},
}

WINDOW: dict[str, Any] = {
    "start": datetime.datetime(2026, 11, 7, 9, tzinfo=datetime.timezone.utc),
    "length": datetime.timedelta(hours=1),
    "token": uuid.UUID("6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40"),
    "day": datetime.date(2026, 11, 7),
    "server": {"kept": True},
}

JOB_JSON: dict[str, Any] = {
    "id": 1,
    "window": WINDOW_JSON,
    "windows": [WINDOW_JSON],
    "tree": {"name": "root", "children": [{"name": "leaf", "children": []}]},
    "days": {"start": "2026-11-07"},
    "labels": None,
}


def job() -> Job:
    """Return a job read from ``JOB_JSON``.

    Returns:
        The job, with a copy of the JSON.
    """
    return Job(json={**JOB_JSON, "window": dict(WINDOW_JSON)})


@pytest.mark.parametrize(
    ("ty", "expected"),
    [
        (Window, TypedDictType(Window)),
        (Window | None, OptionalType(TypedDictType(Window))),
        (list[Window], CollectionType(list, TypedDictType(Window))),
        (dict[str, int], MappingType(BaseType(int))),
        (Mapping[str, Window], MappingType(TypedDictType(Window))),
        (dict[Any, str], MappingType(BaseType(str))),
        (dict[str, Any], MappingType(BaseType(Any))),
        (dict, BaseType(dict)),
        (tuple[int, ...], CollectionType(tuple, BaseType(int))),
    ],
)
def test_typed_dicts_and_mappings_analyze_as_structure(
    ty: Any, expected: Any
) -> None:
    """A ``TypedDict`` and a mapping with string keys convert their values.

    Args:
        ty: Field annotation.
        expected: Its analyzed type.
    """
    assert analyze_type(ty) == expected


def test_typed_dict_keys_are_analyzed_without_qualifiers() -> None:
    """``Required``, ``NotRequired``, and ``ReadOnly`` do not affect keys."""
    assert TypedDictType(Window).keys == {
        "start": BaseType(datetime.datetime),
        "length": BaseType(datetime.timedelta),
        "note": BaseType(str),
        "token": BaseType(uuid.UUID),
        "day": BaseType(datetime.date),
    }


def test_typed_dict_reads_declared_keys_and_keeps_others() -> None:
    """Declared keys convert, and an undeclared key stays as JSON."""
    item = job()

    assert dict(item.window) == WINDOW
    assert [dict(window) for window in item.windows] == [WINDOW]
    assert item.days == {"start": datetime.date(2026, 11, 7)}
    assert item.labels is None
    # A missing key is absent, as a field that the server leaves out is.
    assert "note" not in item.window


def test_typed_dict_reads_a_tree_of_itself() -> None:
    """A ``TypedDict`` that refers to itself converts at every depth."""
    assert job().tree == {
        "name": "root",
        "children": [{"name": "leaf", "children": []}],
    }


def test_typed_dict_writes_declared_keys_and_keeps_others(
    requests_mock: Any,
) -> None:
    """Assigning a ``TypedDict`` value writes each key in its JSON form."""
    requests_mock.patch(URL, json={})
    item = job()

    item.window = cast(
        "Window", {**WINDOW, "length": datetime.timedelta(minutes=90)}
    )
    item.days = {"end": datetime.date(2026, 11, 8)}
    item.save()

    assert requests_mock.last_request.json() == {
        "window": {
            **WINDOW_JSON,
            "start": "2026-11-07T09:00:00+00:00",
            "length": "PT1H30M",
        },
        "days": {"end": "2026-11-08"},
    }


def test_typed_dict_changed_in_place_is_saved(requests_mock: Any) -> None:
    """A change to a converted value in place is detected and written."""
    requests_mock.patch(URL, json={})
    item = job()

    item.window["length"] = datetime.timedelta(hours=2)
    item.tree["children"].append({"name": "new", "children": []})
    item.save()

    sent = requests_mock.last_request.json()
    assert sent["window"]["length"] == "PT2H"
    assert sent["tree"]["children"][-1] == {"name": "new", "children": []}


def test_typed_dict_round_trips_unchanged(requests_mock: Any) -> None:
    """Reading and saving without a change sends nothing.

    The JSON's ``Z`` reads as UTC, which writes as ``+00:00``, so the
    comparison is between encodings, not spellings.
    """
    item = job()

    _ = item.window, item.tree, item.days
    item.save()

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("window", ["a"], "Window values must be mappings"),
        ("days", "2026-11-07", "Mapping values must be mappings"),
        ("days", {"start": "x"}, "ISO 8601"),
    ],
)
def test_typed_dict_reads_reject_other_shapes(
    field: str, value: Any, message: str
) -> None:
    """A JSON value that is not an object, or a bad key value, raises.

    Args:
        field: Name of the field read.
        value: Its JSON value.
        message: Pattern that the error should match.
    """
    item = Job(json={**JOB_JSON, field: value})

    with pytest.raises(FieldConversionError, match=message):
        getattr(item, field)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("window", ["a"], "Window values must be mappings"),
        ("days", {1: datetime.date(2026, 1, 1)}, "keys must be strings"),
        (
            "window",
            {
                "day": datetime.datetime(
                    2026, 1, 1, tzinfo=datetime.timezone.utc
                )
            },
            "must be dates",
        ),
    ],
)
def test_typed_dict_writes_reject_other_shapes(
    requests_mock: Any, field: str, value: Any, message: str
) -> None:
    """A value that cannot be written raises, and sends nothing.

    Args:
        requests_mock: Mocked HTTP transport.
        field: Name of the field assigned.
        value: The value assigned.
        message: Pattern that the error should match.
    """
    item = job()

    with pytest.raises(FieldConversionError, match=message):
        setattr(item, field, value)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("ty", "message"),
    [
        (dict[str, Owner], "holds the model Owner"),
        (dict[str, list[Owner | None]], "holds the model Owner"),
        (dict[str, dict[str, Owner]], "holds the model Owner"),
        (GenericWindow[int], "parameterized generic TypedDict"),
        (Closed, "closed or declares extra items"),
        (Extra, "closed or declares extra items"),
        (tuple[int, str], "fixed-length tuple"),
        (Point, "NamedTuple"),
        (Pair, "dataclass"),
        (dict[int, str], "keys are not strings"),
    ],
)
def test_types_read_as_another_type_are_refused(ty: Any, message: str) -> None:
    """A type whose JSON form would read as another type raises.

    So does a model inside a mapping, which eazyrest does not convert yet.

    Args:
        ty: Field annotation.
        message: Pattern that the error should match.
    """
    with pytest.raises(TypeError, match=message):
        analyze_type(ty)


@json_object
class Lazy(TypedBase):
    """Model whose ``TypedDict`` keys cannot be converted."""

    class_url = "/lazy/"

    id: int
    holder: Holder
    unresolved: Unresolved


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("holder", "holds the model Owner"),
        ("unresolved", "cannot be resolved"),
    ],
)
def test_typed_dict_keys_are_checked_when_first_used(
    field: str, message: str
) -> None:
    """A key's annotation is analyzed lazily, and a bad one raises then.

    Args:
        field: Name of a ``Lazy`` field.
        message: Pattern that the error should match.
    """
    item = Lazy(json={"id": 1, field: {}})

    with pytest.raises(FieldConversionError, match=message):
        getattr(item, field)


def test_json_schema_describes_typed_dicts_and_mappings() -> None:
    """A ``TypedDict`` and a mapping are JSON objects in the schema."""
    properties = Job.json_schema()["properties"]

    assert properties["window"] == {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date-time"},
            "length": {"type": "string", "format": "duration"},
            "note": {"type": "string"},
            "token": {"type": "string", "format": "uuid"},
            "day": {"type": "string", "format": "date"},
        },
        "additionalProperties": True,
    }
    # A TypedDict inside itself allows any object there.
    assert properties["tree"] == {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "children": {"type": "array", "items": {"type": "object"}},
        },
        "additionalProperties": True,
    }
    assert properties["days"] == {
        "type": "object",
        "additionalProperties": {"type": "string", "format": "date"},
    }
    assert properties["labels"] == {
        "anyOf": [
            {"type": "object", "additionalProperties": {"type": "string"}},
            {"type": "null"},
        ]
    }


def test_encode_json_writes_values_as_fields_write_them() -> None:
    """``encode_json()`` encodes by the annotation, as a field would."""
    assert encode_json(WINDOW, Window) == {
        **WINDOW_JSON,
        "start": "2026-11-07T09:00:00+00:00",
    }
    assert encode_json([WINDOW], list[Window])[0]["length"] == "PT1H"
    assert (
        encode_json(datetime.date(2026, 1, 2), datetime.date) == "2026-01-02"
    )
    assert encode_json(None, Window) is None


@pytest.mark.parametrize(
    ("value", "annotation", "message"),
    [
        (
            {"days": [Owner(id=1)]},
            dict[str, list[int]],
            r"at \[key\]\[0\] has the type Owner",
        ),
        ({"a": {1: "x"}}, dict[str, Any], r"at \[key\] has a key"),
        (object(), Any, "^The value has the type object"),
        ({"day": "x"}, Window, None),
    ],
)
def test_encode_json_refuses_values_that_are_not_json(
    value: Any, annotation: Any, message: str | None
) -> None:
    """A value that does not encode to JSON raises, naming where it is.

    A value that the annotation's conversion leaves alone, such as a string
    under a date key, is JSON, so it is written as it is.

    Args:
        value: Value to encode.
        annotation: Its annotation.
        message: Pattern that the error should match, or ``None`` if the
            value encodes.
    """
    if message is None:
        assert encode_json(value, annotation) == value
        return

    with pytest.raises(TypeError, match=message):
        encode_json(value, annotation)


JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.text()

WINDOWS = st.fixed_dictionaries(
    {
        "start": st.datetimes(timezones=st.just(datetime.timezone.utc)),
        "length": st.timedeltas(),
        "token": st.uuids(),
        "day": st.dates(),
    },
    optional={"note": st.text()},
).flatmap(
    lambda window: st.dictionaries(
        st.text().filter(lambda key: key not in Window.__annotations__),
        st.recursive(
            JSON_SCALARS,
            lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner),
            max_leaves=5,
        ),
        max_size=3,
    ).map(lambda extra: {**extra, **window})
)


@given(WINDOWS)
def test_typed_dict_values_read_back_as_written(
    window: dict[str, Any],
) -> None:
    """A ``TypedDict`` value encodes to JSON that reads back as the value.

    Undeclared keys, which hold JSON, are kept as they are.

    Args:
        window: Generated value, with undeclared keys.
    """
    encoded = encode_json(window, Window)
    item = Job(json={**JOB_JSON, "window": encoded})

    assert dict(item.window) == window
    assert encode_json(item.window, Window) == encoded


class Other(TypedDict):
    """Second mapping, for unions."""

    name: str


@pytest.mark.parametrize(
    "ty",
    [Window | Other, list[Window | Other | None], dict[str, Window | int]],
)
def test_unions_that_hold_a_typed_dict_are_refused(ty: Any) -> None:
    """A union that holds a ``TypedDict`` raises rather than pass through.

    Args:
        ty: Field annotation.
    """
    with pytest.raises(TypeError, match="union that holds a TypedDict"):
        analyze_type(ty)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("window", {**WINDOW_JSON, "day": "x"}, r"^Job\.window: \['day'\]: "),
        ("days", {"secret-key": "x"}, r"^Job\.days: \[key\]: "),
    ],
)
def test_typed_dict_errors_name_declared_keys_only(
    field: str, value: Any, message: str
) -> None:
    """An error names a declared key, but not a key that the input chose.

    Args:
        field: Name of the field read.
        value: Its JSON value, with a bad value under one key.
        message: Pattern that the error should match.
    """
    item = Job(json={**JOB_JSON, field: value})

    with pytest.raises(FieldConversionError, match=message) as info:
        getattr(item, field)

    assert "secret-key" not in str(info.value)


def test_typed_dict_writes_refuse_values_that_are_not_json_under_other_keys(
    requests_mock: Any,
) -> None:
    """A non-JSON value under an undeclared key raises, and sends nothing."""
    item = job()

    with pytest.raises(
        FieldConversionError, match=r"\[key\]: The value has the type date"
    ):
        item.window = cast(
            "Window", {**WINDOW, "extra": datetime.date(2026, 1, 1)}
        )

    assert requests_mock.call_count == 0


def test_errors_do_not_show_keys_that_the_input_chose() -> None:
    """A key of a value checked as JSON can be a secret, so it is hidden."""
    job: Any = Job(json=dict(JOB_JSON))

    with pytest.raises(FieldConversionError) as caught:
        job.window = {**WINDOW, "secret-key": {"inner": object()}}
    assert "secret" not in str(caught.value)
    assert "inner" not in str(caught.value)
    assert "[key]" in str(caught.value)
