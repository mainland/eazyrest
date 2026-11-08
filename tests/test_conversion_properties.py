"""Hypothesis properties of conversion through nested annotations.

Each property generates an annotation tree: leaves that eazyrest converts, or
JSON scalars, inside optionals, lists, tuples, sets, mappings, and
TypedDicts, nested a few levels. It then checks a promise that every path
that reads or writes JSON keeps: writes are JSON that reads back as the
value, changes made in place are saved, queries send what writes send,
written values match the schema, and errors never quote the input.

The properties use only public API, so they hold across changes to how
conversion is implemented.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import ipaddress
import itertools
import json
import keyword
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

import jsonschema
import pytest
import requests_mock
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typing_extensions import TypedDict

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://conversion.example.com/"
URL = BASE + "values/1/"


class Shade(enum.Enum):
    """Enum with values of two JSON types."""

    DARK = "dark"
    ONE = 1


def _decode_address(value: Any, cls: type[Any]) -> Any:
    """Read an IPv4 address, without quoting a bad one in the error.

    Args:
        value: JSON value.
        cls: The declared type.

    Returns:
        The address.

    Raises:
        ValueError: If ``value`` is not an IPv4 address.
    """
    try:
        return cls(value)
    except ValueError:
        # The address's own error quotes the value.
        raise ValueError("Value is not an IPv4 address") from None


class ValueBase(JSONObject):
    """Base model of these properties, with a conversion of its own."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        ipaddress.IPv4Address: Conversion(
            decode=_decode_address,
            encode=lambda value, cls: str(value),
            schema={"type": "string", "format": "ipv4"},
        )
    }


ValueBase.register_api(API(BASE))

# Offsets that ISO 8601 can write: whole minutes, under a day.
_OFFSETS = st.integers(min_value=-(24 * 60 - 1), max_value=24 * 60 - 1).map(
    lambda minutes: datetime.timezone(datetime.timedelta(minutes=minutes))
)


@dataclass(frozen=True)
class Node:
    """A generated annotation, with what the properties need to know of it.

    Attributes:
        annotation: The field annotation.
        values: Values of the annotation.
        kind: ``"leaf"``, ``"optional"``, ``"list"``, ``"tuple"``, ``"set"``,
            ``"dict"``, or ``"typeddict"``.
        hashable: Whether its values can be set members.
        query: Whether a query can send its values, which are then scalars.
        children: The item node of a container, or the key nodes of a
            TypedDict.
        bad: JSON values that the annotation cannot read, as a strategy over
            the input marker, or ``None`` if every JSON value reads.
    """

    annotation: Any
    values: st.SearchStrategy[Any]
    kind: str = "leaf"
    hashable: bool = True
    query: bool = True
    children: tuple[tuple[str, Node], ...] = ()
    bad: Callable[[str], st.SearchStrategy[Any]] | None = field(
        default=None, compare=False
    )


def _leaf(
    annotation: Any,
    values: st.SearchStrategy[Any],
    bad: Callable[[str], st.SearchStrategy[Any]] | None = None,
) -> Node:
    """Return a leaf node.

    Args:
        annotation: The leaf type.
        values: Its values.
        bad: Strategy over the input marker for JSON values it rejects.

    Returns:
        The node.
    """
    return Node(annotation, values, bad=bad)


def _marked(marker: str) -> st.SearchStrategy[Any]:
    """Return a string that no conversion reads, holding the marker.

    Args:
        marker: Text that an error message must not quote.

    Returns:
        A strategy for the string.
    """
    return st.just(f"not {marker} valid")


# A union of members that pass through unchanged.
_UNION: Any = int | str

LEAVES = [
    _leaf(int, st.integers(min_value=-(2**53), max_value=2**53)),
    _leaf(str, st.text(max_size=8)),
    _leaf(bool, st.booleans()),
    _leaf(float, st.floats(allow_nan=False, allow_infinity=False)),
    _leaf(
        datetime.datetime,
        st.datetimes(timezones=st.none() | _OFFSETS),
        _marked,
    ),
    _leaf(datetime.date, st.dates(), _marked),
    _leaf(datetime.time, st.times(timezones=st.none() | _OFFSETS), _marked),
    _leaf(
        datetime.timedelta,
        st.timedeltas(
            min_value=-datetime.timedelta(days=10**6),
            max_value=datetime.timedelta(days=10**6),
        ),
        _marked,
    ),
    _leaf(uuid.UUID, st.uuids(), _marked),
    _leaf(
        decimal.Decimal,
        st.decimals(
            min_value=-(10**12),
            max_value=10**12,
            places=4,
            allow_nan=False,
            allow_infinity=False,
        ),
        _marked,
    ),
    _leaf(Shade, st.sampled_from(Shade), _marked),
    _leaf(ipaddress.IPv4Address, st.ip_addresses(v=4), _marked),
    _leaf(
        _UNION,
        st.integers(min_value=-(2**53), max_value=2**53) | st.text(max_size=8),
    ),
]

_TYPED_DICT_NAMES = (f"Generated{n}" for n in itertools.count())
_KEYS = st.text(alphabet="abcdefgh", min_size=1, max_size=4)


def _optional(node: Node) -> Node:
    """Return the node for ``T | None``.

    Args:
        node: The node for ``T``.

    Returns:
        The optional node.
    """
    return Node(
        _OPTIONAL[node.annotation],
        st.none() | node.values,
        "optional",
        node.hashable,
        False,
        (("", node),),
        node.bad,
    )


# Annotations built from generated types are not types to mypy.
_OPTIONAL: Any = Optional
_LIST: Any = list
_TUPLE: Any = tuple
_FROZENSET: Any = frozenset
_DICT: Any = dict
_TYPED_DICT: Any = TypedDict
_NAMED_TUPLE: Any = NamedTuple

Bad = Callable[[str], st.SearchStrategy[Any]]


def _bad_items(bad: Bad | None) -> Bad | None:
    """Return a strategy for lists that hold at least one bad item.

    Args:
        bad: Strategy over the marker for one bad item, or ``None``.

    Returns:
        The strategy over the marker, or ``None`` if items cannot be bad.
    """
    if bad is None:
        return None
    return lambda marker: st.lists(bad(marker), min_size=1, max_size=2)


def _bad_values(bad: Bad | None) -> Bad | None:
    """Return a strategy for mappings whose value, under a chosen key, is bad.

    The key holds the marker too, because the input chooses it.

    Args:
        bad: Strategy over the marker for one bad value, or ``None``.

    Returns:
        The strategy over the marker, or ``None`` if values cannot be bad.
    """
    if bad is None:
        return None
    return lambda marker: st.dictionaries(
        st.just(f"key-{marker}"), bad(marker), min_size=1
    )


def _bad_key(key: str, bad: Bad) -> Bad:
    """Return a strategy for TypedDict values whose declared key is bad.

    Args:
        key: The declared key.
        bad: Strategy over the marker for its bad value.

    Returns:
        The strategy over the marker.
    """
    return lambda marker: st.fixed_dictionaries({key: bad(marker)})


def _container(kind: str, node: Node) -> Node:
    """Return the node for a container of ``node``'s values.

    Args:
        kind: ``"list"``, ``"tuple"``, ``"set"``, or ``"dict"``.
        node: The item node.

    Returns:
        The container node.
    """
    children = (("", node),)
    if kind == "list":
        return Node(
            _LIST[node.annotation],
            st.lists(node.values, max_size=3),
            kind,
            False,
            node.kind == "leaf",
            children,
            _bad_items(node.bad),
        )
    if kind == "tuple":
        return Node(
            _TUPLE[node.annotation, ...],
            st.lists(node.values, max_size=3).map(tuple),
            kind,
            node.hashable,
            False,
            children,
            _bad_items(node.bad),
        )
    if kind == "set":
        return Node(
            _FROZENSET[node.annotation],
            st.frozensets(node.values, max_size=3),
            kind,
            True,
            False,
            children,
        )
    return Node(
        _DICT[str, node.annotation],
        st.dictionaries(_KEYS, node.values, max_size=3),
        kind,
        False,
        False,
        children,
        _bad_values(node.bad),
    )


@st.composite
def _typed_dict(draw: st.DrawFn, children: st.SearchStrategy[Node]) -> Node:
    """Draw a TypedDict node with up to three keys.

    Args:
        draw: Hypothesis's draw function.
        children: Strategy for the key nodes.

    Returns:
        The node, whose values may leave keys out.
    """
    keys = draw(st.lists(_KEYS, min_size=1, max_size=3, unique=True))
    nodes = tuple((key, draw(children)) for key in keys)
    cls = _TYPED_DICT(
        next(_TYPED_DICT_NAMES),
        {key: node.annotation for key, node in nodes},
        total=False,
    )
    values = st.fixed_dictionaries(
        {}, optional={key: node.values for key, node in nodes}
    )
    bad = next(
        (_bad_key(key, node.bad) for key, node in nodes if node.bad), None
    )
    return Node(cls, values, "typeddict", False, False, nodes, bad)


_STRUCTURE_NAMES = (f"Structure{n}" for n in itertools.count())


@st.composite
def _positional(
    draw: st.DrawFn, children: st.SearchStrategy[Node], kind: str
) -> Node:
    """Draw a fixed-length tuple or a NamedTuple of one to three items.

    Args:
        draw: Hypothesis's draw function.
        children: Strategy for the item nodes.
        kind: ``"fixed"`` or ``"namedtuple"``.

    Returns:
        The node.
    """
    nodes = tuple(
        (f"f{index}", draw(children))
        for index in range(draw(st.integers(min_value=1, max_value=3)))
    )
    items = st.tuples(*(node.values for _, node in nodes))
    hashable = all(node.hashable for _, node in nodes)
    if kind == "fixed":
        annotation = _TUPLE[tuple(node.annotation for _, node in nodes)]
        return Node(annotation, items, kind, hashable, False, nodes)
    cls = _NAMED_TUPLE(
        next(_STRUCTURE_NAMES), [(key, node.annotation) for key, node in nodes]
    )
    return Node(
        cls, items.map(lambda item: cls(*item)), kind, hashable, False, nodes
    )


@st.composite
def _dataclass(draw: st.DrawFn, children: st.SearchStrategy[Node]) -> Node:
    """Draw a dataclass of one to three fields.

    Args:
        draw: Hypothesis's draw function.
        children: Strategy for the field nodes.

    Returns:
        The node.
    """
    # A field's name must not be a keyword, such as "def".
    names = _KEYS.filter(lambda key: not keyword.iskeyword(key))
    keys = draw(st.lists(names, min_size=1, max_size=3, unique=True))
    nodes = tuple((key, draw(children)) for key in keys)
    cls = dataclasses.make_dataclass(
        next(_STRUCTURE_NAMES), [(key, node.annotation) for key, node in nodes]
    )
    values = st.fixed_dictionaries(
        {key: node.values for key, node in nodes}
    ).map(lambda fields: cls(**fields))
    return Node(cls, values, "dataclass", False, False, nodes)


def _extend(children: st.SearchStrategy[Node]) -> st.SearchStrategy[Node]:
    """Return nodes one level deeper than ``children``.

    Args:
        children: Strategy for the nodes inside.

    Returns:
        Strategy for containers of them.
    """
    return st.one_of(
        children.map(_optional),
        children.map(lambda node: _container("list", node)),
        children.map(lambda node: _container("tuple", node)),
        children.filter(lambda node: node.hashable).map(
            lambda node: _container("set", node)
        ),
        children.map(lambda node: _container("dict", node)),
        _typed_dict(children),
        _positional(children, "fixed"),
        _positional(children, "namedtuple"),
        _dataclass(children),
    )


NODES = st.recursive(st.sampled_from(LEAVES), _extend, max_leaves=5)


def _encode(value: Any, annotation: Any) -> Any:
    """Return the JSON that a field of these models writes for a value.

    Args:
        value: The value.
        annotation: Its annotation.

    Returns:
        Its JSON, by the base model's conversion table.
    """
    return encode_json(value, annotation, model=ValueBase)


def _model(annotation: Any) -> Any:
    """Return a model with one field of the generated annotation.

    Args:
        annotation: Annotation of the ``value`` field.

    Returns:
        The model, at ``/values/``.
    """
    namespace = {
        "__annotations__": {"id": int, "value": annotation},
        "class_url": "/values/",
        "__module__": __name__,
    }
    return json_object(type("Generated", (ValueBase,), namespace))


_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@_SETTINGS
@given(st.data())
def test_writes_are_json_that_reads_back(data: st.DataObject) -> None:
    """A value writes as JSON, and that JSON reads back as the value.

    The value read writes JSON that reads back as the same value. The JSON
    can differ in the order of a set's items, which has no meaning, and
    in-place change detection compares one object's encodings, which keep
    one order.
    """
    node = data.draw(NODES)
    value = data.draw(node.values)
    model = _model(node.annotation)

    encoded = _encode(value, node.annotation)
    json.dumps(encoded, allow_nan=False)
    read = model(json={"id": 1, "value": encoded}).value

    assert read == value
    rewritten = _encode(read, node.annotation)
    assert model(json={"id": 1, "value": rewritten}).value == value


@_SETTINGS
@given(st.data())
def test_field_writes_send_what_encode_json_writes(
    data: st.DataObject,
) -> None:
    """Assigning a field and saving sends the value's JSON, and only it.

    The field starts as ``null``, so assigning ``None`` changes nothing and
    sends nothing.
    """
    node = data.draw(NODES)
    value = data.draw(node.values)
    model = _model(node.annotation)
    obj = model(json={"id": 1, "value": None})

    with requests_mock.Mocker() as mock:
        patch = mock.patch(URL, status_code=204)
        obj.value = value
        obj.save()

    if value is None:
        assert not patch.called
    else:
        assert patch.last_request.json() == {
            "value": _encode(value, node.annotation)
        }


def _mutable_paths(value: Any, node: Node) -> list[tuple[Any, Node]]:
    """Return the mutable containers inside a value, with their nodes.

    Args:
        value: Value of ``node``'s annotation.
        node: Its node.

    Returns:
        Each list, dict, TypedDict, or dataclass value at any depth, with
        the node that describes it. Tuples, NamedTuples, and frozensets
        cannot change, but what they hold can.
    """
    if value is None:
        return []
    found: list[tuple[Any, Node]] = []
    if node.kind == "optional":
        return _mutable_paths(value, node.children[0][1])
    if node.kind in ("list", "dict", "typeddict", "dataclass"):
        found.append((value, node))
    if node.kind in ("list", "tuple"):
        for item in value:
            found += _mutable_paths(item, node.children[0][1])
    elif node.kind in ("fixed", "namedtuple"):
        for item, (_, item_node) in zip(value, node.children, strict=True):
            found += _mutable_paths(item, item_node)
    elif node.kind == "dataclass":
        for key, item_node in node.children:
            found += _mutable_paths(getattr(value, key), item_node)
    elif node.kind == "dict":
        for item in value.values():
            found += _mutable_paths(item, node.children[0][1])
    elif node.kind == "typeddict":
        keys = dict(node.children)
        for key, item in value.items():
            found += _mutable_paths(item, keys[key])
    return found


@_SETTINGS
@given(st.data())
def test_changes_made_in_place_are_saved(data: st.DataObject) -> None:
    """A change made in place at any depth is saved, and nothing else is.

    Reading a value and saving without changing it sends nothing.
    """
    node = data.draw(NODES)
    value = data.draw(node.values)
    model = _model(node.annotation)
    encoded = _encode(value, node.annotation)
    obj = model(json={"id": 1, "value": encoded})
    read = obj.value

    with requests_mock.Mocker() as mock:
        patch = mock.patch(URL, status_code=204)
        obj.save()
        assert not patch.called

        paths = _mutable_paths(read, node)
        if not paths:
            return
        container, container_node = data.draw(st.sampled_from(paths))
        if container_node.kind == "list":
            item = container_node.children[0][1]
            container.append(data.draw(item.values))
        elif container_node.kind == "dict":
            item = container_node.children[0][1]
            container[data.draw(_KEYS)] = data.draw(item.values)
        elif container_node.kind == "dataclass":
            key, item = data.draw(st.sampled_from(container_node.children))
            setattr(container, key, data.draw(item.values))
        else:
            key, item = data.draw(st.sampled_from(container_node.children))
            container[key] = data.draw(item.values)
        changed = _encode(read, node.annotation)
        obj.save()

    if changed == encoded:
        assert not patch.called
    else:
        assert patch.last_request.json() == {"value": changed}


def _query_text(item: Any) -> str:
    """Return the text that a query sends for one encoded scalar.

    Args:
        item: A JSON scalar.

    Returns:
        ``true`` or ``false`` for a boolean, as JSON writes it, and the
        scalar's text otherwise.
    """
    if isinstance(item, bool):
        return "true" if item else "false"
    return str(item)


_QUERY_NODES = st.one_of(
    st.sampled_from(LEAVES),
    st.sampled_from(LEAVES).map(lambda node: _container("list", node)),
)


@_SETTINGS
@given(st.data())
def test_queries_send_what_writes_send(data: st.DataObject) -> None:
    """A query on a field sends the value's JSON, one parameter per item."""
    node = data.draw(_QUERY_NODES)
    value = data.draw(
        node.values.filter(lambda value: value != [])
        if node.kind == "list"
        else node.values
    )
    model = _model(node.annotation)
    encoded = _encode(value, node.annotation)
    items = encoded if isinstance(encoded, list) else [encoded]

    with requests_mock.Mocker() as mock:
        get = mock.get(BASE + "values/", json=[])
        list(model.filter(value=value))

    sent = parse_qs(
        urlparse(get.last_request.url).query, keep_blank_values=True
    )
    assert sent == {"value": [_query_text(item) for item in items]}


@_SETTINGS
@given(st.data())
def test_written_values_match_the_schema(data: st.DataObject) -> None:
    """The JSON that a field writes is valid under its read and write schemas.

    Formats are not checked, since they are annotations in the JSON schema
    vocabulary eazyrest uses, but every structural constraint is.
    """
    node = data.draw(NODES)
    value = data.draw(node.values)
    model = _model(node.annotation)
    instance = {"id": 1, "value": _encode(value, node.annotation)}

    for mode in ("read", "write"):
        jsonschema.validate(instance, model.json_schema(mode))


_BAD_NODES = NODES.filter(lambda node: node.bad is not None)


@_SETTINGS
@given(st.data(), st.text(alphabet="XYZ0123456789", min_size=6, max_size=10))
def test_errors_never_quote_the_input(
    data: st.DataObject, marker: str
) -> None:
    """An unreadable value raises without quoting it or the keys it chose.

    The input holds the marker in a value that cannot be read, and in the
    key of a mapping, which the input chooses. The error's message names the
    model, the field, and the declared keys, but quotes neither.
    """
    node = data.draw(_BAD_NODES)
    assert node.bad is not None
    bad = data.draw(node.bad(marker))
    model = _model(node.annotation)
    obj = model(json={"id": 1, "value": bad})

    with pytest.raises(FieldConversionError) as info:
        _ = obj.value

    assert marker not in str(info.value)
    assert str(info.value).startswith("Generated.value: ")
