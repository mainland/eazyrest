"""Every object that a query reaches uses the query's API.

An object that a query builds, and each object that a relation, an embedded
object, a prefetch, or a bulk hook reaches from it, sends its requests through
one API: the ``api=`` that the query was given, or else the API registered for
the class that the query reads. A relation between classes that are registered
on different APIs is refused before any request, and so is a related object
that is given with another API than its parent's.

Each test runs the entry points of the rule over models that are registered on
one API, on two, and on none, with and without ``api=`` and
``request_options=``. Every API has its own origin, its own mounted adapter,
and its own ``Authorization`` header, and the log records which adapter
received each request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object
from eazyrest._models import _api_registry

THING = {"id": 1, "name": "a", "owner": 5, "owners": [5, 6]}
QUERY_HEADER = {"headers": {"X-Query": "q"}}
HOOK = {"mode": "plain"}


@dataclass
class Request:
    """A request that an adapter received."""

    adapter: str
    method: str
    path: str
    auth: str | None
    query_header: str | None
    phase: str


@dataclass
class World:
    """The APIs of a test and the requests that they received."""

    log: list[Request] = field(default_factory=list)
    phase: str = "query"
    apis: dict[str, API] = field(default_factory=dict)

    def make(self, name: str) -> API:
        """Return an API on its own origin whose requests are logged.

        Args:
            name: Name of the API, which its credential carries.

        Returns:
            The API.
        """
        api = API(f"https://{name.lower()}.test/")
        api.session.headers["Authorization"] = f"Bearer {name}"
        adapter = requests_mock.Adapter()
        adapter.register_uri(
            requests_mock.ANY, requests_mock.ANY, json=self._answer(name)
        )
        api.session.mount("https://", adapter)
        self.apis[name] = api
        return api

    def _answer(self, name: str) -> Callable[[Any, Any], Any]:
        """Return the answer of the server behind the API called ``name``."""

        def answer(request: Any, context: Any) -> Any:
            self.log.append(
                Request(
                    name,
                    request.method,
                    request.path,
                    request.headers.get("Authorization"),
                    request.headers.get("X-Query"),
                    self.phase,
                )
            )
            home = f"https://{request.hostname}"
            path, query = request.path.rstrip("/"), request.query
            if path.startswith("/owners"):
                if path == "/owners":
                    return [owner_json(i) for i in ids_of(query)]
                return owner_json(int(path.rsplit("/", 1)[1]))
            if path.startswith("/orgs"):
                if path == "/orgs":
                    return [{"id": i, "title": "t"} for i in ids_of(query)]
                return {"id": int(path.rsplit("/", 1)[1]), "title": "t"}
            if request.method == "POST":
                return dict(THING, id=2, name="n")
            if path != "/things":
                return dict(THING, id=int(path.rsplit("/", 1)[1]))
            if "name=zzz" in query:
                return {"results": [], "next": None}
            if "page=2" in query:
                return {"results": [dict(THING, id=3)], "next": None}
            if query and "page" not in query:
                return {"results": [THING], "next": None}
            return {"results": [THING], "next": f"{home}/things/?page=2"}

        return answer

    def reached(self) -> set[str]:
        """Return the names of the APIs that received a request."""
        return {request.adapter for request in self.log}


def owner_json(pk: int) -> dict[str, Any]:
    """Return the JSON of the owner with key ``pk``."""
    return {"id": pk, "label": f"o{pk}", "org": 9}


def ids_of(query: str) -> list[int]:
    """Return the keys that a query string asks for with ``id__in``."""
    for part in query.split("&"):
        if part.startswith("id__in="):
            return [int(x) for x in unquote(part[7:]).split(",") if x]
    return []


@json_object
class Org(JSONObject):
    """The model at the end of the chain, with a plain bulk lookup."""

    class_url = "/orgs/"
    id: int
    title: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Org]:
        """Read the orgs with the client that the query gave."""
        return {o.pk: o for o in cls.filter(api=api, id__in=sorted(pks))}


@json_object
class Owner(JSONObject):
    """A model whose bulk lookup passes its ``api`` on, as the guide says."""

    class_url = "/owners/"
    id: int
    label: str
    org: Org

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Owner]:
        """Read the owners, and prefetch or read their orgs as set."""
        prefetch = "org" if HOOK["mode"] == "prefetch" else None
        owners = list(
            cls.filter(api=api, id__in=sorted(pks), prefetch=prefetch)
        )
        if HOOK["mode"] == "read":
            for owner in owners:
                _ = owner.org.title
        return {owner.pk: owner for owner in owners}


@json_object(write_only={"boss"})
class Thing(JSONObject):
    """The root of the graph."""

    class_url = "/things/"
    id: int
    name: str
    owner: Owner
    owners: list[Owner]
    boss: Owner


@pytest.fixture
def world() -> World:
    """Return a world with the APIs A, B, and C, and G for ``api=``."""
    HOOK["mode"] = "plain"
    made = World()
    for name in "ABCG":
        made.make(name)
    return made


def register(
    monkeypatch: pytest.MonkeyPatch,
    thing: API | None,
    owner: API | None,
    org: API | None,
) -> None:
    """Register the three models on the given APIs, and none for ``None``."""
    for model, api in ((Thing, thing), (Owner, owner), (Org, org)):
        monkeypatch.delitem(_api_registry, model, raising=False)
        if api is not None:
            monkeypatch.setitem(_api_registry, model, api)


def use(world: World, thing: Thing) -> None:
    """Send every later request of an object and of the objects it relates."""
    world.phase = "after"
    thing.refresh()
    owner = thing.owner
    owner.refresh()
    owner.label = "x"
    owner.save()
    org = owner.org
    org.title = "y"
    org.save()
    org.refresh()
    for other in thing.owners:
        other.refresh()
        other.org.refresh()
    thing.name = "z"
    thing.save()
    thing.save(full=True)
    Thing(json=thing.json, api=thing._api_override).refresh()
    thing.set_write_mode("eager")
    thing.name = "y"
    thing.delete()


def pages(**kwargs: Any) -> list[Thing]:
    """Return the objects of two pages, which are read before any is used."""
    page = Thing.get_page(**kwargs)
    following = page.next_page()
    assert following is not None
    return [*page.items, *following.items]


# Each entry point takes the keyword arguments of the query, which are ``api=``
# when one is given, and returns the root objects that it builds. The flag says
# whether the entry point sends ``request_options=``.
ENTRY_POINTS: dict[str, tuple[bool, Callable[..., list[Thing]]]] = {
    "filter()": (False, lambda **kw: list(Thing.filter(**kw))),
    "all()": (False, lambda **kw: list(Thing.all(**kw))),
    "get()": (False, lambda **kw: [Thing.get(id=1, **kw)]),
    "get_page() and next_page()": (False, pages),
    "create()": (False, lambda **kw: [Thing.create(name="n", **kw)]),
    "get_or_create() that finds": (
        False,
        lambda **kw: [Thing.get_or_create(name="a", **kw)[0]],
    ),
    "get_or_create() that creates": (
        False,
        lambda **kw: [Thing.get_or_create(name="zzz", **kw)[0]],
    ),
    "Thing(id=1)": (False, lambda **kw: [Thing(id=1, **kw)]),
    "filter(prefetch=to-one)": (
        False,
        lambda **kw: list(Thing.filter(prefetch="owner", **kw)),
    ),
    "filter(prefetch=both)": (
        False,
        lambda **kw: list(Thing.filter(prefetch=["owner", "owners"], **kw)),
    ),
    "all(prefetch=to-many)": (
        False,
        lambda **kw: list(Thing.all(prefetch="owners", **kw)),
    ),
    "get_page(prefetch=) and next_page()": (
        False,
        lambda **kw: pages(prefetch="owner", **kw),
    ),
    "filter(request_options=)": (
        True,
        lambda **kw: list(Thing.filter(request_options=QUERY_HEADER, **kw)),
    ),
    "filter(prefetch=, request_options=)": (
        True,
        lambda **kw: list(
            Thing.filter(
                prefetch=["owner", "owners"],
                request_options=QUERY_HEADER,
                **kw,
            )
        ),
    ),
    "get_page(prefetch=, request_options=)": (
        True,
        lambda **kw: pages(
            prefetch="owner", request_options=QUERY_HEADER, **kw
        ),
    ),
    "get_or_create(request_options=)": (
        True,
        lambda **kw: [
            Thing.get_or_create(
                name="zzz", request_options=QUERY_HEADER, **kw
            )[0]
        ],
    ),
    "create(request_options=)": (
        True,
        lambda **kw: [
            Thing.create(name="n", request_options=QUERY_HEADER, **kw)
        ],
    ),
}


@pytest.mark.parametrize("hook", ["plain", "prefetch", "read"])
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_models_on_one_api_send_every_request_through_the_graph_api(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    given: bool,
    hook: str,
) -> None:
    """The registered API, or the given one, sends every request of a graph.

    Args:
        entry: The entry point, which builds the root of the graph.
        given: Whether the query is given ``api=``.
        hook: What the bulk hook of ``Owner`` does besides reading owners.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    HOOK["mode"] = hook
    sends_options, action = ENTRY_POINTS[entry]
    expected = "G" if given else "A"

    objects = action(**({"api": world.apis["G"]} if given else {}))
    for thing in objects:
        use(world, thing)

    assert objects
    assert world.log
    assert world.reached() == {expected}
    for request in world.log:
        assert request.auth == f"Bearer {expected}"
        if request.phase == "after":
            assert request.query_header is None
        elif sends_options:
            assert request.query_header == "q"


@pytest.mark.parametrize("hook", ["plain", "prefetch", "read"])
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_a_hook_that_passes_its_api_on_keeps_the_graphs_api(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    given: bool,
    hook: str,
) -> None:
    """The objects of a hook keep the query's API, or none if it has none.

    Args:
        given: Whether the query is given ``api=``.
        hook: What the bulk hook of ``Owner`` does besides reading owners.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    HOOK["mode"] = hook
    own = world.apis["G"] if given else None

    thing = next(
        Thing.filter(prefetch="owner", request_options=QUERY_HEADER, api=own)
    )

    assert thing._api_override is own
    assert thing.owner._api_override is own
    assert thing.owner.org._api_override is own
    assert thing.owner.org.api is (own or world.apis["A"])


RELATION_READS: dict[str, Callable[[Any], Any]] = {
    "a key": lambda thing: thing.owner,
    "a collection of keys": lambda thing: thing.owners,
}


@pytest.mark.parametrize("read", RELATION_READS)
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_relation_across_apis_is_refused_when_it_is_read(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    read: str,
    given: bool,
) -> None:
    """A relation to a class on another API raises before any request.

    Args:
        read: How the relation is read.
        given: Whether the object is built with ``api=``.
    """
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["B"])
    own = world.apis["G"] if given else None
    thing = Thing(json=dict(THING), api=own)

    with pytest.raises(UsageError, match="different APIs"):
        RELATION_READS[read](thing)

    assert world.log == []


@pytest.mark.parametrize("read", RELATION_READS)
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_relation_across_apis_is_refused_before_the_object_loads(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    read: str,
    given: bool,
) -> None:
    """A relation read of an object not loaded yet sends no request.

    Args:
        read: How the relation is read.
        given: Whether the object is built with ``api=``.
    """
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["B"])
    own = world.apis["G"] if given else None
    thing = Thing(id=1, api=own)

    with pytest.raises(UsageError, match="different APIs"):
        RELATION_READS[read](thing)

    assert world.log == []


def test_relation_across_apis_is_refused_before_a_partial_object_loads(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relation that a partial payload lacks is refused before it loads."""
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["B"])
    thing = Thing(id=1)
    thing.update_from_json({"id": 1, "name": "a"})

    with pytest.raises(UsageError, match="different APIs"):
        _ = thing.owner

    assert world.log == []


@json_object
class Pending(JSONObject):
    """A model with a relation to a class that is not defined."""

    class_url = "/things/"
    id: int
    owner: NotDefinedYet  # type: ignore[name-defined]  # noqa: F821


def test_a_relation_that_does_not_resolve_raises_before_the_load(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relation whose type is not defined yet sends no request."""
    monkeypatch.setitem(_api_registry, Pending, world.apis["A"])
    pending = Pending(id=1)

    with pytest.raises(NameError):
        _ = pending.owner

    assert world.log == []


@json_object
class Plain(JSONObject):
    """A model with a plain field whose class is not defined."""

    class_url = "/things/"
    id: int
    name: NotDefinedYet  # type: ignore[name-defined]  # noqa: F821


def test_a_field_that_does_not_resolve_raises_before_the_load(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field whose type is not defined yet sends no request."""
    monkeypatch.setitem(_api_registry, Plain, world.apis["A"])
    plain = Plain(id=1)

    with pytest.raises(NameError):
        _ = plain.name

    assert world.log == []


def test_embedded_relation_across_apis_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An embedded related object is a relation too."""
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["B"])
    thing = Thing(json=dict(THING, owner=owner_json(5)))

    with pytest.raises(UsageError, match="different APIs"):
        _ = thing.owner

    assert world.log == []


def test_relation_across_apis_is_refused_after_update_from_json(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload that names a related object reads it as any relation is."""
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["B"])
    thing = Thing(id=1)
    thing.update_from_json(dict(THING))

    with pytest.raises(UsageError, match="different APIs"):
        _ = thing.owner

    assert world.log == []


PREFETCHING_QUERIES: dict[str, Callable[..., Any]] = {
    "filter()": lambda **kw: Thing.filter(prefetch="owner", **kw),
    "all()": lambda **kw: Thing.all(prefetch="owner", **kw),
    "get()": lambda **kw: Thing.get(id=1, prefetch="owner", **kw),
    "get_page()": lambda **kw: Thing.get_page(prefetch="owner", **kw),
    "a to-many relation": lambda **kw: Thing.filter(prefetch="owners", **kw),
}


@pytest.mark.parametrize("options", [None, QUERY_HEADER], ids=["bare", "opts"])
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
@pytest.mark.parametrize("query", PREFETCHING_QUERIES)
def test_prefetch_across_apis_is_refused_before_any_request(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    given: bool,
    options: dict[str, Any] | None,
) -> None:
    """A prefetch of a class on another API sends nothing.

    Args:
        query: The query method that prefetches.
        given: Whether the query is given ``api=``.
        options: The query's ``request_options``, or ``None`` for none.
    """
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["B"])
    kwargs: dict[str, Any] = {"request_options": options}
    if given:
        kwargs["api"] = world.apis["G"]

    with pytest.raises(UsageError, match="different APIs"):
        PREFETCHING_QUERIES[query](**kwargs)

    assert world.log == []


@pytest.mark.parametrize("hook", ["plain", "prefetch", "read"])
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_models_on_three_apis_are_refused_whatever_the_hook_does(
    world: World, monkeypatch: pytest.MonkeyPatch, given: bool, hook: str
) -> None:
    """No hook is reached, so none can send a request to the wrong API.

    Args:
        given: Whether the query is given ``api=``.
        hook: What the bulk hook of ``Owner`` would do.
    """
    register(monkeypatch, world.apis["A"], world.apis["B"], world.apis["C"])
    HOOK["mode"] = hook
    kwargs: dict[str, Any] = {"api": world.apis["G"]} if given else {}

    with pytest.raises(UsageError, match="different APIs"):
        Thing.filter(prefetch="owner", **kwargs)

    assert world.log == []


def test_prefetch_inside_a_hook_across_apis_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query that a bulk hook makes is checked as any other is."""
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["C"])

    with pytest.raises(UsageError, match="different APIs"):
        Owner.filter(prefetch="org")

    assert world.log == []


@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_models_on_no_api_use_the_given_one(
    world: World, monkeypatch: pytest.MonkeyPatch, given: bool
) -> None:
    """A class with no registered API agrees with any, if ``api=`` is given.

    Args:
        given: Whether the query is given ``api=``. Without it, the root has no
            API and the query is refused.
    """
    register(monkeypatch, None, None, None)
    if not given:
        with pytest.raises(UsageError, match="No API registered for Thing"):
            Thing.filter()
        with pytest.raises(UsageError, match="No API registered for Owner"):
            Thing.filter(prefetch="owner")
        assert world.log == []
        return

    for query in (
        Thing.filter(prefetch=["owner", "owners"], api=world.apis["G"]),
        Thing.all(api=world.apis["G"]),
    ):
        for thing in list(query):
            use(world, thing)

    assert world.reached() == {"G"}


def test_related_models_without_an_api_are_refused_without_api_given(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefetch has no client for a class that has no API of its own."""
    register(monkeypatch, world.apis["A"], None, None)

    with pytest.raises(UsageError, match="No API registered for Owner"):
        Thing.filter(prefetch="owner")

    assert world.log == []


def test_related_models_without_an_api_use_the_registered_root_with_api_given(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The given API serves the related models that have none."""
    register(monkeypatch, world.apis["A"], None, None)

    thing = next(Thing.filter(prefetch="owner", api=world.apis["G"]))
    use(world, thing)

    assert world.reached() == {"G"}


def other_api(world: World) -> API:
    """Return an API that no model is registered on."""
    return world.apis["C"]


ASSIGNMENTS: dict[str, Callable[[Thing, Owner], None]] = {
    "a related object": lambda thing, owner: setattr(thing, "owner", owner),
    "a collection of them": lambda thing, owner: setattr(
        thing, "owners", [Owner(id=7, api=thing._api_override), owner]
    ),
    "a tuple of them": lambda thing, owner: setattr(thing, "owners", (owner,)),
    "a write-only related object": lambda thing, owner: setattr(
        thing, "boss", owner
    ),
}


@pytest.mark.parametrize("assignment", ASSIGNMENTS)
@pytest.mark.parametrize(
    ("parent_api", "owner_api", "refused"),
    [
        (None, None, False),
        ("G", "G", False),
        ("G", None, True),
        (None, "G", True),
        ("G", "C", True),
        ("C", "G", True),
    ],
    ids=[
        "both registered",
        "both given",
        "parent given, owner registered",
        "owner given, parent registered",
        "parent given, owner on another",
        "parent on another, owner given",
    ],
)
def test_an_assigned_related_object_must_use_its_parents_api(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    assignment: str,
    parent_api: str | None,
    owner_api: str | None,
    refused: bool,
) -> None:
    """Assigning an object of another API raises before anything is sent.

    Args:
        assignment: What is assigned.
        parent_api: The ``api=`` of the parent, or ``None`` for none.
        owner_api: The ``api=`` of the related object, or ``None`` for none.
        refused: Whether the assignment is refused.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    apis = world.apis
    thing = Thing(
        json=dict(THING), api=apis[parent_api] if parent_api else None
    )
    thing.set_write_mode("eager")
    owner = Owner(id=6, api=apis[owner_api] if owner_api else None)

    if not refused:
        ASSIGNMENTS[assignment](thing, owner)
        return
    with pytest.raises(ValueError, match="another API"):
        ASSIGNMENTS[assignment](thing, owner)

    assert world.log == []


@pytest.mark.parametrize(
    ("thing_api", "owner_api"), [(None, "G"), ("G", None), (None, None)]
)
def test_a_model_with_no_api_agrees_with_any_in_an_assignment(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    thing_api: str | None,
    owner_api: str | None,
) -> None:
    """A parent or a related object that has no API is not refused.

    Args:
        thing_api: The ``api=`` of the parent, or ``None`` for none.
        owner_api: The ``api=`` of the related object, or ``None`` for none.
    """
    register(monkeypatch, None, None, None)
    apis = world.apis
    thing = Thing(json=dict(THING), api=apis[thing_api] if thing_api else None)
    owner = Owner(id=6, api=apis[owner_api] if owner_api else None)

    thing.owner = owner

    assert thing.owner is owner


@pytest.mark.parametrize(
    ("parent_api", "owner_api", "refused"),
    [(None, None, False), ("G", "G", False), ("G", None, True)],
    ids=["both registered", "both given", "parent given, owner registered"],
)
@pytest.mark.parametrize("field", ["owner", "boss"])
def test_a_constructor_refuses_a_related_object_of_another_api(
    world: World,
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    parent_api: str | None,
    owner_api: str | None,
    refused: bool,
) -> None:
    """A field given to the constructor is checked as an assignment is.

    Args:
        parent_api: The ``api=`` of the parent, or ``None`` for none.
        owner_api: The ``api=`` of the related object, or ``None`` for none.
        refused: Whether the constructor refuses the related object.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    apis = world.apis
    owner = Owner(id=6, api=apis[owner_api] if owner_api else None)
    api = apis[parent_api] if parent_api else None
    given: dict[str, Any] = {field: owner}

    if not refused:
        assert Thing(id=1, api=api, **given).pk == 1
        return
    # The name is assigned first, and would load the object.
    with pytest.raises(ValueError, match="another API"):
        Thing(id=1, api=api, name="x", **given)

    assert world.log == []


CREATIONS: dict[str, Callable[..., Any]] = {
    "create()": lambda owner, **kw: Thing.create(name="n", owner=owner, **kw),
    "create(), write-only": lambda owner, **kw: Thing.create(
        name="n", boss=owner, **kw
    ),
    "create(fields=)": lambda owner, **kw: Thing.create(
        fields={"name": "n", "owners": [owner]}, **kw
    ),
    "get_or_create() lookup": lambda owner, **kw: Thing.get_or_create(
        owner=owner, **kw
    ),
    "get_or_create() defaults": lambda owner, **kw: Thing.get_or_create(
        name="zzz", defaults={"owner": owner}, **kw
    ),
}


@pytest.mark.parametrize("creation", CREATIONS)
@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_a_related_object_of_another_api_is_refused_in_a_creation(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    creation: str,
    given: bool,
) -> None:
    """A creation sends nothing for a related object of another API.

    Args:
        creation: The call that creates, and is given the related object.
        given: Whether the call is given ``api=``. The related object is built
            with the other choice.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    kwargs: dict[str, Any] = {"api": world.apis["G"]} if given else {}
    owner = Owner(id=6, api=None if given else world.apis["G"])

    with pytest.raises(ValueError, match="another API"):
        CREATIONS[creation](owner, **kwargs)

    assert world.log == []


@pytest.mark.parametrize("creation", CREATIONS)
def test_a_related_object_of_the_same_api_is_taken_in_a_creation(
    world: World, monkeypatch: pytest.MonkeyPatch, creation: str
) -> None:
    """The same API passes, given or registered.

    Args:
        creation: The call that creates, and is given the related object.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])

    CREATIONS[creation](Owner(id=6))
    CREATIONS[creation](Owner(id=6, api=world.apis["G"]), api=world.apis["G"])

    assert world.reached() == {"A", "G"}


def test_a_view_of_the_parents_api_is_the_same_api(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request-scoped view of an API is that API, for a related object."""
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    view = world.apis["G"]._scoped(QUERY_HEADER)
    thing = Thing(json=dict(THING), api=world.apis["G"])

    thing.owner = Owner(id=6, api=view)

    assert thing.owner.pk == 6


class Hooked:
    """Bulk hooks that build objects as a caller may write them."""

    built_with: Any = None


@json_object
class Member(JSONObject):
    """A model whose bulk lookup builds its objects with a client it picks."""

    class_url = "/owners/"
    id: int
    label: str
    org: Org

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Member]:
        """Build each member with the client that ``Hooked`` names."""
        chosen = api if Hooked.built_with is None else Hooked.built_with
        found = {}
        for pk in sorted(pks):
            member = cls(json=owner_json(pk), api=chosen)
            _ = member.org.pk
            found[pk] = member
        return found


@json_object(field_map={"member": "owner"})
class Task(JSONObject):
    """A model that prefetches its member."""

    class_url = "/things/"
    id: int
    member: Member


@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_a_hook_that_builds_an_object_with_another_api_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch, given: bool
) -> None:
    """The error names the hook, and no object that it returned is used.

    Args:
        given: Whether the query is given ``api=``.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    monkeypatch.setitem(_api_registry, Task, world.apis["A"])
    monkeypatch.setitem(_api_registry, Member, world.apis["A"])
    monkeypatch.setattr(Hooked, "built_with", world.apis["C"])
    kwargs: dict[str, Any] = {"api": world.apis["G"]} if given else {}

    with pytest.raises(UsageError, match=r"Member\.bulk_get_by_pks\(\)"):
        list(Task.filter(prefetch="member", **kwargs))


@pytest.mark.parametrize("given", [False, True], ids=["registered", "given"])
def test_a_hook_that_builds_an_object_with_a_view_of_the_query_api_is_taken(
    world: World, monkeypatch: pytest.MonkeyPatch, given: bool
) -> None:
    """The object, and what it read, keep the query's API, not the view.

    Args:
        given: Whether the query is given ``api=``.
    """
    own = world.apis["G"] if given else None
    served = own or world.apis["A"]
    monkeypatch.setitem(_api_registry, Task, world.apis["A"])
    monkeypatch.setitem(_api_registry, Member, world.apis["A"])
    monkeypatch.setitem(_api_registry, Org, world.apis["A"])
    monkeypatch.setattr(Hooked, "built_with", served._scoped(QUERY_HEADER))

    task = next(Task.filter(prefetch="member", api=own))

    assert task.member._api_override is own
    assert task.member.org._api_override is own
    assert task.member.api is served
    assert task.member.org.api is served


QUERIES: dict[str, Callable[..., Any]] = {
    "filter()": lambda **kw: Thing.filter(**kw),
    "all()": lambda **kw: Thing.all(**kw),
    "get()": lambda **kw: Thing.get(id=1, **kw),
    "get_page()": lambda **kw: Thing.get_page(**kw),
    "create()": lambda **kw: Thing.create(name="n", **kw),
    "get_or_create()": lambda **kw: Thing.get_or_create(name="a", **kw),
}


@pytest.mark.parametrize("query", QUERIES)
def test_a_query_refuses_a_client_that_is_no_api_before_any_request(
    world: World, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Only an API, or a view of one, can send a query's requests.

    Args:
        query: The query method that is given the client.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])

    with pytest.raises(TypeError, match="only through an API"):
        QUERIES[query](api=object())

    assert world.log == []


@pytest.mark.parametrize("member", ["url", "absolute_url"])
def test_a_url_is_refused_after_a_class_api_was_assigned(
    world: World, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    """The value of an assigned ``api`` is not the object's API.

    Args:
        member: The property that is read.
    """
    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    thing = Thing(id=1, api=world.apis["G"])
    monkeypatch.setattr(Thing, "api", other_api(world), raising=False)

    with pytest.raises(UsageError, match=r"register_api"):
        getattr(thing, member)


def test_absolute_url_is_refused_when_a_subclass_overrides_url(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal does not depend on how ``url`` is computed."""

    class Fixed(Thing):
        """A model whose URL is not built from its API."""

        @property
        def url(self) -> str:
            """Return a URL that reads no API."""
            return "/fixed/1/"

    register(monkeypatch, world.apis["A"], world.apis["A"], world.apis["A"])
    fixed = Fixed(id=1)
    monkeypatch.setattr(Fixed, "api", other_api(world), raising=False)

    with pytest.raises(UsageError, match=r"register_api"):
        _ = fixed.absolute_url


@pytest.mark.parametrize(
    "call",
    [
        lambda: Thing.create(name=object()),
        lambda: Thing.get_or_create(name=object()),
    ],
    ids=["create()", "get_or_create()"],
)
def test_a_missing_api_is_refused_before_a_refused_field_value(
    world: World, monkeypatch: pytest.MonkeyPatch, call: Callable[[], Any]
) -> None:
    """The class's API is chosen before the fields are encoded.

    Args:
        call: A creating call whose field value is refused.
    """
    register(monkeypatch, None, None, None)

    with pytest.raises(UsageError, match="No API registered for Thing"):
        call()

    assert world.log == []


@pytest.mark.parametrize(
    "base", ["https://api.test/", "mock://api.test/", "mock:/"]
)
def test_a_prefetch_lookup_sends_the_query_request_options(base: str) -> None:
    """A bulk lookup goes to the query's API, with the query's options.

    This holds whatever the scheme of the API's base URL, including a base that
    has no origin, so the options reach the lookup and no other host.
    """
    api = API(base)
    seen: list[tuple[str, str | None]] = []
    adapter = requests_mock.Adapter()

    def answer(request: Any, context: Any) -> Any:
        seen.append((request.url, request.headers.get("X-Trace")))
        if "owners" in request.url:
            return [{"id": 5}]
        return [{"id": 1, "owner": 5}]

    adapter.register_uri(requests_mock.ANY, requests_mock.ANY, json=answer)
    for prefix in ("https://", "mock:"):
        api.session.mount(prefix, adapter)

    @json_object
    class Lookup(JSONObject):
        class_url = "owners/"
        id: int

        @classmethod
        def bulk_get_by_pks(
            cls, pks: set[Any], *, api: Any, **kwargs: Any
        ) -> dict[Any, Lookup]:
            """Read the owners with one request."""
            response = api.get(
                "owners/", params={"id__in": ",".join(map(str, pks))}
            )
            return {
                item["id"]: cls(json=item, api=api) for item in response.json()
            }

    @json_object
    class Holder(JSONObject):
        class_url = "things/"
        # The annotations are objects, because a local class is not found by
        # the name of a string annotation.
        __annotations__ = {"id": int, "owner": Lookup}

    Lookup.register_api(api)
    Holder.register_api(api)

    list(
        Holder.filter(
            prefetch=["owner"], request_options={"headers": {"X-Trace": "1"}}
        )
    )

    assert [trace for _, trace in seen] == ["1", "1"]
    assert len(seen) == 2
    assert "things" in seen[0][0]
    assert "owners" in seen[1][0]
    prefix = base.rstrip("/")
    assert all(url.startswith(prefix) for url, _ in seen)
