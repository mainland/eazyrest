"""A member that a subclass may override is read through one checked call.

The six hooks and the property ``url`` are overridable. A result of the wrong
type raises an error that names the class and the member, before the library's
own code fails on it, and the error is never an ``AttributeError``, which
``hasattr()`` and ``getattr()`` with a default would swallow. A response that
the default ``object_json()`` cannot read as an object is blamed on the
response, not on a hook that the program never wrote.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, ResponseShapeError, json_object

BASE = "https://member-results.example/"
api = API(BASE)

RESULT: Any = None
"""What the overridden members of the models below give."""


def _result(*args: Any, **kwargs: Any) -> Any:
    """Return the result that the current test chose."""
    return RESULT


def _property_result(self: Any) -> Any:
    """Return the result that the current test chose, as a property does."""
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


@json_object
class Located(JSONObject):
    """A model whose ``url`` gives the chosen result."""

    class_url = "/located/"
    id: int
    name: str

    url = property(_property_result)


@json_object
class Plain(JSONObject):
    """A model that overrides no hook."""

    class_url = "/plain/"
    id: int
    name: str


for model in (Member, Team, ByItems, ByResponse, ByPage, Merged, Wrapped):
    model.register_api(api)
Located.register_api(api)
Plain.register_api(api)


@pytest.fixture(autouse=True)
def served() -> Iterator[requests_mock.Mocker]:
    """Serve every resource, and reset the chosen result afterwards."""
    global RESULT
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        mocker.get(BASE + "teams/", json=[{"id": 1, "lead": 7}])
        mocker.get(BASE + "by-items/", json=[{"id": 1}])
        mocker.get(BASE + "by-response/", json=[{"id": 1}])
        mocker.get(BASE + "by-page/", json=[{"id": 1}])
        mocker.get(BASE + "wrapped/1/", json={"id": 1, "name": "a"})
        mocker.patch(BASE + "merged/1/", json={"id": 1, "name": "b"})
        yield mocker
    RESULT = None


def choose(result: Any) -> None:
    """Choose what the overridden members give."""
    global RESULT
    RESULT = result


def lazy_save() -> None:
    """Change a field of a ``Merged`` object and save it."""
    obj = Merged(json={"id": 1, "name": "a"})
    obj.name = "b"
    obj.save()


# Each calls a hook through an entry point that reads it, and gives the class
# and the member that the error names, and the error's class.
HOOKS: dict[str, tuple[Callable[[], Any], str, type[Exception]]] = {
    "bulk_get_by_pks": (
        lambda: list(Team.filter(prefetch="lead")),
        r"Member\.bulk_get_by_pks\(\)",
        TypeError,
    ),
    "collection_items": (
        lambda: list(ByItems.filter()),
        r"ByItems\.collection_items\(\)",
        TypeError,
    ),
    "collection_items_from_response": (
        lambda: list(ByResponse.filter()),
        r"ByResponse\.collection_items_from_response\(\)",
        TypeError,
    ),
    "collection_page": (
        lambda: list(ByPage.filter()),
        r"ByPage\.collection_page\(\)",
        TypeError,
    ),
    "merge_response": (
        lazy_save,
        r"Merged\.merge_response\(\)",
        ResponseShapeError,
    ),
    "object_json": (
        lambda: Wrapped(id=1).name,
        r"Wrapped\.object_json\(\)",
        ResponseShapeError,
    ),
}


@pytest.mark.parametrize("wrong", [None, 3])
@pytest.mark.parametrize(
    ("call", "named", "error"), HOOKS.values(), ids=HOOKS.keys()
)
def test_a_hook_that_gives_the_wrong_type_is_named(
    call: Callable[[], Any], named: str, error: type[Exception], wrong: Any
) -> None:
    """The error names the class and the hook and is no ``AttributeError``."""
    choose(wrong)
    with pytest.raises(error, match=named) as caught:
        call()
    assert not isinstance(caught.value, AttributeError)


def loaded(mode: str = "lazy") -> Located:
    """Return an object that has read its fields, whose URL was valid."""
    choose("/located/1/")
    return Located(
        json={"id": 1, "name": "a"},
        write_mode=mode,  # type: ignore[arg-type]
    )


def unloaded(mode: str = "lazy") -> Located:
    """Return an object that must request its fields."""
    choose("/located/1/")
    return Located(id=1, write_mode=mode)  # type: ignore[arg-type]


def read_a_field(wrong: Any) -> Any:
    """Read a field of an object that has not loaded."""
    obj = unloaded()
    choose(wrong)
    return obj.name


def refresh(wrong: Any) -> None:
    """Refresh an object."""
    obj = loaded()
    choose(wrong)
    obj.refresh()


def delete(wrong: Any) -> None:
    """Delete an object."""
    obj = loaded()
    choose(wrong)
    obj.delete()


def absolute_url(wrong: Any) -> Any:
    """Read an object's absolute URL."""
    obj = loaded()
    choose(wrong)
    return obj.absolute_url


def save(wrong: Any) -> None:
    """Save a field's change by ``PATCH``."""
    obj = loaded()
    obj.name = "b"
    choose(wrong)
    obj.save()


def save_in_full(wrong: Any) -> None:
    """Save every field by ``PUT``."""
    obj = loaded()
    choose(wrong)
    obj.save(full=True)


def assign_eagerly(wrong: Any) -> None:
    """Assign a field of an object that writes at once."""
    obj = loaded("eager")
    choose(wrong)
    obj.name = "b"


# Each makes an object, makes its ``url`` wrong, and reads it through an entry
# point of the library.
URL_USES: dict[str, Callable[[Any], Any]] = {
    "a field read": read_a_field,
    "refresh": refresh,
    "delete": delete,
    "absolute_url": absolute_url,
    "save": save,
    "save(full=True)": save_in_full,
    "an eager write": assign_eagerly,
}


@pytest.mark.parametrize("wrong", [None, 3, b"/located/1/"])
@pytest.mark.parametrize("use", URL_USES.values(), ids=URL_USES.keys())
def test_a_url_that_is_no_string_is_named(
    use: Callable[[Any], Any], wrong: Any, served: requests_mock.Mocker
) -> None:
    """Every request of an object reads ``url`` through one check."""
    with pytest.raises(
        TypeError, match=r"Located\.url must return a str"
    ) as e:
        use(wrong)
    assert not isinstance(e.value, AttributeError)
    assert served.call_count == 0


def test_a_url_that_is_a_string_is_read() -> None:
    """The check refuses nothing that a string is."""
    obj = loaded()
    assert obj.absolute_url == BASE + "located/1/"
    obj.delete()


def test_a_wrong_url_is_not_swallowed_by_hasattr() -> None:
    """A field read with a wrong ``url`` raises, and other names do not.

    ``hasattr()`` does not report a field as missing, and ``getattr()`` with a
    default does not give the default, for an error that is no
    ``AttributeError``. A name that is no field is still missing.
    """
    obj = unloaded()
    choose(None)
    with pytest.raises(TypeError, match=r"Located\.url"):
        hasattr(obj, "name")
    with pytest.raises(TypeError, match=r"Located\.url"):
        getattr(obj, "name", "default")
    assert not hasattr(obj, "no_such_name")
    assert getattr(obj, "no_such_name", "default") == "default"


@pytest.mark.parametrize("body", [7, "text", None, True])
@pytest.mark.parametrize(
    ("use", "phrase"),
    [
        (lambda: Plain(id=1).name, "write or load of a Plain"),
        (lambda: Plain.create(name="a"), "creation of a Plain"),
    ],
    ids=["load", "create"],
)
def test_a_scalar_response_blames_the_response_when_no_hook_is_overridden(
    use: Callable[[], Any],
    phrase: str,
    body: Any,
    served: requests_mock.Mocker,
) -> None:
    """The message blames the response, not a hook that is not written."""
    served.register_uri(
        requests_mock.ANY,
        requests_mock.ANY,
        text=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(ResponseShapeError) as caught:
        use()
    message = str(caught.value)
    assert "object_json" not in message
    assert phrase in message
    assert caught.value.response is not None


def create_one() -> None:
    """Create an object, whose response ``merge_response()`` reads."""
    Merged.create(name="a")


def get_one_page() -> None:
    """Read one page, whose items ``collection_page()`` gives."""
    ByPage.get_page()


# A member that two entry points read gives one error at both.
SHARED_MEMBERS: dict[str, tuple[Callable[[], Any], Callable[[], Any]]] = {
    "collection_page": (lambda: list(ByPage.filter()), get_one_page),
    "merge_response": (lazy_save, create_one),
}


@pytest.mark.parametrize("member", SHARED_MEMBERS)
def test_every_entry_point_that_reads_a_member_says_the_same_words(
    member: str,
) -> None:
    """The class, the member, the type, and the error class agree."""
    choose([1, 2])
    messages = []
    classes = []
    for call in SHARED_MEMBERS[member]:
        with pytest.raises((TypeError, ResponseShapeError)) as caught:
            call()
        messages.append(str(caught.value).split(" (")[0])
        classes.append(type(caught.value))
    assert messages[0] == messages[1]
    assert classes[0] is classes[1]
    assert f".{member}() must return " in messages[0]
    assert messages[0].endswith(", got list")
