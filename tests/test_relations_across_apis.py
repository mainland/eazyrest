"""A relation between models of different APIs is refused at every use.

An object graph uses one API, so a relation field whose model and related model
are registered on different ``API`` objects has no API to load through. The
refusal is one check of the two classes, made whatever the field holds and
whether or not the object has loaded: a read, an assignment, a constructor
keyword, a creation, a full write, and a prefetch. It raises ``UsageError``
before any request. A model that has no registered API agrees with any.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object
from eazyrest._models import _api_registry

THING = {
    "id": 1,
    "name": "t",
    "owner": 5,
    "owners": [5],
    "org": 9,
    "ownerset": [],
    "names": [],
}
EMPTY = dict(THING, owner=None, owners=[])


@json_object
class Team(JSONObject):
    """A model at the end of a chain, with a plain bulk lookup."""

    class_url = "/teams/"
    id: int
    name: str


@json_object
class Org(JSONObject):
    """A model that is registered on no API, and relates to a team."""

    class_url = "/orgs/"
    id: int
    team: Team | None

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Org]:
        """Build the orgs with the client that the query gave."""
        return {
            pk: cls(json={"id": pk, "team": 3}, api=api) for pk in sorted(pks)
        }


@json_object
class Owner(JSONObject):
    """A model whose bulk lookup builds its objects as the hook is set."""

    class_url = "/owners/"
    id: int
    name: str
    org: Org | None

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Owner]:
        """Build the owners with the class and the client that are set."""
        made = {}
        for pk in sorted(pks):
            made[pk] = HOOK["class"](json=owner_json(pk), api=api)
        return made


@json_object
class Special(Owner):
    """A subclass of the owner that a test registers on another API."""


@json_object(write_only={"boss"})
class Thing(JSONObject):
    """The root of the graphs."""

    class_url = "/things/"
    id: int
    name: str
    owner: Owner | None
    org: Org | None
    owners: list[Owner]
    ownerset: set[Owner]
    boss: Owner | None
    names: list[str]


HOOK: dict[str, Any] = {}


EMBEDDED_ORG = {"id": 9, "team": 3}


def owner_json(pk: int, org: Any = EMBEDDED_ORG) -> dict[str, Any]:
    """Return the JSON of the owner with key ``pk``, and its org as given."""
    return {"id": pk, "name": "o", "org": org}


def answer(request: Any, context: Any) -> Any:
    """Answer any request of any API as a server of the models would."""
    path = request.path.rstrip("/")
    if path.startswith("/owners"):
        if path == "/owners":
            return [owner_json(5)]
        return owner_json(int(path.rsplit("/", 1)[1]))
    if path.startswith("/orgs"):
        return EMBEDDED_ORG
    if path.startswith("/teams"):
        return {"id": int(path.rsplit("/", 1)[1]), "name": "tm"}
    if request.method == "POST":
        return dict(THING, id=2)
    if path == "/things":
        return [THING]
    return dict(THING, id=int(path.rsplit("/", 1)[1]))


@pytest.fixture
def mock() -> Iterator[requests_mock.Mocker]:
    """Answer every request, and record it."""
    HOOK["class"] = Owner
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json=answer)
        yield mocker


def register(
    monkeypatch: pytest.MonkeyPatch, **registered: API | None
) -> None:
    """Register the models that are named, and none for ``None``."""
    models = {
        "thing": Thing,
        "owner": Owner,
        "org": Org,
        "team": Team,
        "special": Special,
    }
    for name, model in models.items():
        monkeypatch.delitem(_api_registry, model, raising=False)
        api = registered.get(name)
        if api is not None:
            monkeypatch.setitem(_api_registry, model, api)


A = API("https://a.test/")
B = API("https://b.test/")
C = API("https://c.test/")


def read(obj: Any, field: str) -> Any:
    """Return a field of an object, as a field that has no declared type."""
    return getattr(obj, field)


def write(obj: Any, field: str, value: Any) -> None:
    """Assign a value that the field's declared type does not admit."""
    setattr(obj, field, value)


def loaded(json: dict[str, Any], api: API | None = None) -> Thing:
    """Return an object that has its JSON, and its own API if it is given."""
    return Thing(json=dict(json), api=api)


# Each use builds what it needs, which may send requests that no refusal
# precedes, and returns the call that must be refused.
Use = Callable[[], Callable[[], Any]]


def reading(json: dict[str, Any], field: str, api: API | None = None) -> Use:
    """Return a use that reads a field of an object that has its JSON."""
    return lambda: lambda: getattr(loaded(json, api), field)


def assigning(
    field: str, value: Any, mode: str = "lazy", api: API | None = None
) -> Use:
    """Return a use that assigns a value to a field of a loaded object."""

    def use() -> Callable[[], Any]:
        thing = loaded(THING, api)
        thing.set_write_mode(mode)  # type: ignore[arg-type]
        return lambda: setattr(thing, field, value)

    return use


def after_a_load() -> Callable[[], Any]:
    """Load an object whose relation is null, then read it."""
    thing = next(Thing.filter())
    return lambda: thing.owner


def after_a_refresh() -> Callable[[], Any]:
    """Read a null relation of a loaded object that then refreshes."""
    thing = loaded(EMPTY)

    def read() -> Any:
        thing.refresh()
        return thing.owner

    return read


def partial() -> Callable[[], Any]:
    """Read a relation that a partial payload lacks."""
    thing = Thing(id=1)
    thing.update_from_json({"id": 1, "name": "a"})
    return lambda: thing.owner


USES: dict[str, Use] = {
    "read of a key, loaded": reading(THING, "owner"),
    "read of a collection of keys, loaded": reading(THING, "owners"),
    "read of null": reading(EMPTY, "owner"),
    "read of an empty collection": reading(EMPTY, "owners"),
    "read of an embedded object": reading(
        dict(THING, owner=owner_json(5)), "owner"
    ),
    "read of a set of keys": reading(dict(THING, ownerset=[5]), "ownerset"),
    "read, not loaded": lambda: lambda: Thing(id=1).owner,
    "read, partial": partial,
    "read after a load": after_a_load,
    "read after a refresh": after_a_refresh,
    "assignment of a key": assigning("owner", 7),
    "assignment of null": assigning("owner", None),
    "assignment of an empty collection": assigning("owners", []),
    "assignment of a collection of keys": assigning("owners", [7]),
    "assignment of a set of objects": assigning(
        "ownerset", {Owner(id=7, api=A)}
    ),
    "assignment of an object of the parent's API": assigning(
        "owner", Owner(id=7, api=A)
    ),
    "assignment of an object of another API": assigning(
        "owner", Owner(id=7, api=C)
    ),
    "assignment of an object with no API": assigning("owner", Owner(id=7)),
    "assignment to a write-only field": assigning("boss", 7),
    "eager assignment": assigning("owner", 7, "eager"),
    "constructor keyword of a key": lambda: (
        lambda: Thing(id=1, name="x", owner=7)
    ),
    "constructor keyword of an object": lambda: (
        lambda: Thing(id=1, owner=Owner(id=7, api=A))
    ),
    "constructor keyword, write-only": lambda: lambda: Thing(id=1, boss=7),
    "create() with a key": lambda: lambda: Thing.create(name="n", owner=7),
    "create() with an object": lambda: (
        lambda: Thing.create(name="n", owner=Owner(id=7, api=A))
    ),
    "create() with null": lambda: lambda: Thing.create(owner=None),
    "create(fields=)": lambda: lambda: Thing.create(fields={"owners": []}),
    "create(), write-only": lambda: lambda: Thing.create(boss=7),
    "get_or_create() defaults": lambda: (
        lambda: Thing.get_or_create(name="zzz", defaults={"owner": 7})
    ),
    "get_or_create() lookup": lambda: lambda: Thing.get_or_create(owner=7),
    "save(full=True), not loaded": lambda: lambda: Thing(id=1).save(full=True),
    "save(full=True), loaded": lambda: lambda: loaded(THING).save(full=True),
    "save(full=True), null": lambda: lambda: loaded(EMPTY).save(full=True),
    "prefetch": lambda: lambda: list(Thing.filter(prefetch="owner")),
    "prefetch of a collection": lambda: (
        lambda: list(Thing.filter(prefetch="owners"))
    ),
    "read, given an api=": reading(EMPTY, "owner", C),
    "read, not loaded, given an api=": lambda: (
        lambda: Thing(id=1, api=C).owner
    ),
    "assignment, given an api=": assigning("owner", 7, api=C),
    "assignment of an object of that api=": assigning(
        "owner", Owner(id=7, api=C), api=C
    ),
    "constructor keyword, given an api=": lambda: (
        lambda: Thing(id=1, api=C, owner=Owner(id=7, api=C))
    ),
    "create(), given an api=": lambda: (
        lambda: Thing.create(api=C, owner=Owner(id=7, api=C))
    ),
    "prefetch, given an api=": lambda: (
        lambda: list(Thing.filter(prefetch="owner", api=C))
    ),
}


@pytest.mark.parametrize("use", USES)
def test_a_relation_across_apis_is_refused_at_every_use(
    mock: requests_mock.Mocker, monkeypatch: pytest.MonkeyPatch, use: str
) -> None:
    """The refusal comes before any request that the use itself would send.

    Args:
        use: The use of the relation.
    """
    register(monkeypatch, thing=A, owner=B)
    refused = USES[use]()
    sent = len(mock.request_history)

    with pytest.raises(UsageError, match="different APIs"):
        refused()

    assert len(mock.request_history) == sent


@pytest.mark.parametrize("use", USES)
@pytest.mark.parametrize(
    "registrations",
    [
        {"thing": A, "owner": A},
        {"thing": None, "owner": None},
        {"thing": A, "owner": None},
        {"thing": None, "owner": A},
    ],
    ids=["one API", "none", "parent only", "related only"],
)
def test_a_relation_on_one_api_or_none_is_not_refused(
    mock: requests_mock.Mocker,
    monkeypatch: pytest.MonkeyPatch,
    use: str,
    registrations: dict[str, API | None],
) -> None:
    """A model with no registration agrees with any, and one API agrees.

    Args:
        use: The use of the relation.
        registrations: The APIs that the two classes are registered on.
    """
    register(monkeypatch, **registrations)
    # A use may be refused for another reason, such as an object whose own API
    # is not its parent's, but never for registrations that agree.
    refusal = ""
    try:
        USES[use]()()
    except (UsageError, ValueError) as err:
        refusal = str(err)

    assert "different APIs" not in refusal
    assert "reached through" not in refusal


def test_a_relation_across_apis_is_refused_at_a_read_after_registering_later(
    mock: requests_mock.Mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classes are compared at each use, not when they are declared."""
    register(monkeypatch, thing=A, owner=A)
    thing = loaded(THING)
    assert read(thing, "owner").pk == 5

    monkeypatch.setitem(_api_registry, Owner, B)

    with pytest.raises(UsageError, match="different APIs"):
        _ = thing.owner


def test_a_set_given_to_a_set_of_relations_is_sent_sorted_and_kept(
    mock: requests_mock.Mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On one API, a set of related objects is sent by key and kept."""
    register(monkeypatch, thing=A, owner=A)
    thing = loaded(dict(THING))
    owners = {Owner(id=9), Owner(id=7), Owner(id=8)}

    thing.ownerset = owners
    kept = thing.ownerset
    thing.save()

    assert mock.request_history[-1].json() == {"ownerset": [7, 8, 9]}
    assert kept == owners


def test_a_set_given_to_a_sequence_of_relations_is_refused(
    mock: requests_mock.Mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set has no order, so a sequence of relations does not take it."""
    register(monkeypatch, thing=A, owner=A)
    thing = loaded(THING)

    with pytest.raises(ValueError, match="no order"):
        write(thing, "owners", {Owner(id=9), Owner(id=7)})

    assert mock.request_history == []
    assert [owner.pk for owner in thing.owners] == [5]
