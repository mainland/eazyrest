"""Tests for Pydantic conversion of field values."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import importlib
import subprocess
import sys
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    NamedTuple,
    NewType,
    TypeVar,
    cast,
)
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
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticSerializationError
from typing_extensions import TypeAliasType, TypedDict

import eazyrest.pydantic
from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)
from eazyrest._models import (
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
POINTS = TypeAliasType("POINTS", list[Point])
POSITIVE = TypeAliasType("POSITIVE", Annotated[int, FIELD])
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
        (POINTS, CollectionType(list, PydanticType(Point))),
        (POSITIVE, PydanticType(Annotated[int, FIELD])),
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


class Display(pydantic.BaseModel):
    """Pydantic model whose fields have aliases."""

    model_config = ConfigDict(alias_generator=to_camel)

    dark_mode: bool
    font_size: int = Field(alias="size")


@json_object
class Profile(PydanticBase):
    """Model with a field whose model has aliases."""

    class_url = "/profiles/"

    id: int
    display: Display


def test_pydantic_fields_write_by_alias(requests_mock: Any) -> None:
    """A model with aliases is read and written by its aliases."""
    patch = requests_mock.patch(
        "https://pydantic.example.com/profiles/1/", status_code=204
    )
    post = requests_mock.post(
        "https://pydantic.example.com/profiles/",
        json={"id": 2, "display": {"darkMode": False, "size": 9}},
    )
    profile = Profile(
        json={"id": 1, "display": {"darkMode": True, "size": 12}}
    )

    profile.display.font_size = 14
    profile.save()
    Profile.create(
        display=Display.model_validate({"darkMode": False, "size": 9})
    )

    assert patch.last_request.json() == {
        "display": {"darkMode": True, "size": 14}
    }
    assert post.last_request.json() == {
        "display": {"darkMode": False, "size": 9}
    }


class Login(pydantic.BaseModel):
    """Pydantic model with a secret."""

    password: pydantic.SecretStr


@json_object
class Account(PydanticBase):
    """Model with secret fields, whose writes would send their masks."""

    class_url = "/accounts/"

    id: int
    password: pydantic.SecretStr
    key: pydantic.SecretBytes
    login: Login
    token: Annotated[
        pydantic.SecretStr,
        PlainSerializer(
            lambda secret: secret.get_secret_value(),
            return_type=str,
            when_used="json",
        ),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", pydantic.SecretStr("hunter2")),
        ("password", "hunter2"),
        ("key", pydantic.SecretBytes(b"hunter2")),
        ("login", Login(password=pydantic.SecretStr("hunter2"))),
    ],
)
def test_pydantic_fields_refuse_to_write_secrets(
    requests_mock: Any, field: str, value: Any
) -> None:
    """A secret, which Pydantic writes as a mask, is not sent.

    Args:
        field: Name of the ``Account`` field to write.
        value: Value whose secret would be written as a mask.
    """
    account = Account(json={"id": 1, field: None})

    with pytest.raises(FieldConversionError, match=f"^Account.{field}: "):
        setattr(account, field, value)
    with pytest.raises(FieldConversionError, match=f"^Account.{field}: "):
        Account.create(fields={field: value})

    assert requests_mock.call_count == 0


def test_pydantic_field_writes_a_revealed_secret(requests_mock: Any) -> None:
    """A secret whose serializer reveals it is written."""
    post = requests_mock.post(
        "https://pydantic.example.com/accounts/", json={"id": 2}
    )

    Account.create(token=pydantic.SecretStr("hunter2"))

    assert post.last_request.json() == {"token": "hunter2"}


@json_object
class Vault(PydanticBase):
    """Model with a readable secret field."""

    id: int
    secret: pydantic.SecretStr


def test_pydantic_field_refuses_to_read_a_secret() -> None:
    """A secret field cannot be read, since its changes could not be saved."""
    vault = Vault(json={"id": 1, "secret": "hunter2"})

    with pytest.raises(FieldConversionError, match=r"^Vault.secret: "):
        _ = vault.secret


class Cached(pydantic.BaseModel):
    """Pydantic model with a private attribute, which is not written."""

    model_config = ConfigDict(extra="allow")

    x: int
    _hits: int = pydantic.PrivateAttr(default=0)


@json_object
class Counter(PydanticBase):
    """Model with a field whose model has a private attribute."""

    class_url = "/counters/"

    id: int
    cached: Cached


def test_pydantic_field_writes_a_model_with_private_state(
    requests_mock: Any,
) -> None:
    """A model's private attributes do not stop it from being written."""
    patch = requests_mock.patch(
        "https://pydantic.example.com/counters/1/", status_code=204
    )
    counter = Counter(json={"id": 1, "cached": {"x": 1}})

    counter.cached._hits = 3
    counter.cached.x = 2
    counter.save()

    assert patch.last_request.json() == {"cached": {"x": 2}}


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


def test_pydantic_errors_do_not_quote_the_value() -> None:
    """A Pydantic error says where and why, without the input.

    Pydantic's own message, which is the cause, quotes the input.
    """
    asset = Asset(json={**deepcopy(ASSET_JSON), "rating": -9876})

    with pytest.raises(FieldConversionError) as read:
        _ = asset.rating
    with pytest.raises(FieldConversionError) as write:
        asset.rating = -5432

    assert str(read.value) == "Asset.rating: Input should be greater than 0"
    assert str(write.value) == "Asset.rating: Input should be greater than 0"
    cause = read.value.__cause__
    assert cause is not None
    assert "-9876" in str(cause.__cause__)


class SecretCat(BaseModel):
    """Union member selected by the tag ``cat``."""

    kind: Literal["cat"]
    lives: int


class SecretDog(BaseModel):
    """Union member selected by the tag ``dog``."""

    kind: Literal["dog"]


class Tokened(BaseModel):
    """Model whose UUID Pydantic, not eazyrest, converts."""

    token: uuid.UUID


class StrictOwner(BaseModel):
    """Model that rejects keys it does not declare."""

    model_config = ConfigDict(extra="forbid")

    name: str


@json_object
class Secrets(PydanticBase):
    """Model whose invalid values carry input in Pydantic's messages."""

    class_url = "/secrets/"

    id: int
    pet: Annotated[SecretCat | SecretDog, Field(discriminator="kind")]
    counts: Annotated[dict[str, int], Field()]
    keys: Annotated[dict[int, int], Field()]
    owner: StrictOwner
    pets: Annotated[list[SecretCat | SecretDog], Field()]
    tokened: Tokened


SECRET = "sk_live_DUMMY"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "pet",
            {"kind": SECRET},
            (
                "Secrets.pet: Input tag '[redacted]' found using 'kind' does "
                "not match any of the expected tags: 'cat', 'dog'"
            ),
        ),
        (
            "pet",
            {"kind": "cat", "lives": SECRET},
            (
                "Secrets.pet: cat.lives: Input should be a valid integer, "
                "unable to parse string as an integer"
            ),
        ),
        (
            "counts",
            {SECRET: "x"},
            (
                "Secrets.counts: [key]: Input should be a valid integer, "
                "unable to parse string as an integer"
            ),
        ),
        (
            "keys",
            {SECRET: 1},
            (
                "Secrets.keys: [key].[key]: Input should be a valid integer, "
                "unable to parse string as an integer"
            ),
        ),
        (
            "owner",
            {"name": "a", SECRET: 1},
            "Secrets.owner: [key]: Extra inputs are not permitted",
        ),
        (
            "pets",
            [{"kind": "dog"}, {"kind": "cat", "lives": SECRET}],
            (
                "Secrets.pets: 1.SecretCat.lives: Input should be a valid "
                "integer, unable to parse string as an integer; "
                "1.SecretDog.kind: Input should be 'dog'"
            ),
        ),
    ],
)
def test_pydantic_errors_do_not_quote_input_keys_or_tags(
    field: str, value: Any, message: str
) -> None:
    """Mapping keys and tags from the input are left out of messages.

    Pydantic copies a discriminated union's tag into its message, and a
    mapping key, or a key that ``extra="forbid"`` rejects, into the error's
    location. Declared field names and tags are kept.

    Args:
        field: Name of the ``Secrets`` field to read.
        value: Invalid JSON value for the field that holds ``SECRET``.
        message: Expected error message.
    """
    secrets = Secrets(json={"id": 1, field: value})

    with pytest.raises(FieldConversionError) as info:
        getattr(secrets, field)

    assert str(info.value) == message
    assert SECRET not in str(info.value)


def test_pydantic_uuid_error_does_not_quote_input() -> None:
    """A UUID parsing error, which quotes the input, hides it."""
    secrets = Secrets(json={"id": 1, "tokened": {"token": SECRET}})

    with pytest.raises(FieldConversionError) as info:
        _ = secrets.tokened

    assert str(info.value) == (
        "Secrets.tokened: token: Input should be a valid UUID, [redacted]"
    )


@json_object
class Document(PydanticBase):
    """Model whose fields Pydantic writes differently from their values."""

    class_url = "/documents/"

    id: int
    body: pydantic.Json[dict[str, int]]
    joined: Annotated[
        list[int],
        PlainSerializer(lambda v: ",".join(map(str, v)), return_type=str),
    ]


def test_json_field_reads_and_saves_changes(requests_mock: Any) -> None:
    """A ``Json`` field reads its JSON text and writes changes back as text."""
    patch = requests_mock.patch(
        "https://pydantic.example.com/documents/1/",
        json={"id": 1, "body": '{"a":1,"b":2}'},
    )
    document = Document(json={"id": 1, "body": '{"a": 1}'})

    assert document.body == {"a": 1}
    document.body["b"] = 2
    document.save()

    assert patch.last_request.json() == {"body": '{"a":1,"b":2}'}
    assert document.body == {"a": 1, "b": 2}


def test_field_that_cannot_be_written_fails_on_every_read() -> None:
    """A value whose encoding cannot be read back is never cached.

    The serializer writes a string that the field cannot read as a list, so
    ``save()`` could not detect a change made in place.
    """
    document = Document(json={"id": 1, "joined": [1, 2]})

    for _ in range(2):
        with pytest.raises(FieldConversionError, match=r"^Document\.joined: "):
            _ = document.joined

    assert "joined" not in document._field_cache


def test_pydantic_cannot_convert_eazyrest_models() -> None:
    """A type that Pydantic converts cannot contain an eazyrest model."""
    asset = Asset(json=deepcopy(ASSET_JSON))

    with pytest.raises(FieldConversionError, match="an eazyrest model"):
        _ = asset.owners


def test_pydantic_module_without_pydantic_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eazyrest.pydantic`` without Pydantic says what to install."""
    monkeypatch.setitem(sys.modules, "pydantic", None)
    monkeypatch.delitem(sys.modules, "eazyrest.pydantic")

    with pytest.raises(ImportError, match=r"eazyrest\[pydantic\] extra"):
        importlib.import_module("eazyrest.pydantic")


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


GenericT = TypeVar("GenericT")


@pydantic.dataclasses.dataclass(config=ConfigDict(extra="allow"))
class AllowingBox(Generic[GenericT]):
    """Generic Pydantic dataclass that keeps keys but never writes them."""

    first: GenericT


@pydantic.dataclasses.dataclass
class IgnoringBox(Generic[GenericT]):
    """Generic Pydantic dataclass that drops undeclared keys."""

    first: GenericT


@pydantic.dataclasses.dataclass(config=ConfigDict(extra="forbid"))
class StrictBox(Generic[GenericT]):
    """Generic Pydantic dataclass that rejects undeclared keys."""

    first: GenericT


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
    allowing_box: AllowingBox[int]
    ignoring_boxes: dict[str, IgnoringBox[datetime.date]]
    strict_box: StrictBox[int]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("stripped", "with_config"),
        ("plain", "with_config"),
        ("pair", "never writes them for a dataclass"),
        ("allowing_pair", "never writes them for a dataclass"),
        ("allowing_box", "never writes them for a dataclass"),
        ("ignoring_boxes", "never writes them for a dataclass"),
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
            "allowing_box": {"first": 1, "keep": "server"},
            "ignoring_boxes": {"k": {"first": "2024-01-01", "keep": "server"}},
        }
    )

    with pytest.raises(FieldConversionError, match=message):
        getattr(settings, field)
    with pytest.raises(FieldConversionError, match=message):
        Settings.create(**{field: cast(Any, {})})

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strict", {"name": "a"}),
        ("strict_pair", {"first": 1}),
        ("strict_box", {"first": 1}),
    ],
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


def test_json_text_keys_are_redacted() -> None:
    """Decoded JSON locations must not reveal input-controlled keys."""
    document = Document(json={"id": 1, "body": '{"DUMMY_SECRET":"bad"}'})

    with pytest.raises(FieldConversionError) as info:
        _ = document.body

    assert "DUMMY_SECRET" not in str(info.value)
    assert "[key]" in str(info.value)


def test_integer_mapping_keys_are_redacted() -> None:
    """A numeric mapping key is input data, rather than a list index."""

    @json_object
    class Keyed(PydanticBase):
        id: int
        values: Annotated[dict[int, int], Field()]

    keyed = Keyed(json={"id": 1, "values": {91736123: "bad"}})

    with pytest.raises(FieldConversionError) as info:
        _ = keyed.values

    assert "91736123" not in str(info.value)
    assert "[key]" in str(info.value)


@with_config(ConfigDict(extra="ignore"))
class IgnoringInner(TypedDict):
    """``TypedDict`` that drops the keys it does not declare."""

    v: int


@with_config(ConfigDict(extra="allow"))
class KeepingOuter(TypedDict):
    """``TypedDict`` that keeps keys, around one that drops them."""

    inner: list[IgnoringInner]


@json_object
class Nesting(PydanticBase):
    """Model with a Pydantic-converted ``TypedDict`` that nests one."""

    class_url = "/nestings/"

    id: int
    outer: Annotated[KeepingOuter, Field()]


def test_nested_types_that_drop_keys_are_rejected(requests_mock: Any) -> None:
    """A nested ``TypedDict`` that drops keys is refused, at any depth.

    Writing back a value that dropped them would delete them on the server.
    """
    nesting = Nesting(json={"id": 1, "outer": {"inner": [{"v": 1, "x": 2}]}})

    with pytest.raises(FieldConversionError, match="IgnoringInner would drop"):
        _ = nesting.outer

    assert requests_mock.call_count == 0


@with_config(ConfigDict(extra="allow"))
class KeepingInner(TypedDict):
    """``TypedDict`` that keeps the keys it does not declare."""

    v: int


IgnoringAlias = TypeAliasType("IgnoringAlias", IgnoringInner)
KeepingAlias = TypeAliasType("KeepingAlias", KeepingInner)
# Mypy refuses a NewType of a TypedDict, which Python allows.
IgnoringNew = NewType("IgnoringNew", IgnoringInner)  # type: ignore[valid-newtype]
KeepingNew = NewType("KeepingNew", KeepingInner)  # type: ignore[valid-newtype]
WrappedT = TypeVar("WrappedT")
BoundT = TypeVar("BoundT", bound=IgnoringInner)


class IgnoringPair(NamedTuple):
    """``NamedTuple`` with a field that drops keys."""

    inner: IgnoringInner


class KeepingPair(NamedTuple):
    """``NamedTuple`` with a field that keeps keys."""

    inner: KeepingInner


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass
class WrappingBox(Generic[WrappedT]):
    """Generic dataclass whose field has its type argument's type."""

    inner: WrappedT


@with_config(ConfigDict(extra="allow"))
class BoundBox(TypedDict, Generic[BoundT]):
    """Generic ``TypedDict`` whose type parameter's bound drops keys."""

    inner: BoundT


Nested = TypeAliasType("Nested", "list[Nested] | KeepingInner")


# Mypy does not know extra_items, from PEP 728.
@with_config(ConfigDict(extra="allow"))
class IgnoringExtras(TypedDict, extra_items=IgnoringInner):  # type: ignore[call-arg]
    """``TypedDict`` whose extra items drop keys."""

    name: str


@with_config(ConfigDict(extra="allow"))
class IgnoringNamedExtras(TypedDict, extra_items="IgnoringInner"):  # type: ignore[call-arg]
    """``TypedDict`` whose extra items, named by a string, drop keys."""

    name: str


@with_config(ConfigDict(extra="allow"))
class KeepingExtras(TypedDict, extra_items=KeepingInner):  # type: ignore[call-arg]
    """``TypedDict`` whose extra items keep keys."""

    name: str


def _resolved_by_pydantic() -> type[Any]:
    """Return a Pydantic dataclass that only Pydantic can resolve.

    Its field's annotation names a local alias of ``IgnoringInner``, which
    ``get_type_hints()`` cannot resolve, but which Pydantic resolved when
    the class was rebuilt.

    Returns:
        The dataclass.
    """
    local = IgnoringInner

    @pydantic.dataclasses.dataclass(config=ConfigDict(extra="forbid"))
    class Resolved:
        inner: local  # type: ignore[valid-type]

    pydantic.dataclasses.rebuild_dataclass(
        cast(Any, Resolved), force=True, _types_namespace={"local": local}
    )
    return Resolved


def _wrapped_in(hint: Any) -> type[Any]:
    """Return a model whose field nests ``hint`` inside a ``TypedDict``.

    Args:
        hint: Type of the ``TypedDict``'s key.

    Returns:
        Model with a Pydantic-converted field ``value``.
    """
    outer = with_config(ConfigDict(extra="forbid"))(
        TypedDict("Outer", {"inner": hint})  # type: ignore[operator]
    )
    model = type(
        "Holder",
        (PydanticBase,),
        {
            "class_url": "/holders/",
            "__annotations__": {"id": int, "value": Annotated[outer, Field()]},
        },
    )
    return json_object(model)


@pytest.mark.parametrize(
    ("hint", "raw"),
    [
        (IgnoringAlias, {"v": 1, "x": 2}),
        (IgnoringNew, {"v": 1, "x": 2}),
        (IgnoringPair, [{"v": 1, "x": 2}]),
        (WrappingBox[IgnoringInner], {"inner": {"v": 1, "x": 2}}),
        (WrappingBox[list[IgnoringAlias]], {"inner": [{"v": 1, "x": 2}]}),
        (BoundBox, {"inner": {"v": 1, "x": 2}}),
        (IgnoringExtras, {"name": "a", "e": {"v": 1, "x": 2}}),
        (IgnoringNamedExtras, {"name": "a", "e": {"v": 1, "x": 2}}),
        (_resolved_by_pydantic(), {"inner": {"v": 1, "x": 2}}),
    ],
    ids=[
        "alias",
        "newtype",
        "namedtuple",
        "generic",
        "combined",
        "bound",
        "extra-items",
        "extra-items-string",
        "resolved-by-pydantic",
    ],
)
def test_wrapped_types_that_drop_keys_are_rejected(
    hint: Any, raw: Any, requests_mock: Any
) -> None:
    """A type that drops keys is refused behind any wrapper.

    A named alias, a ``NewType``, a ``NamedTuple``'s field, a generic class's
    type argument, a type variable's bound, a ``TypedDict``'s extra items,
    or a field that only Pydantic resolves must not hide it, or a read would
    drop a server key and the next write would delete it.

    Args:
        hint: Type, wrapping one that drops keys, of a nested key.
        raw: JSON of a value of ``hint`` with an undeclared key.
        requests_mock: Fixture that records requests.
    """
    holder = _wrapped_in(hint)(json={"id": 1, "value": {"inner": raw}})

    with pytest.raises(FieldConversionError, match="IgnoringInner would drop"):
        _ = holder.value

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("hint", "raw", "inner", "sent"),
    [
        (
            KeepingAlias,
            {"v": 1, "x": 2},
            lambda value: value,
            {"v": 2, "x": 2},
        ),
        (
            KeepingNew,
            {"v": 1, "x": 2},
            lambda value: value,
            {"v": 2, "x": 2},
        ),
        (
            KeepingPair,
            [{"v": 1, "x": 2}],
            lambda value: value[0],
            [{"v": 2, "x": 2}],
        ),
        (
            WrappingBox[KeepingInner],
            {"inner": {"v": 1, "x": 2}},
            lambda value: value.inner,
            {"inner": {"v": 2, "x": 2}},
        ),
        (
            Nested,
            [[{"v": 1, "x": 2}]],
            lambda value: value[0][0],
            [[{"v": 2, "x": 2}]],
        ),
        (
            KeepingExtras,
            {"name": "a", "e": {"v": 1, "x": 2}},
            lambda value: value["e"],
            {"name": "a", "e": {"v": 2, "x": 2}},
        ),
    ],
    ids=["alias", "newtype", "namedtuple", "generic", "recursive", "extras"],
)
def test_wrapped_types_that_keep_keys_write_them_back(
    hint: Any, raw: Any, inner: Any, sent: Any, requests_mock: Any
) -> None:
    """A wrapped type that keeps keys reads, and writes them back.

    Args:
        hint: Type, wrapping one that keeps keys, of a nested key.
        raw: JSON of a value of ``hint`` with an undeclared key, ``x``.
        inner: Function that returns the ``TypedDict`` inside a value.
        sent: JSON that a write sends after ``v`` changes to 2.
        requests_mock: Fixture that records requests.
    """
    holder = _wrapped_in(hint)(json={"id": 1, "value": {"inner": raw}})
    patch = requests_mock.patch(
        "https://pydantic.example.com/holders/1/", json={}
    )

    inner(holder.value["inner"])["v"] = 2
    holder.save()

    assert patch.last_request.json() == {"value": {"inner": sent}}


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass
class InitOnly:
    """Dataclass with an init-only field, which it reads but never writes."""

    v: int
    seed: dataclasses.InitVar[int] = 0


def test_init_only_fields_are_rejected(requests_mock: Any) -> None:
    """A dataclass with an init-only field is refused.

    Pydantic reads the field's key but never writes it, so a write would
    delete it on the server.
    """
    holder = _wrapped_in(InitOnly)(
        json={"id": 1, "value": {"inner": {"v": 1, "seed": 2}}}
    )

    with pytest.raises(FieldConversionError, match="init-only field seed"):
        _ = holder.value

    assert requests_mock.call_count == 0


def test_unresolvable_fields_are_rejected(requests_mock: Any) -> None:
    """A class whose annotations cannot be resolved is refused.

    Its fields cannot be checked for types that drop keys.
    """

    @with_config(ConfigDict(extra="forbid"))
    @dataclasses.dataclass
    class Unresolved:
        inner: Missing  # type: ignore[name-defined]  # noqa: F821

    holder = _wrapped_in(Unresolved)(
        json={"id": 1, "value": {"inner": {"inner": {}}}}
    )

    with pytest.raises(
        FieldConversionError, match="annotations cannot be resolved"
    ):
        _ = holder.value

    assert requests_mock.call_count == 0


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass
class WithClassVariable:
    """Dataclass whose class variable's type would drop keys as a field."""

    v: int
    defaults: ClassVar[IgnoringInner] = {"v": 1}


def test_class_variables_are_not_checked(requests_mock: Any) -> None:
    """A dataclass's class variable is not checked for keys it would drop.

    Pydantic never reads or writes it.
    """
    holder = _wrapped_in(WithClassVariable)(
        json={"id": 1, "value": {"inner": {"v": 1}}}
    )
    patch = requests_mock.patch(
        "https://pydantic.example.com/holders/1/", json={}
    )

    holder.value["inner"] = WithClassVariable(2)
    holder.save()

    assert patch.last_request.json() == {"value": {"inner": {"v": 2}}}


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


def _refuse_naive(value: datetime.datetime, cls: type[Any]) -> Any:
    """Refuse a naive datetime, as a conversion refuses a value.

    Args:
        value: Datetime to encode.
        cls: Ignored.

    Returns:
        The ISO 8601 text of an aware datetime.

    Raises:
        ValueError: For a naive datetime.
    """
    if value.tzinfo is None:
        raise ValueError("Naive datetimes are not allowed")
    return value.isoformat()


@pytest.mark.parametrize(
    "hint",
    [
        Annotated[datetime.datetime, Field()],
        Annotated[datetime.datetime | Literal["never"], Field()],
        Annotated[datetime.datetime | int, Field()],
        Annotated[datetime.datetime | Any, Field()],
        Annotated[list[datetime.datetime | Literal["never"]], Field()],
    ],
    ids=["plain", "literal", "scalar", "any", "list"],
)
def test_a_conversions_refusal_is_not_overruled_by_a_union(
    hint: Any, recwarn: pytest.WarningsRecorder
) -> None:
    """A conversion's refusal reaches the caller, also inside a union.

    Pydantic's union serializer would otherwise write the value by another
    member, or by its inferred type, with a warning that quotes it.

    Args:
        hint: Type of the field.
        recwarn: Fixture that records warnings.
    """

    class Strict(PydanticBase):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            datetime.datetime: Conversion(
                decode=lambda value, cls: datetime.datetime.fromisoformat(
                    value
                ),
                encode=_refuse_naive,
            )
        }

    model: Any = json_object(
        type("Token", (Strict,), {"__annotations__": {"id": int, "at": hint}})
    )
    token: Any = model(json={"id": 1, "at": None})
    naive = datetime.datetime(2030, 1, 1, 12, 0)  # noqa: DTZ001

    with pytest.raises(FieldConversionError, match="Naive datetimes"):
        token.at = [naive] if "list" in str(hint) else naive

    assert not recwarn.list


def test_a_conversion_bug_keeps_its_cause() -> None:
    """A conversion's bug propagates through Pydantic with its own cause."""

    def encode(value: datetime.datetime, cls: type[Any]) -> Any:
        try:
            return {"present": 1}["missing"]
        except KeyError as err:
            raise RuntimeError("bug in the conversion") from err

    class Buggy(PydanticBase):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            datetime.datetime: Conversion(
                decode=lambda value, cls: value, encode=encode
            )
        }

    for hint in (
        Annotated[datetime.datetime, Field()],
        Annotated[datetime.datetime | int, Field()],
    ):
        model: Any = json_object(
            type("E", (Buggy,), {"__annotations__": {"id": int, "at": hint}})
        )
        event: Any = model(json={"id": 1, "at": None})
        with pytest.raises(RuntimeError) as raised:
            event.at = datetime.datetime(2024, 1, 1, tzinfo=UTC)
        assert isinstance(raised.value.__cause__, KeyError)


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


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass(frozen=True)
class SetItem:
    """Hashable dataclass, which a set may hold."""

    x: Annotated[int, Field(gt=0)]


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass(frozen=True)
class SecretItem:
    """Hashable dataclass with a secret, which Pydantic writes as a mask."""

    token: pydantic.SecretStr


@json_object
class Sets(PydanticBase):
    """Model with Pydantic sets of dataclasses."""

    class_url = "/sets/"

    id: int
    items: Annotated[set[SetItem], Field()]
    frozen: Annotated[frozenset[SetItem], Field()]


def test_sets_of_dataclasses_read_and_write(requests_mock: Any) -> None:
    """A set of hashable dataclasses reads, and writes as an array."""
    patch = requests_mock.patch(
        "https://pydantic.example.com/sets/1/", json={}
    )
    sets = Sets(json={"id": 1, "items": [{"x": 1}], "frozen": [{"x": 2}]})

    assert sets.items == {SetItem(1)}
    assert sets.frozen == frozenset({SetItem(2)})
    sets.items = {SetItem(1), SetItem(3)}
    sets.frozen = frozenset({SetItem(4)})
    sets.save()

    body = patch.last_request.json()
    assert sorted(item["x"] for item in body["items"]) == [1, 3]
    assert body["frozen"] == [{"x": 4}]


def test_sets_of_dataclasses_compare_in_linear_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set read back is compared to the set written item by item, once.

    A set left by deletions iterates in another order than the set that its
    JSON reads as, which matching items pairwise would compare in quadratic
    time.

    Args:
        monkeypatch: Fixture that counts the comparisons.
    """
    calls = 0
    same_values = eazyrest.pydantic._same_values

    def counted(written: Any, read_back: Any) -> bool:
        nonlocal calls
        calls += 1
        return same_values(written, read_back)

    monkeypatch.setattr(eazyrest.pydantic, "_same_values", counted)
    value = {SetItem(n) for n in range(1, 2001)}
    for n in range(501, 2001):
        value.remove(SetItem(n))
    sets = Sets(json={"id": 1, "items": [], "frozen": []})

    sets.items = value

    assert sorted(item["x"] for item in sets.json["items"]) == list(
        range(1, 501)
    )
    assert calls < len(value)


@pytest.mark.parametrize(
    ("hint", "value"),
    [
        (set[SetItem], {SetItem(0)}),
        (set[SecretItem], {SecretItem(pydantic.SecretStr("hunter2"))}),
    ],
    ids=["constraint", "secret"],
)
def test_sets_of_dataclasses_must_read_back(hint: Any, value: Any) -> None:
    """A set item that breaks a constraint, or holds a secret, is refused.

    A standard library dataclass is not validated when it is made, and
    Pydantic writes a secret as a mask, so neither reads back as written.

    Args:
        hint: Type of the set.
        value: Set that would not read back as written.
    """
    model: Any = json_object(
        type(
            "Holder",
            (PydanticBase,),
            {
                "__annotations__": {
                    "id": int,
                    "value": Annotated[hint, Field()],
                }
            },
        )
    )
    holder: Any = model(json={"id": 1, "value": []})

    with pytest.raises(
        FieldConversionError, match=r"read back as written|greater than 0"
    ):
        holder.value = value


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
