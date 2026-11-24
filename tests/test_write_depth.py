"""A written value nests at most 127 levels, so that a body nests at most 128.

What a field or a request writes is copied, compared, and sent by steps that
recurse, so a value that is deep or holds itself is refused where it is
written, by one count that every later step can take, instead of raising a bare
``RecursionError`` from one of them. A body that holds the value nests one
level more, and 128 levels is the default ``max_json_depth``, so what is
written can be read back from a server that echoes it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://write-depth.example/"
api = API(BASE)


@json_object
class Holder(JSONObject):
    """A model with a field that keeps its JSON as it is."""

    class_url = "/holders/"
    id: int
    value: Any


Holder.register_api(api)


@json_object(write_only={"secret"})
class Vault(JSONObject):
    """A model with a write-only field that keeps its JSON as it is."""

    class_url = "/vaults/"
    id: int
    secret: Any


Vault.register_api(api)


class Payload(TypedDict):
    """A ``TypedDict`` with a member that keeps its JSON as it is."""

    inner: Any


@dataclasses.dataclass
class Box:
    """An embedded dataclass with a field that keeps its JSON as it is."""

    inner: Any = None


@json_object
class Doc(JSONObject):
    """A model with a field of each type that holds a value of any depth."""

    class_url = "/docs/"
    id: int
    anything: Any
    rows: list[Any]
    table: dict[str, Any]
    typed: Payload
    box: Box


Doc.register_api(api)

FIELDS = ["anything", "rows", "table", "typed", "box"]
ANNOTATIONS: dict[str, Any] = {
    "anything": Any,
    "rows": list[Any],
    "table": dict[str, Any],
    "typed": Payload,
    "box": Box,
}
START = {
    "id": 1,
    "anything": [],
    "rows": [],
    "table": {},
    "typed": {"inner": 0},
    "box": {"inner": 0},
}


class Chain(TypedDict, total=False):
    """A recursive ``TypedDict``."""

    next: Chain | None


@dataclasses.dataclass
class Link:
    """A recursive dataclass."""

    next: Link | None = None


class Node(NamedTuple):
    """A recursive ``NamedTuple``."""

    next: Node | None = None


def nested(depth: int) -> Any:
    """Return lists nested to a given depth, a flat list having depth 1."""
    value: Any = []
    for _ in range(depth - 1):
        value = [value]
    return value


def cyclic(kind: type[Any]) -> Any:
    """Return a value of a recursive structure that holds itself."""
    if kind is Chain:
        chain: Any = {}
        chain["next"] = chain
        return chain
    link = Link()
    link.next = link
    return link


def value_of(field: str, depth: int) -> Any:
    """Return a value of a field of ``Doc`` whose encoding nests ``depth``."""
    inner = nested(depth - 1)
    match field:
        case "anything":
            return nested(depth)
        case "rows":
            return [inner]
        case "table":
            return {"key": inner}
        case "typed":
            return {"inner": inner}
        case _:
            return Box(inner)


def echoing(mocker: Any) -> None:
    """Make every write answer with the object that its body describes."""

    def echo(request: Any, context: Any) -> Any:
        return {"id": 1, **request.json()}

    mocker.post(BASE + "docs/", json=echo)
    mocker.patch(BASE + "docs/1/", json=echo)
    mocker.get(BASE + "docs/", json=[])


def doc(**options: Any) -> Doc:
    """Return a document that has read its fields."""
    return Doc(json=dict(START), **options)


def assign(field: str, value: Any, mode: str) -> Doc:
    """Assign a value to a document's field and write it."""
    obj = doc(write_mode=mode)
    setattr(obj, field, value)
    if mode == "lazy":
        obj.save()
    return obj


def change_in_place(field: str, value: Any) -> Doc:
    """Change a field's value in place to ``value`` and save."""
    obj = doc()
    current = getattr(obj, field)
    match field:
        case "anything" | "rows":
            current[:] = value
        case "table" | "typed":
            current.update(value)
        case _:
            current.inner = value.inner
    obj.save()
    return obj


def holder() -> Holder:
    """Return a holder that has read its value."""
    return Holder(json={"id": 1, "value": 0})


# Each writes a value to a field of ``Doc`` and returns what the request held,
# or raises. The first three leave the request to the object, as they send it
# by ``save()``, an eager assignment, and ``save()`` after a change in place.
WRITES: dict[str, Any] = {
    "assignment": lambda f, v: assign(f, v, "lazy"),
    "eager assignment": lambda f, v: assign(f, v, "eager"),
    "change in place": change_in_place,
    "create": lambda f, v: Doc.create(**{f: v}),
    "get_or_create": lambda f, v: Doc.get_or_create(id=2, defaults={f: v})[0],
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
@pytest.mark.parametrize("field", FIELDS)
def test_a_field_takes_127_levels_and_refuses_128(
    write: Any, field: str
) -> None:
    """A body holds 127 levels and one more, as a server's echo reads back.

    The refusal names the field and comes before any request.
    """
    value = value_of(field, 127)
    with requests_mock.Mocker() as mocker:
        echoing(mocker)
        obj = write(field, value)
        assert mocker.last_request.json()[field] == obj.json[field]
        assert getattr(obj, field) == value
    with requests_mock.Mocker() as mocker:
        echoing(mocker)
        with pytest.raises(FieldConversionError, match=rf"Doc\.{field}"):
            write(field, value_of(field, 128))
        assert mocker.call_count == 0


@pytest.mark.parametrize("mode", ["lazy", "eager"])
def test_a_refused_assignment_leaves_the_field_and_the_queue(
    mode: Any,
) -> None:
    """A value of 128 levels changes nothing."""
    obj = doc(write_mode=mode)
    with requests_mock.Mocker() as mocker:
        with pytest.raises(FieldConversionError):
            obj.anything = nested(128)
        assert mocker.call_count == 0
    assert obj.anything == []
    assert obj._pending_updates == {}


EXTRAS: dict[str, Any] = {
    "create": lambda v: Doc.create(extra=v),
    "get_or_create": lambda v: Doc.get_or_create(id=2, defaults={"extra": v}),
}


@pytest.mark.parametrize("write", EXTRAS.values(), ids=EXTRAS.keys())
def test_an_extra_takes_127_levels_and_refuses_128(write: Any) -> None:
    """The echo of 127 levels reads, and the refusal names the extra."""
    value = nested(127)
    with requests_mock.Mocker() as mocker:
        echoing(mocker)
        write(value)
        assert mocker.last_request.json()["extra"] == value
    with requests_mock.Mocker() as mocker:
        echoing(mocker)
        with pytest.raises(ValueError, match=r"value at \[key\] holds"):
            write(nested(128))
        assert mocker.call_count == 0


@pytest.mark.parametrize("field", FIELDS)
def test_encode_json_takes_127_levels_and_refuses_128(field: str) -> None:
    """The value may become a field of a body, so it has the field's bound."""
    annotation = ANNOTATIONS[field]
    value = value_of(field, 127)
    assert encode_json(value, annotation) is not None
    with pytest.raises(ValueError, match="nests too deeply"):
        encode_json(value_of(field, 128), annotation)


def test_a_save_ignores_an_unchanged_value_too_deep_to_write() -> None:
    """A value that was read and not changed is not written."""
    deep = API(BASE + "unlimited/")
    deep.max_json_depth = None

    @json_object
    class Wide(JSONObject):
        """A model whose field holds a value of any depth."""

        class_url = "/wide/"
        id: int
        value: Any
        name: str

    Wide.register_api(deep)
    obj = Wide(json={"id": 1, "value": nested(300), "name": "a"})
    assert obj.value == nested(300)
    obj.name = "b"
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "unlimited/wide/1/", json={})
        obj.save()
    assert mocker.last_request.json() == {"name": "b"}


@pytest.mark.parametrize("depth", [129, 300, 500, 990, 5000])
def test_a_deeper_value_is_refused_by_encode_json(depth: int) -> None:
    """The error is a ``ValueError``, whatever the recursion limit is."""
    with pytest.raises(ValueError, match="nests too deeply") as caught:
        encode_json(nested(depth), Any)
    assert not isinstance(caught.value, RecursionError)


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("depth", [129, 500, 990])
def test_a_deeper_value_is_refused_where_it_is_assigned(
    depth: int, mode: Any
) -> None:
    """The field raises ``FieldConversionError``, and nothing is sent."""
    obj = Holder(json={"id": 1, "value": 0}, write_mode=mode)
    with requests_mock.Mocker() as mocker:
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            obj.value = nested(depth)
        assert mocker.call_count == 0
    assert obj.value == 0
    assert obj._pending_updates == {}


def test_a_value_changed_in_place_to_be_too_deep_is_refused() -> None:
    """Every ``save()`` says so, and sends nothing."""
    obj = Holder(json={"id": 1, "value": []})
    value = obj.value
    for _ in range(500):
        value.append([])
        value = value[0]
    with requests_mock.Mocker() as mocker:
        for _ in range(2):
            with pytest.raises(FieldConversionError, match=r"Holder\.value"):
                obj.save()
        assert mocker.call_count == 0


@pytest.mark.parametrize("depth", [129, 500, 990])
def test_create_refuses_a_deep_value_before_its_request(depth: int) -> None:
    """No resource is made that the caller then cannot reach."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            Holder.create(value=nested(depth))
        with pytest.raises(FieldConversionError, match=r"Holder\.value"):
            Holder.get_or_create(id=1, defaults={"value": nested(depth)})
        assert mocker.call_count == 0


def test_create_copies_its_payload_before_the_request() -> None:
    """A value that cannot be copied is refused while nothing exists."""

    class Uncopyable(dict[str, Any]):
        """A JSON object that refuses to be copied."""

        def __deepcopy__(self, memo: Any) -> Any:
            raise RuntimeError("cannot copy")

    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "holders/", json={"id": 1, "value": {}})
        with pytest.raises(RuntimeError, match="cannot copy"):
            Holder.create(value=Uncopyable())
        assert mocker.call_count == 0


@pytest.mark.parametrize("kind", [Chain, Link])
def test_a_value_that_holds_itself_is_refused_as_too_deep(kind: Any) -> None:
    """The error does not blame the annotations, from any depth of stack."""
    value = cyclic(kind)

    def deeper(frames: int) -> None:
        if frames:
            deeper(frames - 1)
        else:
            encode_json(value, kind)

    for frames in (0, 60):
        with pytest.raises(ValueError, match="nests too deeply") as caught:
            deeper(frames)
        assert "cannot be resolved" not in str(caught.value)


@pytest.mark.parametrize("kind", [Chain, Link, Node])
@pytest.mark.parametrize("depth", [129, 200, 400])
def test_a_deep_chain_of_each_recursive_structure_is_refused(
    kind: Any, depth: int
) -> None:
    """A chain with no cycle is refused by its depth."""
    value: Any = None
    for _ in range(depth):
        value = {"next": value} if kind is Chain else kind(next=value)
    with pytest.raises(ValueError, match="nests too deeply"):
        encode_json(value, kind | None)


@pytest.mark.parametrize("kind", [Chain, Link])
def test_a_field_refuses_a_value_that_holds_itself(kind: Any) -> None:
    """The field raises ``FieldConversionError`` from any depth of stack."""
    model: Any = json_object(
        type(
            "Cycle",
            (JSONObject,),
            {
                "__annotations__": {"id": int, "value": kind | None},
                "class_url": "/cycles/",
            },
        )
    )
    obj = model(json={"id": 1, "value": None})
    value = cyclic(kind)

    def deeper(frames: int) -> None:
        if frames:
            deeper(frames - 1)
        else:
            obj.value = value

    for frames in (0, 60):
        with pytest.raises(FieldConversionError) as caught:
            deeper(frames)
        assert "cannot be resolved" not in str(caught.value)
        assert "nests too deeply" in str(caught.value)


def test_a_write_only_field_takes_127_levels_and_refuses_128() -> None:
    """A write-only field has the bound of every other field."""
    vault = Vault(json={"id": 1})
    vault.secret = nested(127)
    with pytest.raises(FieldConversionError, match=r"Vault\.secret"):
        vault.secret = nested(128)
    assert vault._pending_updates == {"secret": nested(127)}


def test_a_container_that_a_value_holds_twice_is_no_cycle() -> None:
    """Sharing a list is not holding oneself."""
    shared = [1]
    value = [shared, shared, {"again": shared}]
    assert encode_json(value, Any) == [[1], [1], {"again": [1]}]
    obj = holder()
    obj.value = value
    assert obj.value == [[1], [1], {"again": [1]}]


class CountedList(list[Any]):
    """A list that fails the test when it is iterated too many times."""

    walks = 0

    def __iter__(self) -> Iterator[Any]:
        """Count the walk, and give up on one that does not end."""
        type(self).walks += 1
        if type(self).walks > 1000:
            pytest.fail("The walk of a value that holds itself did not end")
        return super().__iter__()


class CountedDict(dict[str, Any]):
    """A dict that fails the test when its items are read too many times."""

    walks = 0

    def items(self) -> Any:
        """Count the walk, and give up on one that does not end."""
        type(self).walks += 1
        if type(self).walks > 1000:
            pytest.fail("The walk of a value that holds itself did not end")
        return super().items()


@pytest.mark.parametrize("kind", [CountedList, CountedDict])
def test_the_walk_of_a_value_that_holds_itself_ends(kind: Any) -> None:
    """A container that is met again while it is open ends the walk."""
    kind.walks = 0
    value = kind()
    if kind is CountedList:
        value.append(value)
    else:
        value["self"] = value
    with pytest.raises(ValueError, match="holds itself"):
        encode_json(value, Any)
    assert kind.walks == 1
