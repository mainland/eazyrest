"""Tests of the checks that a primary key and a related key go through."""

from __future__ import annotations

import dataclasses
import decimal
import enum
import pathlib
import sys
import uuid
from typing import Annotated, Any, Literal, NewType

import pytest

from eazyrest import (
    API,
    Client,
    Conversion,
    FieldConversionError,
    JSONObject,
    ResponseShapeError,
    json_object,
)


class KeyBase(JSONObject):
    """Models served by the mocked API."""


KeyBase.register_api(API("https://keys.example/"))


@json_object
class Account(KeyBase):
    """Model keyed by ``id``."""

    class_url = "/accounts/"

    id: int
    legacy_id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Account]:
        """Return an object of the other key for each key."""
        return {
            pk: LegacyAccount(json={"id": pk, "legacy_id": 900 + pk}, api=api)
            for pk in pks
        }


@json_object(pk="legacy_id")
class LegacyAccount(Account):
    """Subclass that chose another key."""

    class_url = "/legacy-accounts/"

    legacy_id: int


@json_object
class Order(KeyBase):
    """Model that relates to an account."""

    class_url = "/orders/"

    id: int
    account: Account


def _legacy() -> LegacyAccount:
    return LegacyAccount(json={"id": 5, "legacy_id": 900})


def test_relation_assignment_refuses_an_object_of_another_key() -> None:
    """An object whose class is keyed by another field is not written."""
    order = Order(json={"id": 1, "account": 5})

    with pytest.raises(FieldConversionError, match=r"Order\.account"):
        order.account = _legacy()


def test_create_refuses_a_related_object_of_another_key(
    requests_mock: Any,
) -> None:
    """``create()`` refuses it before the request."""
    posted = requests_mock.post("https://keys.example/orders/", json={})

    with pytest.raises(FieldConversionError, match=r"Order\.account"):
        Order.create(id=1, account=_legacy())

    assert not posted.called


def test_query_refuses_a_related_object_of_another_key(
    requests_mock: Any,
) -> None:
    """A query by a related object of another key sends nothing."""
    listed = requests_mock.get("https://keys.example/orders/", json=[])

    with pytest.raises(FieldConversionError, match=r"Order\.account"):
        list(Order.filter(account=_legacy()))

    assert not listed.called


def test_prefetch_refuses_an_object_of_another_key(requests_mock: Any) -> None:
    """A bulk hook's object of another key is not matched to a parent."""
    requests_mock.get(
        "https://keys.example/orders/", json=[{"id": 1, "account": 5}]
    )

    with pytest.raises(TypeError, match=r"Account\.bulk_get_by_pks"):
        list(Order.filter(prefetch="account"))


def test_relation_accepts_a_subclass_that_keeps_the_key() -> None:
    """A subclass with the inherited key is written by that key."""

    @json_object
    class Plain(Account):
        pass

    order = Order(json={"id": 1, "account": 5})
    order.account = Plain(json={"id": 7, "legacy_id": 1})

    assert order.pk == 1


def _declare_key(key_type: Any, **extra: Any) -> Any:
    """Decorate a model whose key has a type, and return the model."""
    namespace = {
        "__annotations__": {"id": key_type},
        "class_url": "/things/",
        **extra,
    }
    return json_object(type("Thing", (KeyBase,), namespace))


class Color(enum.Enum):
    """Enum that keys a model."""

    RED = "red"


Serial = NewType("Serial", int)


class Slug(str):
    """Subclass of ``str``."""


class Code(int):
    """Subclass of ``int``."""


@pytest.mark.parametrize(
    "key_type",
    [
        pytest.param(complex, id="complex"),
        pytest.param(pathlib.Path, id="path"),
        pytest.param(bytes, id="bytes"),
        pytest.param(bytes | None, id="optional-bytes"),
        pytest.param(object, id="object"),
        pytest.param(Any, id="any"),
    ],
)
def test_a_key_class_that_nothing_converts_is_refused(key_type: Any) -> None:
    """A class that is neither listed nor converted cannot be a key."""
    with pytest.raises(
        TypeError,
        match=r"Thing\.id is the primary key, whose type must be the one",
    ):
        _declare_key(key_type)


@pytest.mark.parametrize("key_type", [None, type(None)])
def test_a_key_annotated_as_none_is_refused(key_type: Any) -> None:
    """A key whose annotation is None names no type to convert."""
    with pytest.raises(TypeError, match=r"Thing\.id: its annotation is None"):
        _declare_key(key_type)


@pytest.mark.parametrize(
    "key_type",
    [
        int,
        str,
        uuid.UUID,
        Serial,
        Annotated[int | None, "note"],
        str | None,
    ],
)
def test_a_listed_key_class_decorates(key_type: Any) -> None:
    """The classes that the guide lists as keys decorate."""
    _declare_key(key_type)


class MixedKeys(enum.Enum):
    """Enum whose members' values are a string and an integer of one text."""

    TEXT = "1"
    NUMBER = 1


@pytest.mark.parametrize(
    "key_type",
    [
        decimal.Decimal,
        Color,
        MixedKeys | None,
        Literal["a", "b"],
        Literal["1", 1],
        Literal[1, 2] | None,
    ],
)
def test_a_key_of_a_converted_type_or_a_literal_is_refused(
    key_type: Any,
) -> None:
    """A type that eazyrest converts, or a ``Literal``, is no key type.

    The message names the types that a key may be.
    """
    with pytest.raises(
        TypeError, match=r"Thing\.id is the primary key.*a str, an int, or a"
    ):
        _declare_key(key_type)


@pytest.mark.parametrize(
    "key_type",
    [bytes, pathlib.Path, complex, Serial, uuid.UUID, Slug],
)
def test_a_key_class_that_a_conversion_of_the_model_converts_is_refused(
    key_type: Any,
) -> None:
    """A conversion that the model declares for the key's type refuses it.

    That holds for a ``NewType`` of ``int`` and for ``UUID``, which the model's
    conversion replaces, since a conversion may map two of the service's keys
    to one value.
    """
    conversions = {
        key_type: Conversion(
            decode=lambda value, _: value,
            encode=lambda value, _: str(value),
        )
    }

    with pytest.raises(TypeError, match=r"Thing\.id is the primary key"):
        _declare_key(key_type, conversions=conversions)


@pytest.mark.parametrize("key_type", [str | bytes, bytes | str])
def test_a_key_that_is_a_union_with_bytes_is_refused(key_type: Any) -> None:
    """A union of two classes is not one value."""
    with pytest.raises(TypeError, match=r"Thing\.id is the primary key"):
        _declare_key(key_type)


@json_object
class Me(KeyBase):
    """Singleton whose class names its URL."""

    id: int
    name: str

    @property
    def url(self) -> str:
        """Return the fixed URL."""
        return "/me/"


def test_update_from_json_keeps_the_key_of_a_singleton_that_has_one() -> None:
    """A singleton that knows its key still refuses another."""
    me = Me(json={"id": 7, "name": "Ada"})

    with pytest.raises(ResponseShapeError):
        me.update_from_json({"id": 8, "name": "Grace"})


@json_object
class Item(KeyBase):
    """Model keyed by an ``int``."""

    class_url = "/items/"

    id: int


@pytest.mark.parametrize("key_type", [Slug, Code, Slug | None])
def test_a_key_of_a_str_or_int_subclass_is_refused_with_its_remedy(
    key_type: Any,
) -> None:
    """A subclass reads back as a plain str or int, so it is no key."""
    with pytest.raises(TypeError, match=r"Thing\.id.*NewType.*conversion"):
        _declare_key(key_type)


@pytest.mark.parametrize("field_type", [Slug, Code, list[Slug], Slug | None])
def test_a_field_of_a_str_or_int_subclass_is_refused_with_its_remedy(
    field_type: Any,
) -> None:
    """A field of a subclass of str or int has no JSON form to read."""
    namespace = {
        "__annotations__": {"id": int, "value": field_type},
        "class_url": "/things/",
    }

    with pytest.raises(
        TypeError, match=r"Thing\.value.*register a Conversion"
    ):
        json_object(type("Thing", (KeyBase,), namespace))


@pytest.mark.parametrize(
    "key_type", [NewType("Handle", str), NewType("Number", int)]
)
def test_a_new_type_of_str_or_int_is_a_key(key_type: Any) -> None:
    """A NewType is the way to a distinct key type without a conversion."""
    _declare_key(key_type)


@dataclasses.dataclass(frozen=True)
class LateCode:
    """Class of an application that a key names before it exists."""

    value: str


LATE_CODE = Conversion(
    decode=lambda value, cls: cls(value),
    encode=lambda value, cls: value.value,
)


def test_a_subclass_whose_conversion_converts_a_late_key_class_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inherited key that names a late class is refused once it resolves.

    The subclass's conversion converts the class, which no key may be, so the
    subclass is refused when it is decorated.
    """
    late = LateCode
    monkeypatch.delattr(sys.modules[__name__], "LateCode")
    base: Any = json_object(
        type(
            "LateBase",
            (KeyBase,),
            {
                "__module__": __name__,
                "class_url": "/late/",
                "__annotations__": {"id": "LateCode"},
            },
        )
    )
    monkeypatch.setattr(sys.modules[__name__], "LateCode", late, raising=False)

    with pytest.raises(TypeError, match=r"LateSub\.id is the primary key"):
        json_object(
            type(
                "LateSub",
                (base,),
                {"__module__": __name__, "conversions": {late: LATE_CODE}},
            )
        )


@json_object
class Member(KeyBase):
    """Model keyed by an ``int``, for subclasses."""

    class_url = "/members/"

    id: int
    name: str


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(NewType("MemberId", int), id="new-type"),
        pytest.param(int | None, id="optional"),
        pytest.param(str, id="another-type"),
        pytest.param("LaterMemberKey", id="name-not-defined-yet"),
    ],
)
def test_a_subclass_that_redeclares_its_key_with_another_type_is_refused(
    annotation: Any,
) -> None:
    """The subclass's objects would be the base's resources by another key.

    The message names the subclass, the field, and the base's type.
    """
    namespace = {"__annotations__": {"id": annotation}}

    with pytest.raises(
        TypeError,
        match=r"^Extra\.id redeclares the primary key that Member declares as "
        r"int\b",
    ) as error:
        json_object(type("Extra", (Member,), namespace))
    assert not isinstance(error.value, FieldConversionError)


def test_a_subclass_that_keeps_its_key_type_is_admitted() -> None:
    """A subclass that inherits the key, or redeclares it alike, decorates."""

    @json_object
    class Inheriting(Member):
        pass

    @json_object
    class Redeclaring(Member):
        id: int

    assert Inheriting(id=1).pk == 1
    assert Redeclaring(id=1).pk == 1
    assert Redeclaring(id=1) != Member(id=1)
