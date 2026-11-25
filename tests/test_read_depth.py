"""A read and a comparison take any depth that decoded.

``API.max_json_depth`` limits what a response may nest. With no limit, or one
that is raised, or for JSON that a caller gives, a field nests as deeply as its
value does, and nothing that reads it counts the depth or recurses past the
stack.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://read-depth.example/"
api = API(BASE)
api.max_json_depth = None


class Payload(TypedDict):
    """A ``TypedDict`` with a member that keeps its JSON as it is."""

    inner: Any


@dataclasses.dataclass
class Box:
    """An embedded dataclass with a field that keeps its JSON as it is."""

    inner: Any = None


@json_object
class Wide(JSONObject):
    """A model with a field of each type that holds a value of any depth."""

    class_url = "/wide/"
    id: int
    anything: Any
    rows: list[Any]
    table: dict[str, Any]
    typed: Payload
    box: Box


Wide.register_api(api)

START: dict[str, Any] = {
    "id": 1,
    "anything": 0,
    "rows": [],
    "table": {},
    "typed": {"inner": 0},
    "box": {"inner": 0},
}


def nested(depth: int) -> Any:
    """Return lists nested to a given depth, a flat list having depth 1."""
    value: Any = []
    for _ in range(depth - 1):
        value = [value]
    return value


def body(depth: int) -> dict[str, Any]:
    """Return the JSON of an object whose fields nest ``depth`` levels."""
    inner = nested(depth - 1)
    return {
        "id": 1,
        "anything": nested(depth),
        "rows": [inner],
        "table": {"key": inner},
        "typed": {"inner": inner},
        "box": {"inner": inner},
    }


def assert_reads(obj: Wide, depth: int) -> None:
    """Check that every field reads the value of ``body(depth)``."""
    inner = nested(depth - 1)
    assert obj.anything == nested(depth)
    assert obj.rows == [inner]
    assert obj.table == {"key": inner}
    assert obj.typed == {"inner": inner}
    assert obj.box == Box(inner)
    assert hasattr(obj, "anything")


def read_fields(obj: Wide) -> Wide:
    """Read every field of an object, so that it holds converted values."""
    for name in ("anything", "rows", "table", "typed", "box"):
        getattr(obj, name)
    return obj


def from_json(depth: int) -> Wide:
    """Make an object from JSON that a caller gives."""
    return Wide(json=body(depth))


def updated(depth: int) -> Wide:
    """Replace the JSON of an object with ``update_from_json()``."""
    obj = Wide(json=dict(START))
    obj.update_from_json(body(depth))
    return obj


def updated_twice(depth: int) -> Wide:
    """Apply the same JSON twice, so that the second finds nothing changed."""
    obj = updated(depth)
    obj.update_from_json(body(depth))
    return obj


def loaded(depth: int) -> Wide:
    """Load an object from a response."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "wide/1/", json=body(depth))
        return read_fields(Wide(id=1))


def refreshed(depth: int) -> Wide:
    """Refresh an object from a response."""
    obj = read_fields(Wide(json=dict(START)))
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "wide/1/", json=body(depth))
        obj.refresh()
        return read_fields(obj)


def created(depth: int) -> Wide:
    """Create an object whose response holds the fields."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "wide/", json=body(depth))
        return Wide.create(anything=1)


def saved_in_full(depth: int) -> Wide:
    """Save an object whose fields were read, with every field sent."""
    obj = read_fields(from_json(depth))
    with requests_mock.Mocker() as mocker:
        mocker.put(
            BASE + "wide/1/", json=lambda request, context: request.json()
        )
        obj.save(full=True)
        assert mocker.last_request.json() == body(depth)
    return obj


READS: dict[str, Any] = {
    "json=": from_json,
    "update_from_json()": updated,
    "update_from_json() of the same JSON": updated_twice,
    "a load": loaded,
    "refresh()": refreshed,
    "a creation's response": created,
    "save(full=True)": saved_in_full,
}


@pytest.mark.parametrize("depth", [129, 600])
@pytest.mark.parametrize("read", READS.values(), ids=READS.keys())
def test_a_field_deeper_than_a_body_may_be_reads(
    read: Any, depth: int
) -> None:
    """Every field reads, and has no error to report."""
    assert_reads(read(depth), depth)


@pytest.mark.parametrize("depth", [129, 600])
def test_a_save_after_reading_a_deep_field_sends_what_changed(
    depth: int,
) -> None:
    """An unchanged field is not written, so its depth is not counted."""
    obj = read_fields(from_json(depth))
    obj.id  # noqa: B018
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "wide/1/", json={})
        obj.save()
        assert mocker.call_count == 0
        obj.anything = [1]
        obj.save()
        assert mocker.last_request.json() == {"anything": [1]}


def test_json_that_holds_itself_is_copied_once() -> None:
    """An object's JSON ends where its deep value holds itself."""
    outer: list[Any] = []
    value = outer
    for _ in range(600):
        value.append([])
        value = value[0]
    value.append(outer)
    obj = Wide(json={**START, "anything": outer})
    copied = obj.json
    assert copied["anything"] is not outer
    loop = copied["anything"]
    for _ in range(601):
        loop = loop[0]
    assert loop is copied["anything"]


@pytest.mark.parametrize("depth", [3, 300])
@pytest.mark.parametrize("field", ["rows", "typed", "box"])
def test_a_query_refuses_a_structured_value_at_any_depth_alike(
    field: str, depth: int
) -> None:
    """A query's value is no body, so its depth is not counted."""
    inner = nested(depth)
    value: dict[str, Any] = {
        "rows": [inner],
        "typed": {"inner": inner},
        "box": Box(inner),
    }
    with pytest.raises(ValueError, match="cannot be sent") as caught:
        list(Wide.filter(params={field: value[field]}))
    assert not isinstance(caught.value, FieldConversionError)


def test_an_assignment_of_a_deep_value_needs_little_stack() -> None:
    """Queuing an update does not recurse once for each level of the value."""
    obj = Wide(json=dict(START))
    value = nested(127)

    def depth() -> int:
        frame: Any = sys._getframe()
        count = 0
        while frame is not None:
            count += 1
            frame = frame.f_back
        return count

    def assign(frames: int) -> None:
        if frames:
            assign(frames - 1)
        else:
            obj.anything = value

    assign(sys.getrecursionlimit() - depth() - 120)
    pending = obj._pending_updates["anything"]
    assert pending == value
    assert pending is not value
