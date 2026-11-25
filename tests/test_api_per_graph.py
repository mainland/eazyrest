"""Every object that a query reaches uses the query's API.

An object that a query builds, and each object that a relation, an embedded
object, a prefetch, or a bulk hook reaches from it, sends its requests through
one API: the ``api=`` that the query was given, or else the API registered for
the class that the query reads.

Each test runs the entry points of the rule over models that are registered on
one API, with and without ``api=`` and ``request_options=``. Every API has its
own origin, its own mounted adapter, and its own ``Authorization`` header, and
the log records which adapter received each request.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object
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


@json_object
class Thing(JSONObject):
    """The root of the graph."""

    class_url = "/things/"
    id: int
    name: str
    owner: Owner
    owners: list[Owner]


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
    copy.deepcopy(thing).refresh()
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
    "get_or_create() finding": (
        False,
        lambda **kw: [Thing.get_or_create(name="a", **kw)[0]],
    ),
    "get_or_create() creating": (
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
