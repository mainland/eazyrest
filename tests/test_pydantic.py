"""Tests for Pydantic conversion of field values."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import subprocess
import sys
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any, ClassVar, Literal, NewType, cast
from urllib.parse import parse_qs, urlparse

import pydantic
import pytest
from annotated_types import Gt
from pydantic import (
    AfterValidator,
    Base64Bytes,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    with_config,
)
from pydantic_core import PydanticSerializationError
from typing_extensions import TypedDict

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)
from eazyrest.eazyrest import (
    BaseType,
    CollectionType,
    OptionalType,
    PydanticType,
    analyze_type,
)
from eazyrest.pydantic import UUID, Date, Datetime, Duration

TOKEN = uuid.UUID("6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40")
UTC = datetime.timezone.utc


class Schedule(TypedDict):
    """Mapping whose values use eazyrest's conversions."""

    starts: Datetime
    day: Date
    length: Duration
    token: UUID


SCHEDULE = TypeAdapter(Schedule)


def test_importing_eazyrest_does_not_import_pydantic() -> None:
    """Only ``eazyrest.pydantic`` needs Pydantic installed."""
    code = "import sys, eazyrest; assert 'pydantic' not in sys.modules"

    subprocess.run([sys.executable, "-c", code], check=True)


def test_types_read_values_as_eazyrest_fields_do() -> None:
    """The types read the JSON forms that eazyrest's fields read."""
    schedule = SCHEDULE.validate_python(
        {
            "starts": "2024-04-01T14:00:00+02:00",
            "day": "2024-04-01",
            "length": "1 02:03:04",
            "token": str(TOKEN),
        }
    )

    assert schedule == {
        "starts": datetime.datetime(
            2024, 4, 1, 12, tzinfo=datetime.timezone.utc
        ),
        "day": datetime.date(2024, 4, 1),
        "length": datetime.timedelta(days=1, hours=2, minutes=3, seconds=4),
        "token": TOKEN,
    }
    # Pydantic's own datetime would use its TzInfo class.
    assert type(schedule["starts"].tzinfo) is datetime.timezone
    assert TypeAdapter(Datetime).validate_python(1711972800) == (
        datetime.datetime(2024, 4, 1, 12, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        # Pydantic's own types accept each of these values.
        (Datetime, 1.7e12),
        (Datetime, True),
        (Date, 0),
        (Date, datetime.datetime(2024, 4, 1)),  # noqa: DTZ001
        (Duration, "P1Y"),
        (Duration, "P1M"),
        (UUID, TOKEN.int),
    ],
)
def test_types_reject_values_that_eazyrest_fields_reject(
    annotation: Any, value: Any
) -> None:
    """A value that an eazyrest field rejects fails validation.

    Args:
        annotation: Type from ``eazyrest.pydantic`` to validate with.
        value: Value that Pydantic's own type accepts but the eazyrest field
            rejects.
    """
    with pytest.raises(pydantic.ValidationError):
        TypeAdapter(annotation).validate_python(value)


def test_types_write_values_as_eazyrest_fields_do() -> None:
    """The types write values in the forms that eazyrest's fields write."""
    schedule: Schedule = {
        "starts": datetime.datetime(2024, 4, 1, 12, tzinfo=UTC),
        "day": datetime.date(2024, 4, 1),
        "length": datetime.timedelta(hours=1, minutes=30),
        "token": TOKEN,
    }

    assert SCHEDULE.dump_python(schedule, mode="json") == {
        "starts": "2024-04-01T12:00:00+00:00",
        "day": "2024-04-01",
        "length": "PT1H30M",
        "token": str(TOKEN),
    }


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (Datetime, "2024-04-01T12:00:00Z"),
        (Date, datetime.datetime(2024, 4, 1, tzinfo=UTC)),
        (Duration, 60),
        (UUID, str(TOKEN)),
    ],
)
def test_types_refuse_to_write_other_values(
    annotation: Any, value: Any
) -> None:
    """Writing a value of another type fails rather than guessing.

    Args:
        annotation: Type from ``eazyrest.pydantic`` to serialize with.
        value: Value that is not of the type.
    """
    with pytest.raises(PydanticSerializationError):
        TypeAdapter(annotation).dump_python(
            value, mode="json", warnings="error"
        )


def test_constraints_apply_after_eazyrest_conversion() -> None:
    """Pydantic constraints check the value that eazyrest's codec reads."""
    adapter = TypeAdapter(Annotated[Duration, Field(ge=datetime.timedelta(0))])

    assert adapter.validate_python("1:30") == datetime.timedelta(seconds=90)
    with pytest.raises(pydantic.ValidationError):
        adapter.validate_python("-PT1M")


def test_types_have_json_schemas() -> None:
    """A model that uses the types still has a JSON schema."""
    properties = SCHEDULE.json_schema()["properties"]

    assert {
        name: (schema["type"], schema["format"])
        for name, schema in properties.items()
    } == {
        "starts": ("string", "date-time"),
        "day": ("string", "date"),
        "length": ("string", "duration"),
        "token": ("string", "uuid"),
    }


class PydanticBase(JSONObject):
    """Base object for models with fields that Pydantic converts."""


PydanticBase.register_api(API("https://pydantic.example.com/"))


@with_config(ConfigDict(extra="allow"))
class Spec(TypedDict):
    """Configuration that keeps the keys it does not declare."""

    name: str
    size: int


class PlainSpec(TypedDict):
    """Configuration that does not mention Pydantic."""

    name: str


class Point(pydantic.BaseModel):
    """Pydantic model."""

    x: int
    y: int


@pydantic.dataclasses.dataclass
class PydanticPair:
    """Pydantic dataclass."""

    first: int
    second: int


@dataclasses.dataclass
class Pair:
    """Standard library dataclass."""

    first: int
    second: int


class Level(enum.IntEnum):
    """Integer enum."""

    LOW = 1
    HIGH = 2


@json_object
class Owner(PydanticBase):
    """Eazyrest model that Pydantic cannot convert."""

    class_url = "/owners/"

    id: int


@json_object
class Asset(PydanticBase):
    """Model whose fields Pydantic converts."""

    class_url = "/assets/"

    id: int
    image: Base64Bytes
    spec: Spec
    specs: list[Spec]
    origin: Point | None
    rating: Annotated[int, Field(gt=0)]
    kind: Annotated[Literal["a", "b"], Field()]
    level: Annotated[Level, Field()]
    starts: Annotated[datetime.datetime, Field(description="Start")]
    length: Annotated[datetime.timedelta, Field(ge=datetime.timedelta(0))]
    windows: Annotated[list[datetime.datetime], Field(max_length=2)]
    owners: dict[str, Owner | Spec]


ASSET_JSON: dict[str, Any] = {
    "id": 1,
    "image": "aGVsbG8=",
    "spec": {"name": "a", "size": 1, "extra": {"tags": ["x"]}},
    "specs": [{"name": "b", "size": 2}],
    "origin": {"x": 1, "y": 2},
    "rating": 5,
    "kind": "a",
    "level": 1,
    "starts": "2024-04-01T14:00:00+02:00",
    "length": "1 02:03:04",
    "windows": [1711972800],
    "owners": {},
}

ASSET_URL = "https://pydantic.example.com/assets/1/"

FIELD = Field(gt=0)
CHOICE: Any = Literal["a", "b"]
NUMBER_OR_TEXT: Any = int | str


@pytest.mark.parametrize(
    ("ty", "expected"),
    [
        (Spec, PydanticType(Spec)),
        (Point | None, OptionalType(PydanticType(Point))),
        (list[Spec], CollectionType(list, PydanticType(Spec))),
        (dict[str, Spec], PydanticType(dict[str, Spec])),
        (Base64Bytes, PydanticType(Base64Bytes)),
        (PydanticPair, PydanticType(PydanticPair)),
        (Annotated[int, FIELD], PydanticType(Annotated[int, FIELD])),
        (PlainSpec, BaseType(PlainSpec)),
        (Pair, BaseType(Pair)),
        (Annotated[int, Gt(0)], BaseType(int)),
        (pydantic.PositiveInt, BaseType(int)),
        (pydantic.conint(gt=0), BaseType(int)),
        (
            Annotated[pydantic.PositiveInt, FIELD],
            PydanticType(Annotated[pydantic.PositiveInt, FIELD]),
        ),
        (pydantic.StrictInt, PydanticType(pydantic.StrictInt)),
        (pydantic.FiniteFloat, PydanticType(pydantic.FiniteFloat)),
        (CHOICE, BaseType(CHOICE)),
        (NUMBER_OR_TEXT, BaseType(NUMBER_OR_TEXT)),
    ],
)
def test_pydantic_converts_types_that_mention_it(
    ty: Any, expected: Any
) -> None:
    """A type converts with Pydantic only when it mentions Pydantic.

    Args:
        ty: Field type to analyze.
        expected: Result that ``analyze_type`` should return for ``ty``.
    """
    assert analyze_type(ty) == expected


def test_pydantic_fields_read_values() -> None:
    """Pydantic reads the field values, with eazyrest's codecs."""
    asset = Asset(json=deepcopy(ASSET_JSON))
    utc = datetime.timezone.utc

    assert asset.image == b"hello"
    assert asset.spec == {"name": "a", "size": 1, "extra": {"tags": ["x"]}}
    assert asset.specs == [{"name": "b", "size": 2}]
    assert asset.origin == Point(x=1, y=2)
    assert asset.rating == 5
    assert asset.kind == "a"
    assert asset.level is Level.LOW
    assert asset.starts == datetime.datetime(2024, 4, 1, 12, tzinfo=utc)
    assert type(asset.starts.tzinfo) is datetime.timezone
    assert asset.length == datetime.timedelta(
        days=1, hours=2, minutes=3, seconds=4
    )
    assert asset.windows == [datetime.datetime(2024, 4, 1, 12, tzinfo=utc)]


def test_pydantic_field_values_share_no_containers_with_json() -> None:
    """Changing a read value in place leaves the object's JSON unchanged."""
    asset = Asset(json=deepcopy(ASSET_JSON))

    spec = cast(dict[str, Any], asset.spec)
    spec["extra"]["tags"].append("y")

    assert asset.json["spec"]["extra"] == {"tags": ["x"]}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec", {"name": 5, "size": 1}),
        ("origin", {"x": "left", "y": 2}),
        ("rating", 0),
        ("kind", "c"),
        # Pydantic itself reads each of these values.
        ("level", "1"),
        ("starts", 1.7e12),
        ("length", "P1Y"),
        ("length", "-PT1M"),
        ("windows", [0, 1, 2]),
    ],
)
def test_pydantic_fields_reject_invalid_values(field: str, value: Any) -> None:
    """A value that Pydantic rejects raises an error that names the field.

    Args:
        field: Name of the ``Asset`` field to read.
        value: JSON value that Pydantic rejects for the field.
    """
    asset = Asset(json={**deepcopy(ASSET_JSON), field: value})

    with pytest.raises(FieldConversionError, match=f"^Asset.{field}: "):
        getattr(asset, field)


def test_pydantic_fields_write_json_values(requests_mock: Any) -> None:
    """Assigned values are written as Pydantic serializes them."""
    patch = requests_mock.patch(ASSET_URL, status_code=204)
    asset = Asset(json=deepcopy(ASSET_JSON))

    asset.spec["size"] = 2
    asset.image = b"bye"
    # Values that are not of the field's type are validated first.
    asset.origin = {"x": 3, "y": 4}  # type: ignore[assignment]
    asset.starts = "2024-04-01T12:00:00Z"  # type: ignore[assignment]
    asset.level = 2  # type: ignore[assignment]
    asset.length = datetime.timedelta(minutes=90)
    asset.save()

    assert patch.last_request.json() == {
        "spec": {"name": "a", "size": 2, "extra": {"tags": ["x"]}},
        "image": "Ynll",
        "origin": {"x": 3, "y": 4},
        "starts": "2024-04-01T12:00:00+00:00",
        "level": 2,
        "length": "PT1H30M",
    }


def test_pydantic_field_changed_in_place_is_saved(requests_mock: Any) -> None:
    """A Pydantic model changed in place is sent by ``save()``."""
    patch = requests_mock.patch(ASSET_URL, status_code=204)
    asset = Asset(json=deepcopy(ASSET_JSON))

    assert asset.origin is not None
    asset.origin.x = 7
    asset.save()

    assert patch.last_request.json() == {"origin": {"x": 7, "y": 2}}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec", {"name": "a"}),
        ("origin", {"x": "left", "y": 2}),
        # The serializer accepts this value, but the field cannot read it.
        ("rating", 0),
        ("kind", "c"),
        ("starts", 1.7e12),
        ("windows", [datetime.datetime(2024, 4, 1)] * 3),  # noqa: DTZ001
    ],
)
def test_pydantic_fields_reject_invalid_writes(
    requests_mock: Any, field: str, value: Any
) -> None:
    """Assigning a value the field cannot read back raises and sends none.

    Args:
        field: Name of the ``Asset`` field to assign.
        value: Value that the field cannot read back.
    """
    asset = Asset(json=deepcopy(ASSET_JSON))

    with pytest.raises(FieldConversionError, match=f"^Asset.{field}: "):
        setattr(asset, field, value)

    assert asset.json == ASSET_JSON
    assert requests_mock.call_count == 0


def test_create_and_filter_encode_pydantic_fields(requests_mock: Any) -> None:
    """``create()`` and queries encode values as assignment does."""
    post = requests_mock.post(
        "https://pydantic.example.com/assets/", json=ASSET_JSON
    )
    get = requests_mock.get("https://pydantic.example.com/assets/", json=[])

    Asset.create(image=b"hello", kind="b", level=Level.LOW)
    list(Asset.filter(kind="a", level=1))

    assert post.last_request.json() == {
        "image": "aGVsbG8=",
        "kind": "b",
        "level": 1,
    }
    assert parse_qs(urlparse(get.last_request.url).query) == {
        "kind": ["a"],
        "level": ["1"],
    }


@pytest.mark.parametrize(
    "query",
    [
        lambda: Asset.filter(origin=Point(x=1, y=2)),
        lambda: Asset.get(specs={"name": "b", "size": 2}),
    ],
)
def test_query_rejects_pydantic_values_encoded_as_mappings(
    requests_mock: Any, query: Any
) -> None:
    """A value that Pydantic encodes as a JSON object is not sent.

    Args:
        query: Callable that runs a query with a Pydantic value that encodes as
            a mapping.
    """
    with pytest.raises(ValueError, match="is a mapping"):
        query()

    assert requests_mock.call_count == 0


def test_field_opts_pydantic_aliases_in_to_conversion() -> None:
    """``Field()`` makes Pydantic check an alias of ``annotated_types`` only.

    ``PositiveInt`` is ``Annotated[int, Gt(0)]``, which does not mention
    Pydantic, so a field of that type reads any integer.
    """

    @json_object
    class Counter(PydanticBase):
        """Model with a plain and a checked positive integer."""

        class_url = "/counters/"

        id: int
        plain: pydantic.PositiveInt
        checked: Annotated[pydantic.PositiveInt, Field()]

    counter = Counter(json={"id": 1, "plain": -1, "checked": -1})

    assert counter.plain == -1
    with pytest.raises(FieldConversionError, match=r"^Counter\.checked: "):
        _ = counter.checked


@pytest.mark.parametrize(
    ("ty", "value"),
    [
        (datetime.datetime, "2024-01-01T00:00:00Z"),
        (datetime.date, "2024-01-01"),
        (datetime.timedelta, "PT1H"),
        (uuid.UUID, "12345678-1234-5678-1234-567812345678"),
        (Level, 1),
    ],
)
def test_strict_metadata_on_eazyrest_types_is_rejected(
    ty: Any, value: Any
) -> None:
    """``strict`` on a type that eazyrest converts names the field.

    Args:
        ty: Type that eazyrest converts in place of Pydantic.
        value: Valid JSON value of ``ty``.
    """
    # This module defers annotations, so a class statement could not name
    # ty, a local.
    strict_model: Any = json_object(
        type(
            "Strict",
            (PydanticBase,),
            {
                "__annotations__": {
                    "id": int,
                    "value": Annotated[ty, Field(strict=True)],
                },
                "class_url": "/strict/",
            },
        )
    )
    strict = strict_model(json={"id": 1, "value": value})

    with pytest.raises(
        FieldConversionError, match=r"^Strict\.value: .*strict"
    ):
        _ = strict.value


def test_pydantic_cannot_convert_eazyrest_models() -> None:
    """A type that Pydantic converts cannot contain an eazyrest model."""
    asset = Asset(json=deepcopy(ASSET_JSON))

    with pytest.raises(FieldConversionError, match="an eazyrest model"):
        _ = asset.owners


def test_pydantic_type_without_pydantic_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field that needs Pydantic says how to install it."""
    monkeypatch.setitem(sys.modules, "eazyrest.pydantic", None)

    @json_object
    class Located(PydanticBase):
        """Model whose field needs Pydantic."""

        id: int
        point: Point

    located = Located(json={"id": 1, "point": {"x": 1, "y": 2}})

    with pytest.raises(FieldConversionError, match=r"eazyrest\[pydantic\]"):
        _ = located.point


@with_config(ConfigDict(str_strip_whitespace=True))
class StrippedSpec(TypedDict):
    """Configuration whose Pydantic config drops undeclared keys."""

    name: str


@with_config(ConfigDict(extra="forbid"))
class StrictSpec(TypedDict):
    """Configuration whose Pydantic config rejects undeclared keys."""

    name: str


@pydantic.dataclasses.dataclass(config=ConfigDict(extra="allow"))
class AllowingPair:
    """Pydantic dataclass that keeps undeclared keys but never writes them."""

    first: int


@pydantic.dataclasses.dataclass(config=ConfigDict(extra="forbid"))
class StrictPair:
    """Pydantic dataclass that rejects undeclared keys."""

    first: int


@json_object
class Settings(PydanticBase):
    """Model with ``TypedDict`` and dataclass fields under several configs."""

    class_url = "/settings/"

    id: int
    stripped: StrippedSpec
    plain: Annotated[dict[str, PlainSpec], Field()]
    strict: StrictSpec
    pair: PydanticPair
    allowing_pair: AllowingPair
    strict_pair: StrictPair


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("stripped", "with_config"),
        ("plain", "with_config"),
        ("pair", "never writes them for a dataclass"),
        ("allowing_pair", "never writes them for a dataclass"),
    ],
)
def test_types_that_drop_keys_are_rejected(
    requests_mock: Any, field: str, message: str
) -> None:
    """A type that would drop undeclared keys cannot be a field.

    Args:
        field: Name of a ``Settings`` field whose type would drop undeclared
            keys.
        message: Pattern that the error message should match.
    """
    settings = Settings(
        json={
            "id": 1,
            "stripped": {"name": "a"},
            "plain": {"k": {"name": "a"}},
            "pair": {"first": 1, "second": 2},
            "allowing_pair": {"first": 1},
        }
    )

    with pytest.raises(FieldConversionError, match=message):
        getattr(settings, field)
    with pytest.raises(FieldConversionError, match=message):
        Settings.create(**{field: cast(Any, {})})

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("strict", {"name": "a"}), ("strict_pair", {"first": 1})],
)
def test_types_may_reject_undeclared_keys(field: str, value: Any) -> None:
    """A type that forbids undeclared keys reads values without them.

    Args:
        field: Name of a ``Settings`` field whose type forbids undeclared keys.
        value: JSON value for the field with only declared keys.
    """
    settings = Settings(json={"id": 1, field: value})
    extended = Settings(json={"id": 1, field: {**value, "x": 1}})

    assert (
        TypeAdapter(Any).dump_python(getattr(settings, field), mode="json")
        == value
    )
    with pytest.raises(FieldConversionError, match=f"^Settings.{field}: "):
        getattr(extended, field)


def _require_aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        raise ValueError("A time zone is required")

    return value


def _utc_text(value: datetime.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@json_object
class Release(PydanticBase):
    """Model whose metadata refines eazyrest's datetime conversion."""

    class_url = "/releases/"

    id: int
    published: Annotated[
        datetime.datetime,
        AfterValidator(_require_aware),
        PlainSerializer(_utc_text, return_type=str),
    ]


def test_metadata_refines_eazyrest_conversion(requests_mock: Any) -> None:
    """Validators see eazyrest's decoded value, and serializers replace it."""
    patch = requests_mock.patch(
        "https://pydantic.example.com/releases/1/", status_code=204
    )
    release = Release(json={"id": 1, "published": 1711972800})
    naive = Release(json={"id": 1, "published": "2024-04-01T12:00:00"})
    hour = datetime.timezone(datetime.timedelta(hours=1))

    assert release.published == datetime.datetime(2024, 4, 1, 12, tzinfo=UTC)
    with pytest.raises(FieldConversionError, match="time zone"):
        _ = naive.published

    release.published = datetime.datetime(2024, 4, 2, 13, tzinfo=hour)
    release.save()

    assert patch.last_request.json() == {"published": "2024-04-02T12:00:00Z"}


def _to_timestamp(value: Any, cls: type[Any]) -> float:
    """Encode a datetime as a Unix timestamp.

    Args:
        value: The datetime.
        cls: The declared type.

    Returns:
        Seconds since the epoch.
    """
    del cls
    return float(value.timestamp())


class TimestampBase(PydanticBase):
    """Base whose datetimes are Unix timestamps, inside Pydantic types too."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        datetime.datetime: Conversion(
            decode=lambda value, cls: datetime.datetime.fromtimestamp(
                value, UTC
            ),
            encode=_to_timestamp,
        )
    }


class Shift(BaseModel):
    """Pydantic model whose start converts as eazyrest's fields do."""

    start: Datetime


@json_object
class Roster(TimestampBase):
    """Model with a Pydantic field that holds an eazyrest datetime."""

    class_url = "/rosters/"

    id: int
    shift: Shift


def test_pydantic_types_convert_by_the_models_table() -> None:
    """``eazyrest.pydantic.Datetime`` follows the model's own conversions.

    Outside a model, it converts by the default table.
    """
    when = datetime.datetime(2026, 11, 7, 9, tzinfo=UTC)
    roster = Roster(json={"id": 1, "shift": {"start": when.timestamp()}})

    assert roster.shift.start == when
    roster.shift = Shift(start=when)
    assert roster.json["shift"] == {"start": when.timestamp()}
    assert TypeAdapter(Shift).dump_python(Shift(start=when), mode="json") == {
        "start": "2026-11-07T09:00:00+00:00"
    }


Opened = NewType("Opened", datetime.datetime)


@json_object
class Opening(PydanticBase):
    """Model with a ``NewType`` of ``datetime``, plain and for Pydantic."""

    class_url = "/openings/"

    id: int
    plain: Opened
    annotated: Annotated[Opened, Field()]
    listed: Annotated[list[Opened], Field(max_length=2)]


def test_new_types_convert_as_their_supertype_inside_pydantic() -> None:
    """A ``NewType`` of ``datetime`` converts there as a plain field does.

    Pydantic alone would write UTC as ``Z`` rather than ``+00:00``.
    """
    stamp = 1_700_000_000
    when = datetime.datetime.fromtimestamp(stamp, UTC)
    opening = Opening(
        json={"id": 1, "plain": stamp, "annotated": stamp, "listed": [stamp]}
    )

    assert opening.plain == when
    assert opening.annotated == when
    assert opening.listed == [when]
    opening.annotated = Opened(when)
    assert opening.json["annotated"] == "2023-11-14T22:13:20+00:00"


@pytest.mark.parametrize(
    "hint",
    [
        datetime.datetime,
        Annotated[datetime.datetime, Field()],
        Annotated[datetime.datetime | str, Field()],
        Annotated[datetime.datetime | int, Field()],
        Annotated[datetime.datetime | Any, Field()],
    ],
    ids=["core", "pydantic", "union", "union-failing", "union-succeeding"],
)
def test_a_conversion_bug_on_the_first_attempt_propagates(hint: Any) -> None:
    """A conversion that fails once, unexpectedly, is not retried.

    An exception that only the first call raises would be lost if a failed
    attempt were retried after validation, or if a union's serializer wrote
    the value by another member.

    Args:
        hint: Type of the field.
    """
    bug = RuntimeError("conversion bug")
    calls: list[datetime.datetime] = []

    def encode(value: datetime.datetime, cls: type[Any]) -> Any:
        calls.append(value)
        if len(calls) == 1:
            raise bug
        return value.isoformat()

    class Custom(PydanticBase):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            datetime.datetime: Conversion(
                decode=lambda value, cls: datetime.datetime.fromisoformat(
                    value
                ),
                encode=encode,
            )
        }

    model: Any = json_object(
        type("Event", (Custom,), {"__annotations__": {"id": int, "at": hint}})
    )
    event: Any = model(json={"id": 1, "at": "2020-01-01T00:00:00+00:00"})

    with pytest.raises(RuntimeError) as raised:
        event.at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

    assert raised.value is bug
    assert len(calls) == 1


def test_a_serializer_given_unvalidated_input_is_retried() -> None:
    """A serializer that fails on a value not yet valid sees it validated.

    Only a table conversion's exceptions are bugs whatever the value. Any
    other serializer may expect the validated value, so the value is
    validated and serialized again.
    """
    hint = Annotated[int, PlainSerializer(lambda value: value.real), Field()]
    model: Any = json_object(
        type(
            "Counted",
            (PydanticBase,),
            {"__annotations__": {"id": int, "n": hint}},
        )
    )
    counted: Any = model(json={"id": 1, "n": 1})

    counted.n = "5"

    assert counted.json["n"] == 5


def _raise_key_error(value: Any, cls: type[Any]) -> Any:
    """Fail as a buggy conversion might.

    Args:
        value: Ignored.
        cls: Ignored.

    Raises:
        KeyError: Always.
    """
    raise KeyError("bug")


def _raise_value_error(value: Any, cls: type[Any]) -> Any:
    """Refuse a value, as a conversion reports one that it cannot convert.

    Args:
        value: Ignored.
        cls: Ignored.

    Raises:
        ValueError: Always.
    """
    raise ValueError("Bad stamp")


@pytest.mark.parametrize(
    ("encode", "error"),
    [(_raise_key_error, KeyError), (_raise_value_error, FieldConversionError)],
)
def test_conversion_errors_propagate_through_pydantic(
    encode: Any, error: type[Exception]
) -> None:
    """A conversion's unexpected exception propagates through Pydantic too.

    A ``TypeError`` or ``ValueError`` reports a value that the conversion
    cannot convert, and becomes a ``FieldConversionError``. Any other
    exception is a bug in the conversion, and propagates unchanged, as for
    a field without Pydantic.

    Args:
        encode: The conversion's ``encode``.
        error: Exception that a write should raise.
    """
    stamp = Conversion(
        decode=lambda value, cls: datetime.datetime.fromtimestamp(
            value, datetime.timezone.utc
        ),
        encode=encode,
    )

    class StampBase(PydanticBase):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            datetime.datetime: stamp
        }

    @json_object
    class Event(StampBase):
        class_url = "/events/"

        id: int
        at: datetime.datetime
        pinned: Annotated[datetime.datetime, Field()]

    event = Event(json={"id": 1, "at": 0, "pinned": 0})
    when = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    for field in ("at", "pinned"):
        with pytest.raises(error):
            setattr(event, field, when)


_Millis = NewType("_Millis", datetime.datetime)
_Plain = NewType("_Plain", datetime.datetime)


def test_a_new_types_entry_applies_under_pydantic() -> None:
    """A ``NewType``'s own entry converts it under Pydantic, as in a field.

    A ``NewType`` without one converts as its supertype, by the table's
    entry for that.
    """

    class MillisBase(PydanticBase):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            _Millis: Conversion(
                decode=lambda value, cls: datetime.datetime.fromtimestamp(
                    value / 1000, datetime.timezone.utc
                ),
                encode=lambda value, cls: value.timestamp() * 1000,
            )
        }

    @json_object
    class Mark(MillisBase):
        class_url = "/marks/"

        id: int
        plain: _Millis
        annotated: Annotated[_Millis, Field()]
        listed: Annotated[list[_Millis], Field()]
        other: Annotated[_Plain, Field()]

    when = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    mark = Mark(
        json={
            "id": 1,
            "plain": 0,
            "annotated": 1577836800000.0,
            "listed": [],
            "other": "2020-01-01T00:00:00Z",
        }
    )

    assert mark.annotated == when
    assert mark.other == when
    mark.plain = _Millis(when)
    mark.annotated = _Millis(when)
    mark.listed = [_Millis(when)]
    mark.other = _Plain(when)
    assert mark.json["plain"] == 1577836800000.0
    assert mark.json["annotated"] == 1577836800000.0
    assert mark.json["listed"] == [1577836800000.0]
    assert mark.json["other"] == "2020-01-01T00:00:00+00:00"


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass
class _Base:
    """Standard library dataclass that rejects undeclared keys."""

    a: int


@dataclasses.dataclass
class _Loose:
    """Standard library dataclass without a config, which would drop keys."""

    a: int


def test_dataclasses_must_reject_keys_that_pydantic_would_drop() -> None:
    """A dataclass that a Pydantic field names must set ``extra="forbid"``.

    Pydantic never writes undeclared keys for a dataclass of the standard
    library either, so one that would drop a server's key on a read is
    refused, and one that rejects such keys reads only what it declares.
    """

    @json_object
    class Holder(PydanticBase):
        class_url = "/holders/"

        id: int
        loose: Annotated[_Loose, Field()]
        strict: Annotated[_Base, Field()]

    holder = Holder(
        json={"id": 1, "loose": {"a": 1}, "strict": {"a": 1, "b": 2}}
    )
    with pytest.raises(FieldConversionError, match="for a dataclass"):
        _ = holder.loose
    with pytest.raises(FieldConversionError, match=r"^Holder\.strict: "):
        _ = holder.strict
    assert Holder(json={"id": 1, "strict": {"a": 3}}).strict == _Base(3)
