"""Tests of the refusals that wait for a field's first use.

A member of a structure that names a class defined later, a late class that has
no conversion, and a union that holds a converting member are refused when the
field is first used. Each operation that uses the field raises
``FieldConversionError`` naming it, as a read does.
"""

import dataclasses
import datetime
from collections.abc import Callable, Iterator
from typing import Any, ClassVar, NamedTuple, NewType, TypedDict

import pytest

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://first-use.example.com/"


class Plain:
    """Class of an application that nothing converts."""


@dataclasses.dataclass
class Waiting:
    """Dataclass whose member names a class that is defined later."""

    inner: "LaterPlain"  # type: ignore[name-defined]  # noqa: F821


@dataclasses.dataclass
class Missing:
    """Dataclass whose member names a class that is never defined."""

    inner: "NeverDefined"  # type: ignore[name-defined]  # noqa: F821


def _model(hint: Any) -> Any:
    """Decorate a model with one field ``value`` of a type.

    Args:
        hint: Annotation of the field.

    Returns:
        The decorated class, with an API registered.
    """
    model: Any = json_object(
        type(
            "Holder",
            (JSONObject,),
            {
                "__module__": __name__,
                "class_url": "/holders/",
                "__annotations__": {"id": int, "value": hint},
            },
        )
    )
    model.register_api(API(BASE))
    return model


def _filter(model: Any) -> Any:
    return list(model.filter(value=1))


def _get(model: Any) -> Any:
    return model.get(value=1)


def _get_page(model: Any) -> Any:
    return model.get_page(value=1)


def _get_or_create(model: Any) -> Any:
    return model.get_or_create(value=1)


def _get_or_create_of_a_generator(model: Any) -> Any:
    return model.get_or_create(value=(item for item in [1, 2]))


def _prefetch(model: Any) -> Any:
    return list(model.filter(prefetch="value"))


def _save_in_full(model: Any) -> Any:
    model(json={"id": 1, "value": {"inner": 1}}).save(full=True)


_ENTRY_POINTS: dict[str, Callable[[Any], Any]] = {
    "filter": _filter,
    "get": _get,
    "get_page": _get_page,
    "get_or_create": _get_or_create,
    "get_or_create_of_a_generator": _get_or_create_of_a_generator,
    "prefetch": _prefetch,
    "save_in_full": _save_in_full,
}


@pytest.fixture
def define_late_plain() -> Iterator[Callable[[], None]]:
    """Yield a function that defines the class that ``Waiting`` names.

    The model decorates before the class exists, so the test calls the function
    after it decorates one.

    Yields:
        The function that defines the class.
    """

    def define() -> None:
        globals()["LaterPlain"] = Plain

    yield define
    globals().pop("LaterPlain", None)


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (Missing, "name 'NeverDefined' is not defined"),
        (Waiting, "Plain has no conversion"),
    ],
    ids=["undefined_member", "late_class_without_a_conversion"],
)
def test_an_operation_refuses_a_member_that_is_refused_at_first_use(
    entry_point: str,
    member: type[Any],
    reason: str,
    define_late_plain: Callable[[], None],
    requests_mock: Any,
) -> None:
    """Each operation raises ``FieldConversionError`` naming the field.

    Nothing is sent.

    Args:
        entry_point: Name of the operation.
        member: Structure whose member is refused when its field is first used.
        reason: Text of the reason that the error gives.
        define_late_plain: Fixture that defines the late class.
        requests_mock: Mock of the transport.
    """
    model = _model(member)
    define_late_plain()

    with pytest.raises(
        FieldConversionError, match=rf"Holder\.value.*{reason}"
    ):
        _ENTRY_POINTS[entry_point](model)

    assert not requests_mock.called


@pytest.mark.parametrize("value", [[], [{"inner": 1}]], ids=["empty", "item"])
def test_an_unanalyzable_member_is_refused_for_any_value(
    value: list[Any],
) -> None:
    """A read of a list of such a structure raises, though it may be empty.

    Args:
        value: JSON of the field.
    """
    obj = _model(list[Missing])(json={"id": 1, "value": value})

    with pytest.raises(FieldConversionError, match=r"Holder\.value"):
        _ = obj.value


class _Mixed(TypedDict):
    """``TypedDict`` with a union that has a converting member."""

    when: str | datetime.datetime


class _MixedTuple(NamedTuple):
    """``NamedTuple`` with a union that has a converting member."""

    when: str | datetime.datetime


@dataclasses.dataclass
class _MixedDataclass:
    """Dataclass with a union that has a converting member."""

    when: str | datetime.datetime


_STRUCTURES = [_Mixed, _MixedTuple, _MixedDataclass]


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_a_union_in_a_structure_is_not_refused_when_decorated(
    structure: type[Any],
) -> None:
    """The class decorates, and the refusal waits for the field's use.

    Args:
        structure: Structure with a union that has a converting member.
    """
    _model(structure)
    _model(list[structure])  # type: ignore[valid-type]
    _model(structure | None)


@pytest.mark.parametrize("structure", _STRUCTURES)
@pytest.mark.parametrize(
    ("hint", "value"),
    [
        (lambda cls: list[cls], []),  # type: ignore[valid-type]
        (lambda cls: dict[str, cls], {}),  # type: ignore[valid-type]
        (lambda cls: cls | None, None),
    ],
    ids=["empty_list", "empty_mapping", "null"],
)
def test_a_union_in_a_structure_is_refused_whatever_the_value(
    structure: type[Any], hint: Callable[[Any], Any], value: Any
) -> None:
    """A read of a value that holds no instance of the structure raises.

    Args:
        structure: Structure with a union that has a converting member.
        hint: Builds the field's annotation from the structure.
        value: JSON that holds no instance of the structure.
    """
    model = _model(hint(structure))
    obj = model(json={"id": 1, "value": value})

    with pytest.raises(FieldConversionError, match=r"Holder\.value"):
        _ = obj.value


class _Partial(TypedDict, total=False):
    """``TypedDict`` whose union member may be missing."""

    when: str | datetime.datetime


def test_a_union_in_a_typed_dict_is_refused_when_its_key_is_missing() -> None:
    """A value without the key, or an empty one, still raises."""
    model = _model(_Partial)

    for value in ({}, {"other": 1}):
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            _ = model(json={"id": 1, "value": value}).value


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_json_schema_refuses_a_union_in_a_structure(
    structure: type[Any],
) -> None:
    """The schema of a field with such a union is refused too.

    Args:
        structure: Structure with a union that has a converting member.
    """
    model = _model(structure)

    with pytest.raises(
        FieldConversionError, match=r"Holder\.value.*do not all pass through"
    ):
        model.json_schema()


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_encode_json_refuses_a_union_in_a_structure_for_an_empty_list(
    structure: type[Any],
) -> None:
    """An empty list of such a structure is refused, not written as ``[]``.

    Args:
        structure: Structure with a union that has a converting member.
    """
    with pytest.raises(TypeError, match="do not all pass through"):
        encode_json([], list[structure])  # type: ignore[valid-type]


_Code = NewType("_Code", str)
"""String type that a subclass's conversions may convert."""

_CODE_CONVERSION = Conversion(
    decode=lambda value, _: _Code(value), encode=lambda value, _: str(value)
)


@json_object(read_only={"seen"})
class _Coded(JSONObject):
    """Model whose fields pass a ``NewType`` through, as its table stands."""

    class_url = "/coded/"

    id: int
    value: _Code | int | None = None
    seen: _Code | int | None = None


_Coded.register_api(API(BASE))


class _Converting(_Coded):
    """Subclass, not decorated, that converts the ``NewType``.

    Its inherited fields were checked against its base's conversions, not its
    own, so every use of it is refused.
    """

    conversions: ClassVar[dict[Any, Conversion]] = {_Code: _CODE_CONVERSION}


class _Further(_Converting):
    """Undecorated subclass of a class that sets conversions."""


_REFUSAL = (
    r"_Converting is not decorated.*Decorate _Converting with @json_object"
)


_USES: dict[str, Callable[[Any], Any]] = {
    "init": lambda model: model(json={"id": 1}),
    "init-with-fields": lambda model: model(id=1, value=None),
    "filter": lambda model: list(model.filter(value=1)),
    "all": lambda model: list(model.all()),
    "get": lambda model: model.get(id=1),
    "get-page": lambda model: model.get_page(value=1),
    "get-or-create": lambda model: model.get_or_create(id=1),
    "create": lambda model: model.create(id=1, value=None),
    "json-schema": lambda model: model.json_schema(),
}


@pytest.mark.parametrize("use", _USES)
def test_an_undecorated_subclass_that_sets_conversions_refuses_every_use(
    use: str, requests_mock: Any
) -> None:
    """Making an instance, a query, a write, and a schema raise TypeError.

    Whatever the value, null included, and before any request is sent.

    Args:
        use: Name of the operation.
        requests_mock: Mock of the transport.
    """
    with pytest.raises(TypeError, match=_REFUSAL):
        _USES[use](_Converting)

    assert not requests_mock.called


@pytest.mark.parametrize("use", ["init", "filter", "json-schema"])
def test_a_subclass_of_an_undecorated_one_that_sets_conversions_is_refused(
    use: str,
) -> None:
    """The refusal names the class in use and the one that sets conversions.

    Args:
        use: Name of the operation.
    """
    with pytest.raises(
        TypeError, match=r"_Further is not decorated.*_Converting sets"
    ):
        _USES[use](_Further)


class _PlainMixin:
    """Class that is not a model and sets conversions."""

    conversions: ClassVar[dict[Any, Conversion]] = {_Code: _CODE_CONVERSION}


class _ModelMixin(JSONObject):
    """Undecorated model class that sets conversions."""

    conversions: ClassVar[dict[Any, Conversion]] = {_Code: _CODE_CONVERSION}


class _AfterPlain(_Coded, _PlainMixin):  # type: ignore[misc]
    """Undecorated, with a mixin that sets conversions after the base."""


class _AfterModel(_Coded, _ModelMixin):
    """Undecorated, with a model mixin that sets conversions after the base."""


class _BeforePlain(_PlainMixin, _Coded):  # type: ignore[misc]
    """Undecorated, with a mixin that sets conversions before the base."""


@pytest.mark.parametrize("model", [_AfterPlain, _AfterModel, _BeforePlain])
@pytest.mark.parametrize("use", ["init", "filter", "json-schema"])
def test_an_undecorated_class_with_a_conversions_mixin_is_refused(
    model: Any, use: str
) -> None:
    """A mixin changes the table wherever it stands in the bases.

    Args:
        model: Undecorated class with a mixin that sets conversions.
        use: Name of the operation.
    """
    with pytest.raises(
        TypeError,
        match=rf"{model.__name__} is not decorated.*Mixin sets conversions",
    ):
        _USES[use](model)


def test_the_refusal_is_not_a_field_conversion_error() -> None:
    """The class is wrong, not a field's value, so the error is a TypeError."""
    with pytest.raises(TypeError) as excinfo:
        _Converting(json={"id": 1})

    assert not isinstance(excinfo.value, FieldConversionError)


@dataclasses.dataclass
class _Other:
    """Class of an application that a decorated subclass converts."""

    text: str


@json_object
class _Decorated(_Coded):
    """Decorated subclass that sets conversions."""

    conversions: ClassVar[dict[Any, Conversion]] = {
        _Other: Conversion(
            decode=lambda value, _: _Other(value),
            encode=lambda value, _: value.text,
        )
    }


def test_a_decorated_subclass_that_sets_conversions_is_used() -> None:
    """Decorating checks the inherited fields against the subclass's table."""
    obj = _Decorated(json={"id": 1, "value": "a"})

    assert obj.value == "a"
    assert _Decorated.json_schema()["type"] == "object"


def test_an_undecorated_subclass_without_conversions_is_used() -> None:
    """A subclass that keeps its base's conversions needs no decorator."""

    class Mixed(_Coded):
        """Undecorated subclass that sets no conversions."""

    assert Mixed(json={"id": 1, "value": "a"}).value == "a"


def test_a_conversions_mixin_without_a_decorated_ancestor_is_not_refused() -> (
    None
):
    """Only a class that inherits decorated fields can contradict them."""

    class Mixin(JSONObject):
        """Base class that sets conversions, as a model's base class does."""

        conversions: ClassVar[dict[Any, Conversion]] = {
            _Code: _CODE_CONVERSION
        }

    @json_object
    class Child(Mixin):
        """Decorated model of the base class."""

        class_url = "/children/"

        id: int
        code: _Code

    assert Child(json={"id": 1, "code": "a"}).code == "a"


def test_a_base_still_passes_the_union_through() -> None:
    """The base's own conversions leave the union as it is."""
    obj = _Coded(json={"id": 1, "seen": None})

    assert obj.seen is None
    assert _Coded(json={"id": 1, "seen": "a"}).seen == "a"
