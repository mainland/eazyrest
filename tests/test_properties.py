"""Hypothesis properties of conversion, writes, and error messages.

The example-based tests check chosen cases of these contracts. The properties
here check them over generated values, payloads, and declarations.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import enum
import importlib.util
import itertools
import json
import re
import typing
import uuid
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar
from urllib.parse import parse_qsl, urlparse

import isodate
import pytest
import requests
import requests_mock
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from typing_extensions import TypeAliasType

from eazyrest import (
    API,
    DEFAULT_CONVERSIONS,
    Conversion,
    FieldConversionError,
    JSONObject,
    ResponseShapeError,
    WriteMode,
    json_object,
    parse_duration,
)
from eazyrest._models import (
    CollectionType,
    OptionalType,
    _json_equal,
    analyze_type,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

HAVE_PYDANTIC = importlib.util.find_spec("pydantic") is not None
HAVE_ANNOTATED_TYPES = importlib.util.find_spec("annotated_types") is not None
if HAVE_ANNOTATED_TYPES:
    import annotated_types

BASE_URL = "https://properties.example/"


class PropertyBase(JSONObject):
    """Models served by this module's mocked API."""


PropertyBase.register_api(API(BASE_URL))


# JSON values as Python's decoder produces them. The decoder that responses use
# rejects NaN and the infinities, so they are not JSON values here.
_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=5),
)
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda items: st.one_of(
        st.lists(items, max_size=4),
        st.dictionaries(st.text(max_size=3), items, max_size=4),
    ),
    max_leaves=12,
)

# JSON number text, including exponents that overflow a float, and the
# non-finite constants that Python's decoder accepts although JSON has none.
_JSON_NUMBER_TEXT = st.one_of(
    st.from_regex(
        r"-?(0|[1-9][0-9]{0,5})(\.[0-9]{1,5})?([eE][+-]?[0-9]{1,4})?",
        fullmatch=True,
    ),
    st.sampled_from(["NaN", "Infinity", "-Infinity"]),
)


def _response(body: str) -> requests.Response:
    """Return a successful response that holds ``body``.

    Args:
        body: Response body text.

    Returns:
        Response to a ``GET`` of the base URL, with the body already read.
    """
    resp = requests.Response()
    resp.status_code = 200
    resp.url = BASE_URL
    resp.request = requests.Request("GET", BASE_URL).prepare()
    resp._content = body.encode()
    return resp


@json_object
class Holder(PropertyBase):
    """Model with a field that holds any JSON value."""

    class_url = "/holders/"

    id: int
    data: Any


@settings(max_examples=100)
@given(value=_JSON_VALUES)
def test_decoded_json_equals_itself(value: Any) -> None:
    """Every decoded JSON value equals itself, so it is never a change.

    ``NaN`` once made a field's value unequal to itself, so every ``save()``
    sent it again. A value read from a field and saved unchanged must send
    nothing.

    Args:
        value: Generated JSON value.
    """
    decoded = Holder.api.json(_response(json.dumps(value)))

    assert _json_equal(decoded, decoded)
    assert _json_equal(decoded, copy.deepcopy(decoded))

    holder = Holder(json={"id": 1, "data": decoded})
    _ = holder.data
    with requests_mock.Mocker() as mock:
        holder.save()

    assert not mock.called


@settings(max_examples=100)
@given(text=_JSON_NUMBER_TEXT)
def test_json_numbers_decode_to_equal_values_or_raise(text: str) -> None:
    """A JSON number decodes to a value equal to itself, or raises.

    ``NaN`` is unequal to itself, so a field that held it would be sent by
    every ``save()``. The response decoder rejects it, the infinities, and an
    exponent too large for a float, such as ``1e400``.

    Args:
        text: Generated JSON number.
    """
    try:
        decoded = Holder.api.json(_response(f"[{text}]"))
    except ResponseShapeError:
        return

    assert _json_equal(decoded, decoded)


class Color(enum.Enum):
    """Enum with values of two JSON types."""

    RED = "red"
    ONE = 1


@json_object
class Codecs(PropertyBase):
    """Model with a field of each type that has a built-in conversion."""

    class_url = "/codecs/"

    id: int
    when: datetime.datetime
    day: datetime.date
    token: uuid.UUID
    length: datetime.timedelta
    color: Color


# Every UTC offset that Python allows, including offsets with seconds and
# microseconds, which ISO 8601 cannot write. Whole minutes are drawn often,
# since they are the offsets that encode.
_ONE_MICROSECOND = datetime.timedelta(microseconds=1)

_UTC_OFFSETS = st.one_of(
    st.integers(min_value=-(24 * 60 - 1), max_value=24 * 60 - 1).map(
        lambda minutes: datetime.timedelta(minutes=minutes)
    ),
    st.timedeltas(
        min_value=-datetime.timedelta(hours=24) + _ONE_MICROSECOND,
        max_value=datetime.timedelta(hours=24) - _ONE_MICROSECOND,
    ),
).map(datetime.timezone)

_CODEC_VALUES = st.one_of(
    st.tuples(
        st.just("when"), st.datetimes(timezones=st.none() | _UTC_OFFSETS)
    ),
    st.tuples(st.just("day"), st.dates()),
    st.tuples(st.just("token"), st.uuids()),
    st.tuples(st.just("length"), st.timedeltas()),
    st.tuples(st.just("color"), st.sampled_from(Color)),
)


@settings(max_examples=200)
@given(field_and_value=_CODEC_VALUES)
def test_built_in_conversions_read_back_what_they_write(
    field_and_value: tuple[str, Any],
) -> None:
    """A field reads back the value whose JSON it writes, or refuses it.

    A datetime whose UTC offset is not a whole number of minutes has no ISO
    8601 form, so encoding it raises.

    Args:
        field_and_value: Name of a ``Codecs`` field and a value of its type.
    """
    field, value = field_and_value
    conversion = getattr(Codecs, field).analyzed_type
    offset = (
        value.utcoffset() if isinstance(value, datetime.datetime) else None
    )
    if offset is not None and offset % datetime.timedelta(minutes=1):
        with pytest.raises(ValueError, match="whole minutes"):
            Codecs._to_json(value, conversion)
        return

    encoded = Codecs._to_json(value, conversion)
    json.dumps(encoded)

    decoded = getattr(Codecs(json={"id": 1, field: encoded}), field)

    assert decoded == value
    assert type(decoded) is type(value)
    if isinstance(value, datetime.datetime):
        assert decoded.utcoffset() == value.utcoffset()


@settings(max_examples=200)
@given(delta=st.timedeltas())
def test_parse_duration_reads_iso_8601_durations(
    delta: datetime.timedelta,
) -> None:
    """``parse_duration()`` reads every duration that isodate writes.

    Args:
        delta: Generated duration.
    """
    assert parse_duration(isodate.duration_isoformat(delta)) == delta


@json_object(
    read_only={"stamp"},
    field_map={"title": "titleText"},
    write_map={"rank": "rank_id"},
)
class Replaceable(PropertyBase):
    """Model whose full save renames, maps, and omits fields."""

    class_url = "/replaceables/"

    id: int
    count: int
    title: str
    rank: int | None
    stamp: str


# Each writable field's JSON key, write key, and value strategy.
_WRITABLE: dict[str, tuple[str, str, st.SearchStrategy[Any]]] = {
    "count": ("count", "count", st.integers()),
    "title": ("titleText", "titleText", st.text(max_size=4)),
    "rank": ("rank", "rank_id", st.none() | st.integers()),
}


@st.composite
def _full_save_cases(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], str, frozenset[str], dict[str, Any]]:
    """Draw a server object, a partial state, and local changes.

    Args:
        draw: Hypothesis draw function.

    Returns:
        The server's JSON, how the object starts, the fields whose server
        values the object was pushed, and assignments to make before saving.
    """
    server: dict[str, Any] = {"id": 1}
    for json_key, _, values in _WRITABLE.values():
        if draw(st.booleans()):
            server[json_key] = draw(values)
    if draw(st.booleans()):
        server["stamp"] = draw(st.text(max_size=4))

    start = draw(st.sampled_from(["unloaded", "pushed", "created"]))
    known = frozenset(draw(st.sets(st.sampled_from(sorted(server)))))
    # A field that the server's JSON lacks cannot be assigned.
    changes = {
        field: draw(values)
        for field, (json_key, _, values) in _WRITABLE.items()
        if json_key in server and draw(st.booleans())
    }
    return server, start, known, changes


@settings(max_examples=100)
@given(case=_full_save_cases())
def test_full_save_sends_every_writable_field(
    case: tuple[dict[str, Any], str, frozenset[str], dict[str, Any]],
) -> None:
    """A full save sends every writable field that the server holds.

    The object may know only some of its fields, from a push or from a
    creation response that holds only its key. A full save loads the rest
    before it sends the replacement, so an API that replaces the whole object
    keeps the fields that the object had not read. Local changes take the
    place of the server's values, and a read-only field is never sent.

    Args:
        case: Server JSON, starting state, pushed keys, and assignments.
    """
    server, start, known, changes = case
    url = f"{BASE_URL}replaceables/1/"
    with requests_mock.Mocker() as mock:
        mock.get(url, json=server)
        mock.post(f"{BASE_URL}replaceables/", json={"id": 1})
        put = mock.put(url, json={})

        if start == "created":
            obj = Replaceable.create(id=1)
        else:
            obj = Replaceable(id=1)
            if start == "pushed":
                obj.update_from_json({key: server[key] for key in known})
        for field, value in changes.items():
            setattr(obj, field, value)
        obj.save(full=True)

        sent = put.last_request.json()

    expected = {"id": 1}
    for field, (json_key, write_key, _) in _WRITABLE.items():
        if field in changes:
            expected[write_key] = changes[field]
        elif json_key in server:
            expected[write_key] = server[json_key]
    assert sent == expected


@json_object
class Owner(PropertyBase):
    """Related model that ``Synced`` refers to by primary key."""

    class_url = "/owners/"

    id: int
    name: str


@json_object
class Synced(PropertyBase):
    """Model whose fields both write modes change."""

    class_url = "/synced/"

    id: int
    title: str
    tags: list[str]
    meta: dict[str, int]
    owner: Owner


SYNCED_JSON: dict[str, Any] = {
    "id": 1,
    "title": "t",
    "tags": ["a"],
    "meta": {},
    "owner": 1,
}

# An operation on a Synced object: an assignment of a field, an in-place change
# of a value read from a field, or a change of the primary key.
_SYNC_OPERATIONS = st.one_of(
    st.tuples(st.just("title"), st.text(max_size=3)),
    st.tuples(st.just("tags"), st.lists(st.text(max_size=2), max_size=3)),
    st.tuples(
        st.just("meta"),
        st.dictionaries(st.text(max_size=2), st.integers(), max_size=2),
    ),
    st.tuples(st.just("owner"), st.integers(min_value=1, max_value=3)),
    st.tuples(st.just("append"), st.text(max_size=2)),
    st.tuples(st.just("put"), st.tuples(st.text(max_size=2), st.integers())),
    st.tuples(st.just("id"), st.integers(min_value=1, max_value=4)),
)


def _serve_synced(
    mock: requests_mock.Mocker, store: dict[int, dict[str, Any]], body: str
) -> None:
    """Serve ``Synced`` objects from ``store``, applying each ``PATCH``.

    Args:
        mock: Mocker to register the server's routes with.
        store: Server objects, keyed by primary key. A ``PATCH`` that changes
            the key moves its object.
        body: ``"object"`` to answer a ``PATCH`` with the stored object, or
            ``"empty"`` to answer it with ``204 No Content``.
    """
    pattern = re.compile(re.escape(f"{BASE_URL}synced/") + r"(\d+)/$")

    def key(request: Any) -> int:
        """Return the primary key in a request's URL."""
        match = pattern.match(request.url)
        assert match is not None
        return int(match.group(1))

    def get(request: Any, context: Any) -> str:
        """Answer a ``GET`` with the stored object."""
        return json.dumps(store[key(request)])

    def patch(request: Any, context: Any) -> str:
        """Apply a ``PATCH`` to the stored object and answer it."""
        obj = store.pop(key(request))
        obj.update(request.json())
        store[obj["id"]] = obj
        if body == "empty":
            context.status_code = 204
            return ""
        return json.dumps(obj)

    mock.get(pattern, text=get)
    mock.patch(pattern, text=patch)


def _run_synced(
    mode: WriteMode,
    start: str,
    body: str,
    operations: list[tuple[str, Any]],
) -> tuple[Synced, dict[int, dict[str, Any]], tuple[Any, ...]]:
    """Apply operations to a ``Synced`` object in one write mode and save it.

    Args:
        mode: ``"lazy"`` or ``"eager"``.
        start: ``"loaded"`` to start from the object's JSON, or ``"lazy"`` to
            start from its primary key.
        body: Body of the server's ``PATCH`` responses, as for
            ``_serve_synced()``.
        operations: Operations to apply, in order.

    Returns:
        The object, the server's objects after the final ``save()``, and the
        object's field values then, with the owner as its primary key.
    """
    store = {1: copy.deepcopy(SYNCED_JSON)}
    with requests_mock.Mocker() as mock:
        _serve_synced(mock, store, body)
        if start == "loaded":
            obj = Synced(json=copy.deepcopy(SYNCED_JSON), write_mode=mode)
        else:
            obj = Synced(id=1, write_mode=mode)
        for kind, value in operations:
            if kind == "append":
                obj.tags.append(value)
            elif kind == "put":
                obj.meta[value[0]] = value[1]
            elif kind == "owner":
                obj.owner = Owner(id=value)
            else:
                setattr(obj, kind, value)
            if mode == "eager" and kind not in ("append", "put"):
                # An eager assignment is sent at once.
                assert obj._pending_updates == {}
                (server,) = store.values()
                assert server[kind] == value
        # An in-place change is detected only by save() or by the next eager
        # PATCH, as the guide and save() say, so an eager object also saves.
        obj.save()
        state = (obj.id, obj.title, obj.tags, obj.meta, obj.owner.pk)
        assert obj._pending_updates == {}
        assert obj.json == store[obj.id]
    return obj, store, state


@settings(max_examples=150)
@given(
    start=st.sampled_from(["loaded", "lazy"]),
    body=st.sampled_from(["object", "empty"]),
    operations=st.lists(_SYNC_OPERATIONS, max_size=6),
)
def test_eager_and_lazy_writes_converge(
    start: str, body: str, operations: list[tuple[str, Any]]
) -> None:
    """Eager and lazy objects that make the same changes end the same.

    A lazy object sends its changes in one ``PATCH`` at ``save()``. An eager
    object sends each assignment at once, and a change made in place to a
    value read from a field at its next ``PATCH`` or at ``save()``. Either way,
    the server and the object end with the same state, whether a ``PATCH``
    answers with the object or with no body.

    Args:
        start: Whether the object starts loaded or from its primary key.
        body: Body of the server's ``PATCH`` responses.
        operations: Assignments, in-place changes, and key changes to make.
    """
    lazy, lazy_store, lazy_state = _run_synced("lazy", start, body, operations)
    eager, eager_store, eager_state = _run_synced(
        "eager", start, body, operations
    )

    # Both modes deliver every change, in-place changes included.
    expected = copy.deepcopy(SYNCED_JSON)
    for kind, value in operations:
        if kind == "append":
            expected["tags"].append(value)
        elif kind == "put":
            expected["meta"][value[0]] = value[1]
        else:
            expected[kind] = copy.deepcopy(value)
    assert lazy_store == {expected["id"]: expected}
    assert lazy_store == eager_store
    assert lazy_state == eager_state
    assert lazy.json == eager.json


# Python names of the declared fields, and other names that a call can pass.
_DECLARED = ("f0", "f1", "f2")
_NAMES = st.sampled_from([*_DECLARED, "k0", "k1", "x"])


@st.composite
def _wire_declarations(
    draw: st.DrawFn,
) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Draw read and write key mappings and the keys of one call.

    Args:
        draw: Hypothesis draw function.

    Returns:
        A ``field_map``, a ``write_map``, and keyword arguments for a call.
    """
    field_map = draw(
        st.dictionaries(st.sampled_from(_DECLARED), _NAMES, max_size=3)
    )
    write_map = draw(
        st.dictionaries(st.sampled_from(_DECLARED), _NAMES, max_size=3)
    )
    call = draw(st.dictionaries(_NAMES, st.integers(0, 9), max_size=4))
    return field_map, write_map, call


def _declare(
    field_map: Mapping[str, str], write_map: Mapping[str, str]
) -> type[PropertyBase] | None:
    """Declare a model with three integer fields and the given mappings.

    Args:
        field_map: Read keys of the fields.
        write_map: Write keys of the fields.

    Returns:
        The model, or ``None`` if the decorator rejects the mappings.
    """

    class Mapped(PropertyBase):
        class_url = "/mapped/"

        id: int
        f0: int
        f1: int
        f2: int

    try:
        return json_object(
            field_map=dict(field_map), write_map=dict(write_map)
        )(Mapped)
    except (TypeError, ValueError):
        return None


@settings(max_examples=150)
@given(case=_wire_declarations())
def test_create_sends_each_name_under_its_own_key(
    case: tuple[dict[str, str], dict[str, str], dict[str, int]],
) -> None:
    """``create()`` sends distinct names under distinct keys, or raises.

    A field's write key and a name passed as is can be the same string. One
    value must not then replace the other in the body. The call must raise
    before anything is sent instead.

    Args:
        case: Mappings and the fields passed to ``create()``.
    """
    field_map, write_map, call = case
    model = _declare(field_map, write_map)
    assume(model is not None)
    assert model is not None

    with requests_mock.Mocker() as mock:
        mock.post(f"{BASE_URL}mapped/", json={"id": 1})
        try:
            model.create(fields=call)
        except (TypeError, ValueError, AttributeError):
            assert not mock.called
            return

        sent = mock.last_request.json()

    assert len(sent) == len(call)
    assert sorted(sent.values()) == sorted(call.values())


@settings(max_examples=150)
@given(case=_wire_declarations())
def test_query_sends_each_name_under_its_own_key(
    case: tuple[dict[str, str], dict[str, str], dict[str, int]],
) -> None:
    """A query sends distinct names under distinct parameters, or raises.

    Args:
        case: Mappings and the parameters passed to ``filter()``.
    """
    field_map, write_map, call = case
    model = _declare(field_map, write_map)
    assume(model is not None)
    assert model is not None

    with requests_mock.Mocker() as mock:
        mock.get(f"{BASE_URL}mapped/", json=[])
        try:
            list(model.filter(params=call))
        except (TypeError, ValueError):
            assert not mock.called
            return

        query = parse_qsl(urlparse(mock.last_request.url).query)

    assert len(query) == len(call)
    assert len({name for name, _ in query}) == len(call)
    assert sorted(int(value) for _, value in query) == sorted(call.values())


if HAVE_PYDANTIC:
    from typing import Annotated, Literal

    from pydantic import (
        AliasChoices,
        BaseModel,
        ConfigDict,
        Field,
    )
    from pydantic import (
        Json as PydanticJson,
    )

    class Cat(BaseModel):
        """Member of a discriminated union."""

        kind: Literal["cat"]
        lives: int

    class Dog(BaseModel):
        """Member of a discriminated union."""

        kind: Literal["dog"]
        good: bool

    class Strict(BaseModel):
        """Model with aliases that rejects undeclared keys."""

        model_config = ConfigDict(extra="forbid")

        name: str = Field(validation_alias=AliasChoices("name", "label"))
        size: int = Field(alias="sizeValue")

    @json_object
    class Validated(PropertyBase):
        """Model whose Pydantic fields transform their input."""

        class_url = "/validated/"

        id: int
        text: PydanticJson[dict[str, int]]
        pet: Annotated[Cat | Dog, Field(discriminator="kind")]
        nested: Annotated[list[dict[str, int | list[int]] | Strict], Field()]
        keyed: Annotated[dict[int, Strict], Field()]
        strict: Strict


_SENTINEL = "Zq9SENTINEL"
"""Text that no message would contain unless it quoted the input."""


def _with_sentinel(depth: int = 2) -> st.SearchStrategy[Any]:
    """Return JSON values that hold the sentinel as keys and strings.

    Args:
        depth: Greatest nesting depth of the generated values.

    Returns:
        Strategy for JSON values in which the sentinel appears.
    """
    leaves = st.one_of(
        st.just(_SENTINEL),
        st.integers(-2, 2),
        st.booleans(),
        st.none(),
        st.sampled_from(["cat", "dog", "name", "label", "sizeValue"]),
    )
    keys = st.sampled_from(
        [_SENTINEL, "kind", "lives", "good", "name", "label", "sizeValue", "1"]
    )
    return st.recursive(
        leaves,
        lambda items: st.one_of(
            st.lists(items, max_size=3),
            st.dictionaries(keys, items, max_size=3),
        ),
        max_leaves=8,
    )


@pytest.mark.skipif(not HAVE_PYDANTIC, reason="Pydantic is not installed")
@settings(max_examples=200)
@given(
    field=st.sampled_from(["text", "pet", "nested", "keyed", "strict"]),
    value=st.deferred(_with_sentinel),
    as_text=st.booleans(),
)
def test_pydantic_errors_never_quote_the_input(
    field: str, value: Any, as_text: bool
) -> None:
    """A conversion error never quotes a key or value from the input.

    Pydantic's messages and locations copy input mapping keys, discriminator
    tags, and values, including those that JSON text holds once it is decoded
    during validation. ``FieldConversionError`` must redact them.

    Args:
        field: Name of the ``Validated`` field to read.
        value: Generated JSON value that holds the sentinel.
        as_text: Whether to send the value as JSON text, which only ``text``
            decodes during validation.
    """
    if as_text:
        value = json.dumps(value)
    obj = Validated(json={"id": 1, field: value})

    message = ""
    try:
        getattr(obj, field)
    except FieldConversionError as err:
        message = str(err)

    assert _SENTINEL not in message


_ALIAS_NAMES = (f"Alias{n}" for n in itertools.count())
"""Names for the aliases that the type strategies create."""

_T = TypeVar("_T")
_Box = TypeAliasType("_Box", list[_T], type_params=(_T,))
"""Generic alias, which expands to a list of its argument."""

# Recursive aliases, whose quoted references resolve in this module. A field
# that reaches one cannot be converted.
_Loop = TypeAliasType("_Loop", "list[_Loop] | int")
_Ping = TypeAliasType("_Ping", "list[_Pong]")
_Pong = TypeAliasType("_Pong", "_Ping | None")


class _TypePair(typing.NamedTuple):
    """A type that may use aliases, and the same type with them expanded."""

    aliased: Any
    expanded: Any
    recursive: bool


_LEAF_TYPES = st.sampled_from(
    [
        _TypePair(int, int, False),
        _TypePair(str, str, False),
        _TypePair(datetime.date, datetime.date, False),
        _TypePair(Owner, Owner, False),
    ]
)
_RECURSIVE_LEAVES = st.sampled_from(
    [_TypePair(_Loop, None, True), _TypePair(_Ping, None, True)]
)


# Type constructors applied to types built at run time, which a type checker
# would otherwise read as type expressions.
_LIST: Any = list
_TUPLE: Any = tuple
_SET: Any = set
_UNION: Any = typing.Union
_ALIAS: Any = TypeAliasType
_BOX: Any = _Box

_CONSTRUCTORS = st.sampled_from(
    [
        lambda ty: _LIST[ty],
        lambda ty: _TUPLE[ty, ...],
        lambda ty: _SET[ty],
        lambda ty: _UNION[ty, None],
    ]
)


def _type_nodes(
    children: st.SearchStrategy[_TypePair],
) -> st.SearchStrategy[_TypePair]:
    """Return pairs built from smaller pairs by one type constructor.

    Args:
        children: Strategy for the pairs to build on.

    Returns:
        Strategy for lists, tuples, sets, and optional types of the children,
        each as written and as expanded.
    """
    return st.tuples(_CONSTRUCTORS, children).map(
        lambda t: _TypePair(
            t[0](t[1].aliased), t[0](t[1].expanded), t[1].recursive
        )
    )


def _aliased(pair: _TypePair, how: str) -> _TypePair:
    """Return a pair whose written type is wrapped in an alias.

    Args:
        pair: Pair to wrap.
        how: ``"plain"`` for a named alias of the type, ``"generic"`` for the
            generic list alias applied to it, or ``"none"`` to leave it.

    Returns:
        The wrapped pair, with the same expansion as the alias.
    """
    if how == "plain":
        alias = _ALIAS(next(_ALIAS_NAMES), pair.aliased)
        return _TypePair(alias, pair.expanded, pair.recursive)
    if how == "generic":
        return _TypePair(
            _BOX[pair.aliased], _LIST[pair.expanded], pair.recursive
        )
    return pair


def _alias_trees() -> st.SearchStrategy[_TypePair]:
    """Return type pairs of up to three levels, with aliases at any level.

    Returns:
        Strategy for pairs whose written type uses aliases.
    """
    hows = st.sampled_from(["plain", "generic", "none", "none"])
    leaves = st.one_of(_LEAF_TYPES, _RECURSIVE_LEAVES)
    return st.recursive(
        st.tuples(leaves, hows).map(lambda t: _aliased(*t)),
        lambda children: st.tuples(_type_nodes(children), hows).map(
            lambda t: _aliased(*t)
        ),
        max_leaves=3,
    )


def _field_model(annotation: Any) -> Any:
    """Return a model whose ``value`` field has a type.

    Args:
        annotation: Type of the field.

    Returns:
        A decorated model with the fields ``id`` and ``value``.
    """
    return json_object(
        type(
            "Generated",
            (PropertyBase,),
            {
                "__annotations__": {"id": int, "value": annotation},
                "class_url": "/generated/",
            },
        )
    )


# JSON values that the leaf types accept, and some that they reject, nested in
# lists as the collection types read them.
_ALIAS_JSON = st.recursive(
    st.one_of(
        st.none(),
        st.integers(-3, 3),
        st.sampled_from(["a", "2024-01-01", "not a date", "1"]),
    ),
    lambda items: st.lists(items, max_size=3),
    max_leaves=6,
)


def _plain(value: Any) -> Any:
    """Return a value read from a field in a form that compares by content.

    Args:
        value: Value read from a field.

    Returns:
        ``value``, with each related object replaced by its class name and
        primary key, and each collection tagged with its type, so that a list
        and a tuple of the same items differ.
    """
    if isinstance(value, JSONObject):
        return type(value).__name__, value.pk
    if isinstance(value, (set, frozenset)):
        return "set", sorted(repr(_plain(item)) for item in value)
    if isinstance(value, (list, tuple)):
        return type(value).__name__, [_plain(item) for item in value]
    return value


def _read_outcome(model: Any, value: Any) -> tuple[str, Any]:
    """Read a ``value`` field, then write the result back as JSON.

    Args:
        model: Model from ``_field_model()``.
        value: JSON value of the field.

    Returns:
        ``("ok", (read, reread))`` with the value read and the value read
        again from the JSON that writing it gives, each in ``_plain()`` form,
        so that a set compares regardless of its JSON order, or
        ``("raise", type)`` with the type of the exception that reading or
        writing raised.
    """
    obj = model(json={"id": 1, "value": copy.deepcopy(value)})
    conversion = model.value.analyzed_type
    try:
        read = obj.value
        written = model._to_json(read, conversion)
        reread = model(json={"id": 1, "value": written}).value
        return "ok", (_plain(read), _plain(reread))
    except (FieldConversionError, TypeError, ValueError) as err:
        return "raise", type(err)


@settings(max_examples=150)
@given(pair=_alias_trees(), value=_ALIAS_JSON)
def test_named_type_aliases_convert_as_their_expansions(
    pair: _TypePair, value: Any
) -> None:
    """A field typed by aliases converts as one typed by their expansion.

    Named aliases, generic aliases, and aliases inside other types expand
    before conversion. A field that reaches a recursive alias cannot be
    converted, and raises when it is first read.

    Args:
        pair: Type as written with aliases and as expanded.
        value: JSON value of the field.
    """
    aliased = _field_model(pair.aliased)

    if pair.recursive:
        with pytest.raises(FieldConversionError, match="refers to itself"):
            _ = aliased(json={"id": 1, "value": value}).value
        return

    expanded = _field_model(pair.expanded)
    assert _read_outcome(aliased, value) == _read_outcome(expanded, value)


@dataclasses.dataclass(frozen=True)
class _Note:
    """Metadata of another library, which eazyrest must ignore."""

    text: str


_ANNOTATED: Any = typing.Annotated

# Metadata that is not for Pydantic. annotated_types constraints, which
# Pydantic also reads, are not Pydantic's own, so they do not opt in either.
_NEUTRAL_METADATA = st.one_of(
    st.text(max_size=3),
    st.integers(),
    st.builds(_Note, st.text(max_size=3)),
    *(
        [st.sampled_from([annotated_types.Gt(0), annotated_types.MaxLen(3)])]
        if HAVE_ANNOTATED_TYPES
        else []
    ),
)


def _annotated(pair: _TypePair, metadata: list[Any]) -> _TypePair:
    """Return a pair whose written type carries neutral metadata.

    Args:
        pair: Pair to annotate, whose written type is the bare type.
        metadata: Metadata to add, or nothing to leave the type bare.

    Returns:
        The pair, with its written type annotated and its bare type unchanged.
    """
    if not metadata:
        return pair
    return _TypePair(
        _ANNOTATED[(pair.aliased, *metadata)], pair.expanded, pair.recursive
    )


def _annotated_trees() -> st.SearchStrategy[_TypePair]:
    """Return type pairs of up to three levels, with metadata at any level.

    Returns:
        Strategy for pairs whose written type is annotated and whose other
        type is the bare type.
    """
    metadata = st.lists(_NEUTRAL_METADATA, max_size=2)
    return st.recursive(
        st.tuples(_LEAF_TYPES, metadata).map(lambda t: _annotated(*t)),
        lambda children: st.tuples(_type_nodes(children), metadata).map(
            lambda t: _annotated(*t)
        ),
        max_leaves=3,
    )


def _collapsed(analyzed: Any) -> Any:
    """Return analyzed type metadata with nested optional types collapsed.

    ``Annotated`` keeps ``(T | None) | None`` from flattening as the bare union
    does, and both convert the same way.

    Args:
        analyzed: Result of ``analyze_type()``.

    Returns:
        ``analyzed`` with each optional of an optional made one optional.
    """
    if isinstance(analyzed, OptionalType):
        inner = _collapsed(analyzed.inner_type)
        return (
            inner if isinstance(inner, OptionalType) else OptionalType(inner)
        )
    if isinstance(analyzed, CollectionType):
        return CollectionType(
            analyzed.collection_type, _collapsed(analyzed.item_type)
        )
    return analyzed


@settings(max_examples=150)
@given(pair=_annotated_trees(), value=_ALIAS_JSON)
def test_neutral_metadata_leaves_conversion_unchanged(
    pair: _TypePair, value: Any
) -> None:
    """``Annotated`` metadata that is not for Pydantic changes nothing.

    A field typed ``Annotated[T, m]`` analyzes as ``T``, so it never opts in
    to Pydantic, and it reads and writes every value as ``T`` does.

    Args:
        pair: Type as written with metadata and as bare.
        value: JSON value of the field.
    """
    assert _collapsed(analyze_type(pair.aliased)) == _collapsed(
        analyze_type(pair.expanded)
    )

    annotated = _field_model(pair.aliased)
    bare = _field_model(pair.expanded)
    assert _read_outcome(annotated, value) == _read_outcome(bare, value)


_UNWRITABLE_DATE = datetime.date(2000, 1, 1)
"""Date that ``Fragile`` reads but cannot write."""


def _encode_fragile_date(value: datetime.date) -> str:
    """Write a date, except the one that ``Fragile`` cannot write.

    Args:
        value: The date.

    Returns:
        Its ISO 8601 form.

    Raises:
        ValueError: If ``value`` is the unwritable date.
    """
    if value == _UNWRITABLE_DATE:
        raise ValueError("This date cannot be written")
    return value.isoformat()


@json_object
class Fragile(PropertyBase):
    """Model whose fields can fail to read, by decoding or by encoding."""

    class_url = "/fragile/"

    id: int
    name: str
    day: datetime.date
    when: datetime.datetime
    token: uuid.UUID
    length: datetime.timedelta
    color: Color
    owner: Owner
    days: list[datetime.date]
    lengths: list[datetime.timedelta]

    # A changeable value, such as a list, is encoded when it is read, to
    # record its encoding for change detection, so a date that cannot be
    # written makes a read fail after decoding.
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        datetime.date: Conversion(
            decode=DEFAULT_CONVERSIONS[datetime.date].decode,
            encode=lambda value, cls: _encode_fragile_date(value),
        )
    }


_FRAGILE_FIELDS = (
    "day",
    "when",
    "token",
    "length",
    "color",
    "owner",
    "days",
    "lengths",
)

# Values that fields read, fail to decode, or decode but fail to encode.
_FRAGILE_LEAVES = st.one_of(
    _JSON_SCALARS,
    st.sampled_from(
        [
            "2000-01-01",
            "2024-02-30",
            "2024-01-01",
            "2024-01-01T00:00:00Z",
            "red",
            "P1D",
            "1:30",
            str(uuid.UUID(int=1)),
            "x",
        ]
    ),
)
_FRAGILE_VALUES = st.one_of(
    _FRAGILE_LEAVES,
    st.lists(_FRAGILE_LEAVES, max_size=3),
    st.dictionaries(st.text(max_size=2), _FRAGILE_LEAVES, max_size=2),
)


@settings(max_examples=200)
@given(field=st.sampled_from(_FRAGILE_FIELDS), value=_FRAGILE_VALUES)
def test_failed_read_leaves_no_cached_value(field: str, value: Any) -> None:
    """A read that fails caches nothing, and fails again the next time.

    A value is cached, with the snapshot that detects changes to it, only
    once it has been decoded and, if it can be changed in place, encoded.
    Either step can fail, and then the field's next read must fail the same
    way rather than return a value that ``save()`` would mishandle.

    Args:
        field: Name of the ``Fragile`` field to read.
        value: JSON value of the field.
    """
    obj = Fragile(json={"id": 1, "name": "n", field: value})

    try:
        getattr(obj, field)
    except FieldConversionError:
        pass
    else:
        assert field in obj._field_cache
        return

    assert field not in obj._field_cache
    assert field not in obj._field_snapshots
    with pytest.raises(
        FieldConversionError, match=re.escape(f"Fragile.{field}")
    ):
        getattr(obj, field)
    assert obj.name == "n"
    assert obj._pending_updates == {}


def _from_hex(value: Any) -> int:
    """Read an integer from hexadecimal text, as a custom Pydantic type does.

    Args:
        value: Input value.

    Returns:
        The integer that ``value`` writes.

    Raises:
        ValueError: If ``value`` is not hexadecimal text.
    """
    if not isinstance(value, str):
        raise ValueError("Expected hexadecimal text")  # noqa: TRY004
    return int(value, 16)


if HAVE_PYDANTIC:
    from pydantic import Base64Bytes, PlainSerializer, PlainValidator

    @json_object
    class Encoded(PropertyBase):
        """Model whose Pydantic fields write values in other forms."""

        class_url = "/encoded/"

        id: int
        text: PydanticJson[dict[str, int]]
        blob: Base64Bytes
        hexed: Annotated[int, PlainValidator(_from_hex), PlainSerializer(hex)]


ENCODED_JSON = {"id": 1, "text": '{"a": 1}', "blob": "AA==", "hexed": "0x1"}

# Values assigned to Encoded's fields, each of the field's type or not.
_ENCODED_VALUES = {
    "text": st.one_of(
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=2), st.text(max_size=2), max_size=2),
        st.integers(),
    ),
    "blob": st.one_of(st.binary(max_size=8), st.text(max_size=3)),
    "hexed": st.one_of(
        st.integers(), st.floats(allow_nan=False), st.text(max_size=3)
    ),
}


def _of_field_type(field: str, value: Any) -> bool:
    """Return whether a value already has an ``Encoded`` field's type.

    Pydantic also accepts values of other types, such as text for bytes, and
    reads them back as the field's type instead.

    Args:
        field: Name of the ``Encoded`` field.
        value: Value assigned to it.

    Returns:
        Whether the field reads ``value`` back unchanged.
    """
    if field == "text":
        return isinstance(value, dict) and all(
            type(item) is int for item in value.values()
        )
    if field == "blob":
        return isinstance(value, bytes)
    return type(value) is int


def _saved_json(field: str, value: Any) -> Any:
    """Assign a value to a field of a loaded object, and save it.

    Args:
        field: Name of the ``Encoded`` field.
        value: Value to assign.

    Returns:
        The field's JSON in the ``PATCH`` that ``save()`` sends, or the JSON
        that the object already holds when the value encodes as it does, and
        ``save()`` sends nothing.
    """
    obj = Encoded(json=copy.deepcopy(ENCODED_JSON))
    setattr(obj, field, value)
    if not obj._pending_updates:
        return obj.json[field]
    with requests_mock.Mocker() as mock:
        patch = mock.patch(f"{BASE_URL}encoded/1/", status_code=204)
        obj.save()
        return patch.last_request.json()[field]


@pytest.mark.skipif(not HAVE_PYDANTIC, reason="Pydantic is not installed")
@settings(max_examples=150)
@given(data=st.data(), field=st.sampled_from(sorted(_ENCODED_VALUES)))
def test_accepted_pydantic_writes_read_back(
    data: st.DataObject, field: str
) -> None:
    """A value that a Pydantic field accepts reads back from what it writes.

    An assignment either raises and queues nothing, or the ``PATCH`` that
    ``save()`` sends holds JSON that another object reads, and writing the
    value read sends the same JSON again. The JSON differs from the value:
    JSON text, base64 text, or hexadecimal text. A value of the field's type
    reads back unchanged.

    Args:
        data: Hypothesis data, to draw a value for the field.
        field: Name of the ``Encoded`` field to assign.
    """
    value = data.draw(_ENCODED_VALUES[field], label="value")
    obj = Encoded(json=copy.deepcopy(ENCODED_JSON))

    try:
        setattr(obj, field, value)
    except FieldConversionError:
        assert obj._pending_updates == {}
        assert obj.json == ENCODED_JSON
        return

    sent = _saved_json(field, value)
    read = getattr(Encoded(json={**ENCODED_JSON, field: sent}), field)

    assert _saved_json(field, read) == sent
    if _of_field_type(field, value):
        assert read == value
