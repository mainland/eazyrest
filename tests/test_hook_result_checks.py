"""An overridable hook that gives a result of the wrong kind is named.

Each hook that a query, a write, or a load calls is checked where it is called,
so that a wrong result raises an error that names the hook and what it must
return, before the library's own code fails on it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, ResponseShapeError, json_object

BASE = "https://hook-results.example/"
api = API(BASE)


def _items() -> Iterator[dict[str, Any]]:
    """Yield one JSON object, as a hook may return an iterator."""
    yield {"id": 1}


RESULT: Any = None
"""What the hooks of the models below return, set by each test."""


def _result(*args: Any, **kwargs: Any) -> Any:
    """Return the result that the current test chose."""
    return RESULT


@json_object
class Member(JSONObject):
    """A related model whose bulk lookup gives the chosen result."""

    class_url = "/members/"
    id: int

    bulk_get_by_pks = classmethod(_result)


@json_object
class Team(JSONObject):
    """A model with a prefetched relation."""

    class_url = "/teams/"
    id: int
    lead: Member


@json_object
class ByItems(JSONObject):
    """A model whose collection hook gives the chosen result."""

    class_url = "/by-items/"
    id: int

    collection_items = classmethod(_result)


@json_object
class ByResponse(JSONObject):
    """A model whose response hook gives the chosen result."""

    class_url = "/by-response/"
    id: int

    collection_items_from_response = classmethod(_result)


@json_object
class ByPage(JSONObject):
    """A model whose page hook gives the chosen result."""

    class_url = "/by-page/"
    id: int

    collection_page = classmethod(_result)


@json_object
class Merged(JSONObject):
    """A model whose write hook gives the chosen result."""

    class_url = "/merged/"
    id: int
    name: str

    merge_response = _result


@json_object
class Wrapped(JSONObject):
    """A model whose single-object hook gives the chosen result."""

    class_url = "/wrapped/"
    id: int
    name: str

    object_json = classmethod(_result)


for model in (Member, Team, ByItems, ByResponse, ByPage, Merged, Wrapped):
    model.register_api(api)

WRONG: dict[str, Callable[[], Any]] = {
    "None": lambda: None,
    "a list": lambda: [{"id": 1}],
    "a generator": lambda: _items(),
}
"""Results that no hook accepts, except where a hook accepts an iterable."""


@pytest.fixture(autouse=True)
def mocked() -> Iterator[requests_mock.Mocker]:
    """Serve every resource, and reset the chosen result afterwards."""
    global RESULT
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "teams/", json=[{"id": 1, "lead": 7}])
        mocker.get(BASE + "by-items/", json=[{"id": 1}])
        mocker.get(BASE + "by-response/", json=[{"id": 1}])
        mocker.get(BASE + "by-page/", json=[{"id": 1}])
        mocker.get(BASE + "wrapped/1/", json={"id": 1, "name": "a"})
        mocker.patch(BASE + "merged/1/", json={"id": 1, "name": "b"})
        mocker.post(BASE + "merged/", json={"id": 1, "name": "b"})
        yield mocker
    RESULT = None


def choose(result: Any) -> None:
    """Choose what the hooks return."""
    global RESULT
    RESULT = result


@pytest.mark.parametrize("wrong", sorted(WRONG))
@pytest.mark.parametrize(
    "query",
    [
        lambda: list(Team.filter(prefetch="lead")),
        lambda: Team.get_page(prefetch="lead"),
    ],
    ids=["filter", "get_page"],
)
def test_a_bulk_lookup_must_return_a_mapping(
    query: Callable[[], Any], wrong: str
) -> None:
    """A list, a generator, or ``None`` from the bulk lookup is refused."""
    choose(WRONG[wrong]())
    with pytest.raises(TypeError, match=r"Member\.bulk_get_by_pks\(\) must"):
        query()


@pytest.mark.parametrize(
    ("hook", "query"),
    [
        ("collection_items", lambda: list(ByItems.filter())),
        ("collection_items_from_response", lambda: list(ByResponse.filter())),
    ],
)
@pytest.mark.parametrize("wrong", ["None"])
def test_a_collection_hook_must_return_an_iterable(
    hook: str, query: Callable[[], Any], wrong: str
) -> None:
    """``None`` from a collection hook names the hook."""
    choose(WRONG[wrong]())
    with pytest.raises(TypeError, match=rf"\.{hook}\(\) must return"):
        query()


@pytest.mark.parametrize(
    "query",
    [
        lambda: [o.pk for o in ByItems.filter()],
        lambda: [o.pk for o in ByResponse.filter()],
    ],
    ids=["collection_items", "collection_items_from_response"],
)
@pytest.mark.parametrize("make", ["a list", "a generator"])
def test_a_collection_hook_may_return_a_list_or_a_generator(
    query: Callable[[], Any], make: str
) -> None:
    """Any iterable of JSON objects is a collection."""
    choose(WRONG[make]())
    assert query() == [1]


@pytest.mark.parametrize("wrong", sorted(WRONG))
def test_a_page_hook_must_return_a_page(wrong: str) -> None:
    """A list, a generator, or ``None`` from the page hook is refused."""
    choose(WRONG[wrong]())
    with pytest.raises(TypeError, match=r"ByPage\.collection_page\(\) must"):
        list(ByPage.filter())


@pytest.mark.parametrize("wrong", sorted(WRONG))
def test_a_write_hook_must_return_a_mapping(wrong: str) -> None:
    """A list, a generator, or ``None`` from the write hook is refused."""
    choose(WRONG[wrong]())
    obj = Merged(json={"id": 1, "name": "a"})
    obj.name = "b"
    with pytest.raises(
        ResponseShapeError, match=r"Merged\.merge_response\(\) must"
    ) as info:
        obj.save()
    assert info.value.response is not None


@pytest.mark.parametrize("wrong", sorted(WRONG))
def test_a_single_object_hook_must_return_a_mapping(wrong: str) -> None:
    """A list, a generator, or ``None`` from ``object_json()`` is refused."""
    choose(WRONG[wrong]())
    with pytest.raises(
        ResponseShapeError, match=r"Wrapped\.object_json\(\) must"
    ) as info:
        _ = Wrapped(id=1).name
    assert info.value.response is not None


NOT_COLLECTIONS: dict[str, Callable[[], Any]] = {
    "a mapping": lambda: {"results": [{"id": 1}]},
    "a string": lambda: "abc",
    "bytes": lambda: b"ab",
}
"""Iterable results that are no collection of items."""


@pytest.mark.parametrize("wrong", sorted(NOT_COLLECTIONS))
@pytest.mark.parametrize(
    ("hook", "query"),
    [
        ("collection_items", lambda: list(ByItems.filter())),
        ("collection_items_from_response", lambda: list(ByResponse.filter())),
    ],
    ids=["collection_items-filter", "collection_items_from_response-filter"],
)
def test_a_collection_hook_must_return_a_collection_of_items(
    hook: str, query: Callable[[], Any], wrong: str
) -> None:
    """A mapping, a string, or bytes is no collection, though iterable."""
    choose(NOT_COLLECTIONS[wrong]())
    with pytest.raises(TypeError, match=rf"\.{hook}\(\) must return"):
        query()


BAD_NAMES: dict[str, Any] = {
    "an integer": {"id": 1, 7: "x"},
    "None": {"id": 1, None: "x"},
}


@pytest.mark.parametrize("names", sorted(BAD_NAMES))
@pytest.mark.parametrize(
    ("hook", "run"),
    [
        ("merge_response", lambda: Merged.create(name="n")),
        ("merge_response", lambda: _saved()),
        ("object_json", lambda: Wrapped(id=1).name),
    ],
    ids=["merge_response-create", "merge_response-save", "object_json-load"],
)
def test_a_hook_result_with_a_name_that_is_no_string_holds_the_response(
    hook: str, run: Callable[[], Any], names: str
) -> None:
    """The error names the hook, and holds the response, as for any shape."""
    choose(BAD_NAMES[names])
    with pytest.raises(
        ResponseShapeError, match=rf"\.{hook}\(\) must"
    ) as info:
        run()
    assert info.value.response is not None
    assert "json=" not in str(info.value)


def _saved() -> None:
    """Save a changed object."""
    obj = Merged(json={"id": 1, "name": "a"})
    obj.name = "b"
    obj.save()
