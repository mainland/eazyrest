"""One count of JSON depth bounds a write at 127 and a response at 128.

What a caller writes nests one level less than what the default
``max_json_depth`` reads, so a body that holds a written value reads back. The
count is the same walk for both, with the same default.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, LimitExceeded, json_object
from eazyrest._errors import _DEFAULT_MAX_JSON_DEPTH, _json_depth
from eazyrest._models import _MAX_WRITE_DEPTH, _check_write_depth

BASE = "https://json-depth-rule.example/"
api = API(BASE)


@json_object
class Holder(JSONObject):
    """A model with a field that keeps its JSON as it is."""

    class_url = "/holders/"
    id: int
    value: Any


Holder.register_api(api)


def nested(depth: int, kind: str = "list") -> Any:
    """Return a value of the given depth, a flat list having depth 1."""
    value: Any = 0
    for level in range(depth):
        if kind == "list" or (kind == "mixed" and level % 2):
            value = [value]
        else:
            value = {"k": value}
    return value


def test_the_default_bounds_are_tied() -> None:
    """The write bound is one less than the read default."""
    assert _DEFAULT_MAX_JSON_DEPTH == 128
    assert _MAX_WRITE_DEPTH == _DEFAULT_MAX_JSON_DEPTH - 1
    assert API(BASE).max_json_depth == _DEFAULT_MAX_JSON_DEPTH


@pytest.mark.parametrize("kind", ["list", "dict", "mixed"])
@pytest.mark.parametrize("depth", [0, 1, 2, 126, 127, 128, 129, 400])
def test_the_count_is_the_depth_and_a_limit_stops_it(
    depth: int, kind: str
) -> None:
    """A limit reports no more than one level past itself."""
    assert _json_depth(nested(depth, kind)) == depth
    for limit in (0, 1, 127, 128):
        assert _json_depth(nested(depth, kind), limit) == min(depth, limit + 1)


def test_a_value_that_holds_itself_ends_a_limited_count() -> None:
    """A limited count ends on a value that nests without end."""
    loop: list[Any] = []
    loop.append(loop)
    assert _json_depth(loop, _MAX_WRITE_DEPTH) == _MAX_WRITE_DEPTH + 1


def test_a_write_check_ends_on_a_value_that_holds_itself() -> None:
    """The write check refuses a value that holds itself without recursing."""
    loop: list[Any] = []
    loop.append(loop)
    with pytest.raises(ValueError, match="nest"):
        _check_write_depth(loop, "")


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [(126, True), (127, True), (128, False), (129, False)],
)
def test_a_write_is_bounded_by_the_write_depth(
    depth: int, accepted: bool
) -> None:
    """A field's value of 127 levels is written and one of 128 is not."""
    obj = Holder(json={"id": 1, "value": 0})
    with requests_mock.Mocker() as mocker:
        mocker.patch(
            BASE + "holders/1/", json=lambda request, context: request.json()
        )
        if accepted:
            obj.value = nested(depth)
            obj.save()
            assert obj.value == nested(depth)
        else:
            with pytest.raises(ValueError, match="nest"):
                obj.value = nested(depth)


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [(127, True), (128, True), (129, False), (130, False)],
)
def test_a_response_is_bounded_by_the_default_depth(
    depth: int, accepted: bool
) -> None:
    """A response of 128 levels is read and one of 129 is not."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            BASE + "holders/",
            json=[{"id": 1, "value": nested(depth - 2)}],
        )
        if accepted:
            assert Holder.get(id=1).value == nested(depth - 2)
        else:
            with pytest.raises(LimitExceeded, match="max_json_depth"):
                Holder.get(id=1)


def test_what_is_written_at_the_bound_reads_back() -> None:
    """A body that echoes a value of 127 levels reads under the default."""
    obj = Holder(json={"id": 1, "value": 0})
    obj.value = nested(_MAX_WRITE_DEPTH)
    with requests_mock.Mocker() as mocker:
        mocker.patch(
            BASE + "holders/1/", json=lambda request, context: request.json()
        )
        obj.save()
