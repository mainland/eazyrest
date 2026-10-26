"""Tests for Pydantic conversion of field values."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import subprocess
import sys
import uuid
from copy import deepcopy
from typing import Annotated, Any, Literal, cast
from urllib.parse import parse_qs, urlparse

import pydantic
import pytest
from annotated_types import Gt
from pydantic import (
    AfterValidator,
    Base64Bytes,
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
    BaseType,
    CollectionType,
    FieldConversionError,
    JSONObject,
    OptionalType,
    PydanticType,
    json_object,
)
from eazyrest.eazyrest import analyze_type
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
        ("pair", "dataclass never writes"),
        ("allowing_pair", "dataclass never writes"),
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
