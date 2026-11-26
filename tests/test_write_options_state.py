"""A write_options() block leaves the object as it found it."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object

api = API("https://options.example/")
HEADERS = {"If-Match": "etag"}


@json_object
class Person(JSONObject):
    """A related model."""

    class_url = "/people/"
    id: int
    name: str


@json_object
class Item(JSONObject):
    """A model that relates to a person."""

    class_url = "/items/"
    id: int
    title: str
    owner: Person


Person.register_api(api)
Item.register_api(api)


def item() -> Item:
    """Return an item with its owner embedded."""
    return Item(json={"id": 1, "title": "t", "owner": {"id": 7, "name": "a"}})


def test_a_block_leaves_no_write_options_on_the_object() -> None:
    """An object that had none has none again."""
    obj = item()
    with obj.write_options(headers=HEADERS):
        with obj.write_options(params={"x": "1"}):
            assert vars(obj)["_write_options"]
        assert vars(obj)["_write_options"] == {"headers": HEADERS}
    assert "_write_options" not in vars(obj)


def test_a_block_that_raises_leaves_no_write_options_on_the_object() -> None:
    """The options go also when the block fails."""
    obj = item()
    with pytest.raises(RuntimeError), obj.write_options(headers=HEADERS):
        raise RuntimeError
    assert "_write_options" not in vars(obj)


def test_blocks_that_end_in_the_order_they_began_are_refused() -> None:
    """The first block to end raises, and no options stay on the object."""

    def edit(obj: Item, etag: str) -> Any:
        """Hold a block open across a ``yield``."""
        with obj.write_options(headers={"If-Match": etag}):
            yield

    obj = item()
    first = edit(obj, "v1")
    next(first)
    second = edit(obj, "v2")
    next(second)
    with pytest.raises(UsageError, match="block that began later"):
        first.close()
    # The block that is still open keeps its options until it ends.
    assert vars(obj)["_write_options"]["headers"]["If-Match"] == "v2"
    second.close()
    assert "_write_options" not in vars(obj)
    obj.title = "b"
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://options.example/items/1/", json={})
        obj.save()
        assert "If-Match" not in mocker.last_request.headers


def test_blocks_that_end_in_the_reverse_order_are_not_refused() -> None:
    """Nested blocks end in reverse order and restore each level."""
    obj = item()
    with obj.write_options(headers={"X-Outer": "1"}):
        with obj.write_options(headers={"X-Inner": "2"}):
            pass
        assert vars(obj)["_write_options"] == {"headers": {"X-Outer": "1"}}
    assert "_write_options" not in vars(obj)


TITLES = itertools.count()


def edit(obj: Item, headers: dict[str, str]) -> Any:
    """Hold a block of the object open across a ``yield``."""
    with obj.write_options(headers=headers):
        yield


def sent_headers(obj: Item) -> Any:
    """Save a change to the object and return the headers that it sent."""
    obj.title = f"title {next(TITLES)}"
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://options.example/items/1/", json={})
        obj.save()
    return mocker.last_request.headers


def test_an_ended_block_keeps_none_of_its_options_for_the_next_write() -> None:
    """The block that is still open sends only what it and the class set."""
    obj = item()
    first = edit(obj, {"If-Match": "v1"})
    next(first)
    second = edit(obj, {"X-Second": "2"})
    next(second)
    with pytest.raises(UsageError, match="block that began later"):
        first.close()
    headers = sent_headers(obj)
    assert headers["X-Second"] == "2"
    assert "If-Match" not in headers
    second.close()


def test_the_object_takes_the_merge_of_the_blocks_still_open() -> None:
    """Of three nested blocks, the middle one ends first."""
    obj = item()
    blocks = [
        edit(obj, {"X-First": "1", "X-Shared": "first"}),
        edit(obj, {"X-Second": "2"}),
        edit(obj, {"X-Third": "3", "X-Shared": "third"}),
    ]
    for block in blocks:
        next(block)
    with pytest.raises(UsageError, match="block that began later"):
        blocks[1].close()
    headers = sent_headers(obj)
    assert "X-Second" not in headers
    assert (headers["X-First"], headers["X-Third"]) == ("1", "3")
    # The blocks that remain merge in the order that they began.
    assert headers["X-Shared"] == "third"
    with pytest.raises(UsageError, match="block that began later"):
        blocks[0].close()
    headers = sent_headers(obj)
    assert "X-First" not in headers
    assert headers["X-Third"] == "3"
    blocks[2].close()
    assert "_write_options" not in vars(obj)


def hook(response: Any, **kwargs: Any) -> None:
    """Do nothing with a response."""


BAD_MERGED_OPTIONS = {
    "headers 5": {"headers": 5},
    "headers list": {"headers": [("If-Match", "v")]},
    "hooks list": {"hooks": [hook]},
    "hooks callable": {"hooks": hook},
    "header name int": {"headers": {5: "x"}},
    "hooks entry int": {"hooks": {"response": 5}},
}
MAPPINGS = {
    "headers": {"headers": HEADERS},
    "hooks": {"hooks": {"response": [hook]}},
}


OPTION_REFUSAL = r"The (headers|hooks) request option"


def state(obj: Item) -> tuple[Any, Any]:
    """Return what a block keeps on the object."""
    return vars(obj).get("_write_options"), vars(obj).get("_write_blocks")


@pytest.mark.parametrize(
    "bad", BAD_MERGED_OPTIONS.values(), ids=BAD_MERGED_OPTIONS
)
def test_a_refused_block_opened_alone_leaves_the_object_as_it_was(
    bad: dict[str, Any],
) -> None:
    """The refusal comes before anything is registered."""
    obj = item()
    with (
        pytest.raises(TypeError, match=OPTION_REFUSAL),
        obj.write_options(**bad),
    ):
        pytest.fail("the block was entered")
    assert "_write_blocks" not in vars(obj)
    assert "_write_options" not in vars(obj)


@pytest.mark.parametrize(
    "bad", BAD_MERGED_OPTIONS.values(), ids=BAD_MERGED_OPTIONS
)
@pytest.mark.parametrize("outer", MAPPINGS.values(), ids=MAPPINGS)
def test_a_refused_nested_block_leaves_the_open_block_as_it_was(
    outer: dict[str, Any], bad: dict[str, Any]
) -> None:
    """The outer block ends without error and no refused option stays."""
    obj = item()
    with obj.write_options(**outer):
        before = state(obj)
        with pytest.raises(TypeError), obj.write_options(**bad):
            pytest.fail("the block was entered")
        assert state(obj) == before
    assert state(obj) == (None, None)
    headers = sent_headers(obj)
    assert "If-Match" not in headers


def test_a_block_whose_options_cannot_be_merged_is_not_registered() -> None:
    """Two hooks mappings whose entries cannot be combined are refused."""
    obj = item()
    with obj.write_options(hooks={"response": [hook]}):
        before = state(obj)
        with (
            pytest.raises(TypeError),
            obj.write_options(hooks={"response": 5}),
        ):
            pytest.fail("the block was entered")
        assert state(obj) == before
    assert state(obj) == (None, None)


def test_the_outer_block_exit_raises_nothing_after_a_refused_inner_block() -> (
    None
):
    """The refusal propagates alone, and the outer block ends in order."""
    obj = item()
    with (
        pytest.raises(TypeError, match="headers request option"),
        obj.write_options(headers=HEADERS),
        obj.write_options(headers=5),
    ):
        pytest.fail("the block was entered")
    assert state(obj) == (None, None)


@pytest.mark.parametrize(
    "bad", BAD_MERGED_OPTIONS.values(), ids=BAD_MERGED_OPTIONS
)
def test_a_refused_block_does_not_end_blocks_held_open_across_a_yield(
    bad: dict[str, Any],
) -> None:
    """An open block keeps its options after a refused block."""
    obj = item()
    first = edit(obj, {"If-Match": "v1"})
    next(first)
    before = state(obj)
    with pytest.raises(TypeError), obj.write_options(**bad):
        pytest.fail("the block was entered")
    assert state(obj) == before
    first.close()
    assert state(obj) == (None, None)


def test_a_block_that_enables_streaming_leaves_the_object_as_it_was() -> None:
    """A refusal of another option leaves the object as it was too."""
    obj = item()
    with (
        pytest.raises(TypeError, match="stream"),
        obj.write_options(headers=HEADERS, stream=True),
    ):
        pytest.fail("the block was entered")
    assert state(obj) == (None, None)


def test_headers_and_hooks_of_none_are_absent() -> None:
    """None for an option is no option, so a block of them holds none."""
    obj = item()
    with obj.write_options(headers=None, hooks=None):
        assert vars(obj)["_write_options"] == {}
    assert state(obj) == (None, None)


def write_by_save(obj: Item, options: Any) -> None:
    """Save a change with ``request_options``."""
    obj.title = "changed"
    obj.save(request_options=options)


def write_by_delete(obj: Item, options: Any) -> None:
    """Delete with ``request_options``."""
    obj.delete(request_options=options)


def write_by_create(obj: Item, options: Any) -> None:
    """Create an item with ``request_options``."""
    Item.create(title="t", owner=obj.owner, request_options=options)


def write_by_get_or_create(obj: Item, options: Any) -> None:
    """Look up or create an item with ``request_options``."""
    Item.get_or_create(
        id=1, title="t", owner=obj.owner, request_options=options
    )


def read_by_filter(obj: Item, options: Any) -> None:
    """Query with ``request_options``."""
    list(Item.filter(request_options=options))


def read_by_get(obj: Item, options: Any) -> None:
    """Get one item with ``request_options``."""
    Item.get(id=1, request_options=options)


def read_by_get_page(obj: Item, options: Any) -> None:
    """Read a page with ``request_options``."""
    Item.get_page(request_options=options)


ENTRY_POINTS = {
    func.__name__: func
    for func in (
        write_by_save,
        write_by_delete,
        write_by_create,
        write_by_get_or_create,
        read_by_filter,
        read_by_get,
        read_by_get_page,
    )
}


@pytest.mark.parametrize(
    "bad", BAD_MERGED_OPTIONS.values(), ids=BAD_MERGED_OPTIONS
)
@pytest.mark.parametrize("entry", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
@pytest.mark.parametrize("inside_block", [False, True])
def test_request_options_that_cannot_be_merged_are_refused_before_a_request(
    entry: Callable[[Item, Any], None],
    bad: dict[str, Any],
    inside_block: bool,
) -> None:
    """No request is sent and the object's pending changes stay."""
    obj = item()
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        if inside_block:
            with obj.write_options(
                headers=HEADERS, hooks={"response": [hook]}
            ):
                with pytest.raises(TypeError, match=OPTION_REFUSAL):
                    entry(obj, bad)
                assert vars(obj)["_write_options"]["headers"] == HEADERS
        else:
            with pytest.raises(TypeError, match=OPTION_REFUSAL):
                entry(obj, bad)
    assert mocker.call_count == 0
    assert state(obj) == (None, None)


def test_a_refused_save_keeps_the_change_for_the_next_save() -> None:
    """The change is still pending and the next save sends it."""
    obj = item()
    with pytest.raises(TypeError):
        write_by_save(obj, {"headers": 5})
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://options.example/items/1/", json={})
        obj.save()
    assert mocker.last_request.json() == {"title": "changed"}


UNCHECKED_BY_US = {
    "hooks list holding a non-callable": {"hooks": {"response": [5]}},
    "unknown hook event": {"hooks": {"other": [hook]}},
    "header value of the wrong type": {"headers": {"X-Count": 5}},
    "bytes header name": {"headers": {b"X-Name": "v"}},
    "hook entry of None": {"hooks": {"response": None}},
    "hook entry that is one callable": {"hooks": {"response": hook}},
}


@pytest.mark.parametrize(
    "options", UNCHECKED_BY_US.values(), ids=UNCHECKED_BY_US
)
def test_options_that_requests_judges_are_not_refused_when_a_block_opens(
    options: dict[str, Any],
) -> None:
    """A block takes what the merge can combine and requests can read."""
    obj = item()
    with obj.write_options(**options):
        assert vars(obj)["_write_options"] == options
    assert state(obj) == (None, None)


class Unreadable:
    """A hooks entry that is iterable and fails when it is read."""

    def __iter__(self) -> Any:
        """Fail as an iterator that cannot be read does."""
        raise RuntimeError("unreadable")


@json_object
class Preset(Item):
    """A model whose class already holds hooks."""

    class_url = "/presets/"
    _write_options = MappingProxyType({"hooks": {"response": [hook]}})


@pytest.mark.parametrize("opened_first", [False, True])
def test_a_block_whose_merge_fails_leaves_no_state_on_the_object(
    opened_first: bool,
) -> None:
    """The merge of the options comes before anything is registered."""
    obj = Preset(json={"id": 1, "title": "t", "owner": {"id": 7, "name": "a"}})
    bad = {"hooks": {"response": Unreadable()}}
    if opened_first:
        outer = obj.write_options(hooks={"response": [hook]})
        outer.__enter__()
    before = state(obj)
    with (
        pytest.raises(RuntimeError, match="unreadable"),
        obj.write_options(**bad),
    ):
        pytest.fail("the block was entered")
    assert state(obj) == before
    if opened_first:
        outer.__exit__(None, None, None)
    assert "_write_blocks" not in vars(obj)
    assert "_write_options" not in vars(obj)


def test_a_header_name_of_the_wrong_type_is_named_in_the_refusal() -> None:
    """The message gives the type of the name."""
    with pytest.raises(TypeError, match="got a name of type int"):
        item().write_options(headers={5: "x"}).__enter__()
